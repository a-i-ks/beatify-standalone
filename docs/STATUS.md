# Where this stands

Snapshot at the end of the first build session, 27 August 2026. Written so the
work can be picked up cold — what is done, what is verified on hardware, what is
merely written, and what is known broken.

## Done and verified on the box

A Raspberry Pi 4B in a Retroflag NESPi 4 case, running Batocera 43.1, at
`batocera.local`.

| | |
|---|---|
| **Upstream Beatify** | `v4.3.0`, vendored byte-identical, runs against the shim |
| **Boots on its own** | `system.services=beatify`; verified across real reboots |
| **Spotify playback** | Real tracks from bundled playlists, seek, volume, stop |
| **Audio** | 3.5 mm and HDMI both verified with a real track |
| **Wi-Fi** | Home network + phone hotspot as a second network, joined automatically |
| **SSD** | UAS quirk applied; 90 GB moved under load with zero kernel errors |
| **Tests** | 201, green on the desktop and on the Pi |

Administration is entirely from a phone, all pages PIN-free by default:

    http://batocera.local/                    front door
                        /beatify/admin        host a game
                        /beatify/audio        output + test tone
                        /beatify/wifi         join a network
                        /beatify/bluetooth    pair a controller

## Not done

**A full game with real phones has never been played.** The driver is verified
in isolation — three consecutive rounds started the right track — but the lobby,
round flow, reveal and leaderboard have only been exercised by the test suite,
never by people with phones in a room. This is the largest untested surface and
should be the next thing done, at home, before travelling.

**Voice announcements.** Upstream has twelve announcement types
(`game/tts_phrases.py`), driven through Home Assistant's `tts.speak`. Piper is
installed on the box with a German voice and demonstrably speaks Beatify's own
phrases at ~3x realtime, but nothing is wired up: it needs a synthetic `tts.*`
entity so the wizard offers it, and a pause/announce/resume sequence, because a
Spotify Connect player cannot be handed a WAV.

**Bluetooth controllers do not work, and the cause is not ours.** The driver
says so directly:

    xpadneo: BLE firmware version 5.09, please upgrade for better stability

The controller pairs, xpadneo binds, the welcome rumble fires, and the link dies
between 9 and 69 seconds later having delivered zero input events. Firmware
updates only through the Xbox Accessories app on Windows or an Xbox console.
**Use a USB cable until then.** See `HARDWARE-FINDINGS.md` for what was ruled
out, so it is not re-investigated.

## Where the state lives

Everything survives a reboot. `/etc` is a tmpfs overlay, so nothing there would
— which is why the Bluetooth tuning is re-applied by `/boot/postshare.sh` on
every boot rather than edited in place.

| Path | Holds |
|---|---|
| `/boot/cmdline.txt` | the `usb-storage.quirks` for the JMS567 bridge |
| `/boot/batocera-boot.conf` | Wi-Fi for the earliest boot stage |
| `/boot/postshare.sh` | SSH key, persistent Wi-Fi, Bluetooth tuning — runs as root |
| `/userdata/beatify/` | app, Python bundle, go-librespot, Piper voice, tokens |
| `/userdata/system/services/beatify` | the autostart service |

## Picking it up again

    .venv/bin/python -m pytest              # 201 tests
    .venv/bin/python tools/check_ha_surface.py   # before any upstream bump
    BEATIFY_PI=<ip> bin/pi <command>        # ssh to the box

Deploying a change is: tar the tree, copy it over, replace
`/userdata/beatify/app`, restart the service. There is no build step.
