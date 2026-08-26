"""The Connect-daemon supervisor: invocation and config generation."""

from __future__ import annotations

from pathlib import Path

from beatify_standalone.librespot import FLAVOR_GO, FLAVOR_RUST, LibrespotSupervisor


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
