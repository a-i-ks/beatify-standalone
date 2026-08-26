"""Validation stubs — imported by `config_flow.py`, which never runs here."""

from __future__ import annotations

from typing import Any


def _identity(value: Any) -> Any:
    return value


string = _identity
boolean = _identity
positive_int = _identity


def __getattr__(name: str) -> Any:
    """Any other validator resolves to a pass-through."""
    return _identity
