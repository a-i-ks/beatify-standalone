"""Logging must not double up."""

from __future__ import annotations

import logging
from pathlib import Path

from beatify_standalone.__main__ import _configure_logging


def _reset() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def test_a_log_file_means_exactly_one_handler(tmp_path: Path):
    """Regression: the app wrote a FileHandler while the Batocera service also
    redirected stdout into the same file, so every line appeared twice."""
    _reset()
    logfile = tmp_path / "beatify.log"
    _configure_logging("INFO", logfile)

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.FileHandler)

    logging.getLogger("test").info("once")
    for handler in handlers:
        handler.flush()
    assert logfile.read_text().count("once") == 1
    _reset()


def test_without_a_log_file_it_logs_to_stdout(tmp_path: Path):
    _reset()
    _configure_logging("INFO", None)
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0], logging.FileHandler)
    _reset()
