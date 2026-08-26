"""Sidebar panel registration — meaningless without HA's frontend.

Upstream registers a "Beatify" panel in HA's sidebar. The standalone build is
reached directly at `/beatify/admin`, so these are no-ops that exist to keep
the import and the call sites working.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def async_register_built_in_panel(hass: Any, *args: Any, **kwargs: Any) -> None:
    _LOGGER.debug("sidebar panel ignored (no HA frontend in standalone)")


def async_remove_panel(hass: Any, *args: Any, **kwargs: Any) -> None:
    return None
