"""`EntityComponent` — used by upstream only in an isinstance() scan.

`services/tts.py` walks `hass.data` looking for a value that is an
EntityComponent with `domain == "tts"`, to resolve a TTS entity object. In the
standalone build no such object is ever stored, so the scan finds nothing and
upstream falls back to the plain `tts.speak` service call — which is the path
`beatify_standalone.tts_driver` implements.
"""

from __future__ import annotations

from typing import Any


class EntityComponent:
    """Deliberately inert: nothing here should ever satisfy the isinstance scan."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.domain: str | None = None

    def get_entity(self, entity_id: str) -> Any:
        return None
