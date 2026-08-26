"""Joining a new network from a phone."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from beatify_standalone.auth import AuthManager
from beatify_standalone.wifi_setup import _escape, register_wifi_routes


@pytest.fixture
async def client(data_dir: Path, monkeypatch):
    auth = AuthManager(data_dir, pin="424242", require_pin=False)

    async def fake_scan():
        return ["Flying Fox", "iPhone André", "Fuchsbau"]

    applied: list[tuple] = []

    async def fake_apply(ssid, password, slot=3):
        applied.append((ssid, password, slot))
        return True, "Saved and applied."

    monkeypatch.setattr("beatify_standalone.wifi_setup.scan_networks", fake_scan)
    monkeypatch.setattr("beatify_standalone.wifi_setup.apply_network", fake_apply)

    app = web.Application()
    register_wifi_routes(app, auth, 8123)
    async with TestClient(TestServer(app)) as test_client:
        test_client.applied = applied  # type: ignore[attr-defined]
        yield test_client


async def test_the_page_renders_without_waiting_for_the_scan(client):
    """A scan takes up to 25 s on the Pi; the page must not block on it."""
    body = await (await client.get("/beatify/wifi")).text()

    assert "Suche läuft" in body
    assert "/beatify/wifi/scan" in body, "the list is fetched separately"


async def test_the_scan_endpoint_lists_networks_in_range(client):
    payload = await (await client.get("/beatify/wifi/scan")).json()

    assert "Flying Fox" in payload["options"]
    assert "Fuchsbau" in payload["options"]


async def test_an_ssid_with_an_accent_survives_the_scan(client):
    """Hotspot names carry spaces and accents as a matter of course."""
    payload = await (await client.get("/beatify/wifi/scan")).json()

    assert "iPhone André" in payload["options"]


async def test_no_pin_field_when_no_pin_is_demanded(data_dir: Path):
    """The field should be absent, not merely ignored."""
    from beatify_standalone.wifi_setup import _pin_field

    open_auth = AuthManager(data_dir, pin="424242", require_pin=False)
    closed_auth = AuthManager(data_dir, pin="424242", require_pin=True)

    assert _pin_field(open_auth) == ""
    assert "Admin-PIN" in _pin_field(closed_auth)


async def test_without_a_pin_requirement_anything_is_accepted(data_dir: Path):
    auth = AuthManager(data_dir, pin="424242", require_pin=False)

    assert auth.check_pin("") is True
    assert auth.check_pin("wrong") is True


async def test_with_a_pin_requirement_it_is_enforced(data_dir: Path):
    auth = AuthManager(data_dir, pin="424242", require_pin=True)

    assert auth.check_pin("") is False
    assert auth.check_pin("424242") is True


async def test_applying_a_network_needs_no_pin_by_default(client):
    response = await client.post(
        "/beatify/wifi", data={"ssid_manual": "Gastnetz", "password": "geheim"}
    )

    assert "Gespeichert" in await response.text()
    assert client.applied == [("Gastnetz", "geheim", 3)]


async def test_an_empty_ssid_is_refused(client):
    response = await client.post("/beatify/wifi", data={"ssid_manual": "", "password": "x"})

    assert "Kein Netzwerk" in await response.text()
    assert client.applied == []


async def test_it_never_touches_the_home_or_hotspot_slot(client):
    """Slots 1 and 2 are home and the phone; a venue network must not evict them."""
    await client.post("/beatify/wifi", data={"ssid_manual": "Gastnetz", "password": "x"})

    assert client.applied[0][2] == 3


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [("plain", "plain"), ("a#b", r"a\#b"), ("a;b", r"a\;b"), ("a$b", r"a\$b")],
)
def test_conf_breaking_characters_are_escaped(raw, escaped):
    assert _escape(raw) == escaped


def test_credentials_with_spaces_and_accents_are_read_verbatim(tmp_path: Path):
    """Regression: the credentials file used to be `source`d.

    `HOTSPOT_SSID=iPhone André` then means "run the command André with
    HOTSPOT_SSID=iPhone in its environment", so the value silently arrived
    empty and the hotspot was skipped without a word. Wi-Fi names have spaces
    and accents constantly, so the format must not require shell quoting.
    """
    cred = tmp_path / "creds"
    cred.write_text(
        textwrap.dedent(
            """\
            # a comment
            SSID=Fuchsbau
            PASSWORD=pa#ss;word$1
            HOTSPOT_SSID=iPhone André
            HOTSPOT_PASSWORD="quoted secret"
            """
        ),
        encoding="utf-8",
    )

    script = Path(__file__).resolve().parent.parent / "deploy" / "prepare-sd-headless.sh"
    reader = textwrap.dedent(
        f"""\
        CRED_FILE={cred!s}
        {_extract_reader(script)}
        printf '%s|%s|%s|%s' "$(read_setting SSID)" "$(read_setting PASSWORD)" \\
                             "$(read_setting HOTSPOT_SSID)" "$(read_setting HOTSPOT_PASSWORD)"
        """
    )
    out = subprocess.run(
        ["bash", "-c", reader], capture_output=True, text=True, check=True
    ).stdout
    ssid, password, hotspot, hotspot_pw = out.split("|")

    assert ssid == "Fuchsbau"
    assert password == "pa#ss;word$1", "a '#' inside a password is not a comment"
    assert hotspot == "iPhone André"
    assert hotspot_pw == "quoted secret", "one layer of quotes is stripped"


def _extract_reader(script: Path) -> str:
    """Pull read_setting() out of the shell script so it is tested as shipped."""
    text = script.read_text(encoding="utf-8")
    start = text.index("read_setting() {")
    end = text.index("\n}", start) + 2
    return text[start:end]
