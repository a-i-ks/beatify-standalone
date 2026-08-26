"""Shared fixtures.

Path order is the one thing that must not drift: the shim's `homeassistant`
package has to be importable before the vendored upstream tree, or upstream
picks up whatever else is installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT / "src"))

UPSTREAM = ROOT / "vendor" / "custom_components" / "beatify"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory


@pytest.fixture
async def hass(data_dir: Path):
    """A shim HomeAssistant wired the way the bootstrap wires it.

    Async so there is a running loop, mirroring production: the bootstrap only
    ever builds `hass` from inside the event loop.
    """
    from homeassistant.core import HomeAssistant

    instance = HomeAssistant(data_dir, "DE")
    yield instance
    await instance.async_shutdown()
