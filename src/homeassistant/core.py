"""The pieces of `homeassistant.core` that Beatify actually touches.

This is not an emulation of Home Assistant. It is the smallest object graph
that satisfies the surface `tools/check_ha_surface.py` measures — currently 24
`hass.*` attributes. Everything here exists because upstream reaches for it;
nothing here exists because Home Assistant happens to have it.

The two interesting pieces are `StateMachine` and `ServiceRegistry`. In Home
Assistant those are fed by dozens of integrations. Here they are fed by exactly
one driver (`beatify_standalone.spotify_driver`), which publishes a single
`media_player` state and answers the media_player service calls. Upstream never
learns the difference.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .exceptions import ServiceNotFound

_LOGGER = logging.getLogger(__name__)

CALLBACK_TYPE = Callable[[], None]


def callback(func):
    """Mark a function as safe to run in the event loop.

    In Home Assistant this is a real scheduling hint. Here everything already
    runs on the one loop, so the decorator only needs to preserve the function
    and the attribute HA code sometimes checks for.
    """
    setattr(func, "_hass_callback", True)
    return func


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class State:
    """A snapshot of one entity, shaped like `homeassistant.core.State`.

    Upstream reads `.state`, `.attributes`, `.entity_id`, `.name` and `.domain`
    — measured, not guessed — so those are what this provides.
    """

    __slots__ = ("entity_id", "state", "attributes", "last_changed", "last_updated")

    def __init__(
        self,
        entity_id: str,
        state: str,
        attributes: Mapping[str, Any] | None = None,
        last_changed: datetime | None = None,
        last_updated: datetime | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.last_changed = last_changed or _utcnow()
        self.last_updated = last_updated or self.last_changed

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def object_id(self) -> str:
        return self.entity_id.split(".", 1)[1]

    @property
    def name(self) -> str:
        return self.attributes.get("friendly_name") or self.object_id.replace("_", " ")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<State {self.entity_id}={self.state}>"


class StateMachine:
    """Entity states, plus the change notifications upstream subscribes to."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._states: dict[str, State] = {}
        self._listeners: list[tuple[frozenset[str] | None, Callable[[Any], Any]]] = []

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)

    def async_all(self, domain_filter: str | Iterable[str] | None = None) -> list[State]:
        if domain_filter is None:
            return list(self._states.values())
        domains = {domain_filter} if isinstance(domain_filter, str) else set(domain_filter)
        return [s for s in self._states.values() if s.domain in domains]

    def async_set(
        self,
        entity_id: str,
        state: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        """Publish a new state and notify listeners.

        Only the drivers call this; upstream is a pure reader.
        """
        old = self._states.get(entity_id)
        new = State(
            entity_id,
            state,
            attributes,
            # Preserve last_changed when only attributes moved: upstream's media
            # position maths reads media_position_updated_at, and a state object
            # that claims to have "changed" on every poll would be a lie.
            last_changed=old.last_changed if old is not None and old.state == state else None,
        )
        self._states[entity_id] = new
        self._notify(entity_id, old, new)

    def async_remove(self, entity_id: str) -> None:
        old = self._states.pop(entity_id, None)
        if old is not None:
            self._notify(entity_id, old, None)

    def _notify(self, entity_id: str, old: State | None, new: State | None) -> None:
        if not self._listeners:
            return
        event = _StateChangedEvent(entity_id, old, new)
        for entity_ids, listener in list(self._listeners):
            if entity_ids is not None and entity_id not in entity_ids:
                continue
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    self._hass.async_create_task(result)
            except Exception:  # noqa: BLE001 - a bad listener must not stop the game
                _LOGGER.exception("state listener failed for %s", entity_id)

    def async_listen(
        self, entity_ids: Iterable[str] | str | None, listener: Callable[[Any], Any]
    ) -> CALLBACK_TYPE:
        if entity_ids is None:
            key = None
        elif isinstance(entity_ids, str):
            key = frozenset({entity_ids})
        else:
            key = frozenset(entity_ids)
        record = (key, listener)
        self._listeners.append(record)

        def _unsub() -> None:
            try:
                self._listeners.remove(record)
            except ValueError:
                pass

        return _unsub


class _StateChangedEvent:
    """Shaped like HA's state_changed event: `event.data["new_state"]` etc."""

    __slots__ = ("data",)

    def __init__(self, entity_id: str, old: State | None, new: State | None) -> None:
        self.data = {"entity_id": entity_id, "old_state": old, "new_state": new}


class ServiceRegistry:
    """Domain/service dispatch.

    Unregistered services raise `ServiceNotFound` exactly as Home Assistant
    does. That matters: upstream imports `ServiceNotFound` and uses it to fall
    back between providers, so raising it is a supported code path rather than
    an error — it is how Beatify discovers that, say, `music_assistant` is not
    around.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._services: dict[str, dict[str, Callable[..., Any]]] = {}

    def async_register(
        self, domain: str, service: str, handler: Callable[..., Any]
    ) -> None:
        self._services.setdefault(domain, {})[service] = handler

    def has_service(self, domain: str, service: str) -> bool:
        return service in self._services.get(domain, {})

    def async_services(self) -> dict[str, dict[str, Any]]:
        return {d: dict.fromkeys(s, {}) for d, s in self._services.items()}

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: Mapping[str, Any] | None = None,
        blocking: bool = False,
        context: Any = None,
        target: Mapping[str, Any] | None = None,
        return_response: bool = False,
    ) -> Any:
        handler = self._services.get(domain, {}).get(service)
        if handler is None:
            raise ServiceNotFound(domain, service)

        data = dict(service_data or {})
        if target:
            data.update(target)

        result = handler(data)
        if asyncio.iscoroutine(result):
            result = await result
        return result if return_response else None


class Config:
    """`hass.config` — upstream uses `.path()` and `.country`."""

    def __init__(self, config_dir: Path | str, country: str | None = None) -> None:
        self.config_dir = str(config_dir)
        self.country = country

    def path(self, *parts: str) -> str:
        return str(Path(self.config_dir, *parts))


class HomeAssistant:
    """The object upstream threads through everything as `hass`."""

    def __init__(self, config_dir: Path | str, country: str | None = None) -> None:
        self.data: dict[str, Any] = {}
        self.config = Config(config_dir, country)
        self.states = StateMachine(self)
        self.services = ServiceRegistry(self)
        self.auth: Any = None  # set by the bootstrap (see beatify_standalone.auth)
        self.http: Any = None  # set by the bootstrap (see components.http)
        self.config_entries: Any = None  # set by the bootstrap
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """Resolved on access, not at construction.

        `hass` is built during bootstrap and used from inside the running loop
        (upstream reaches for `hass.loop.call_later` in `services/stats.py`).
        Binding the loop in `__init__` would make construction order matter and
        raises outright on modern Python when no loop is running yet.
        """
        return asyncio.get_running_loop()

    def async_add_executor_job(self, target: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
        return self.loop.run_in_executor(None, target, *args)

    def async_create_task(
        self, target: Coroutine[Any, Any, Any], name: str | None = None, **_: Any
    ) -> asyncio.Task[Any]:
        task = self.loop.create_task(target, name=name)
        self._track(task)
        return task

    def async_create_background_task(
        self, target: Coroutine[Any, Any, Any], name: str | None = None, **_: Any
    ) -> asyncio.Task[Any]:
        return self.async_create_task(target, name)

    def _track(self, task: asyncio.Task[Any]) -> None:
        """Hold a strong reference so the loop cannot garbage-collect the task."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def async_shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
