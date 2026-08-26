"""Pair a Bluetooth controller from a phone.

The chicken-and-egg this solves: to reach Batocera's own pairing menu you need
an input device, and the input device you have is the controller you are trying
to pair. The usual escape is a USB cable. This is the escape for when you did
not pack one.

Batocera already knows how to do the pairing — `batocera-bluetooth trust input`
starts discovery and pairs whatever input device shows up, driven by the
`batocera-bluetooth-agent` daemon, and reports progress through
`/var/run/bt_status`. That is exactly what the ES menu item runs. This module
drives that same command rather than reimplementing pairing against bluetoothctl,
so a controller paired here behaves identically to one paired from the menu.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

# `batocera-bluetooth list` prints one XML-ish line per trusted device:
#   <device id="AA:BB:CC:DD:EE:FF" name="Xbox Wireless Controller" type="joystick" connected="yes" />
_DEVICE = re.compile(
    r'<device\s+id="(?P<id>[^"]*)"\s+name="(?P<name>[^"]*)"\s+'
    r'type="(?P<type>[^"]*)"\s+connected="(?P<connected>[^"]*)"'
)
_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.I)

# `trust input` polls for about a minute before giving up.
PAIR_TIMEOUT = 90


class PairingSession:
    """One pairing attempt, so the page can poll its progress."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self) -> bool:
        if self.running:
            return False
        self.lines = ["Suche läuft — Controller jetzt in den Kopplungsmodus bringen."]
        self.task = asyncio.get_running_loop().create_task(self._run())
        return True

    async def _run(self) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                "batocera-bluetooth", "trust", "input",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (OSError, ValueError) as err:
            self.lines.append(f"Konnte die Suche nicht starten: {err}")
            return

        async def pump() -> None:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.lines.append(text)

        try:
            await asyncio.wait_for(pump(), timeout=PAIR_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            self.lines.append("Zeit abgelaufen — keine Kopplung zustande gekommen.")
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()
            self.lines.append("Suche beendet.")

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()


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
        return 124, "timed out"
    return process.returncode or 0, out.decode("utf-8", "replace")


async def paired_devices() -> list[dict[str, str]]:
    code, out = await _run("batocera-bluetooth", "list")
    if code != 0:
        _LOGGER.warning("bluetooth list failed (%s): %s", code, out.strip()[:200])
        return []
    return [match.groupdict() for match in _DEVICE.finditer(out)]


async def remove_device(address: str) -> tuple[bool, str]:
    if not _MAC.match(address):
        return False, "Keine gültige Geräteadresse."
    code, out = await _run("batocera-bluetooth", "remove", address)
    if code != 0:
        return False, out.strip()[:200] or "Entfernen fehlgeschlagen."
    return True, "Gerät entfernt."


_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Beatify - Controller</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #12121a; color: #f2f2f7; padding: 1.5rem 1rem 3rem; }}
  main {{ max-width: 30rem; margin: 0 auto; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .25rem; }}
  h2 {{ font-size: 1rem; margin: 2rem 0 .5rem; color: #a0a0b0; font-weight: 600; }}
  p.sub {{ color: #a0a0b0; font-size: .9rem; margin: 0 0 1.25rem; line-height: 1.5; }}
  label {{ display: block; font-size: .85rem; color: #a0a0b0; margin: 1rem 0 .35rem; }}
  input, button {{ width: 100%; padding: .85rem; font-size: 1rem; border-radius: .6rem;
         border: 1px solid #3a3a4a; background: #1e1e2a; color: inherit;
         -webkit-appearance: none; appearance: none; }}
  button {{ margin-top: 1rem; background: #6c5ce7; border: 0; color: #fff; font-weight: 600; }}
  button.ghost {{ background: transparent; border: 1px solid #3a3a4a; color: #a0a0b0;
                  margin-top: .5rem; font-weight: 400; }}
  .dev {{ background: #1e1e2a; border-radius: .6rem; padding: .85rem; margin-bottom: .6rem;
          display: flex; justify-content: space-between; align-items: center; gap: .75rem; }}
  .dev small {{ color: #8a8a9a; display: block; }}
  .dot {{ width: .6rem; height: .6rem; border-radius: 50%; flex: none; }}
  .on {{ background: #4ade80; }} .off {{ background: #6b7280; }}
  #log {{ background: #0d0d14; border-radius: .6rem; padding: .85rem; font-size: .8rem;
          line-height: 1.6; white-space: pre-wrap; max-height: 14rem; overflow-y: auto;
          margin-top: 1rem; }}
  .msg {{ margin-top: 1rem; padding: .85rem; border-radius: .6rem; font-size: .9rem; }}
  .ok {{ background: #14532d; }} .err {{ background: #5b1a1a; }}
</style></head><body><main>
<h1>Controller koppeln</h1>
<p class="sub">
  Halte am Xbox-Controller die <strong>Pair-Taste</strong> oben gedrückt, bis das
  Xbox-Logo schnell blinkt. Dann hier auf „Suche starten“ tippen.
</p>
{message}

<h2>Gekoppelt</h2>
<div id="devices">{devices}</div>

<form method="post">
  <input type="hidden" name="action" value="pair">
  <label for="pin">Admin-PIN</label>
  <input id="pin" name="pin" type="password" inputmode="numeric"
         autocomplete="one-time-code" placeholder="------">
  <button type="submit">Suche starten</button>
</form>
<div id="log">{log}</div>
<script>
  const log = document.getElementById('log');
  const devices = document.getElementById('devices');
  async function poll() {{
    try {{
      const r = await fetch('/beatify/bluetooth/status');
      const d = await r.json();
      if (d.log && d.log.length) {{
        log.textContent = d.log.join('\\n');
        log.scrollTop = log.scrollHeight;
      }}
      if (d.devices_html !== undefined) devices.innerHTML = d.devices_html;
      if (d.pairing) setTimeout(poll, 1500);
    }} catch (e) {{ /* keep the page usable if a poll fails */ }}
  }}
  poll();
</script>
</main></body></html>
"""


def _devices_html(devices: list[dict[str, str]]) -> str:
    if not devices:
        return '<p class="sub">Noch kein Gerät gekoppelt.</p>'
    rows = []
    for device in devices:
        connected = device.get("connected") == "yes"
        rows.append(
            '<div class="dev"><div>'
            f'<span class="dot {"on" if connected else "off"}"></span> '
            f'{html.escape(device.get("name") or "Unbenannt")}'
            f'<small>{html.escape(device.get("id") or "")} '
            f'{"verbunden" if connected else "nicht verbunden"}</small>'
            "</div>"
            '<form method="post" style="width:auto;margin:0">'
            '<input type="hidden" name="action" value="remove">'
            f'<input type="hidden" name="address" value="{html.escape(device.get("id") or "", quote=True)}">'
            '<input name="pin" type="password" inputmode="numeric" placeholder="PIN" '
            'style="width:5.5rem;padding:.5rem;font-size:.85rem">'
            '<button class="ghost" style="margin-top:.4rem;padding:.5rem;font-size:.8rem">'
            "Entfernen</button></form></div>"
        )
    return "".join(rows)


def register_bluetooth_routes(app: web.Application, auth: Any) -> None:
    session = PairingSession()

    async def render(message: str = "") -> web.Response:
        return web.Response(
            text=_PAGE.format(
                message=message,
                devices=_devices_html(await paired_devices()),
                log=html.escape("\n".join(session.lines)),
            ),
            content_type="text/html",
        )

    async def get(request: web.Request) -> web.StreamResponse:
        return await render()

    async def status(request: web.Request) -> web.StreamResponse:
        return web.json_response(
            {
                "pairing": session.running,
                "log": session.lines[-40:],
                "devices_html": _devices_html(await paired_devices()),
            }
        )

    async def post(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        if not auth.check_pin(str(form.get("pin") or "")):
            return await render('<div class="msg err">Falsche Admin-PIN.</div>')

        action = str(form.get("action") or "")
        if action == "remove":
            ok, detail = await remove_device(str(form.get("address") or ""))
            css = "ok" if ok else "err"
            return await render(f'<div class="msg {css}">{html.escape(detail)}</div>')

        if action == "pair":
            if not await session.start():
                return await render('<div class="msg err">Eine Suche läuft bereits.</div>')
            return await render('<div class="msg ok">Suche gestartet.</div>')

        return await render('<div class="msg err">Unbekannte Aktion.</div>')

    app.router.add_route("GET", "/beatify/bluetooth", get)
    app.router.add_route("POST", "/beatify/bluetooth", post)
    app.router.add_route("GET", "/beatify/bluetooth/status", status)
