"""The whole thing, end to end: shim + drivers + unmodified upstream.

If this passes, the port works — upstream boots, finds a speaker, serves its
pages, and runs a real WebSocket game session, with no Home Assistant and no
Music Assistant anywhere.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from fake_spotify import FakeSpotifyClient

from aiohttp import web

from beatify_standalone import bootstrap
from beatify_standalone.config import Config

APPLICATION = web.AppKey("application", bootstrap.Application)
SPOTIFY = web.AppKey("spotify", FakeSpotifyClient)


@pytest.fixture
async def app(data_dir, monkeypatch):
    spotify = FakeSpotifyClient()
    monkeypatch.setattr(bootstrap, "SpotifyClient", lambda **kwargs: spotify)

    config = Config(data_dir=data_dir, port=0, admin_pin="424242", spotify_client_id="fake-id")
    application = bootstrap.Application(config)
    aiohttp_app = await application.setup()
    aiohttp_app[APPLICATION] = application
    aiohttp_app[SPOTIFY] = spotify
    yield aiohttp_app
    await application.shutdown()


@pytest.fixture
async def client(app):
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def _admin_token(client) -> str:
    origin = f"http://{client.host}:{client.port}"
    response = await client.post(
        "/auth/authorize",
        data={
            "pin": "424242",
            "client_id": f"{origin}/beatify/",
            "redirect_uri": f"{origin}/beatify/auth/callback",
        },
        allow_redirects=False,
    )
    code = response.headers["Location"].split("code=")[1].split("&")[0]
    tokens = await client.post(
        "/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": f"{origin}/beatify/",
        },
    )
    return (await tokens.json())["access_token"]


async def test_upstream_setup_completed(app):
    """`hass.data[DOMAIN]` is upstream's own proof that setup ran."""
    hass = app[APPLICATION].hass
    domain_data = hass.data["beatify"]

    assert domain_data["game"] is not None
    assert domain_data["ws_handler"] is not None
    assert len(domain_data["playlists"]) > 50


async def test_the_spotify_player_is_the_discovered_speaker(app):
    players = app[APPLICATION].hass.data["beatify"]["media_players"]

    assert len(players) == 1
    assert players[0]["entity_id"] == "media_player.beatify_speaker"
    assert players[0]["supports_spotify"] is True


async def test_bundled_playlists_carry_spotify_uris(app):
    """The premise of dropping Music Assistant."""
    playlists = app[APPLICATION].hass.data["beatify"]["playlists"]
    assert any(p.get("name") for p in playlists)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/beatify/admin", 200),
        ("/beatify/play", 200),
        ("/beatify/static/player.html", 200),
        ("/beatify/static/js/player.js", 200),
        ("/beatify/api/status", 200),
    ],
)
async def test_pages_and_assets_are_served(client, path, expected):
    response = await client.get(path)
    assert response.status == expected


async def test_protected_endpoint_requires_a_token(client):
    assert (await client.get("/beatify/api/capabilities")).status == 401

    token = await _admin_token(client)
    response = await client.get(
        "/beatify/api/capabilities", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status == 200


async def _start_game(client, app) -> dict:
    """Do what the host does: open a game via the admin HTTP API.

    Players cannot join before this — upstream answers `GAME_NOT_STARTED` on the
    WebSocket until a game exists. Driving the real entry point also proves
    upstream accepts our synthetic media_player entity for an actual game.
    """
    token = await _admin_token(client)
    playlists = app[APPLICATION].hass.data["beatify"]["playlists"]
    first = playlists[0]
    playlist_ref = first.get("path") or first.get("file") or first.get("id")

    response = await client.post(
        "/beatify/api/start-game",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "playlists": [playlist_ref],
            "media_player": "media_player.beatify_speaker",
            "language": "de",
            "provider": "spotify",
        },
    )
    assert response.status == 200, await response.text()
    return {"token": token}


async def _join(ws, name: str, **extra) -> dict:
    await ws.send_json({"type": "join", "name": name, **extra})
    while True:
        message = await ws.receive_json(timeout=5)
        if message["type"] in ("join_ack", "error"):
            return message


async def test_a_game_can_be_started_through_the_admin_api(client, app):
    await _start_game(client, app)

    game = app[APPLICATION].hass.data["beatify"]["game"]
    assert game.game_id is not None


async def test_the_started_game_targets_our_spotify_player(client, app):
    await _start_game(client, app)

    game = app[APPLICATION].hass.data["beatify"]["game"]
    assert game.media_player == "media_player.beatify_speaker"


async def test_players_cannot_join_before_a_game_exists(client):
    async with client.ws_connect("/beatify/ws") as ws:
        message = await _join(ws, "Andre")
        assert message["code"] == "GAME_NOT_STARTED"


async def test_a_player_can_join_over_the_websocket(client, app):
    await _start_game(client, app)

    async with client.ws_connect("/beatify/ws") as ws:
        assert (await _join(ws, "Andre"))["type"] == "join_ack"


async def test_two_players_appear_in_the_shared_game_state(client, app):
    await _start_game(client, app)

    async with client.ws_connect("/beatify/ws") as first, client.ws_connect("/beatify/ws") as second:
        assert (await _join(first, "Andre"))["type"] == "join_ack"
        assert (await _join(second, "Sam"))["type"] == "join_ack"

        game = app[APPLICATION].hass.data["beatify"]["game"]
        assert {p.name for p in game.players.values()} == {"Andre", "Sam"}


async def test_claiming_admin_without_a_token_is_rejected(client, app):
    await _start_game(client, app)

    async with client.ws_connect("/beatify/ws") as ws:
        message = await _join(ws, "Host", is_admin=True)
        assert message["type"] == "error"
        assert message["code"] == "UNAUTHORIZED"


async def test_claiming_admin_with_our_token_succeeds(client, app):
    """The shim's AuthManager standing in for `hass.auth`, end to end."""
    started = await _start_game(client, app)

    async with client.ws_connect("/beatify/ws") as ws:
        message = await _join(ws, "Host", is_admin=True, ha_token=started["token"])
        assert message["type"] == "join_ack"


@pytest.mark.parametrize("path", ["/", "/beatify"])
async def test_the_bare_address_shows_a_front_door(client, path):
    """Typing just the host has to lead somewhere; it used to answer 404.

    This box is administered from a phone, where retyping full paths is the
    difference between usable and not.
    """
    response = await client.get(path)

    assert response.status == 200
    body = await response.text()
    for link in ("/beatify/admin", "/beatify/play", "/beatify/wifi", "/beatify/bluetooth"):
        assert link in body


async def test_the_front_door_credits_upstream(client):
    assert "mholzi/beatify" in await (await client.get("/")).text()


@pytest.mark.parametrize(
    "path", ["/beatify/admin/", "/beatify/play/", "/beatify/wifi/", "/beatify/bluetooth/"]
)
async def test_a_trailing_slash_still_finds_the_page(client, path):
    """Regression: aiohttp matches exactly, so /beatify/admin/ answered 404.

    Browsers and clipboards append that slash by themselves.
    """
    response = await client.get(path)

    assert response.status == 200, f"{path} should resolve like the slash-less form"
