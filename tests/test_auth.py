"""The OAuth slice ha-auth.js drives, unmodified."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from beatify_standalone.auth import AuthManager, register_auth_routes


@pytest.fixture
def auth(data_dir):
    return AuthManager(data_dir, pin="424242")


@pytest.fixture
async def client(auth):
    app = web.Application()
    register_auth_routes(app, auth)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


def test_pin_is_generated_once_and_persists(data_dir):
    first = AuthManager(data_dir)
    generated = first.pin

    assert len(generated) == 6
    assert AuthManager(data_dir).pin == generated


def test_validation_is_synchronous(auth):
    """Upstream calls this without `await` at both call sites."""
    code = auth.create_authorization_code("http://h/beatify/", "http://h/cb")
    tokens = auth.exchange_code(code, "http://h/beatify/")

    result = auth.async_validate_access_token(tokens["access_token"])
    assert result is not None
    assert not hasattr(result, "__await__")


def test_an_authorization_code_cannot_be_replayed(auth):
    code = auth.create_authorization_code("http://h/beatify/", "http://h/cb")
    assert auth.exchange_code(code, "http://h/beatify/") is not None
    assert auth.exchange_code(code, "http://h/beatify/") is None


def test_code_is_bound_to_its_client(auth):
    code = auth.create_authorization_code("http://h/beatify/", "http://h/cb")
    assert auth.exchange_code(code, "http://other/beatify/") is None


def test_refresh_survives_a_restart_but_access_tokens_do_not(data_dir):
    first = AuthManager(data_dir, pin="424242")
    code = first.create_authorization_code("http://h/beatify/", "http://h/cb")
    tokens = first.exchange_code(code, "http://h/beatify/")

    restarted = AuthManager(data_dir, pin="424242")
    # The browser holds the refresh token in an HttpOnly cookie and re-bootstraps
    # from it on every page load, so this is the path that must survive.
    assert restarted.refresh(tokens["refresh_token"], "http://h/beatify/") is not None
    assert restarted.async_validate_access_token(tokens["access_token"]) is None


def test_revoked_refresh_token_stops_working(auth):
    code = auth.create_authorization_code("http://h/beatify/", "http://h/cb")
    tokens = auth.exchange_code(code, "http://h/beatify/")

    auth.revoke(tokens["refresh_token"])
    assert auth.refresh(tokens["refresh_token"], "http://h/beatify/") is None


async def test_login_page_is_served(client):
    origin = f"http://{client.host}:{client.port}"
    response = await client.get(
        "/auth/authorize",
        params={"client_id": f"{origin}/beatify/", "redirect_uri": f"{origin}/cb", "state": "s"},
    )
    assert response.status == 200
    assert "Admin-PIN" in await response.text()


async def test_login_page_from_a_foreign_origin_is_refused(client):
    response = await client.get(
        "/auth/authorize",
        params={"client_id": "http://evil.example/", "redirect_uri": "http://evil.example/cb"},
    )
    assert response.status == 400


async def test_correct_pin_redirects_with_code_and_state(client):
    response = await client.post(
        "/auth/authorize",
        data={
            "pin": "424242",
            "client_id": f"http://{client.host}:{client.port}/beatify/",
            "redirect_uri": f"http://{client.host}:{client.port}/cb",
            "state": "xyz",
        },
        allow_redirects=False,
    )
    assert response.status == 302
    location = response.headers["Location"]
    assert "code=" in location
    assert "state=xyz" in location


async def test_wrong_pin_re_renders_the_form_without_a_code(client):
    response = await client.post(
        "/auth/authorize",
        data={
            "pin": "000000",
            "client_id": f"http://{client.host}:{client.port}/beatify/",
            "redirect_uri": f"http://{client.host}:{client.port}/cb",
        },
        allow_redirects=False,
    )
    assert response.status == 200
    assert "Falsche PIN" in await response.text()


async def test_foreign_redirect_uri_is_refused(client):
    """`/auth/authorize` must not become an open redirector."""
    response = await client.post(
        "/auth/authorize",
        data={
            "pin": "424242",
            "client_id": f"http://{client.host}:{client.port}/beatify/",
            "redirect_uri": "http://evil.example/steal",
        },
        allow_redirects=False,
    )
    assert response.status == 400


async def test_unsupported_grant_type_is_rejected(client):
    response = await client.post("/auth/token", data={"grant_type": "password"})
    assert response.status == 400
    assert (await response.json())["error"] == "unsupported_grant_type"


async def test_invalid_code_is_rejected(client):
    response = await client.post(
        "/auth/token", data={"grant_type": "authorization_code", "code": "nope", "client_id": "x"}
    )
    assert response.status == 400
    assert (await response.json())["error"] == "invalid_grant"


@pytest.fixture
async def open_client(data_dir):
    """A box configured the way it ships: no PIN demanded."""
    app = web.Application()
    register_auth_routes(app, AuthManager(data_dir, pin="424242", require_pin=False))
    async with TestClient(TestServer(app)) as client:
        yield client


async def test_no_pin_field_is_shown_when_none_is_demanded(open_client):
    """An input that accepts anything reads as a lock that does not lock."""
    origin = f"http://{open_client.host}:{open_client.port}"
    body = await (
        await open_client.get(
            "/auth/authorize",
            params={"client_id": f"{origin}/beatify/", "redirect_uri": f"{origin}/cb"},
        )
    ).text()

    assert 'name="pin"' not in body
    assert "Anmelden" in body


async def test_login_succeeds_without_a_pin(open_client):
    origin = f"http://{open_client.host}:{open_client.port}"
    response = await open_client.post(
        "/auth/authorize",
        data={"client_id": f"{origin}/beatify/", "redirect_uri": f"{origin}/cb", "state": "s"},
        allow_redirects=False,
    )

    assert response.status == 302
    assert "code=" in response.headers["Location"]


async def test_a_foreign_redirect_is_still_refused_without_a_pin(open_client):
    """Dropping the PIN must not turn this into an open redirector."""
    origin = f"http://{open_client.host}:{open_client.port}"
    response = await open_client.post(
        "/auth/authorize",
        data={"client_id": f"{origin}/beatify/", "redirect_uri": "http://evil.example/steal"},
        allow_redirects=False,
    )

    assert response.status == 400
