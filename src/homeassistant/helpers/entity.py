"""`DeviceInfo` — a dict in HA, a dict here."""

from __future__ import annotations

from typing import Any


class DeviceInfo(dict):
    """HA's DeviceInfo is a TypedDict; upstream only ever constructs one."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
