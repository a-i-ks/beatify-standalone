"""Config-entry stand-ins.

Upstream's real config flow (`config_flow.py`) is dead code here — the
standalone build configures itself through Beatify's own web wizard
(`www/js/wizard.js` + `server/setup_state.py`). These classes exist so the
module still imports, and so `async_setup_entry(hass, entry)` receives an
object with the four members it actually uses: `entry_id`, `data`, `options`
and `async_on_unload` / `add_update_listener`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

CALLBACK_TYPE = Callable[[], None]


class ConfigEntry:
    """A config entry backed by a plain JSON file."""

    def __init__(
        self,
        entry_id: str = "beatify",
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        path: Path | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.domain = "beatify"
        self.title = "Beatify"
        self.data: dict[str, Any] = dict(data or {})
        self.options: dict[str, Any] = dict(options or {})
        self._path = path
        self._unloaders: list[CALLBACK_TYPE] = []
        self._update_listeners: list[Callable[..., Any]] = []

    @classmethod
    def load(cls, path: Path, entry_id: str = "beatify") -> ConfigEntry:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = {}
        return cls(entry_id, raw.get("data", {}), raw.get("options", {}), path)

    def save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"data": self.data, "options": self.options}, indent=2),
            encoding="utf-8",
        )

    def async_on_unload(self, func: CALLBACK_TYPE) -> None:
        self._unloaders.append(func)

    def add_update_listener(self, listener: Callable[..., Any]) -> CALLBACK_TYPE:
        self._update_listeners.append(listener)

        def _unsub() -> None:
            try:
                self._update_listeners.remove(listener)
            except ValueError:
                pass

        return _unsub

    def run_unloaders(self) -> None:
        while self._unloaders:
            unloader = self._unloaders.pop()
            try:
                unloader()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass


class ConfigEntries:
    """`hass.config_entries` — one entry, no discovery, no reloads."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    def async_entries(self, domain: str | None = None) -> list[ConfigEntry]:
        if domain in (None, "beatify"):
            return [self._entry]
        # Notably: `music_assistant`. Returning empty is how upstream's library
        # code discovers that MA is not installed, which is exactly true here.
        return []

    def async_get_entry(self, entry_id: str) -> ConfigEntry | None:
        return self._entry if entry_id == self._entry.entry_id else None

    async def async_forward_entry_setups(self, entry: ConfigEntry, platforms: list[str]) -> None:
        """Sensor/binary_sensor platforms carry no meaning without HA's UI."""
        return None

    async def async_unload_platforms(self, entry: ConfigEntry, platforms: list[str]) -> bool:
        return True


class ConfigFlow:  # noqa: D101 - import target only
    pass


class ConfigFlowResult(dict):  # noqa: D101 - import target only
    pass


class OptionsFlow:  # noqa: D101 - import target only
    pass
