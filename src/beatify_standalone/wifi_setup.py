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

from .webui import message, page

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


def _network_options(networks: list[str]) -> str:
    if not networks:
        return '<option value="">Keine Netze gefunden</option>'
    return '<option value="">-- auswählen --</option>' + "".join(
        f'<option value="{html.escape(n, quote=True)}">{html.escape(n)}</option>'
        for n in networks
    )


_SCRIPT = """<script>
  // The scan takes up to 25 s on the Pi. Rather than make the page wait for it,
  // it renders immediately and fills the list when the result arrives.
  const sel = document.getElementById('ssid_select');
  fetch('/beatify/wifi/scan')
    .then(r => r.json())
    .then(d => {
      sel.innerHTML = d.options;
      sel.disabled = false;
      document.getElementById('scanning').style.display = 'none';
    })
    .catch(() => { document.getElementById('scanning').textContent = 'Suche fehlgeschlagen'; });

  // Typing a name by hand and picking one from the list are alternatives;
  // showing both as active invites filling in two different networks.
  const manual = document.getElementById('ssid_manual');
  sel.addEventListener('change', () => { if (sel.value) manual.value = ''; });
  manual.addEventListener('input', () => { if (manual.value) sel.value = ''; });
</script>"""


def register_wifi_routes(app: web.Application, auth: Any, port: int) -> None:
    """Mount the setup page."""

    def render(msg: str = "") -> web.Response:
        body = f"""{msg}
<form method="post">
  <label for="ssid_select">Netzwerk in Reichweite</label>
  <select id="ssid_select" name="ssid_select" disabled>
    <option value="">Suche läuft…</option>
  </select>
  <p id="scanning" class="empty"><span class="spinner"></span>Netze werden gesucht…</p>

  <label for="ssid_manual">…oder Namen selbst eintippen</label>
  <input id="ssid_manual" name="ssid_manual" autocapitalize="none"
         autocorrect="off" spellcheck="false" placeholder="SSID">

  <label for="password">Passwort</label>
  <input id="password" name="password" type="password" autocapitalize="none"
         autocorrect="off" spellcheck="false" autocomplete="off">
  {_pin_field(auth)}
  <button type="submit">Verbinden</button>
</form>
<h2>Gut zu wissen</h2>
<p class="empty">
  Heimnetz und Handy-Hotspot bleiben gespeichert — die Box nimmt automatisch
  das Netz, das gerade erreichbar ist. Nach dem Wechsel ist sie unter
  <strong>batocera.local</strong> erreichbar.
</p>"""
        return page("Beatify · WLAN", "WLAN einrichten",
                    "Verbindet die Box mit einem neuen Netz.", body, _SCRIPT)

    async def get(request: web.Request) -> web.StreamResponse:
        return render()

    async def scan(request: web.Request) -> web.StreamResponse:
        return web.json_response({"options": _network_options(await scan_networks())})

    async def post(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        ssid = str(form.get("ssid_manual") or form.get("ssid_select") or "").strip()
        password = str(form.get("password") or "")

        if not auth.check_pin(str(form.get("pin") or "")):
            return render(message("Falsche Admin-PIN.", ok=False))
        if not ssid:
            return render(message("Kein Netzwerk gewählt.", ok=False))

        ok, detail = await apply_network(ssid, password)
        if not ok:
            return render(message(html.escape(detail), ok=False))

        return render(message(
            f"<strong>Gespeichert.</strong><br>Die Box wechselt jetzt zu "
            f"<em>{html.escape(ssid)}</em>. Verbinde dein Telefon mit demselben "
            f"Netz und öffne dann <strong>batocera.local</strong>."
        ))

    app.router.add_route("GET", "/beatify/wifi", get)
    app.router.add_route("POST", "/beatify/wifi", post)
    app.router.add_route("GET", "/beatify/wifi/scan", scan)


def _pin_field(auth: Any) -> str:
    if not getattr(auth, "require_pin", True):
        return ""
    return """
  <label for="pin">Admin-PIN</label>
  <input id="pin" name="pin" type="password" inputmode="numeric"
         autocomplete="one-time-code" placeholder="------">"""
