"""Music Assistant client lookup — always empty here.

The standalone build replaces Music Assistant with librespot plus the Spotify
Web API, so there is no MA client to hand out. `library/ma_client.py` imports
this lazily inside a try block and treats a failure as "MA not available",
which is precisely the truth.
"""

from __future__ import annotations

from typing import Any


def get_music_assistant_client(hass: Any, config_entry_id: str) -> Any:
    raise RuntimeError("Music Assistant is not part of the standalone build")
