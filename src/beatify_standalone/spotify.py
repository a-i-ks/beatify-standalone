"""Spotify Web API client — the replacement for Music Assistant.

Beatify's 54 curated playlists store `spotify:track:…` URIs for every song, so
the Web API's player endpoints are a direct fit: Beatify says "play this URI,
seek to this position", and Spotify routes it to whichever Connect device is
active — here, the librespot instance running on the same Pi.

Two constraints shaped this module:

**Redirect URIs.** Since 27 November 2025 Spotify accepts only HTTPS redirect
URIs, with one exception: loopback IP *literals* (`http://127.0.0.1:PORT`,
`http://[::1]:PORT`) — `localhost` is explicitly rejected. A party box on
192.168.x.x can therefore never be an OAuth callback target directly. The
supported route is an SSH tunnel from the machine running the browser:

    ssh -N -L 8123:127.0.0.1:8123 root@<pi>

...then authorise at `http://127.0.0.1:8123/beatify/spotify/login`. This is a
one-time step: the resulting refresh token is stored and keeps working, which
is what makes the box usable on the road without ever repeating it.

**PKCE.** The Authorization-Code + PKCE flow needs no client secret, so nothing
secret has to live on an SD card that travels.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Only what the game needs: read what is playing, and control playback.
SCOPES = "user-read-playback-state user-modify-playback-state"

# Refresh a little early so a call never races its own token expiry.
TOKEN_REFRESH_MARGIN = 60


class SpotifyError(RuntimeError):
    """Any non-recoverable Spotify API failure."""


class SpotifyAuthRequired(SpotifyError):
    """No usable token — the operator has to run the login flow."""


@dataclass
class _Tokens:
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: float = 0.0

    @property
    def valid(self) -> bool:
        return bool(self.access_token) and self.expires_at - TOKEN_REFRESH_MARGIN > time.time()


class SpotifyClient:
    """Thin async client over the Spotify Web API player endpoints."""

    def __init__(
        self,
        client_id: str,
        data_dir: Path,
        session: aiohttp.ClientSession,
        redirect_uri: str,
    ) -> None:
        self._client_id = client_id
        self._session = session
        self._redirect_uri = redirect_uri
        self._path = data_dir / "spotify_token.json"
        self._tokens = _Tokens()
        self._verifiers: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._load()

    # -- token persistence ------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LOGGER.warning("spotify token store unreadable: %s", self._path)
            return
        self._tokens = _Tokens(
            raw.get("access_token"), raw.get("refresh_token"), raw.get("expires_at", 0.0)
        )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "access_token": self._tokens.access_token,
                    "refresh_token": self._tokens.refresh_token,
                    "expires_at": self._tokens.expires_at,
                }
            ),
            encoding="utf-8",
        )
        self._path.chmod(0o600)

    @property
    def authorized(self) -> bool:
        return bool(self._tokens.refresh_token)

    # -- OAuth (Authorization Code + PKCE) --------------------------------

    def authorize_url(self) -> tuple[str, str]:
        """Return (url, state). The verifier is held until the callback."""
        verifier = secrets.token_urlsafe(64)[:128]
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(16)
        self._verifiers[state] = verifier

        query = urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "redirect_uri": self._redirect_uri,
                "state": state,
                "scope": SCOPES,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        return f"{AUTH_URL}?{query}", state

    async def complete_authorization(self, code: str, state: str) -> None:
        verifier = self._verifiers.pop(state, None)
        if verifier is None:
            raise SpotifyError("unknown or expired OAuth state")

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": self._client_id,
            "code_verifier": verifier,
        }
        await self._token_request(payload)

    async def _token_request(self, payload: dict[str, str]) -> None:
        async with self._session.post(
            TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as response:
            body = await response.json()
            if response.status != 200:
                raise SpotifyError(f"token request failed ({response.status}): {body}")

        self._tokens = _Tokens(
            access_token=body["access_token"],
            # A refresh grant may or may not return a new refresh token; keep
            # the old one when it does not, or the box logs itself out.
            refresh_token=body.get("refresh_token") or self._tokens.refresh_token,
            expires_at=time.time() + int(body.get("expires_in", 3600)),
        )
        self._save()

    async def _ensure_token(self) -> str:
        async with self._lock:
            if self._tokens.valid:
                return self._tokens.access_token  # type: ignore[return-value]
            if not self._tokens.refresh_token:
                raise SpotifyAuthRequired(
                    "Spotify is not authorised — visit /beatify/spotify/login "
                    "through an SSH tunnel to 127.0.0.1"
                )
            await self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self._tokens.refresh_token,
                    "client_id": self._client_id,
                }
            )
            return self._tokens.access_token  # type: ignore[return-value]

    # -- request plumbing -------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        _retried: bool = False,
    ) -> Any:
        token = await self._ensure_token()
        url = f"{API_BASE}{path}"
        headers = {"Authorization": f"Bearer {token}"}

        async with self._session.request(
            method, url, params=params, json=json_body, headers=headers
        ) as response:
            # 204 is the normal success for every player command.
            if response.status in (200, 202, 204):
                if response.status == 204 or not response.content_length:
                    return None
                try:
                    return await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    return None

            if response.status == 401 and not _retried:
                # Token rejected mid-flight; force a refresh and try once more.
                self._tokens.expires_at = 0.0
                return await self._request(
                    method, path, params=params, json_body=json_body, _retried=True
                )

            if response.status == 429 and not _retried:
                delay = int(response.headers.get("Retry-After", "1"))
                _LOGGER.warning("Spotify rate limit, retrying in %ss", delay)
                await asyncio.sleep(min(delay, 10))
                return await self._request(
                    method, path, params=params, json_body=json_body, _retried=True
                )

            text = await response.text()
            raise SpotifyError(f"{method} {path} -> {response.status}: {text[:300]}")

    # -- player endpoints -------------------------------------------------

    async def devices(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/me/player/devices")
        return (result or {}).get("devices", [])

    async def current_playback(self) -> dict[str, Any] | None:
        return await self._request("GET", "/me/player")

    async def transfer(self, device_id: str, play: bool = False) -> None:
        await self._request(
            "PUT", "/me/player", json_body={"device_ids": [device_id], "play": play}
        )

    async def play(
        self,
        uris: list[str] | None = None,
        device_id: str | None = None,
        position_ms: int | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if uris:
            body["uris"] = uris
        if position_ms is not None:
            body["position_ms"] = int(position_ms)
        params = {"device_id": device_id} if device_id else None
        await self._request("PUT", "/me/player/play", params=params, json_body=body or None)

    async def pause(self, device_id: str | None = None) -> None:
        await self._request(
            "PUT", "/me/player/pause", params={"device_id": device_id} if device_id else None
        )

    async def seek(self, position_ms: int, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"position_ms": int(position_ms)}
        if device_id:
            params["device_id"] = device_id
        await self._request("PUT", "/me/player/seek", params=params)

    async def set_volume(self, percent: int, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"volume_percent": max(0, min(100, int(percent)))}
        if device_id:
            params["device_id"] = device_id
        await self._request("PUT", "/me/player/volume", params=params)

    async def set_shuffle(self, state: bool, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"state": "true" if state else "false"}
        if device_id:
            params["device_id"] = device_id
        await self._request("PUT", "/me/player/shuffle", params=params)

    async def set_repeat(self, state: str, device_id: str | None = None) -> None:
        # Spotify accepts track | context | off
        params: dict[str, Any] = {"state": state}
        if device_id:
            params["device_id"] = device_id
        await self._request("PUT", "/me/player/repeat", params=params)
