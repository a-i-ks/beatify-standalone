"""`Store` — HA's versioned JSON store, reduced to a file.

Upstream calls exactly `async_load()` and `async_save()`. HA's real store
batches writes through a delayed-save queue; here a direct write off the event
loop is both simpler and safer, since a travel box can lose power at any time.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class Store:
    """Persist one JSON document, keyed by name, under `<config>/.storage`."""

    def __init__(self, hass: Any, version: int, key: str, **_: Any) -> None:
        self.hass = hass
        self.version = version
        self.key = key
        self.path = Path(hass.config.path(".storage", key))

    async def async_load(self) -> Any:
        return await self.hass.async_add_executor_job(self._load)

    async def async_save(self, data: Any) -> None:
        await self.hass.async_add_executor_job(self._save, data)

    async def async_remove(self) -> None:
        await self.hass.async_add_executor_job(self._remove)

    def _load(self) -> Any:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LOGGER.exception("could not read store %s", self.key)
            return None
        # HA wraps the payload; mirror that so a store written by a real HA
        # install (a copied .storage directory) still loads here.
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload

    def _save(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self.version, "key": self.key, "data": data}
        # Write-then-rename: a half-written store is worse than a missing one,
        # and this box will be unplugged mid-party sooner or later.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.path)

    def _remove(self) -> None:
        self.path.unlink(missing_ok=True)
