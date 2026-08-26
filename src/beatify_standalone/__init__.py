"""Beatify without Home Assistant.

This package supplies everything a normal Home Assistant install would have
given upstream Beatify: an HTTP server, a config entry, persistence, an auth
manager, and — the interesting part — a `media_player` implementation backed by
librespot and the Spotify Web API instead of Music Assistant.

Upstream's own source is vendored untouched under `vendor/custom_components/`.
Nothing in this package edits it; `tools/check_ha_surface.py` guards the seams
we depend on so an upstream bump fails loudly instead of silently.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
