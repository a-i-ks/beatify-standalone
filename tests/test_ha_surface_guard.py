"""The guard has to fail when upstream drifts — proven, not assumed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, UPSTREAM

GUARD = ROOT / "tools" / "check_ha_surface.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *args], capture_output=True, text=True, check=False
    )


def test_pinned_upstream_passes() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: upstream stays inside" in result.stdout


def test_baseline_matches_the_vendored_tree() -> None:
    """A stale baseline would make the guard blind."""
    result = _run("--report")
    baseline = json.loads((ROOT / "tools" / "ha_surface.json").read_text())
    for item in baseline["imports"]:
        assert item in result.stdout


def test_a_new_ha_import_fails_the_check(tmp_path: Path) -> None:
    tree = tmp_path / "beatify"
    shutil.copytree(UPSTREAM, tree)
    (tree / "drifted.py").write_text(
        "from homeassistant.components.recorder import history\n", encoding="utf-8"
    )

    result = _run(str(tree))
    assert result.returncode == 1
    assert "homeassistant.components.recorder.history" in result.stdout
    assert "NEW imports" in result.stdout


def test_a_new_service_call_fails_the_check(tmp_path: Path) -> None:
    tree = tmp_path / "beatify"
    shutil.copytree(UPSTREAM, tree)
    (tree / "drifted.py").write_text(
        'async def go(hass):\n    await hass.services.async_call("notify", "persistent_notification", {})\n',
        encoding="utf-8",
    )

    result = _run(str(tree))
    assert result.returncode == 1
    assert "notify.persistent_notification" in result.stdout


def test_a_new_hass_attribute_fails_the_check(tmp_path: Path) -> None:
    tree = tmp_path / "beatify"
    shutil.copytree(UPSTREAM, tree)
    (tree / "drifted.py").write_text(
        "def go(hass):\n    return hass.bus.async_fire\n", encoding="utf-8"
    )

    result = _run(str(tree))
    assert result.returncode == 1
    assert "hass.bus" in result.stdout


def test_breaking_the_sonos_seam_fails_the_check(tmp_path: Path) -> None:
    """The single most important assumption the port makes."""
    tree = tmp_path / "beatify"
    shutil.copytree(UPSTREAM, tree)
    target = tree / "services" / "media_player.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            'if self._platform == "sonos":', 'if self._platform == "sonos_v2":'
        ),
        encoding="utf-8",
    )

    result = _run("--seams-only", str(tree))
    assert result.returncode == 1
    assert "_play_song still dispatches platform 'sonos'" in result.stdout


def test_seams_pass_on_the_pinned_tree() -> None:
    result = _run("--seams-only")
    assert result.returncode == 0, result.stdout
    assert "FAIL" not in result.stdout
