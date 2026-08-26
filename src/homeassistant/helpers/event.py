"""The two event helpers upstream subscribes with."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

CALLBACK_TYPE = Callable[[], None]


def async_track_time_interval(
    hass: Any, action: Callable[[datetime], Any], interval: timedelta, **_: Any
) -> CALLBACK_TYPE:
    """Call `action(now)` every `interval` until unsubscribed.

    Beatify uses this for the round supervisor — the backstop that rescues a
    round whose timer task died. It therefore must survive a raising action,
    which is why the exception is logged and the loop continues.
    """
    seconds = interval.total_seconds()
    stopped = asyncio.Event()

    async def _runner() -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=seconds)
                return
            except (TimeoutError, asyncio.TimeoutError):
                pass
            try:
                result = action(datetime.now(timezone.utc))
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the supervisor must keep ticking
                _LOGGER.exception("tracked interval action failed")

    task = hass.async_create_background_task(_runner(), "beatify-interval")

    def _unsub() -> None:
        stopped.set()
        task.cancel()

    return _unsub


def async_track_state_change_event(
    hass: Any, entity_ids: Iterable[str] | str, action: Callable[[Any], Any], **_: Any
) -> CALLBACK_TYPE:
    return hass.states.async_listen(entity_ids, action)
