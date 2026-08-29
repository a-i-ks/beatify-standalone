"""Wire the shim, the drivers and upstream Beatify into one running server.

Order matters in one place: the media_player driver must publish its state
*before* upstream's `async_setup_entry` runs, because that function scans
`hass.states.async_all("media_player")` to discover speakers. An entity that is
not in the state machine at that moment simply does not exist to Beatify.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from .audio_setup import register_audio_routes, reapply_pipewire_output_at_boot
from .auth import AuthManager, register_auth_routes
from .bluetooth_setup import register_bluetooth_routes
from .config import Config
from .landing import register_landing_routes
from .librespot import LibrespotSupervisor, resolve_alsa_device
from .media_player_driver import MediaPlayerDriver
from .spotify import SpotifyClient, SpotifyError
from .wifi_setup import register_wifi_routes

_LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_PATH = REPO_ROOT / "vendor"
SHIM_PATH = REPO_ROOT / "src"


def install_import_paths() -> None:
    """Make the shim and the vendored upstream importable.

    The shim must come first: it provides the `homeassistant` package that
    upstream imports. Nothing else on the path may shadow it.
    """
    for path in (str(SHIM_PATH), str(VENDOR_PATH)):
        if path not in sys.path:
            sys.path.insert(0, path)


class Application:
    """Owns the whole runtime: server, shim, drivers, upstream integration."""

    def __init__(self, config: Config) -> None:
        self.config = config
        # A trailing slash is something browsers and clipboards add on their own,
        # and aiohttp matches paths exactly — so /beatify/admin/ answered 404
        # while /beatify/admin worked. Normalising is kinder than expecting
        # people to notice a slash.
        self.app = web.Application(
            middlewares=[web.normalize_path_middleware(append_slash=False, remove_slash=True)]
        )
        self.hass: Any = None
        self.auth = AuthManager(config.data_dir, config.admin_pin, config.require_admin_pin)
        self.librespot = LibrespotSupervisor(
            config.librespot_binary,
            config.librespot_name,
            resolve_alsa_device(config.librespot_device),
            config.librespot_bitrate,
            config.librespot_extra_args,
            config.librespot_flavor,
            config.data_dir / "librespot",
        )
        self.spotify: SpotifyClient | None = None
        self.driver: MediaPlayerDriver | None = None
        self._entry: Any = None

    async def setup(self) -> web.Application:
        install_import_paths()

        from homeassistant.components.http import HttpComponent
        from homeassistant.config_entries import ConfigEntries, ConfigEntry
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        hass = HomeAssistant(self.config.data_dir, self.config.country)
        hass.auth = self.auth
        hass.http = HttpComponent(hass, self.app, self.config.port)
        self._entry = ConfigEntry.load(self.config.data_dir / "config_entry.json")
        hass.config_entries = ConfigEntries(self._entry)
        self.hass = hass

        register_landing_routes(self.app)
        register_auth_routes(self.app, self.auth)
        register_wifi_routes(self.app, self.auth, self.config.port)
        register_bluetooth_routes(self.app, self.auth)
        register_audio_routes(self.app, self.config, self.librespot)
        self._register_spotify_routes()

        # librespot first: the Connect device needs to exist before the driver
        # goes looking for it, though the driver copes if it is slow to appear.
        await self.librespot.start()

        # Best-effort and non-blocking: a pinned PipeWire output does not
        # survive a reboot on its own (/var is tmpfs, WirePlumber remembers
        # nothing), and PipeWire itself can still be starting up right now.
        asyncio.create_task(reapply_pipewire_output_at_boot(self.config))

        session = async_get_clientsession(hass)
        if not self.config.spotify_client_id:
            _LOGGER.error(
                "No Spotify client id configured — set spotify_client_id in %s "
                "or BEATIFY_SPOTIFY_CLIENT_ID. Playback will not work.",
                self.config.config_path,
            )
        self.spotify = SpotifyClient(
            client_id=self.config.spotify_client_id or "",
            data_dir=self.config.data_dir,
            session=session,
            redirect_uri=f"http://127.0.0.1:{self.config.port}/beatify/spotify/callback",
        )

        self.driver = MediaPlayerDriver(hass, self.spotify, self.config.librespot_name)
        await self.driver.async_setup()

        if not self.spotify.authorized:
            _LOGGER.warning(
                "Spotify not authorised yet. Tunnel in and open the login page:\n"
                "    ssh -N -L %s:127.0.0.1:%s <user>@<this-host>\n"
                "    http://127.0.0.1:%s/beatify/spotify/login",
                self.config.port,
                self.config.port,
                self.config.port,
            )

        # Now upstream, unmodified.
        from custom_components.beatify import async_setup_entry

        ok = await async_setup_entry(hass, self._entry)
        if not ok:
            raise RuntimeError("upstream async_setup_entry returned False")

        _LOGGER.info(
            "Beatify standalone ready on http://%s:%s/  (admin PIN: %s)",
            self.config.host,
            self.config.port,
            self.auth.pin if self.config.require_admin_pin else "not required",
        )
        return self.app

    # -- Spotify authorisation endpoints ----------------------------------

    def _register_spotify_routes(self) -> None:
        async def login(request: web.Request) -> web.StreamResponse:
            if not _is_loopback(request.remote):
                return web.Response(
                    text=(
                        "Spotify authorisation must be started over a loopback "
                        "connection, because Spotify only accepts http:// redirect "
                        "URIs for 127.0.0.1. Open an SSH tunnel first:\n\n"
                        f"    ssh -N -L {self.config.port}:127.0.0.1:{self.config.port} <user>@<this-host>\n\n"
                        f"then browse to http://127.0.0.1:{self.config.port}/beatify/spotify/login\n"
                    ),
                    status=403,
                )
            if self.spotify is None or not self.config.spotify_client_id:
                return web.Response(text="No Spotify client id configured.", status=503)
            url, _state = self.spotify.authorize_url()
            raise web.HTTPFound(url)

        async def callback(request: web.Request) -> web.StreamResponse:
            if self.spotify is None:
                return web.Response(text="Spotify client unavailable.", status=503)
            error = request.query.get("error")
            if error:
                return web.Response(text=f"Spotify authorisation denied: {error}", status=400)
            code = request.query.get("code")
            state = request.query.get("state")
            if not code or not state:
                return web.Response(text="Missing code or state.", status=400)
            try:
                await self.spotify.complete_authorization(code, state)
            except SpotifyError as err:
                return web.Response(text=f"Authorisation failed: {err}", status=400)
            return web.Response(
                text=(
                    "Spotify authorised. You can close this tab and the SSH tunnel — "
                    "the refresh token is stored, so this does not need repeating."
                ),
                content_type="text/plain",
            )

        self.app.router.add_route("GET", "/beatify/spotify/login", login)
        self.app.router.add_route("GET", "/beatify/spotify/callback", callback)

    async def shutdown(self) -> None:
        from homeassistant.helpers.aiohttp_client import async_close_clientsession

        if self.driver is not None:
            await self.driver.async_stop()
        await self.librespot.stop()
        if self._entry is not None:
            self._entry.run_unloaders()
        if self.hass is not None:
            await self.hass.async_shutdown()
            await async_close_clientsession(self.hass)


def _is_loopback(remote: str | None) -> bool:
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False
