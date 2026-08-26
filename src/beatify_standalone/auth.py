"""Auth: the small slice of Home Assistant's OAuth that Beatify's admin needs.

Beatify's admin UI (`www/js/ha-auth.js`) is unmodified, so it does what it does
against a real Home Assistant:

  1. redirect the browser to ``/auth/authorize?response_type=code&client_id=…``
  2. expect a redirect back to ``redirect_uri?code=…&state=…``
  3. have the *server* (`server/views.py:BeatifyAuthCallbackView`) exchange that
     code at ``/auth/token`` over loopback, then stash a refresh cookie
  4. mint fresh access tokens from ``/auth/token`` with ``grant_type=refresh_token``

So those three endpoints are what this module provides, plus the
``hass.auth.async_validate_access_token`` that upstream calls (synchronously —
verified at both call sites) to check the resulting Bearer tokens.

Scope note: this is a LAN party box, not an identity provider. A single admin
PIN gates the authorize step, and tokens are opaque random strings rather than
signed JWTs — there is no second party that needs to verify them offline.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

ACCESS_TOKEN_TTL = 1800  # seconds; ha-auth.js refreshes well before this
AUTH_CODE_TTL = 120  # a code is redeemed by the callback view within a second


@dataclass(frozen=True)
class AccessTokenInfo:
    """Stand-in for HA's RefreshToken; upstream only checks `is not None`."""

    token_id: str
    expires_at: float


class AuthManager:
    """Issues and validates the tokens the admin UI carries."""

    def __init__(
        self, data_dir: Path, pin: str | None = None, require_pin: bool = True
    ) -> None:
        self.require_pin = require_pin
        self._path = data_dir / "auth.json"
        self._refresh_tokens: dict[str, dict[str, Any]] = {}
        self._access_tokens: dict[str, AccessTokenInfo] = {}
        self._codes: dict[str, dict[str, Any]] = {}
        self._pin = pin
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LOGGER.warning("auth store unreadable, starting fresh: %s", self._path)
            return
        self._refresh_tokens = raw.get("refresh_tokens", {})
        if self._pin is None:
            self._pin = raw.get("pin")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"refresh_tokens": self._refresh_tokens, "pin": self._pin}),
            encoding="utf-8",
        )
        # The PIN lives here; keep it off other local accounts.
        self._path.chmod(0o600)

    # -- PIN --------------------------------------------------------------

    @property
    def pin(self) -> str:
        """The admin PIN, generated on first use and then stable."""
        if not self._pin:
            self._pin = f"{secrets.randbelow(10**6):06d}"
            self._save()
            _LOGGER.warning("Generated admin PIN: %s  (stored in %s)", self._pin, self._path)
        return self._pin

    def check_pin(self, candidate: str) -> bool:
        """Validate a PIN, or wave it through when none is demanded.

        Kept as one method rather than scattering `if require_pin` across every
        page: a check that is sometimes skipped is easier to reason about in one
        place than a check that is sometimes absent in five.
        """
        if not self.require_pin:
            return True
        return secrets.compare_digest(str(candidate or ""), self.pin)

    # -- tokens -----------------------------------------------------------

    def async_validate_access_token(self, token: str) -> AccessTokenInfo | None:
        """Validate a Bearer token.

        Synchronous on purpose: upstream calls this without `await` in both
        `server/companion_auth.py` and `server/ws_handlers/_helpers.py`.
        """
        info = self._access_tokens.get(token)
        if info is None:
            return None
        if info.expires_at < time.time():
            self._access_tokens.pop(token, None)
            return None
        return info

    def create_authorization_code(self, client_id: str, redirect_uri: str) -> str:
        code = secrets.token_urlsafe(24)
        self._codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "expires_at": time.time() + AUTH_CODE_TTL,
        }
        self._prune()
        return code

    def exchange_code(self, code: str, client_id: str) -> dict[str, Any] | None:
        entry = self._codes.pop(code, None)
        if entry is None or entry["expires_at"] < time.time():
            return None
        if entry["client_id"] != client_id:
            _LOGGER.warning("code exchange with mismatched client_id")
            return None

        refresh_token = secrets.token_urlsafe(32)
        self._refresh_tokens[refresh_token] = {
            "client_id": client_id,
            "created": time.time(),
        }
        self._save()
        return {**self._mint_access(refresh_token), "refresh_token": refresh_token}

    def refresh(self, refresh_token: str, client_id: str | None = None) -> dict[str, Any] | None:
        entry = self._refresh_tokens.get(refresh_token)
        if entry is None:
            return None
        # client_id is echoed by ha-auth.js; a mismatch means the cookie was
        # carried to a different origin, so refuse rather than mint.
        if client_id and entry.get("client_id") != client_id:
            return None
        return self._mint_access(refresh_token)

    def revoke(self, refresh_token: str) -> None:
        if self._refresh_tokens.pop(refresh_token, None) is not None:
            self._save()

    def _mint_access(self, refresh_token: str) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + ACCESS_TOKEN_TTL
        self._access_tokens[token] = AccessTokenInfo(refresh_token, expires_at)
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
        }

    def _prune(self) -> None:
        now = time.time()
        for code, entry in list(self._codes.items()):
            if entry["expires_at"] < now:
                del self._codes[code]
        for token, info in list(self._access_tokens.items()):
            if info.expires_at < now:
                del self._access_tokens[token]


# -- HTTP endpoints -------------------------------------------------------

_LOGIN_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Beatify - Anmeldung</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; display: grid; place-items: center;
         min-height: 100dvh; margin: 0; background: #12121a; color: #f2f2f7; }}
  form {{ background: #1e1e2a; padding: 2rem; border-radius: 1rem; width: min(20rem, 90vw);
          box-shadow: 0 1rem 3rem rgb(0 0 0 / .4); }}
  h1 {{ font-size: 1.25rem; margin: 0 0 1.25rem; }}
  input {{ width: 100%; box-sizing: border-box; padding: .85rem; font-size: 1.5rem;
           text-align: center; letter-spacing: .3em; border-radius: .5rem;
           border: 1px solid #3a3a4a; background: #12121a; color: inherit; }}
  /* 3rem tall so it is comfortably tappable one-handed on a small phone. */
  button {{ width: 100%; margin-top: 1rem; padding: .95rem; font-size: 1rem;
            min-height: 3rem; border: 0; border-radius: .5rem;
            background: #6c5ce7; color: #fff; font-weight: 600; }}
  .err {{ color: #ff6b6b; font-size: .875rem; margin-top: .75rem; }}
</style></head><body>
<form method="post">
  <h1>{heading}</h1>
  {pin_field}
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <button type="submit">Anmelden</button>
  {error}
</form></body></html>
"""


def _same_origin(request: web.Request, *urls: str) -> bool:
    """Only redirect back to the host the request came in on.

    Home Assistant's own rule for indieauth clients is that client_id and
    redirect_uri share the page's host; ha-auth.js relies on that auto-allow.
    Enforcing it here is what keeps `/auth/authorize` from becoming an open
    redirector.
    """
    host = request.headers.get("X-Forwarded-Host") or request.host
    for url in urls:
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != host:
            return False
    return True


def register_auth_routes(app: web.Application, auth: AuthManager) -> None:
    """Wire the three OAuth endpoints ha-auth.js expects onto the app."""

    async def authorize(request: web.Request) -> web.StreamResponse:
        # On POST the parameters ride in the form body, not the query string —
        # the login page round-trips them as hidden fields. Read the body first,
        # then validate, or a submitted form looks like a request with no
        # client_id at all.
        client_id = request.query.get("client_id", "")
        redirect_uri = request.query.get("redirect_uri", "")
        state = request.query.get("state", "")
        form: Any = {}
        if request.method == "POST":
            form = await request.post()
            client_id = str(form.get("client_id", client_id))
            redirect_uri = str(form.get("redirect_uri", redirect_uri))
            state = str(form.get("state", state))

        if not client_id or not redirect_uri:
            return web.Response(text="missing client_id or redirect_uri", status=400)
        if not _same_origin(request, client_id, redirect_uri):
            return web.Response(text="Invalid redirect URI", status=400)

        error = ""
        if request.method == "POST":
            if auth.check_pin(str(form.get("pin", ""))):
                code = auth.create_authorization_code(client_id, redirect_uri)
                params = {"code": code}
                if state:
                    params["state"] = state
                separator = "&" if "?" in redirect_uri else "?"
                raise web.HTTPFound(f"{redirect_uri}{separator}{urlencode(params)}")
            error = '<p class="err">Falsche PIN.</p>'

        # With no PIN demanded, showing an input that accepts anything is worse
        # than showing none: it reads as a lock that does not lock.
        if auth.require_pin:
            heading = "Beatify - Admin-PIN"
            pin_field = (
                '<input name="pin" type="password" inputmode="numeric" '
                'autocomplete="one-time-code" autofocus placeholder="------">'
            )
        else:
            heading = "Beatify - Anmelden"
            pin_field = ""

        return web.Response(
            text=_LOGIN_PAGE.format(
                heading=heading,
                pin_field=pin_field,
                client_id=html.escape(client_id, quote=True),
                redirect_uri=html.escape(redirect_uri, quote=True),
                state=html.escape(state, quote=True),
                error=error,
            ),
            content_type="text/html",
        )

    async def token(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        grant_type = str(form.get("grant_type", ""))
        client_id = str(form.get("client_id", "")) or None

        if grant_type == "authorization_code":
            result = auth.exchange_code(str(form.get("code", "")), client_id or "")
        elif grant_type == "refresh_token":
            result = auth.refresh(str(form.get("refresh_token", "")), client_id)
        else:
            return web.json_response({"error": "unsupported_grant_type"}, status=400)

        if result is None:
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(result)

    async def revoke(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        auth.revoke(str(form.get("token", "")))
        return web.json_response({})

    app.router.add_route("GET", "/auth/authorize", authorize)
    app.router.add_route("POST", "/auth/authorize", authorize)
    app.router.add_route("POST", "/auth/token", token)
    app.router.add_route("POST", "/auth/revoke", revoke)
