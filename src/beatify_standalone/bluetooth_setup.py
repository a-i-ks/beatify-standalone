"""Pair a Bluetooth controller from a phone.

The chicken-and-egg this solves: to reach Batocera's own pairing menu you need
an input device, and the input device you have is the controller you are trying
to pair. The usual escape is a USB cable. This is the escape for when you did
not pack one.

Batocera already knows how to pair — `batocera-bluetooth trust input` starts
discovery and trusts whatever input device turns up, driven by the
`batocera-bluetooth-agent` daemon. That is exactly what the menu item runs, so a
controller paired here behaves identically to one paired from the television.
This module drives that command rather than reimplementing pairing against
bluetoothctl.

What it adds is knowing when to stop. The command itself polls for about a
minute and says little; on a phone, a page that sits there saying "searching"
with no way to tell success from a hang is worse than useless. So the session
snapshots which devices were already trusted, watches for a new one, reports it
the moment it appears, and can be cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
from typing import Any

from aiohttp import web

from .webui import message, page

_LOGGER = logging.getLogger(__name__)

# `batocera-bluetooth list` prints one XML-ish line per trusted device:
#   <device id="AA:BB:CC:DD:EE:FF" name="Xbox Wireless Controller" type="joystick" connected="yes" />
_DEVICE = re.compile(
    r'<device\s+id="(?P<id>[^"]*)"\s+name="(?P<name>[^"]*)"\s+'
    r'type="(?P<type>[^"]*)"\s+connected="(?P<connected>[^"]*)"'
)
_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.I)

PAIR_TIMEOUT = 75
POLL_INTERVAL = 2.0


async def _run(*argv: str, timeout: float = 20) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except (OSError, ValueError) as err:
        return 127, str(err)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        process.kill()
        await process.wait()
        return 124, "timed out"
    return process.returncode or 0, out.decode("utf-8", "replace")


async def paired_devices() -> list[dict[str, str]]:
    code, out = await _run("batocera-bluetooth", "list")
    if code != 0:
        _LOGGER.warning("bluetooth list failed (%s): %s", code, out.strip()[:200])
        return []
    return [m.groupdict() for m in _DEVICE.finditer(out)]


async def remove_device(address: str) -> tuple[bool, str]:
    if not _MAC.match(address):
        return False, "Keine gültige Geräteadresse."
    code, out = await _run("batocera-bluetooth", "remove", address)
    if code != 0:
        return False, out.strip()[:200] or "Entfernen fehlgeschlagen."
    return True, "Gerät entfernt."


class PairingSession:
    """One pairing attempt, observable from the page while it runs."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.found: str | None = None
        self.started_at: float = 0.0
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def remaining(self) -> int:
        if not self.running:
            return 0
        return max(0, int(PAIR_TIMEOUT - (time.monotonic() - self.started_at)))

    def _say(self, text: str) -> None:
        self.lines.append(text)
        del self.lines[:-40]

    async def start(self) -> bool:
        if self.running:
            return False
        self.lines = []
        self.found = None
        self.started_at = time.monotonic()
        self._say("Suche gestartet. Halte die Pair-Taste, bis das Logo schnell blinkt.")
        self._task = asyncio.get_running_loop().create_task(self._run_session())
        return True

    async def cancel(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._say("Suche abgebrochen.")

    async def _run_session(self) -> None:
        # Knowing what was already there is what makes "a controller appeared"
        # detectable at all — the command itself does not announce success in a
        # form worth parsing.
        before = {d["id"] for d in await paired_devices()}
        try:
            await self._pair(before)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed pairing must not kill the server
            _LOGGER.exception("pairing session failed")
            self._say("Unerwarteter Fehler — siehe Log auf der Box.")
        finally:
            await self._terminate()

    async def _pair(self, before: set[str]) -> None:
        try:
            self._process = await asyncio.create_subprocess_exec(
                "batocera-bluetooth", "trust", "input",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (OSError, ValueError) as err:
            self._say(f"Konnte die Suche nicht starten: {err}")
            return

        reader = asyncio.get_running_loop().create_task(self._pump())
        deadline = time.monotonic() + PAIR_TIMEOUT
        try:
            while time.monotonic() < deadline:
                if self._process.returncode is not None:
                    self._say("Suchvorgang beendet.")
                    break
                new = [d for d in await paired_devices() if d["id"] not in before]
                if new:
                    device = new[0]
                    self.found = device["id"]
                    self._say(f"Gekoppelt: {device['name']}")
                    return
                await asyncio.sleep(POLL_INTERVAL)
            else:
                self._say("Zeit abgelaufen — kein Controller gefunden.")
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

        # The command may have paired something on its way out.
        new = [d for d in await paired_devices() if d["id"] not in before]
        if new and self.found is None:
            self.found = new[0]["id"]
            self._say(f"Gekoppelt: {new[0]['name']}")

    async def _pump(self) -> None:
        """Surface whatever the command says, so a stall is visible."""
        if self._process is None or self._process.stdout is None:
            return
        while True:
            line = await self._process.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self._say(text)

    async def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            process.kill()
            await process.wait()


def _devices_html(devices: list[dict[str, str]]) -> str:
    if not devices:
        return '<p class="empty">Noch kein Controller gekoppelt.</p>'
    cards = []
    for device in devices:
        connected = device.get("connected") == "yes"
        address = html.escape(device.get("id") or "", quote=True)
        cards.append(
            '<div class="card"><div class="row">'
            f'<span class="dot {"on" if connected else "off"}"></span>'
            '<span class="grow">'
            f'<strong>{html.escape(device.get("name") or "Unbenannt")}</strong>'
            f'<small>{html.escape(device.get("id") or "")} · '
            f'{"verbunden" if connected else "nicht verbunden"}</small></span>'
            '<form method="post" style="width:auto;margin:0">'
            '<input type="hidden" name="action" value="remove">'
            f'<input type="hidden" name="address" value="{address}">'
            '<button class="ghost" style="margin:0;padding:.6rem .8rem;min-height:0;'
            'font-size:.85rem;width:auto">Entfernen</button>'
            "</form></div></div>"
        )
    return "".join(cards)


_SCRIPT = """<script>
  const devices = document.getElementById('devices');
  const log = document.getElementById('log');
  const status = document.getElementById('status');
  const startBtn = document.getElementById('start');
  const cancelBtn = document.getElementById('cancel');

  async function poll() {
    let d;
    try {
      d = await (await fetch('/beatify/bluetooth/status')).json();
    } catch (e) {
      // A dropped poll is not a failed pairing; try again rather than alarm.
      setTimeout(poll, 3000);
      return;
    }
    devices.innerHTML = d.devices_html;
    log.textContent = d.log.join('\\n');
    log.scrollTop = log.scrollHeight;
    log.style.display = d.log.length ? 'block' : 'none';

    if (d.pairing) {
      status.innerHTML = '<span class="spinner"></span>Suche läuft — noch ' + d.remaining + ' s';
      startBtn.style.display = 'none';
      cancelBtn.style.display = 'block';
      setTimeout(poll, 1500);
    } else {
      status.textContent = d.found ? 'Controller gekoppelt.' : '';
      startBtn.style.display = 'block';
      startBtn.disabled = false;
      cancelBtn.style.display = 'none';
    }
  }
  poll();
</script>"""


def register_bluetooth_routes(app: web.Application, auth: Any) -> None:
    session = PairingSession()

    def pin_field() -> str:
        if not getattr(auth, "require_pin", True):
            return ""
        return (
            '<label for="pin">Admin-PIN</label>'
            '<input id="pin" name="pin" type="password" inputmode="numeric" '
            'autocomplete="one-time-code" placeholder="------">'
        )

    async def render(msg: str = "") -> web.Response:
        body = f"""{msg}
<h2>Gekoppelt</h2>
<div id="devices">{_devices_html(await paired_devices())}</div>

<h2>Neuen Controller koppeln</h2>
<p class="empty">
  Halte am Xbox-Controller die <strong>Pair-Taste</strong> oben gedrückt, bis das
  Xbox-Logo schnell blinkt. Dann hier starten.
</p>
<p id="status" class="empty"></p>
<form method="post" id="startform">
  <input type="hidden" name="action" value="pair">
  {pin_field()}
  <button type="submit" id="start">Suche starten</button>
</form>
<form method="post" id="cancelform">
  <input type="hidden" name="action" value="cancel">
  <button type="submit" class="ghost" id="cancel" style="display:none">Suche abbrechen</button>
</form>
<pre class="log" id="log" style="display:none"></pre>"""
        return page("Beatify · Controller", "Controller koppeln",
                    "Verbindet ein Gamepad per Bluetooth.", body, _SCRIPT)

    async def get(request: web.Request) -> web.StreamResponse:
        return await render()

    async def status(request: web.Request) -> web.StreamResponse:
        return web.json_response(
            {
                "pairing": session.running,
                "remaining": session.remaining,
                "found": session.found,
                "log": session.lines[-40:],
                "devices_html": _devices_html(await paired_devices()),
            }
        )

    async def post(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        action = str(form.get("action") or "")

        if action == "cancel":
            await session.cancel()
            return await render(message("Suche abgebrochen."))

        if not auth.check_pin(str(form.get("pin") or "")):
            return await render(message("Falsche Admin-PIN.", ok=False))

        if action == "remove":
            ok, detail = await remove_device(str(form.get("address") or ""))
            return await render(message(html.escape(detail), ok=ok))

        if action == "pair":
            if not await session.start():
                return await render(message("Eine Suche läuft bereits.", ok=False))
            return await render(message("Suche gestartet."))

        return await render(message("Unbekannte Aktion.", ok=False))

    app.router.add_route("GET", "/beatify/bluetooth", get)
    app.router.add_route("POST", "/beatify/bluetooth", post)
    app.router.add_route("GET", "/beatify/bluetooth/status", status)
