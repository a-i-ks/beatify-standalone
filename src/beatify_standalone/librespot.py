"""Supervise the Spotify Connect daemon that turns the Pi into a speaker.

Two flavours are supported, because the obvious choice is not the available one:

* **go-librespot** (default). A single static Go binary, published as
  `go-librespot_linux_arm64.tar.gz` — the same daemon Music Assistant uses. It
  is configured through a `config.yml`, which this module writes. Static linking
  is what makes it the default: Batocera is a buildroot system whose glibc and
  libasound are not the ones a Debian binary expects.

* **librespot** (Rust). Configured entirely with CLI flags. Upstream publishes
  *no* release binaries at all (v0.8.0 ships zero assets), so this flavour means
  either building it yourself or lifting `/usr/bin/librespot` out of the
  raspotify arm64 `.deb`. Dynamically linked, so verify it actually runs on the
  target before relying on it.

Either way the daemon announces itself over zeroconf; the Web API client then
finds it by name among the account's Connect devices.

The supervisor restarts it with a backoff. That is not padding: the daemon drops
its session when the network blips, and on a phone hotspot at a party it will.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections import deque
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

RESTART_DELAY_MIN = 2
RESTART_DELAY_MAX = 30

FLAVOR_GO = "go"
FLAVOR_RUST = "rust"


class LibrespotSupervisor:
    """Runs a Spotify Connect daemon for the life of the application."""

    def __init__(
        self,
        binary: str,
        device_name: str,
        alsa_device: str | None = None,
        bitrate: int = 320,
        extra_args: list[str] | None = None,
        flavor: str = FLAVOR_GO,
        config_dir: Path | None = None,
    ) -> None:
        self._binary = binary
        self._device_name = device_name
        self._alsa_device = alsa_device
        self._bitrate = bitrate
        self._extra_args = list(extra_args or [])
        self._flavor = flavor
        self._config_dir = config_dir
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stopping = False
        # Last lines the daemon printed, so a failed start can say WHY. Without
        # this the supervisor logs "exited (1), restarting" forever and the
        # actual reason — a bad flag, a busy audio device — stays invisible.
        self._recent: deque[str] = deque(maxlen=10)

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def available(self) -> bool:
        return shutil.which(self._binary) is not None or Path(self._binary).is_file()

    # -- configuration ----------------------------------------------------

    def write_config(self) -> Path | None:
        """Write go-librespot's config.yml; the Rust flavour needs no file."""
        if self._flavor != FLAVOR_GO or self._config_dir is None:
            return None

        self._config_dir.mkdir(parents=True, exist_ok=True)
        path = self._config_dir / "config.yml"
        lines = [
            f"device_name: {self._device_name}",
            f"audio_device: {self._alsa_device or 'default'}",
            f"bitrate: {self._bitrate}",
            "zeroconf_enabled: true",
            "credentials:",
            "  type: zeroconf",
            "  zeroconf:",
            # Persisted so the box does not need re-pairing from a phone every
            # time it boots in a new place — the whole point of a travel build.
            "    persist_credentials: true",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _argv(self) -> list[str]:
        if self._flavor == FLAVOR_GO:
            argv = [self._binary]
            if self._config_dir is not None:
                # Two dashes: go-librespot uses pflag, where `-c` is the short
                # form of `--conf`. A single-dash `-config_dir` is parsed as
                # `-c onfig_dir` and dies with "invalid config override format".
                argv += ["--config_dir", str(self._config_dir)]
            return argv + self._extra_args

        argv = [
            self._binary,
            "--name", self._device_name,
            "--bitrate", str(self._bitrate),
            "--backend", "alsa",
            # Beatify seeks into every track, so a cache buys nothing and only
            # writes to the SD card.
            "--disable-audio-cache",
        ]
        if self._alsa_device:
            argv += ["--device", self._alsa_device]
        return argv + self._extra_args

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if not self.available:
            _LOGGER.error(
                "Spotify Connect daemon %r not found — there will be no audio output. "
                "Install go-librespot (linux_arm64) or point librespot_binary at a build.",
                self._binary,
            )
            return
        self.write_config()
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        delay = RESTART_DELAY_MIN
        while not self._stopping:
            argv = self._argv()
            _LOGGER.info("starting Spotify Connect daemon: %s", " ".join(argv))
            self._recent.clear()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except (OSError, ValueError):
                _LOGGER.exception("could not launch %s", self._binary)
                return

            await self._pump_output(self._process)
            code = await self._process.wait()
            self._process = None
            if self._stopping:
                return

            if code != 0 and self._recent:
                _LOGGER.error(
                    "Connect daemon exited (%s). Its last output was:\n  %s",
                    code,
                    "\n  ".join(self._recent),
                )
            else:
                _LOGGER.warning("Connect daemon exited (%s)", code)
            _LOGGER.warning("restarting in %ss", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RESTART_DELAY_MAX)

    async def _pump_output(self, process: Any) -> None:
        """Forward the daemon's output into our log until it closes the pipe."""
        if process.stdout is None:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            self._recent.append(text)
            _LOGGER.debug("librespot: %s", text)

    async def stop(self) -> None:
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
