"""`BinarySensorEntity` — see `sensor.py`; same reasoning."""

from __future__ import annotations

from typing import Any


class BinarySensorEntity:
    _attr_is_on: Any = None
    _attr_should_poll = False

    def async_write_ha_state(self) -> None:
        return None

    async def async_added_to_hass(self) -> None:
        return None
