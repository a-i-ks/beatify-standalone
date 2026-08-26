"""The `media_player` domain, backed by Spotify Connect.

This is the load-bearing piece of the port. Upstream's
`services/media_player.py` is unmodified: it looks up an entity in the registry,
reads its `platform`, and dispatches accordingly. This module supplies exactly
one entity and answers the service calls that result.

**Why the entity claims platform "sonos".**

`MediaPlayerService._play_song` has no generic branch — it dispatches to
`_play_via_music_assistant`, `_play_via_sonos`, or `_play_via_alexa`, and logs
"Unsupported platform" for anything else. Of the three, the Sonos path is a
plain, provider-agnostic call:

    media_player.play_media {entity_id, media_content_id: <uri>, media_content_type: "music"}

...with no Sonos-specific service anywhere in it, while the MA path additionally
calls `music_assistant.*` for queue save/restore. So "sonos" is the narrowest
seam that reaches a generic play call, and its capability entry in
`PLATFORM_CAPABILITIES` (supported, spotify-only, method "uri") happens to
describe librespot exactly.

This is an assumption about upstream internals rather than about the HA API, so
`tools/check_ha_surface.py --seams` asserts it holds on every version bump.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.helpers import entity_registry as er

from .spotify import SpotifyAuthRequired, SpotifyClient, SpotifyError

_LOGGER = logging.getLogger(__name__)

ENTITY_ID = "media_player.beatify_speaker"
PLATFORM = "sonos"  # see the module docstring — this is a deliberate seam

# Poll cadence. Upstream confirms a track started by watching `media_title`
# change after `play_media`, so a slow poll would make every song look like a
# playback failure. The fast rate applies while a command is settling.
POLL_IDLE = 5.0
POLL_ACTIVE = 1.0
SETTLE_TIMEOUT = 8.0
# Spotify needs a moment after a transfer before it will accept commands.
TRANSFER_SETTLE = 1.0


class MediaPlayerDriver:
    """Publishes one media_player entity and services the calls against it."""

    def __init__(self, hass: Any, client: SpotifyClient, device_name: str) -> None:
        self._hass = hass
        self._client = client
        self._device_name = device_name
        self._device_id: str | None = None
        # track URI -> album URI. Spotify needs a *context* to start an exact
        # track reliably, and the album is the smallest honest one. Cached
        # because a playlist replays the same songs across games.
        self._context_cache: dict[str, str] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._active_until = 0.0

    # -- lifecycle --------------------------------------------------------

    async def async_setup(self) -> None:
        registry = er.async_get(self._hass)
        registry.register(
            er.RegistryEntry(
                entity_id=ENTITY_ID,
                unique_id="beatify_librespot",
                platform=PLATFORM,
                original_name=self._device_name,
                config_entry_id="beatify",
            )
        )
        # Publish an initial state before upstream's setup scans for players;
        # an entity that is not in the state machine is invisible to it.
        self._publish(None)
        self._register_services()
        self._poll_task = self._hass.async_create_background_task(
            self._poll_loop(), "beatify-spotify-poll"
        )

    async def async_stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    def _register_services(self) -> None:
        services = self._hass.services
        for name, handler in (
            ("play_media", self._svc_play_media),
            ("media_play", self._svc_play),
            ("media_pause", self._svc_pause),
            ("media_stop", self._svc_pause),
            ("media_seek", self._svc_seek),
            ("volume_set", self._svc_volume_set),
            ("shuffle_set", self._svc_shuffle_set),
            ("repeat_set", self._svc_repeat_set),
        ):
            services.async_register("media_player", name, handler)
        # Upstream calls this to force a state refresh after a command.
        services.async_register("homeassistant", "update_entity", self._svc_update_entity)

    # -- device resolution ------------------------------------------------

    async def _resolve_device(self) -> str | None:
        """Find our librespot instance among the account's Connect devices."""
        try:
            devices = await self._client.devices()
        except SpotifyAuthRequired:
            raise
        except SpotifyError as err:
            _LOGGER.warning("could not list Spotify devices: %s", err)
            return self._device_id

        for device in devices:
            if device.get("name") == self._device_name:
                if device.get("id") != self._device_id:
                    _LOGGER.info("librespot Connect device resolved: %s", device.get("id"))
                self._device_id = device.get("id")
                return self._device_id

        _LOGGER.warning(
            "Spotify Connect device %r not found (visible: %s) — is librespot running "
            "and logged into the same account?",
            self._device_name,
            [d.get("name") for d in devices],
        )
        self._device_id = None
        return None

    async def _target_device(self) -> str | None:
        return self._device_id or await self._resolve_device()

    # -- service handlers -------------------------------------------------

    async def _svc_play_media(self, data: dict[str, Any]) -> None:
        uri = data.get("media_content_id")
        if not uri:
            _LOGGER.error("play_media without media_content_id")
            return

        device_id = await self._target_device()
        if device_id is None:
            raise SpotifyError(f"Connect device {self._device_name!r} unavailable")

        await self._ensure_active(device_id)

        context = await self._resolve_context(uri)

        async def _start() -> None:
            if context:
                await self._client.play(
                    context_uri=context, offset={"uri": uri}, device_id=device_id
                )
            else:
                await self._client.play(uris=[uri], device_id=device_id)

        try:
            await _start()
        except SpotifyError as err:
            # The device can also go idle between our check and the command.
            # Transfer and retry once rather than losing the round.
            if "Restriction violated" not in str(err):
                raise
            _LOGGER.info("play was refused, transferring and retrying once")
            await self._client.transfer(device_id, play=False)
            await asyncio.sleep(TRANSFER_SETTLE)
            await _start()

        await self._settle(expect_uri=uri)

    async def _resolve_context(self, track_uri: str) -> str | None:
        """Find the album a track belongs to, to play it as a context.

        Starting an exact track with `uris` fails against Spotify with
        `403 Restriction violated` where the same track started as an album
        context plus an offset succeeds. That is an API quirk, not a bug here —
        verified on hardware and long reported by others — so the album lookup
        is the reliable path rather than an optimisation.
        """
        cached = self._context_cache.get(track_uri)
        if cached is not None:
            return cached or None

        try:
            track = await self._client.track(track_uri)
        except SpotifyError as err:
            _LOGGER.warning("could not resolve album for %s: %s", track_uri, err)
            return None

        album = ((track or {}).get("album") or {}).get("uri")
        # Cache the miss too, so a track without an album is looked up once.
        self._context_cache[track_uri] = album or ""
        return album

    async def _ensure_active(self, device_id: str) -> None:
        """Make our Connect device the active one before commanding it.

        Spotify refuses `play` on an idle device with
        `403 Player command failed: Restriction violated` — a device that is
        merely visible is not a device that accepts commands. A party box sits
        idle between rounds, so this is the normal case, not an edge case.
        """
        try:
            devices = await self._client.devices()
        except SpotifyError as err:
            _LOGGER.debug("could not check device state: %s", err)
            return

        active = next((d for d in devices if d.get("id") == device_id and d.get("is_active")), None)
        if active is not None:
            return

        _LOGGER.info("Connect device is idle — transferring playback to it")
        await self._client.transfer(device_id, play=False)
        await asyncio.sleep(TRANSFER_SETTLE)

    async def _svc_play(self, data: dict[str, Any]) -> None:
        await self._client.play(device_id=await self._target_device())
        self._go_active()

    async def _svc_pause(self, data: dict[str, Any]) -> None:
        try:
            await self._client.pause(device_id=await self._target_device())
        except SpotifyError as err:
            # Pausing an already-paused player is a 403 from Spotify; upstream
            # stops songs defensively, so this must not surface as an error.
            _LOGGER.debug("pause ignored: %s", err)
        self._go_active()

    async def _svc_seek(self, data: dict[str, Any]) -> None:
        position = data.get("seek_position", data.get("position", 0))
        await self._client.seek(int(float(position) * 1000), await self._target_device())
        self._go_active()

    async def _svc_volume_set(self, data: dict[str, Any]) -> None:
        level = float(data.get("volume_level", 0.5))
        await self._client.set_volume(round(level * 100), await self._target_device())
        self._go_active()

    async def _svc_shuffle_set(self, data: dict[str, Any]) -> None:
        await self._client.set_shuffle(bool(data.get("shuffle")), await self._target_device())

    async def _svc_repeat_set(self, data: dict[str, Any]) -> None:
        mapping = {"off": "off", "all": "context", "one": "track"}
        state = mapping.get(str(data.get("repeat", "off")), "off")
        await self._client.set_repeat(state, await self._target_device())

    async def _svc_update_entity(self, data: dict[str, Any]) -> None:
        await self._refresh()

    # -- state publication ------------------------------------------------

    def _go_active(self) -> None:
        """Switch to the fast poll for a while and wake the loop now."""
        loop = asyncio.get_running_loop()
        self._active_until = loop.time() + 30
        self._wake.set()

    async def _settle(self, expect_uri: str) -> None:
        """Poll until Spotify reports the track we just asked for.

        Upstream decides a track failed when `media_title` does not change after
        `play_media`. Spotify's own state lags the command by a beat, so we
        close that gap here rather than letting every song look like a failure.
        """
        self._go_active()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_TIMEOUT
        while loop.time() < deadline:
            playback = await self._refresh()
            item = (playback or {}).get("item") or {}
            if item.get("uri") == expect_uri:
                return
            await asyncio.sleep(0.25)
        _LOGGER.warning("Spotify did not confirm %s within %ss", expect_uri, SETTLE_TIMEOUT)

    async def _refresh(self) -> dict[str, Any] | None:
        try:
            playback = await self._client.current_playback()
        except SpotifyAuthRequired as err:
            self._publish(None, unavailable=True, reason=str(err))
            return None
        except SpotifyError as err:
            _LOGGER.debug("playback poll failed: %s", err)
            return None
        self._publish(playback)
        return playback

    def _publish(
        self,
        playback: dict[str, Any] | None,
        unavailable: bool = False,
        reason: str | None = None,
    ) -> None:
        """Translate Spotify's playback object into an HA-shaped state."""
        attributes: dict[str, Any] = {
            "friendly_name": self._device_name,
            "supported_features": 0,
        }
        if reason:
            attributes["beatify_reason"] = reason

        if unavailable:
            self._hass.states.async_set(ENTITY_ID, "unavailable", attributes)
            return

        if not playback:
            self._hass.states.async_set(ENTITY_ID, "idle", attributes)
            return

        item = playback.get("item") or {}
        device = playback.get("device") or {}
        images = (item.get("album") or {}).get("images") or []
        artists = item.get("artists") or []

        attributes.update(
            {
                "media_title": item.get("name"),
                "media_artist": artists[0].get("name") if artists else None,
                "media_album_name": (item.get("album") or {}).get("name"),
                "media_content_id": item.get("uri"),
                "media_content_type": "music",
                "media_duration": (item.get("duration_ms") or 0) / 1000,
                "media_position": (playback.get("progress_ms") or 0) / 1000,
                "media_position_updated_at": datetime.now(timezone.utc),
                "entity_picture": images[0].get("url") if images else None,
            }
        )
        volume = device.get("volume_percent")
        if volume is not None:
            attributes["volume_level"] = volume / 100
        if device.get("name"):
            attributes["friendly_name"] = device["name"]

        state = "playing" if playback.get("is_playing") else "paused"
        self._hass.states.async_set(ENTITY_ID, state, attributes)

    async def _poll_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            interval = POLL_ACTIVE if loop.time() < self._active_until else POLL_IDLE
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
                self._wake.clear()
            except (TimeoutError, asyncio.TimeoutError):
                pass
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the poll must outlive any single failure
                _LOGGER.exception("state poll failed")
