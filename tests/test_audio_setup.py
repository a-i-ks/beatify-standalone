"""Choosing where the music comes out."""

from __future__ import annotations

import json
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

# Trimmed to the fields this code reads, modelled on what `pactl -f json`
# actually answered on the real box (see HARDWARE-FINDINGS.md). vc4hdmi0 has
# no display attached, which on the real box means it never gets past `off`/
# `pro-audio` and never produces a sink — exactly the case that matters here.
PW_CARDS = [
    {
        "name": "alsa_card.headphones",
        "active_profile": "output:stereo-fallback",
        "properties": {"alsa.id": "Headphones", "alsa.card": "0"},
        "profiles": {
            "off": {"sinks": 0},
            "output:stereo-fallback": {"sinks": 1},
            "pro-audio": {"sinks": 1},
        },
    },
    {
        "name": "alsa_card.hdmi0",
        "active_profile": "off",
        "properties": {"alsa.id": "vc4hdmi0", "alsa.card": "1"},
        "profiles": {"off": {"sinks": 0}, "pro-audio": {"sinks": 1}},
    },
    {
        "name": "alsa_card.hdmi1",
        "active_profile": "output:hdmi-stereo",
        "properties": {"alsa.id": "vc4hdmi1", "alsa.card": "2"},
        "profiles": {
            "off": {"sinks": 0},
            "output:hdmi-stereo": {"sinks": 1},
            "pro-audio": {"sinks": 1},
        },
    },
]

PW_SINKS = [
    {"name": "sink.headphones", "properties": {"alsa.card": "0"}},
    # No sink for card "1" (vc4hdmi0) — nothing is plugged into that port.
    {"name": "sink.hdmi1", "properties": {"alsa.card": "2"}},
]


class FakeSupervisor:
    def __init__(self) -> None:
        self.device: str | None = None

    async def set_audio_device(self, device):
        self.device = device


@pytest.fixture
async def client(data_dir: Path, monkeypatch):
    calls: list[tuple[str, ...]] = []
    sink_inputs: list[dict] = []

    async def fake_run(*argv, **kwargs):
        if argv[0] == "aplay" and argv[1] == "-l":
            return 0, APLAY
        if argv[:4] == ("pactl", "-f", "json", "list"):
            table = {"cards": PW_CARDS, "sinks": PW_SINKS, "sink-inputs": sink_inputs}
            return 0, json.dumps(table[argv[4]])
        if argv[0] in ("pactl", "pw-play"):
            calls.append(argv)
            return 0, ""
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
        test_client.calls = calls  # type: ignore[attr-defined]
        test_client.sink_inputs = sink_inputs  # type: ignore[attr-defined]
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


# -- pw: destinations — switch live, no daemon restart ------------------------


async def test_pw_destinations_are_offered_next_to_the_direct_fallback(client):
    """The recommended path is up front; the restart-based one is tucked away."""
    body = await (await client.get("/beatify/audio")).text()

    before_details = body.split("<details>")[0]
    assert "Klinke" in before_details
    assert "HDMI 2" in before_details
    assert "Erweitert: direkt, ohne PipeWire" in body
    assert "Klinke (3,5 mm), direkt" in body


async def test_selecting_a_pw_destination_switches_instantly_when_already_on_pipewire(client):
    """Coming from "default" the daemon never touches its device — no restart."""
    response = await client.post(
        "/beatify/audio", data={"device": "pw:Headphones", "action": "select"}
    )
    body = await response.text()

    assert "sofort, ohne Neustart" in body
    assert client.config.librespot_device == "pw:Headphones"
    assert client.supervisor.device is None  # never touched
    assert ("pactl", "set-default-sink", "sink.headphones") in client.calls


async def test_selecting_a_pw_destination_restarts_once_from_a_direct_device(client):
    """Coming from a direct-ALSA device the daemon's own config does change."""
    client.config.librespot_device = "plughw:CARD=vc4hdmi1"

    response = await client.post(
        "/beatify/audio", data={"device": "pw:Headphones", "action": "select"}
    )
    body = await response.text()

    assert "einmalig neu" in body
    assert client.config.librespot_device == "pw:Headphones"
    assert client.supervisor.device == "default"


async def test_switching_between_two_pw_destinations_never_restarts(client):
    client.config.librespot_device = "pw:Headphones"

    response = await client.post(
        "/beatify/audio", data={"device": "pw:vc4hdmi1", "action": "select"}
    )

    assert "sofort, ohne Neustart" in await response.text()
    assert client.supervisor.device is None


async def test_a_pw_destination_with_no_sink_is_refused_without_touching_state(client):
    """vc4hdmi0 has no display attached — same limitation the direct path has."""
    response = await client.post(
        "/beatify/audio", data={"device": "pw:vc4hdmi0", "action": "select"}
    )
    body = await response.text()

    assert "Kein Ausgang entstanden" in body
    assert client.config.librespot_device == "default"  # unchanged
    assert not any(call[:2] == ("pactl", "set-default-sink") for call in client.calls)


async def test_a_live_track_is_moved_to_the_new_sink(client):
    """Switching mid-song must be heard immediately, not on the next track."""
    client.sink_inputs.append(
        {"index": 42, "properties": {"application.name": "go-librespot"}}
    )

    await client.post("/beatify/audio", data={"device": "pw:Headphones", "action": "select"})

    assert ("pactl", "move-sink-input", "42", "sink.headphones") in client.calls


async def test_testing_a_pw_destination_targets_it_directly_and_does_not_persist(client):
    response = await client.post(
        "/beatify/audio", data={"device": "pw:Headphones", "action": "test"}
    )

    assert "Testton" in await response.text()
    assert client.config.librespot_device == "default"
    assert client.supervisor.device is None
    play_calls = [c for c in client.calls if c[0] == "pw-play"]
    assert play_calls == [("pw-play", "--target", "sink.headphones", play_calls[0][3])]


# -- re-pinning the pw: choice after a reboot ---------------------------------


async def test_reapply_at_boot_does_nothing_for_a_non_pw_device(data_dir: Path, monkeypatch):
    from beatify_standalone.audio_setup import reapply_pipewire_output_at_boot

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "beatify_standalone.audio_setup._run",
        lambda *argv, **kw: calls.append(argv) or _ok(),
    )
    for device in (None, "default", "plughw:CARD=Headphones"):
        await reapply_pipewire_output_at_boot(Config(data_dir=data_dir, librespot_device=device))

    assert calls == []


async def test_reapply_at_boot_retries_until_pipewire_is_up(data_dir: Path, monkeypatch):
    """PipeWire can still be starting when Beatify does — the same boot race
    the Connect daemon itself already tolerates."""
    from beatify_standalone.audio_setup import reapply_pipewire_output_at_boot

    attempts = {"n": 0}

    async def fake_run(*argv, **kwargs):
        if argv[:4] == ("pactl", "-f", "json", "list") and argv[4] == "cards":
            attempts["n"] += 1
            if attempts["n"] < 3:
                return 1, "Could not connect to PipeWire"
            return 0, json.dumps(PW_CARDS)
        if argv[:4] == ("pactl", "-f", "json", "list") and argv[4] == "sinks":
            return 0, json.dumps(PW_SINKS)
        if argv[:4] == ("pactl", "-f", "json", "list") and argv[4] == "sink-inputs":
            return 0, "[]"
        return 0, ""

    monkeypatch.setattr("beatify_standalone.audio_setup._run", fake_run)
    monkeypatch.setattr("beatify_standalone.audio_setup.BOOT_REAPPLY_DELAY", 0)

    config = Config(data_dir=data_dir, librespot_device="pw:Headphones")
    await reapply_pipewire_output_at_boot(config)

    assert attempts["n"] == 3


async def test_reapply_at_boot_gives_up_and_warns_after_the_last_attempt(
    data_dir: Path, monkeypatch, caplog
):
    from beatify_standalone.audio_setup import reapply_pipewire_output_at_boot

    monkeypatch.setattr(
        "beatify_standalone.audio_setup._run",
        lambda *argv, **kw: _fail("PipeWire never came up"),
    )
    monkeypatch.setattr("beatify_standalone.audio_setup.BOOT_REAPPLY_DELAY", 0)
    monkeypatch.setattr("beatify_standalone.audio_setup.BOOT_REAPPLY_ATTEMPTS", 2)

    with caplog.at_level("WARNING"):
        await reapply_pipewire_output_at_boot(
            Config(data_dir=data_dir, librespot_device="pw:Headphones")
        )

    assert "could not re-pin" in caplog.text


async def _ok() -> tuple[int, str]:
    return 0, ""


async def _fail(message: str) -> tuple[int, str]:
    return 1, message
