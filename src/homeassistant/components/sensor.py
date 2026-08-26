"""`SensorEntity` — base class for upstream's sensor platform.

The platform is never set up (`async_forward_entry_setups` is a no-op), so
these entities are constructed nowhere. The class exists for the import.
"""

from __future__ import annotations

from typing import Any


class SensorEntity:
    _attr_native_value: Any = None
    _attr_should_poll = False

    def async_write_ha_state(self) -> None:
        return None

    async def async_added_to_hass(self) -> None:
        return None
