"""The Spotify HTTP layer — response handling above all."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from beatify_standalone.spotify import SpotifyClient


@pytest.fixture
async def spotify(data_dir: Path, monkeypatch):
    """A client pointed at a local stand-in for api.spotify.com."""
    routes = web.RouteTableDef()

    @routes.get("/v1/me/player/devices")
    async def devices(request):
        # Chunked, exactly like Spotify: no Content-Length header at all.
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(json.dumps({"devices": [{"id": "d1", "name": "Beatify"}]}).encode())
        await response.write_eof()
        return response

    @routes.get("/v1/me/player")
    async def player(request):
        return web.json_response({"is_playing": True, "item": {"uri": "spotify:track:x"}})

    @routes.put("/v1/me/player/play")
    async def play(request):
        return web.Response(status=204)

    @routes.get("/v1/plaintext")
    async def plaintext(request):
        # Exactly what /me/player/seek answers: a request id, text/plain, 200.
        return web.Response(status=200, text="72iqe6X9om_ZNEUkXZB8DLUcPG0")

    @routes.get("/v1/empty")
    async def empty(request):
        return web.Response(status=200, body=b"")

    app = web.Application()
    app.add_routes(routes)

    async with TestClient(TestServer(app)) as client:
        base = f"http://{client.host}:{client.port}/v1"
        monkeypatch.setattr("beatify_standalone.spotify.API_BASE", base)
        instance = SpotifyClient("client-id", data_dir, client.session, "http://127.0.0.1/cb")
        # Skip the OAuth dance; this is about response handling.
        instance._tokens.access_token = "token"
        instance._tokens.expires_at = 1e12
        yield instance


async def test_chunked_response_is_not_discarded(spotify):
    """Regression: `not response.content_length` threw away valid 200s.

    aiohttp reports Content-Length as None for chunked and compressed replies,
    and Spotify sends both. The old check turned a full device list into an
    empty one — and, worse, made current_playback() report idle forever, so
    upstream scored every track as a playback failure.
    """
    devices = await spotify.devices()

    assert devices == [{"id": "d1", "name": "Beatify"}]


async def test_current_playback_is_parsed(spotify):
    playback = await spotify.current_playback()

    assert playback is not None
    assert playback["is_playing"] is True
    assert playback["item"]["uri"] == "spotify:track:x"


async def test_204_means_success_with_no_body(spotify):
    assert await spotify.play(uris=["spotify:track:x"], device_id="d1") is None


async def test_an_empty_200_body_is_tolerated(spotify):
    assert await spotify._request("GET", "/empty") is None


async def test_a_plain_text_200_is_success_not_a_warning(spotify, caplog):
    """Spotify answers seek/pause with a bare request id and HTTP 200.

    Beatify seeks once per round, so treating that as an anomaly would bury the
    log in noise and hide the failures that matter.
    """
    assert await spotify._request("GET", "/plaintext") is None
    assert "malformed JSON" not in caplog.text
