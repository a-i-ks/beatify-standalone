"""Nabu Casa cloud detection.

`server/companion_auth.py` calls this to refuse its auth bypass for requests
that arrived over HA's cloud tunnel. The standalone build has no such tunnel:
every request reaches it directly over the local network, so the honest answer
is always False.
"""

from __future__ import annotations

from typing import Any


def is_cloud_connection(hass: Any) -> bool:
    return False
