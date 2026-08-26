"""An in-memory stand-in for the Spotify Web API.

Records the calls the driver makes and answers `current_playback` from a
simple model, so the driver's translation layer can be tested without a
network, an account, or a running librespot.
"""

from __future__ import annotations

from typing import Any


class FakeSpotifyClient:
    DEVICE_ID = "device-abc"

    def __init__(self, device_name: str = "Beatify", authorized: bool = True) -> None:
        self.device_name = device_name
        self.authorized = authorized
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.current_uri: str | None = None
        self.is_playing = False
        self.progress_ms = 0
        self.volume_percent = 50
        self._devices = [{"id": self.DEVICE_ID, "name": device_name, "is_active": True}]

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def devices(self) -> list[dict[str, Any]]:
        return self._devices

    async def play(self, uris=None, device_id=None, position_ms=None) -> None:
        self._record("play", uris=uris, device_id=device_id, position_ms=position_ms)
        if uris:
            self.current_uri = uris[0]
            self.progress_ms = position_ms or 0
        self.is_playing = True

    async def pause(self, device_id=None) -> None:
        self._record("pause", device_id=device_id)
        self.is_playing = False

    async def seek(self, position_ms, device_id=None) -> None:
        self._record("seek", position_ms=position_ms, device_id=device_id)
        self.progress_ms = position_ms

    async def set_volume(self, percent, device_id=None) -> None:
        self._record("set_volume", percent=percent, device_id=device_id)
        self.volume_percent = percent

    async def set_shuffle(self, state, device_id=None) -> None:
        self._record("set_shuffle", state=state, device_id=device_id)

    async def set_repeat(self, state, device_id=None) -> None:
        self._record("set_repeat", state=state, device_id=device_id)

    async def transfer(self, device_id, play=False) -> None:
        self._record("transfer", device_id=device_id, play=play)

    async def current_playback(self) -> dict[str, Any] | None:
        if self.current_uri is None:
            return None
        return {
            "is_playing": self.is_playing,
            "progress_ms": self.progress_ms,
            "device": {"name": self.device_name, "volume_percent": self.volume_percent},
            "item": {
                "uri": self.current_uri,
                "name": "Er gehört zu mir",
                "duration_ms": 210_000,
                "artists": [{"name": "Marianne Rosenberg"}],
                "album": {
                    "name": "Best Of",
                    "images": [{"url": "https://example.invalid/art.jpg"}],
                },
            },
        }
