"""The Connect-daemon supervisor: invocation and config generation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from beatify_standalone.librespot import (
    FLAVOR_GO,
    FLAVOR_RUST,
    LibrespotSupervisor,
    resolve_alsa_device,
)


def test_go_flavor_is_invoked_with_a_config_dir(tmp_path: Path):
    supervisor = LibrespotSupervisor(
        "go-librespot", "Beatify", "hw:0", flavor=FLAVOR_GO, config_dir=tmp_path
    )
    assert supervisor._argv() == ["go-librespot", "--config_dir", str(tmp_path)]


def test_go_config_uses_the_documented_keys(tmp_path: Path):
    supervisor = LibrespotSupervisor(
        "go-librespot", "Beatify", "hw:CARD=vc4hdmi0", 320, flavor=FLAVOR_GO, config_dir=tmp_path
    )
    written = supervisor.write_config()

    assert written is not None
    config = written.read_text()
    assert "device_name: Beatify" in config
    assert "audio_device: hw:CARD=vc4hdmi0" in config
    assert "bitrate: 320" in config
    # Without persistence the box would need re-pairing from a phone on every
    # boot, which defeats the point of a travel build.
    assert "persist_credentials: true" in config


def test_go_config_defaults_the_audio_device(tmp_path: Path):
    supervisor = LibrespotSupervisor(
        "go-librespot", "Beatify", None, flavor=FLAVOR_GO, config_dir=tmp_path
    )
    assert "audio_device: default" in supervisor.write_config().read_text()


def test_rust_flavor_is_configured_purely_by_flags():
    supervisor = LibrespotSupervisor("librespot", "Beatify", "hw:0", 320, flavor=FLAVOR_RUST)
    argv = supervisor._argv()

    assert argv[0] == "librespot"
    assert argv[argv.index("--name") + 1] == "Beatify"
    assert argv[argv.index("--device") + 1] == "hw:0"
    assert argv[argv.index("--backend") + 1] == "alsa"


def test_rust_flavor_writes_no_config(tmp_path: Path):
    supervisor = LibrespotSupervisor(
        "librespot", "Beatify", flavor=FLAVOR_RUST, config_dir=tmp_path
    )
    assert supervisor.write_config() is None
    assert list(tmp_path.iterdir()) == []


def test_extra_args_are_appended_in_both_flavors(tmp_path: Path):
    go = LibrespotSupervisor(
        "go-librespot", "B", extra_args=["-debug"], flavor=FLAVOR_GO, config_dir=tmp_path
    )
    rust = LibrespotSupervisor("librespot", "B", extra_args=["--verbose"], flavor=FLAVOR_RUST)
    assert go._argv()[-1] == "-debug"
    assert rust._argv()[-1] == "--verbose"


def test_missing_binary_is_reported_not_raised(tmp_path: Path):
    supervisor = LibrespotSupervisor("definitely-not-installed-xyz", "B", config_dir=tmp_path)
    assert supervisor.available is False


def test_an_absolute_path_counts_as_available(tmp_path: Path):
    binary = tmp_path / "go-librespot"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert LibrespotSupervisor(str(binary), "B").available is True


def test_go_flavor_uses_a_double_dashed_config_flag(tmp_path: Path):
    """Regression: go-librespot parses `-config_dir` as `-c onfig_dir`.

    pflag treats `-c` as the short form of `--conf`, so a single dash makes the
    daemon exit immediately with "invalid config override format: onfig_dir".
    Found on the real Pi, not in review.
    """
    supervisor = LibrespotSupervisor(
        "go-librespot", "Beatify", flavor=FLAVOR_GO, config_dir=tmp_path
    )
    argv = supervisor._argv()

    assert "--config_dir" in argv
    assert "-config_dir" not in argv


def test_daemon_output_is_retained_for_failure_reporting(tmp_path: Path):
    """A silently restarting audio daemon is the worst failure mode there is."""
    supervisor = LibrespotSupervisor("go-librespot", "B", config_dir=tmp_path)
    for i in range(15):
        supervisor._recent.append(f"line {i}")

    assert len(supervisor._recent) == 10, "keeps a bounded tail"
    assert supervisor._recent[-1] == "line 14", "keeps the most recent lines"


def test_early_failures_are_not_reported_as_errors(tmp_path: Path, caplog):
    """At boot the service can beat PipeWire to the audio device.

    That resolves itself in seconds. Logging ERROR about it on every boot would
    train whoever reads the log to ignore the one time it matters.
    """
    import logging

    from beatify_standalone.librespot import STARTUP_GRACE_ATTEMPTS

    supervisor = LibrespotSupervisor("go-librespot", "B", config_dir=tmp_path)
    assert supervisor._consecutive_failures == 0
    assert STARTUP_GRACE_ATTEMPTS >= 3, "a few seconds of grace, not one attempt"


async def test_set_audio_device_survives_and_relaunches(tmp_path: Path, monkeypatch):
    """Regression: set_audio_device swaps self._process to None to grab and
    kill it, right where the restart loop's own `await self._process.wait()`
    was reading that same attribute — a `None.wait()` that crashed the whole
    loop, silently, so the daemon never came back after a mid-round output
    switch. Found live on the box, not in review.
    """
    import beatify_standalone.librespot as librespot_module

    monkeypatch.setattr(librespot_module, "RESTART_DELAY_MIN", 0)

    script = tmp_path / "fake-librespot"
    script.write_text("#!/bin/sh\ntrap 'exit 0' TERM\nwhile true; do sleep 0.05; done\n")
    script.chmod(0o755)

    supervisor = LibrespotSupervisor(str(script), "Beatify", "hw:0", config_dir=tmp_path)
    try:
        await supervisor.start()
        await asyncio.sleep(0.2)
        assert supervisor._process is not None
        first_pid = supervisor._process.pid

        await supervisor.set_audio_device("default")
        await asyncio.sleep(0.5)

        assert supervisor._task is not None and not supervisor._task.done(), (
            "the restart loop must survive the switch"
        )
        assert supervisor._process is not None, "the daemon must come back up"
        assert supervisor._process.pid != first_pid
    finally:
        await supervisor.stop()


def test_resolve_alsa_device_translates_pw_prefix_to_default():
    """A `pw:` id steers PipeWire itself; the daemon only ever sees "default"."""
    assert resolve_alsa_device("pw:Headphones") == "default"
    assert resolve_alsa_device("pw:vc4hdmi1") == "default"


def test_resolve_alsa_device_leaves_everything_else_alone():
    assert resolve_alsa_device("plughw:CARD=Headphones") == "plughw:CARD=Headphones"
    assert resolve_alsa_device("default") == "default"
    assert resolve_alsa_device(None) is None


def test_a_persistent_failure_is_escalated(tmp_path: Path):
    """After the grace window the reader must be told there will be no audio."""
    from beatify_standalone.librespot import STARTUP_GRACE_ATTEMPTS

    supervisor = LibrespotSupervisor("go-librespot", "B", config_dir=tmp_path)
    supervisor._consecutive_failures = STARTUP_GRACE_ATTEMPTS + 1

    # The counter is what drives the escalation; assert the boundary explicitly
    # so a future refactor cannot quietly turn every failure back into noise.
    assert supervisor._consecutive_failures > STARTUP_GRACE_ATTEMPTS
