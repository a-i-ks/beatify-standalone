"""Choose where the music comes out, from a phone.

The box moves between a television and a portable speaker, and which socket the
cable is in is not something the software can decide for you. Two details make
this worth a page rather than a config file:

* **The two HDMI sockets are not interchangeable and are unlabelled.** DRM
  connector HDMI-A-1 is ALSA card `vc4hdmi0`, HDMI-A-2 is `vc4hdmi1`. Picking
  the wrong one routes audio to an empty socket while every command reports
  success. So the page reads which socket actually has a screen and says so.

* **You cannot hear a setting.** A test tone turns "I think it is right" into
  "I heard it", which on arrival somewhere unfamiliar is the whole difference.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import struct
import tempfile
import re
import wave
from pathlib import Path
from typing import Any

from aiohttp import web

from .webui import message, page

_LOGGER = logging.getLogger(__name__)

PIPEWIRE_RUNTIME_DIR = "/var/run"
_CARD_LINE = re.compile(r"^card \d+:\s+(\S+)\s+\[", re.M)


async def _run(*argv: str, timeout: float = 20, env: dict[str, str] | None = None) -> tuple[int, str]:
    import os

    merged = {**os.environ, **(env or {})}
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=merged,
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


def hdmi_status() -> dict[str, str]:
    """Which HDMI socket has a screen, keyed by the ALSA card it maps to."""
    status: dict[str, str] = {}
    for index in (1, 2):
        card = f"vc4hdmi{index - 1}"
        state = "unknown"
        for path in Path("/sys/class/drm").glob(f"card*-HDMI-A-{index}/status"):
            try:
                state = path.read_text(encoding="utf-8").strip()
            except OSError:
                state = "unknown"
            break
        status[card] = state
    return status


async def available_outputs() -> list[dict[str, str]]:
    """The outputs worth offering, each with a plain-language note."""
    code, out = await _run("aplay", "-l")
    cards = set()
    if code == 0:
        # `card 0: Headphones [bcm2835 Headphones], device 0: ...`
        # The name ALSA answers to is the bare word after the number, not the
        # description in brackets — plughw:CARD=Headphones, not
        # plughw:CARD=bcm2835 Headphones.
        cards = set(_CARD_LINE.findall(out))

    hdmi = hdmi_status()
    outputs: list[dict[str, str]] = [
        {
            "id": "default",
            "name": "Automatisch (PipeWire)",
            "note": "Folgt der Systemeinstellung. Wechselt zum Fernseher, sobald HDMI steckt.",
            "state": "",
        }
    ]
    if "Headphones" in cards:
        outputs.append(
            {
                "id": "plughw:CARD=Headphones",
                "name": "Klinke (3,5 mm)",
                "note": "Kopfhörerbuchse des Pi — für eine mitgebrachte Box.",
                "state": "",
            }
        )
    for index, card in enumerate(("vc4hdmi0", "vc4hdmi1"), start=1):
        if card not in cards:
            continue
        state = hdmi.get(card, "unknown")
        outputs.append(
            {
                "id": f"plughw:CARD={card}",
                "name": f"HDMI {index}",
                "note": "Bildschirm angeschlossen" if state == "connected" else "kein Bildschirm erkannt",
                "state": state,
            }
        )
    return outputs


def _tone(path: Path, seconds: float = 1.5, hz: int = 440) -> None:
    """A short sine, generated rather than shipped as an asset."""
    rate = 48000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            # Fade in and out so it does not click.
            fade = min(1.0, i / (rate * 0.05), (rate * seconds - i) / (rate * 0.05))
            value = int(11000 * fade * math.sin(2 * math.pi * hz * i / rate))
            frames += struct.pack("<hh", value, value)
        handle.writeframes(bytes(frames))


async def play_test_tone(device: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tone.wav"
        await asyncio.get_running_loop().run_in_executor(None, _tone, path)
        # PipeWire's ALSA bridge needs the runtime dir; a service has no session.
        env = {"XDG_RUNTIME_DIR": PIPEWIRE_RUNTIME_DIR} if device == "default" else None
        code, out = await _run("aplay", "-D", device, str(path), timeout=15, env=env)
    if code != 0:
        return False, out.strip()[:200] or "Wiedergabe fehlgeschlagen."
    return True, "Testton gespielt."


def register_audio_routes(app: web.Application, config: Any, supervisor: Any) -> None:
    async def render(msg: str = "") -> web.Response:
        current = config.librespot_device or "default"
        cards = []
        for output in await available_outputs():
            selected = output["id"] == current
            note = output["note"]
            dot = ""
            if output["state"] == "connected":
                dot = '<span class="dot on"></span>'
            elif output["state"] == "disconnected":
                dot = '<span class="dot off"></span>'
            cards.append(
                f'<div class="card" style="{"border-color:var(--accent)" if selected else ""}">'
                '<div class="row">'
                f"{dot}"
                '<span class="grow">'
                f'<strong>{html.escape(output["name"])}'
                f'{" · aktiv" if selected else ""}</strong>'
                f'<small>{html.escape(note)}</small></span>'
                "</div>"
                '<form method="post" style="display:flex;gap:.5rem;margin-top:.6rem">'
                f'<input type="hidden" name="device" value="{html.escape(output["id"], quote=True)}">'
                '<button name="action" value="select" style="margin:0;padding:.7rem;'
                'min-height:0;font-size:.9rem">Auswählen</button>'
                '<button name="action" value="test" class="ghost" style="margin:0;padding:.7rem;'
                'min-height:0;font-size:.9rem;width:auto;white-space:nowrap">Testton</button>'
                "</form></div>"
            )
        body = msg + "".join(cards) + (
            '<h2>Hinweis</h2><p class="empty">'
            "Der Testton geht sofort auf den gewählten Ausgang, ohne die Einstellung zu ändern. "
            "Erst „Auswählen“ schaltet die Musik dorthin um."
            "</p>"
        )
        return page("Beatify · Audio", "Audioausgang", "Wo die Musik herauskommt.", body)

    async def get(request: web.Request) -> web.StreamResponse:
        return await render()

    async def post(request: web.Request) -> web.StreamResponse:
        form = await request.post()
        device = str(form.get("device") or "")
        action = str(form.get("action") or "")

        valid = {o["id"] for o in await available_outputs()}
        if device not in valid:
            return await render(message("Unbekannter Ausgang.", ok=False))

        if action == "test":
            ok, detail = await play_test_tone(device)
            return await render(message(html.escape(detail), ok=ok))

        if action == "select":
            config.librespot_device = device
            config.save()
            await supervisor.set_audio_device(device)
            return await render(message(
                "Umgeschaltet. Der Connect-Dienst startet neu — das dauert ein paar Sekunden."
            ))

        return await render(message("Unbekannte Aktion.", ok=False))

    app.router.add_route("GET", "/beatify/audio", get)
    app.router.add_route("POST", "/beatify/audio", post)
