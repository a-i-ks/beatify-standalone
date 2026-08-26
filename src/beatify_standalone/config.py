"""Runtime configuration for the standalone build.

Everything lives under one data directory (on the Pi: `/userdata/beatify/data`),
which is also what upstream sees as its "config dir" — so `hass.config.path()`
and `Store` land inside it, and a whole install is one directory to back up.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "beatify_standalone.json"


@dataclass
class Config:
    """Settings the standalone runner needs before upstream is even loaded."""

    data_dir: Path
    port: int = 8123
    host: str = "0.0.0.0"  # noqa: S104 - a party box must be reachable from the LAN
    # A second listener on the default HTTP port. Browsers now try to upgrade a
    # typed address to HTTPS; on port 8123 that upgrade reaches this very server,
    # which answers a TLS handshake with plaintext, and the browser reports a
    # protocol error instead of falling back. Port 80's upgrade goes to 443,
    # where nothing is listening, so the fallback to HTTP is clean.
    # Set to null to disable.
    extra_port: int | None = 80
    country: str | None = "DE"

    # Spotify application credentials (Developer Dashboard). The client secret
    # is optional: the Authorization-Code + PKCE flow does not need one, which
    # is why the default flow here is PKCE.
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None

    # Spotify Connect daemon. "go" = go-librespot (static Go binary, the
    # default because Batocera's buildroot glibc makes dynamic linking a gamble);
    # "rust" = the original librespot, configured by CLI flags.
    librespot_flavor: str = "go"
    librespot_binary: str = "go-librespot"
    librespot_name: str = "Beatify"
    librespot_device: str | None = None  # ALSA device, e.g. "hw:CARD=vc4hdmi0"
    librespot_bitrate: int = 320
    librespot_extra_args: list[str] = field(default_factory=list)

    # Admin login for the OAuth-compatible auth endpoints.
    admin_pin: str | None = None

    @property
    def config_path(self) -> Path:
        return self.data_dir / CONFIG_FILENAME

    @classmethod
    def load(cls, data_dir: Path | str | None = None) -> Config:
        """Load config from the data directory, with environment overrides."""
        resolved = Path(
            data_dir
            or os.environ.get("BEATIFY_DATA_DIR")
            or Path(__file__).resolve().parents[2] / "data"
        )
        resolved.mkdir(parents=True, exist_ok=True)

        raw: dict[str, Any] = {}
        path = resolved / CONFIG_FILENAME
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))

        config = cls(
            data_dir=resolved,
            port=int(raw.get("port", 8123)),
            extra_port=(None if raw.get("extra_port", 80) in (None, 0, "")
                        else int(raw.get("extra_port", 80))),
            host=raw.get("host", "0.0.0.0"),  # noqa: S104 - see field default
            country=raw.get("country", "DE"),
            spotify_client_id=raw.get("spotify_client_id"),
            spotify_client_secret=raw.get("spotify_client_secret"),
            librespot_flavor=raw.get("librespot_flavor", "go"),
            librespot_binary=raw.get("librespot_binary", "go-librespot"),
            librespot_name=raw.get("librespot_name", "Beatify"),
            librespot_device=raw.get("librespot_device"),
            librespot_bitrate=int(raw.get("librespot_bitrate", 320)),
            librespot_extra_args=list(raw.get("librespot_extra_args", [])),
            admin_pin=raw.get("admin_pin"),
        )

        # Environment wins over the file — handy for the Batocera service script
        # and for keeping the client secret out of a file on a shared box.
        env_map = {
            "BEATIFY_PORT": ("port", int),
            "BEATIFY_SPOTIFY_CLIENT_ID": ("spotify_client_id", str),
            "BEATIFY_SPOTIFY_CLIENT_SECRET": ("spotify_client_secret", str),
            "BEATIFY_LIBRESPOT_DEVICE": ("librespot_device", str),
            "BEATIFY_LIBRESPOT_BINARY": ("librespot_binary", str),
            "BEATIFY_ADMIN_PIN": ("admin_pin", str),
        }
        for env_name, (attr, caster) in env_map.items():
            value = os.environ.get(env_name)
            if value:
                setattr(config, attr, caster(value))

        return config

    def save(self) -> None:
        payload = {
            "port": self.port,
            "extra_port": self.extra_port,
            "host": self.host,
            "country": self.country,
            "spotify_client_id": self.spotify_client_id,
            "spotify_client_secret": self.spotify_client_secret,
            "librespot_flavor": self.librespot_flavor,
            "librespot_binary": self.librespot_binary,
            "librespot_name": self.librespot_name,
            "librespot_device": self.librespot_device,
            "librespot_bitrate": self.librespot_bitrate,
            "librespot_extra_args": self.librespot_extra_args,
            "admin_pin": self.admin_pin,
        }
        self.config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
