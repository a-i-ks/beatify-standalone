"""A registry holding exactly the players our drivers publish.

Upstream uses this to learn an entity's `platform` and `unique_id` — that is
how it decides which playback path to take. See `beatify_standalone.player`
for why the librespot player registers itself as platform "sonos".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_KEY = "_beatify_shim_entity_registry"


@dataclass
class RegistryEntry:
    entity_id: str
    unique_id: str
    platform: str
    original_name: str | None = None
    config_entry_id: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


class EntityRegistry:
    def __init__(self) -> None:
        self.entities: dict[str, RegistryEntry] = {}

    def async_get(self, entity_id: str) -> RegistryEntry | None:
        return self.entities.get(entity_id)

    def register(self, entry: RegistryEntry) -> RegistryEntry:
        self.entities[entry.entity_id] = entry
        return entry


def async_get(hass: Any) -> EntityRegistry:
    registry = hass.data.get(_KEY)
    if registry is None:
        registry = EntityRegistry()
        hass.data[_KEY] = registry
    return registry
