"""Choosing where the music comes out."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from beatify_standalone.audio_setup import _tone, register_audio_routes
from beatify_standalone.config import Config

APLAY = """\
**** List of PLAYBACK Hardware Devices ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones []
card 1: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 []
card 2: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 []
"""


class FakeSupervisor:
    def __init__(self) -> None:
        self.device: str | None = None

    async def set_audio_device(self, device):
        self.device = device


@pytest.fixture
async def client(data_dir: Path, monkeypatch):
    async def fake_run(*argv, **kwargs):
        if argv[0] == "aplay" and argv[1] == "-l":
            return 0, APLAY
        return 0, ""

    # HDMI-A-2 has the screen, which is the awkward case the page exists for.
    monkeypatch.setattr("beatify_standalone.audio_setup._run", fake_run)
    monkeypatch.setattr(
        "beatify_standalone.audio_setup.hdmi_status",
        lambda: {"vc4hdmi0": "disconnected", "vc4hdmi1": "connected"},
    )

    config = Config(data_dir=data_dir, librespot_device="default")
    supervisor = FakeSupervisor()
    app = web.Application()
    register_audio_routes(app, config, supervisor)
    async with TestClient(TestServer(app)) as test_client:
        test_client.config = config  # type: ignore[attr-defined]
        test_client.supervisor = supervisor  # type: ignore[attr-defined]
        yield test_client


async def test_all_real_outputs_are_offered(client):
    body = await (await client.get("/beatify/audio")).text()

    assert "Klinke" in body
    assert "HDMI 1" in body
    assert "HDMI 2" in body
    assert "Automatisch" in body


async def test_it_says_which_hdmi_socket_has_a_screen(client):
    """The sockets are unlabelled on the case; guessing sends audio nowhere."""
    body = await (await client.get("/beatify/audio")).text()

    connected = body.index("HDMI 2")
    assert "Bildschirm angeschlossen" in body[connected : connected + 400]


async def test_the_active_output_is_marked(client):
    body = await (await client.get("/beatify/audio")).text()

    assert "Automatisch (PipeWire) · aktiv" in body


async def test_selecting_an_output_persists_it_and_moves_the_daemon(client):
    response = await client.post(
        "/beatify/audio", data={"device": "plughw:CARD=vc4hdmi1", "action": "select"}
    )

    assert "Umgeschaltet" in await response.text()
    assert client.config.librespot_device == "plughw:CARD=vc4hdmi1"
    assert client.supervisor.device == "plughw:CARD=vc4hdmi1"
    # Written to disk, or the choice is lost on the next restart.
    assert "vc4hdmi1" in client.config.config_path.read_text()


async def test_a_test_tone_does_not_change_the_setting(client):
    """Trying an output must be safe; only picking one commits."""
    response = await client.post(
        "/beatify/audio", data={"device": "plughw:CARD=Headphones", "action": "test"}
    )

    assert "Testton" in await response.text()
    assert client.config.librespot_device == "default"
    assert client.supervisor.device is None


async def test_an_unknown_device_is_refused(client):
    """The value reaches a shell command; it must be one we offered."""
    response = await client.post(
        "/beatify/audio", data={"device": "plughw:CARD=; rm -rf /", "action": "select"}
    )

    assert "Unbekannter Ausgang" in await response.text()
    assert client.supervisor.device is None


def test_the_test_tone_is_a_valid_wav(tmp_path: Path):
    path = tmp_path / "tone.wav"
    _tone(path, seconds=0.3)

    with wave.open(str(path)) as handle:
        assert handle.getnchannels() == 2
        assert handle.getframerate() == 48000
        assert handle.getnframes() > 0
