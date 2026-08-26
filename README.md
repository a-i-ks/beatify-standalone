# Beatify Standalone

> **All credit for the game itself goes to [Markus Holzhäuser (@mholzi)](https://github.com/mholzi)
> and the [Beatify](https://github.com/mholzi/beatify) project.** This repository
> is not a fork and not a replacement — it is a runtime wrapper. Every line of
> game logic, every playlist, the entire frontend, all of it is upstream's work,
> vendored here **unmodified** under `vendor/`. If you run Home Assistant, use
> upstream directly; it is the better path and this project exists only because
> some hardware cannot host Home Assistant at all.
>
> Beatify is MIT-licensed. Its licence is preserved in
> [`vendor/LICENSE.upstream`](vendor/LICENSE.upstream). Bugs in *this* wrapper
> belong here, not in upstream's issue tracker.

[Beatify](https://github.com/mholzi/beatify) — the music party game — running
**without Home Assistant and without Music Assistant**, small enough to live on
a Batocera retro-gaming box and travel in a suitcase.

Upstream Beatify is a Home Assistant custom integration. This project runs the
**same upstream source, unmodified**, against a purpose-built `homeassistant`
shim, and replaces Music Assistant with a Spotify Connect daemon driven through
the Spotify Web API.

## Why a shim instead of a fork

Measured against upstream `v4.3.0`:

| | |
|---|---|
| Python in upstream | 67,328 LOC across 185 files |
| Files touching `homeassistant` | 41 |
| LOC in Home-Assistant-free files | 44,635 (66 %) |
| Distinct `homeassistant` imports | 25 |
| HA services called | 15 |

The coupling is broad but shallow — most of it is `hass.data` (a dict) and
`hass.config.path`. And upstream ships roughly **12 commits a day**, with
releases every couple of days: a refactor fork would be unmaintainable within
weeks.

So `vendor/custom_components/beatify/` is byte-identical to upstream. All the
port-specific code lives beside it, and `tools/check_ha_surface.py` fails the
build if a version bump reaches for anything the shim does not provide.

## Architecture

```
 Phone hotspot (party WLAN + internet)
        │
   ┌────┴──────────────────────────────────────┐
   │  Pi 4B / Batocera              :8123      │
   │  ┌─────────────────────────────────────┐  │
   │  │ beatify_standalone   (bootstrap)    │  │
   │  │   aiohttp app · auth · drivers      │  │
   │  ├─────────────────────────────────────┤  │
   │  │ homeassistant/       (the shim)     │  │
   │  ├─────────────────────────────────────┤  │
   │  │ custom_components/beatify/          │  │
   │  │              UNMODIFIED UPSTREAM    │  │
   │  └─────────────────────────────────────┘  │
   │        │ Spotify Web API                  │
   │        ▼                                  │
   │  go-librespot  ──ALSA──▶ HDMI / 3.5 mm    │
   └───────────────────────────────────────────┘
```

All 54 bundled playlists carry `spotify:track:` URIs, which is what makes
dropping Music Assistant possible at all.

**Deliberately not supported:** Sonos/Alexa, Apple Music, Tidal, Deezer, YouTube
Music, local libraries, party lights, HA cloud TTS. Requires **Spotify Premium**.

### The one assumption about upstream internals

`MediaPlayerService._play_song` has no generic branch — it dispatches only to
`_play_via_music_assistant`, `_play_via_sonos` or `_play_via_alexa`. The Sonos
path is the narrowest one that reaches a plain, provider-agnostic
`media_player.play_media` call, so the synthetic entity registers itself with
`platform = "sonos"`. `tools/check_ha_surface.py --seams-only` asserts this
still holds on every bump.

## Status

Running **on real hardware**: a Raspberry Pi 4B in a Retroflag NESPi 4 case,
Batocera 43.1. Upstream boots against the shim, discovers the Spotify player,
serves its pages, and go-librespot advertises itself on the LAN as a Spotify
Connect device. 143 tests green, on the desktop and on the Pi.

Everything measured on that box — including three bugs only hardware found — is
written up in [docs/HARDWARE-FINDINGS.md](docs/HARDWARE-FINDINGS.md) so a rebuild
from a blank card does not have to rediscover it.

**Still open:** no Spotify client id has been configured yet, so no audio has
actually played end to end. HDMI audio is unverified (no display was attached).
The NESPi 4's SATA bridge needed a UAS quirk, which the installer now applies
automatically — see the findings document.

## Setup on Batocera

Batocera's root filesystem is a read-only SquashFS; only `/userdata` persists.
Everything below stays inside it.

```sh
# on the Pi
mkdir -p /userdata/beatify/app
# copy this repository there (scp/rsync from your machine), then:
sh /userdata/beatify/app/deploy/install-batocera.sh
```

The installer fetches a self-contained CPython and `go-librespot`, installs the
dependencies, and writes the service. Then:

1. **Pick the audio output.** `aplay -l` lists the ALSA devices; put the one you
   want in `librespot_device` (HDMI on a Pi 4 is usually `hw:CARD=vc4hdmi0`, the
   3.5 mm jack `hw:CARD=Headphones`).

2. **Create a Spotify app** at the [developer dashboard](https://developer.spotify.com/dashboard).
   The redirect URI must be exactly `http://127.0.0.1:8123/beatify/spotify/callback`.
   Copy the client id into `spotify_client_id`. No client secret is needed — the
   flow is Authorization Code + PKCE, so nothing secret travels on the SD card.

3. **Start it.**
   ```sh
   batocera-services enable beatify
   batocera-services start beatify
   ```

4. **Authorise Spotify once.** Since 27 November 2025 Spotify accepts `http://`
   redirect URIs *only* for loopback literals, so this cannot be done against
   the Pi's LAN address. Tunnel in from a machine with a browser:
   ```sh
   ssh -N -L 8123:127.0.0.1:8123 root@<pi>
   ```
   then open `http://127.0.0.1:8123/beatify/spotify/login`. The refresh token is
   stored, so this does not need repeating on the road.

5. **Host a game.** The admin PIN is written to `/userdata/beatify/beatify.log`
   on first start. Open `http://<pi>:8123/beatify/admin`; guests scan the QR.

### Networking

Batocera has no access-point mode, and AirBnB WiFi very often isolates clients
from each other — which would stop guests reaching the box at all. Use a **phone
hotspot** as the party WLAN: it solves client isolation and internet in one
step. Point Batocera at it via `wifi.ssid` / `wifi.key` in
`/userdata/system/batocera.conf`.

## Updating upstream

```sh
python3 tools/check_ha_surface.py --tag v4.4.0    # does the shim still cover it?
```

Only if that passes: replace `vendor/custom_components/beatify/` with the new
tag, update `VERSION.upstream`, and run the tests. If it fails, the output is
the exact list of shim work the bump requires.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # 139 tests
.venv/bin/python tools/check_ha_surface.py
.venv/bin/python bin/beatify-standalone --data-dir ./data --port 8123
```

`vendor/` is upstream's code and is never linted, formatted or edited.


## Credits and licence

| | |
|---|---|
| **Beatify** — the game, the playlists, the frontend, all game logic | [mholzi/beatify](https://github.com/mholzi/beatify) by Markus Holzhäuser, MIT |
| **go-librespot** — the Spotify Connect daemon | [devgianlu/go-librespot](https://github.com/devgianlu/go-librespot) |
| **python-build-standalone** — the self-contained CPython | [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone) |
| **Batocera.linux** — the host system | [batocera-linux/batocera.linux](https://github.com/batocera-linux/batocera.linux) |

This wrapper is MIT-licensed — see [LICENSE](LICENSE). It is an independent
project and is neither endorsed by nor affiliated with any of the above.
