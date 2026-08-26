"""Join a new Wi-Fi network from a phone, with no screen or keyboard.

The problem this solves: you arrive somewhere with the box and a phone, and
nothing else. The box knows your home network, which is not here. A phone
hotspot is not an answer for a party — iOS caps it at about five devices — so
the box has to get onto the venue's network, and the only tool you have is the
phone in your hand.

The way through is to use the hotspot as a *setup channel* rather than as the
party network: two devices, well inside the cap. The box joins the hotspot, you
open this page, you type the venue's credentials, and the box moves over. Then
the hotspot goes off and everyone uses the venue's Wi-Fi.

Written into slot `wifi3` so the home network (`wifi`) and the hotspot (`wifi2`)
both survive — Batocera's connman config gives every slot `Autoconnect=true`,
so the box simply joins whichever it can see.
"""

from __future__ import annotations

import asyncio
import html
import logging
import pathlib
import re
from typing import Any

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

# Slot 3 by default: never clobber home (1) or the hotspot (2).
DEFAULT_SLOT = 3
SCAN_TIMEOUT = 25
# batocera.conf is a flat key=value file; these three break its parser.
_ESCAPE = re.compile(r"([#;$])")


def _escape(value: str) -> str:
    return _ESCAPE.sub(r"\\\1", value.replace("\\", "\\\\"))


async def _run(*argv: str, timeout: float = 15) -> tuple[int, str]:
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


async def scan_networks() -> list[str]:
    """SSIDs in range, strongest first, as Batocera reports them."""
    code, out = await _run("batocera-wifi", "scanlist", timeout=SCAN_TIMEOUT)
    if code != 0:
        _LOGGER.warning("wifi scan failed (%s): %s", code, out.strip()[:200])
        return []
    seen: list[str] = []
    for line in out.splitlines():
        ssid = line.strip()
        if ssid and ssid not in seen:
            seen.append(ssid)
    return seen


BOOT_CONF = "/boot/batocera-boot.conf"


async def _write_boot_conf(entries: dict[str, str]) -> str | None:
    """Mirror the network into the early-boot config.

    This is not belt-and-braces, it is required. `S08connman` runs *before*
    `S11share` mounts /userdata, so at that moment `/userdata/system/batocera.conf`
    does not exist and connman falls back to `/boot/batocera-boot.conf`. Batocera
    normally keeps the two in sync via `S65values4boot` — but that script starts
    with `[ "$1" = "stop" ] || exit 0`, so it only ever runs on a clean shutdown.
    Pull the plug instead, and the new network is silently forgotten.

    A party box gets unplugged rather than shut down. Writing here directly means
    the network survives either way.
    """
    code, _ = await _run("mount", "-o", "remount,rw", "/boot")
    if code != 0:
        return "could not make /boot writable"
    try:
        path = pathlib.Path(BOOT_CONF)
        if not path.exists():
            return f"{BOOT_CONF} is missing"
        kept = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not any(line.startswith(f"{key}=") for key in entries)
        ]
        kept += [f"{key}={value}" for key, value in entries.items()]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError as err:
        return f"could not write {BOOT_CONF}: {err}"
    finally:
        await _run("sync")
        await _run("mount", "-o", "remount,ro", "/boot")
    return None


async def apply_network(ssid: str, password: str, slot: int = DEFAULT_SLOT) -> tuple[bool, str]:
    """Persist a network — in both config files — and ask connman to take it."""
    prefix = "wifi" if slot == 1 else f"wifi{slot}"
    escaped = {f"{prefix}.ssid": _escape(ssid), f"{prefix}.key": _escape(password)}

    for key, value in escaped.items():
        code, out = await _run("batocera-settings-set", key, value)
        if code != 0:
            return False, f"could not write {key}: {out.strip()[:200]}"

    boot_error = await _write_boot_conf(escaped)
    if boot_error:
        _LOGGER.warning("early-boot config not updated: %s", boot_error)

    # connman reads its profiles from /var/lib/connman; Batocera regenerates
    # them at boot. Restarting connman is what makes the change take effect now
    # rather than at the next reboot.
    code, out = await _run("/etc/init.d/S08connman", "restart", timeout=45)
    suffix = "" if not boot_error else f" (early-boot config: {boot_error})"
    if code != 0:
        return True, (
            "Saved, but connman did not restart cleanly. It will be picked up "
            f"on the next reboot.{suffix} ({out.strip()[:150]})"
        )
    return True, f"Saved and applied.{suffix}"


_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Beatify - WLAN</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         background: #12121a; color: #f2f2f7; padding: 1.5rem 1rem 3rem; }}
  main {{ max-width: 30rem; margin: 0 auto; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .25rem; }}
  p.sub {{ color: #a0a0b0; font-size: .9rem; margin: 0 0 1.5rem; }}
  label {{ display: block; font-size: .85rem; color: #a0a0b0; margin: 1rem 0 .35rem; }}
  input, select, button {{ width: 100%; padding: .85rem; font-size: 1rem;
         border-radius: .6rem; border: 1px solid #3a3a4a; background: #1e1e2a;
         color: inherit; -webkit-appearance: none; appearance: none; }}
  button {{ margin-top: 1.5rem; background: #6c5ce7; border: 0; color: #fff;
            font-weight: 600; }}
  .msg {{ margin-top: 1.25rem; padding: .85rem; border-radius: .6rem;
          font-size: .9rem; line-height: 1.45; }}
  .ok {{ background: #14532d; }}
  .err {{ background: #5b1a1a; }}
  .hint {{ color: #8a8a9a; font-size: .8rem; margin-top: 1.5rem; line-height: 1.5; }}
  code {{ background: #1e1e2a; padding: .1rem .35rem; border-radius: .3rem; }}
</style></head><body><main>
<h1>WLAN einrichten</h1>
<p class="sub">Verbindet die Beatify-Box mit einem neuen Netz.</p>
{message}
<form method="post">
  <label for="ssid">Netzwerk in Reichweite</label>
  <select id="ssid" name="ssid_select">
    <option value="">-- auswählen --</option>
    {options}
  </select>

  <label for="ssid_manual">…oder Name selbst eintippen</label>
  <input id="ssid_manual" name="ssid_manual" autocapitalize="none"
         autocorrect="off" placeholder="SSID">

  <label for="password">Passwort</label>
  <input id="password" name="password" type="password" autocapitalize="none"
         autocorrect="off" autocomplete="off">

  <label for="pin">Admin-PIN</label>
  <input id="pin" name="pin" type="password" inputmode="numeric"
         autocomplete="one-time-code" placeholder="------">

  <button type="submit">Verbinden</button>
</form>
<p class="hint">
  Das Heimnetz und der Handy-Hotspot bleiben gespeichert — die Box nimmt
  automatisch das Netz, das gerade erreichbar ist. Nach dem Wechsel ist sie
  unter <code>http://batocera.local:8123</code> erreichbar.
</p>
</main></body></html>
"""


def register_wifi_routes(app: web.Application, auth: Any, port: int) -> None:
    """Mount the setup page. Gated by the admin PIN, LAN-only."""

    def render(message: str = "", options: str = "") -> web.Response:
        return web.Response(
            text=_PAGE.format(message=message, options=options), content_type="text/html"
        )

    async def options_html() -> str:
        return "".join(
            f'<option value="{html.escape(s, quote=True)}">{html.escape(s)}</option>'
            for s in await scan_networks()
        )

    async def get(request: web.Request) -> web.StreamResponse:
        return render(options=await options_html())

    async def post(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        ssid = str(form.get("ssid_manual") or form.get("ssid_select") or "").strip()
        password = str(form.get("password") or "")
        pin = str(form.get("pin") or "")

        if not auth.check_pin(pin):
            return render('<div class="msg err">Falsche Admin-PIN.</div>', await options_html())
        if not ssid:
            return render(
                '<div class="msg err">Kein Netzwerk gewählt.</div>', await options_html()
            )

        ok, detail = await apply_network(ssid, password)
        if not ok:
            return render(
                f'<div class="msg err">{html.escape(detail)}</div>', await options_html()
            )

        return render(
            '<div class="msg ok"><strong>Gespeichert.</strong><br>'
            f"Die Box wechselt jetzt zu <em>{html.escape(ssid)}</em>. "
            "Verbinde dein Telefon mit demselben Netz und öffne dann "
            f"<code>http://batocera.local:{port}/beatify/admin</code>.<br><br>"
            f"<small>{html.escape(detail)}</small></div>",
            await options_html(),
        )

    app.router.add_route("GET", "/beatify/wifi", get)
    app.router.add_route("POST", "/beatify/wifi", post)
