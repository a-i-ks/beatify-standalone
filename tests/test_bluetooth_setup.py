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
    assert "Noch kein Gerät" in _devices_html([])


async def test_a_bogus_address_is_refused_before_shelling_out():
    ok, detail = await remove_device("not-a-mac; rm -rf /")

    assert ok is False
    assert "gültige" in detail


@pytest.fixture
async def client(data_dir: Path, monkeypatch):
    auth = AuthManager(data_dir, pin="424242")
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


async def test_pairing_needs_the_pin(client):
    response = await client.post("/beatify/bluetooth", data={"action": "pair", "pin": "000000"})

    assert "Falsche Admin-PIN" in await response.text()
    assert client.started == []


async def test_a_correct_pin_starts_the_search(client):
    response = await client.post("/beatify/bluetooth", data={"action": "pair", "pin": "424242"})

    assert "Suche gestartet" in await response.text()
    assert client.started == [True]


async def test_status_is_pollable_for_the_page(client):
    payload = await (await client.get("/beatify/bluetooth/status")).json()

    assert set(payload) == {"pairing", "log", "devices_html"}
    assert "Xbox Wireless Controller" in payload["devices_html"]
