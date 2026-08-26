"""Entry point: `python -m beatify_standalone`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import web

from .bootstrap import Application
from .config import Config


def _configure_logging(level: str, logfile: Path | None) -> None:
    """Log to stdout, or to a file — never both.

    Writing to both duplicates every line the moment a supervisor also
    redirects stdout into that same file, which is exactly what the Batocera
    service does.
    """
    handlers: list[logging.Handler]
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        # Rotating, because this lives on an SD card. One evening of a flaky
        # network previously produced 3.3 MB of tracebacks; unbounded growth on
        # a card that also holds the game is not acceptable.
        handlers = [
            RotatingFileHandler(
                logfile, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
        ]
    else:
        handlers = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


async def _run(config: Config) -> None:
    application = Application(config)
    app = await application.setup()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        await application.shutdown()
        await runner.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="beatify_standalone")
    parser.add_argument("--data-dir", help="where config, tokens and state live")
    parser.add_argument("--port", type=int, help="override the HTTP port")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-file", help="also write logs to this file")
    args = parser.parse_args(argv)

    config = Config.load(args.data_dir)
    if args.port:
        config.port = args.port

    _configure_logging(args.log_level, Path(args.log_file) if args.log_file else None)

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:  # pragma: no cover - signal path
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
