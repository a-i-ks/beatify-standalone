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

* **Switching output must not need a restart.** The Connect daemon only opens
  its ALSA device once, at startup, so pointing it at a different card the
  ordinary way means restarting it — a few seconds of silence mid-round. A
  `pw:`-prefixed destination instead leaves the daemon on PipeWire's `default`
  forever and does the actual switching in PipeWire itself: pin a card as the
  default sink and, if something is playing right now, move that one stream
  onto it. Verified live on the box: a running stream survives the move with
  no restart and no audible gap. The plain ALSA cards remain, one level down,
  as a fallback for when PipeWire itself is the thing misbehaving — that one
  still restarts the daemon, same as it always has.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import struct
import tempfile
import re
import wave
from pathlib import Path
from typing import Any

from aiohttp import web

from .librespot import PW_PREFIX
from .webui import message, page

_LOGGER = logging.getLogger(__name__)

PIPEWIRE_RUNTIME_DIR = "/var/run"
PIPEWIRE_ENV = {"XDG_RUNTIME_DIR": PIPEWIRE_RUNTIME_DIR}
# Retried at boot because PipeWire/WirePlumber can still be starting when
# Beatify does — the same race the Connect daemon itself already tolerates.
BOOT_REAPPLY_ATTEMPTS = 5
BOOT_REAPPLY_DELAY = 3
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


async def _pactl_json(*args: str) -> list[dict[str, Any]] | None:
    code, out = await _run("pactl", "-f", "json", *args, env=PIPEWIRE_ENV)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


async def _pipewire_card(alsa_id: str) -> dict[str, Any] | None:
    """Find a card by the same short name `aplay -l` and this page use."""
    for card in await _pactl_json("list", "cards") or []:
        if card.get("properties", {}).get("alsa.id") == alsa_id:
            return card
    return None


def _pick_profile(card: dict[str, Any]) -> str | None:
    """The profile to activate for plain stereo output.

    A named profile ("output:stereo-fallback", "output:hdmi-stereo", ...)
    carries a sensible default volume and is preferred; `pro-audio` is the
    fallback every card offers, even one with nothing plugged into it — which
    is exactly the trap: forcing `pro-audio` onto an HDMI port with no display
    attached reports success and produces no sink at all. Caller checks for
    that by looking for the sink afterwards, same as the direct-ALSA path
    already had to.
    """
    profiles = card.get("profiles", {})
    named = [name for name, info in profiles.items() if name not in ("off", "pro-audio") and info.get("sinks", 0)]
    if named:
        return named[0]
    pro_audio = profiles.get("pro-audio")
    if pro_audio and pro_audio.get("sinks", 0):
        return "pro-audio"
    return None


async def _sink_for_alsa_card(alsa_card_number: str) -> str | None:
    if not alsa_card_number:
        return None
    for sink in await _pactl_json("list", "sinks") or []:
        if sink.get("properties", {}).get("alsa.card") == alsa_card_number:
            return sink.get("name")
    return None


async def switch_pipewire_output(alsa_id: str) -> tuple[bool, str]:
    """Point PipeWire's default sink at one card, live, no daemon restart.

    The Connect daemon stays on `audio_device: default` throughout — this
    function is the entire trick. Idle is the common case: go-librespot only
    opens the device while actually playing, so most switches have nothing to
    move and setting the default is enough for the next track. But if a track
    is playing right now, its stream is moved there too, so the switch is
    heard immediately rather than on the next song.
    """
    card = await _pipewire_card(alsa_id)
    if card is None:
        return False, "PipeWire kennt dieses Gerät nicht."

    profile = _pick_profile(card)
    if profile is None:
        return False, "Kein nutzbarer Ausgang (Kabel oder Bildschirm prüfen)."

    if card.get("active_profile") != profile:
        code, out = await _run("pactl", "set-card-profile", card["name"], profile, env=PIPEWIRE_ENV)
        if code != 0:
            return False, out.strip()[:200] or "Profil konnte nicht gesetzt werden."

    sink = await _sink_for_alsa_card(card.get("properties", {}).get("alsa.card", ""))
    if sink is None:
        # Measured live: an HDMI card with no display attached accepts a
        # profile change and still produces no sink. Same limitation the
        # direct-ALSA path has always had, just visible from this side too.
        return False, "Kein Ausgang entstanden — Kabel oder Bildschirm prüfen."

    code, out = await _run("pactl", "set-default-sink", sink, env=PIPEWIRE_ENV)
    if code != 0:
        return False, out.strip()[:200] or "Standardausgang konnte nicht gesetzt werden."

    for item in await _pactl_json("list", "sink-inputs") or []:
        name = str(item.get("properties", {}).get("application.name", ""))
        index = item.get("index")
        if index is not None and "librespot" in name.lower():
            await _run("pactl", "move-sink-input", str(index), sink, env=PIPEWIRE_ENV)

    return True, "Umgeschaltet."


async def play_test_tone_pipewire(alsa_id: str) -> tuple[bool, str]:
    """Test a `pw:` destination directly, without touching the default sink."""
    card = await _pipewire_card(alsa_id)
    if card is None:
        return False, "PipeWire kennt dieses Gerät nicht."

    profile = _pick_profile(card)
    if profile is None:
        return False, "Kein nutzbarer Ausgang (Kabel oder Bildschirm prüfen)."

    if card.get("active_profile") != profile:
        code, out = await _run("pactl", "set-card-profile", card["name"], profile, env=PIPEWIRE_ENV)
        if code != 0:
            return False, out.strip()[:200] or "Profil konnte nicht gesetzt werden."

    sink = await _sink_for_alsa_card(card.get("properties", {}).get("alsa.card", ""))
    if sink is None:
        return False, "Kein Ausgang entstanden — Kabel oder Bildschirm prüfen."

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tone.wav"
        await asyncio.get_running_loop().run_in_executor(None, _tone, path)
        # pw-play, not aplay: it takes a PipeWire node as --target directly,
        # so the test reaches this one sink regardless of the current default.
        code, out = await _run("pw-play", "--target", sink, str(path), timeout=15, env=PIPEWIRE_ENV)
    if code != 0:
        return False, out.strip()[:200] or "Wiedergabe fehlgeschlagen."
    return True, "Testton gespielt."


async def reapply_pipewire_output_at_boot(config: Any) -> None:
    """Re-pin the chosen `pw:` output after every reboot.

    Nothing here survives on its own: `/var` is a tmpfs on Batocera, so
    WirePlumber has nowhere to remember the default sink we set, and starts
    back at its own priority order every time. "default" and the direct-ALSA
    fallback need nothing done at boot; only a `pw:` choice does.
    """
    device = config.librespot_device or ""
    if not device.startswith(PW_PREFIX):
        return
    alsa_id = device[len(PW_PREFIX):]
    for attempt in range(1, BOOT_REAPPLY_ATTEMPTS + 1):
        ok, detail = await switch_pipewire_output(alsa_id)
        if ok:
            _LOGGER.info("re-pinned PipeWire output to %s after boot (attempt %s)", alsa_id, attempt)
            return
        _LOGGER.debug(
            "PipeWire output %s not ready yet (attempt %s/%s): %s",
            alsa_id, attempt, BOOT_REAPPLY_ATTEMPTS, detail,
        )
        await asyncio.sleep(BOOT_REAPPLY_DELAY)
    _LOGGER.warning(
        "could not re-pin PipeWire output to %s after boot (PipeWire not up in time?) — "
        "it will use WirePlumber's own default until chosen again from /beatify/audio",
        alsa_id,
    )


async def available_outputs() -> list[dict[str, str]]:
    """The outputs worth offering, each with a plain-language note.

    Every real card is offered twice: once as a `pw:` destination (switches
    live, no restart — the recommended path) and once as the direct-ALSA
    fallback (restarts the daemon, same as before this existed). `render()`
    below sorts the second copy into a collapsed "advanced" section by its
    `group`.
    """
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
            "group": "auto",
        }
    ]

    def add(alsa_id: str, label: str, note: str, state: str) -> None:
        outputs.append(
            {"id": f"{PW_PREFIX}{alsa_id}", "name": label, "note": note, "state": state, "group": "pipewire"}
        )
        outputs.append(
            {
                "id": f"plughw:CARD={alsa_id}",
                "name": f"{label}, direkt",
                "note": "Umgeht PipeWire — Fallback, falls PipeWire selbst zickt.",
                "state": state,
                "group": "direct",
            }
        )

    if "Headphones" in cards:
        add("Headphones", "Klinke (3,5 mm)", "Kopfhörerbuchse des Pi — für eine mitgebrachte Box.", "")
    for index, card in enumerate(("vc4hdmi0", "vc4hdmi1"), start=1):
        if card not in cards:
            continue
        state = hdmi.get(card, "unknown")
        add(
            card,
            f"HDMI {index}",
            "Bildschirm angeschlossen" if state == "connected" else "kein Bildschirm erkannt",
            state,
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
    def _output_card(output: dict[str, str], current: str) -> str:
        selected = output["id"] == current
        note = output["note"]
        dot = ""
        if output["state"] == "connected":
            dot = '<span class="dot on"></span>'
        elif output["state"] == "disconnected":
            dot = '<span class="dot off"></span>'
        return (
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

    async def render(msg: str = "") -> web.Response:
        current = config.librespot_device or "default"
        outputs = await available_outputs()
        primary = [o for o in outputs if o["group"] != "direct"]
        advanced = [o for o in outputs if o["group"] == "direct"]

        body = msg + "".join(_output_card(o, current) for o in primary)
        if advanced:
            body += (
                "<details><summary>Erweitert: direkt, ohne PipeWire</summary>"
                + "".join(_output_card(o, current) for o in advanced)
                + "</details>"
            )
        body += (
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
            if device.startswith(PW_PREFIX):
                ok, detail = await play_test_tone_pipewire(device[len(PW_PREFIX):])
            else:
                ok, detail = await play_test_tone(device)
            return await render(message(html.escape(detail), ok=ok))

        if action == "select":
            if device.startswith(PW_PREFIX):
                ok, detail = await switch_pipewire_output(device[len(PW_PREFIX):])
                if not ok:
                    return await render(message(html.escape(detail), ok=False))
                # The daemon only needs restarting if it was not already
                # sitting on "default" — otherwise this switch is the whole
                # point: instant, because nothing about the daemon changes.
                previous = config.librespot_device or "default"
                daemon_already_on_pipewire = previous == "default" or previous.startswith(PW_PREFIX)
                config.librespot_device = device
                config.save()
                if daemon_already_on_pipewire:
                    return await render(message("Umgeschaltet — sofort, ohne Neustart."))
                await supervisor.set_audio_device("default")
                return await render(message(
                    "Umgeschaltet. Der Connect-Dienst startet einmalig neu — das dauert ein paar Sekunden."
                ))

            config.librespot_device = device
            config.save()
            await supervisor.set_audio_device(device)
            return await render(message(
                "Umgeschaltet. Der Connect-Dienst startet neu — das dauert ein paar Sekunden."
            ))

        return await render(message("Unbekannte Aktion.", ok=False))

    app.router.add_route("GET", "/beatify/audio", get)
    app.router.add_route("POST", "/beatify/audio", post)
