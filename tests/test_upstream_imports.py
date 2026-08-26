"""Every upstream module must import against the shim.

This is the port's canary. Upstream ships ~12 commits a day; a new
`from homeassistant.…` import anywhere in 72 modules breaks the build, and this
test names the offending module instead of leaving it to fail at runtime in an
AirBnB.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

from conftest import UPSTREAM

# `config_flow.py` is genuinely dead code here: nothing imports it (Home
# Assistant discovers it by filename), and the standalone build configures
# itself through Beatify's own web wizard. It is the only module that pulls in
# `voluptuous`, which we deliberately do not ship to the Pi.
EXCLUDED = {"custom_components.beatify.config_flow"}


def _upstream_modules() -> list[str]:
    modules = []
    for path in sorted(UPSTREAM.rglob("*.py")):
        parts = list(path.relative_to(UPSTREAM.parent.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(".".join(parts))
    return modules


@pytest.mark.parametrize("module", [m for m in _upstream_modules() if m not in EXCLUDED])
def test_module_imports(module: str) -> None:
    importlib.import_module(module)


def test_config_flow_is_still_the_only_exclusion() -> None:
    """If upstream drops voluptuous, the exclusion should go too."""
    source = (UPSTREAM / "config_flow.py").read_text(encoding="utf-8")
    assert "import voluptuous" in source


def test_nothing_imports_config_flow() -> None:
    """The exclusion is only safe while no other module reaches for it."""
    for path in UPSTREAM.rglob("*.py"):
        if path.name == "config_flow.py":
            continue
        assert "config_flow" not in path.read_text(encoding="utf-8"), path
