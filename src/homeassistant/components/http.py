"""The HTTP layer: `HomeAssistantView` on a plain aiohttp application.

In Home Assistant, `hass.http` is a component that owns the web server and
wraps every view in HA's auth middleware. Here the bootstrap owns the server
and this module adapts upstream's view classes onto it.

Auth is deliberately kept where upstream already put it. Beatify sets
`requires_auth = False` on nearly every view with the comment "auth handled
in-handler" — its own admin/player checks live inside the handlers and run
against `hass.auth`. So this layer enforces `requires_auth` only for the views
that actually ask for it, and lets Beatify's own logic do the rest unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

# Views are matched most-specific-first by aiohttp in registration order, and
# aiohttp cannot unregister a resource — the same constraint HA has, which is
# why upstream registers its routes exactly once per run (see `_ROUTES_REGISTERED`).


@dataclass
class StaticPathConfig:
    """Mirror of HA's static-path descriptor."""

    url_path: str
    path: str
    cache_headers: bool = True


class HomeAssistantView:
    """Base class for upstream's ~32 view classes.

    Subclasses declare `url`, `name`, `requires_auth` and define
    `async def get/post(self, request)`. That is the entire contract upstream
    relies on, plus the `self.json()` helper.
    """

    url: str | None = None
    name: str | None = None
    extra_urls: list[str] = []
    requires_auth: bool = True

    @staticmethod
    def json(
        result: Any, status_code: int = 200, headers: dict[str, str] | None = None
    ) -> web.Response:
        return web.json_response(result, status=status_code, headers=headers)

    @staticmethod
    def json_message(
        message: str, status_code: int = 200, message_code: str | None = None
    ) -> web.Response:
        payload: dict[str, Any] = {"message": message}
        if message_code is not None:
            payload["code"] = message_code
        return web.json_response(payload, status=status_code)


class HttpComponent:
    """The object published as `hass.http`."""

    def __init__(self, hass: Any, app: web.Application, server_port: int) -> None:
        self.hass = hass
        self.app = app
        self.server_port = server_port
        # `server/views.py:_loopback_url` reads this to choose http vs https.
        self.ssl_certificate: str | None = None
        self._registered: set[str] = set()

    def register_view(self, view: HomeAssistantView) -> None:
        urls = [view.url, *getattr(view, "extra_urls", [])]
        for url in [u for u in urls if u]:
            for method in ("get", "post", "put", "delete"):
                handler = getattr(view, method, None)
                if handler is None:
                    continue
                route_key = f"{method.upper()} {url}"
                if route_key in self._registered:
                    _LOGGER.debug("route already registered, skipping: %s", route_key)
                    continue
                self.app.router.add_route(
                    method.upper(), url, self._wrap(view, handler)
                )
                self._registered.add(route_key)

    def _wrap(self, view: HomeAssistantView, handler: Any) -> Any:
        requires_auth = getattr(view, "requires_auth", True)

        async def _handle(request: web.Request) -> web.StreamResponse:
            if requires_auth and not await self._is_authenticated(request):
                return web.json_response({"message": "Unauthorized"}, status=401)
            # HA passes URL placeholders as keyword arguments; aiohttp keeps
            # them in match_info.
            return await handler(request, **request.match_info)

        return _handle

    async def _is_authenticated(self, request: web.Request) -> bool:
        auth = getattr(self.hass, "auth", None)
        if auth is None:
            return False
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else request.query.get("token")
        if not token:
            return False
        return auth.async_validate_access_token(token) is not None

    async def async_register_static_paths(self, configs: list[StaticPathConfig]) -> None:
        for config in configs:
            directory = Path(config.path)
            if not directory.is_dir():
                _LOGGER.warning("static path does not exist: %s", directory)
                continue
            self.app.router.add_static(
                config.url_path,
                str(directory),
                # Upstream turns caching off on purpose so a version bump can
                # never be masked by a month-old cached admin.min.js.
                append_version=False,
                show_index=False,
            )
            _LOGGER.debug("static: %s -> %s", config.url_path, directory)
