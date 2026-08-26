"""Pairing a controller from a phone."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from beatify_standalone.auth import AuthManager
from beatify_standalone.bluetooth_setup import (
    _DEVICE,
    _devices_html,
    register_bluetooth_routes,
    remove_device,
)

LIST_OUTPUT = (
    '<device id="AC:8E:BD:11:22:33" name="Xbox Wireless Controller" '
    'type="joystick" connected="yes" />\n'
    '<device id="00:11:22:33:44:55" name="8BitDo SN30" type="joystick" connected="no" />\n'
)


def test_batocera_list_output_is_parsed():
    """The format is `batocera-bluetooth list`'s own, so parse it as shipped."""
    devices = [m.groupdict() for m in _DEVICE.finditer(LIST_OUTPUT)]

    assert len(devices) == 2
    assert devices[0]["name"] == "Xbox Wireless Controller"
    assert devices[0]["id"] == "AC:8E:BD:11:22:33"
    assert devices[0]["connected"] == "yes"
    assert devices[1]["connected"] == "no"


def test_connection_state_is_visible_at_a_glance():
    html = _devices_html([m.groupdict() for m in _DEVICE.finditer(LIST_OUTPUT)])

    assert 'class="dot on"' in html
    assert 'class="dot off"' in html
    assert "Xbox Wireless Controller" in html


def test_an_empty_list_says_so():
    assert "Noch kein Controller" in _devices_html([])


async def test_a_bogus_address_is_refused_before_shelling_out():
    ok, detail = await remove_device("not-a-mac; rm -rf /")

    assert ok is False
    assert "gültige" in detail


@pytest.fixture
async def client(data_dir: Path, monkeypatch):
    auth = AuthManager(data_dir, pin="424242", require_pin=False)
    started: list[bool] = []

    async def fake_paired():
        return [m.groupdict() for m in _DEVICE.finditer(LIST_OUTPUT)]

    monkeypatch.setattr("beatify_standalone.bluetooth_setup.paired_devices", fake_paired)
    monkeypatch.setattr(
        "beatify_standalone.bluetooth_setup.PairingSession.start",
        lambda self: _record(started),
    )

    app = web.Application()
    register_bluetooth_routes(app, auth)
    async with TestClient(TestServer(app)) as test_client:
        test_client.started = started  # type: ignore[attr-defined]
        yield test_client


async def _record(started: list[bool]) -> bool:
    started.append(True)
    return True


async def test_the_page_lists_paired_controllers(client):
    body = await (await client.get("/beatify/bluetooth")).text()

    assert "Xbox Wireless Controller" in body
    assert "Pair-Taste" in body, "tells you how to put the controller into pairing mode"


async def test_pairing_needs_no_pin_by_default(client):
    response = await client.post("/beatify/bluetooth", data={"action": "pair"})

    assert "Suche gestartet" in await response.text()
    assert client.started == [True]


async def test_status_is_pollable_for_the_page(client):
    payload = await (await client.get("/beatify/bluetooth/status")).json()

    assert set(payload) == {"pairing", "remaining", "found", "log", "devices_html"}
    assert "Xbox Wireless Controller" in payload["devices_html"]


# --- session behaviour: the part that makes it usable on a phone -------------


async def test_a_fresh_session_is_idle():
    from beatify_standalone.bluetooth_setup import PairingSession

    session = PairingSession()

    assert session.running is False
    assert session.remaining == 0
    assert session.found is None


async def test_a_finished_session_can_be_started_again(monkeypatch):
    """One failed attempt must not lock the page for good."""
    from beatify_standalone.bluetooth_setup import PairingSession

    session = PairingSession()

    async def instant():
        return

    monkeypatch.setattr(PairingSession, "_run_session", lambda self: instant())
    assert await session.start() is True
    if session._task:
        await session._task
    assert session.running is False
    assert await session.start() is True


async def test_cancelling_an_idle_session_is_harmless():
    from beatify_standalone.bluetooth_setup import PairingSession

    await PairingSession().cancel()  # must not raise


async def test_the_log_is_bounded():
    """A stuck search must not grow the page without limit."""
    from beatify_standalone.bluetooth_setup import PairingSession

    session = PairingSession()
    for i in range(200):
        session._say(f"line {i}")

    assert len(session.lines) == 40
    assert session.lines[-1] == "line 199"


async def test_cancel_needs_no_pin_and_always_works(client):
    """Stopping something is never the dangerous direction."""
    response = await client.post("/beatify/bluetooth", data={"action": "cancel"})

    assert "abgebrochen" in await response.text()
