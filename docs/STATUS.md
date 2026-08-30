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
| **Bluetooth controller** | Xbox Series pad; stayed disconnected until its firmware was updated via the Xbox Accessories app on Windows, now pairs and stays connected |
| **Tests** | 201, green on the desktop and on the Pi |

Administration is entirely from a phone, all pages PIN-free by default:

    http://batocera.local/                    front door
                        /beatify/admin        host a game
                        /beatify/audio        output (instant, no restart) + test tone
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

## Where the state lives

Everything survives a reboot. `/etc` is a tmpfs overlay, so nothing there would
— which is why the Bluetooth tuning is re-applied by `/boot/postshare.sh` on
every boot rather than edited in place.

| Path | Holds |
|---|---|
| `/boot/cmdline.txt` | the `usb-storage.quirks` for the JMS567 bridge |
| `/boot/batocera-boot.conf` | Wi-Fi for the earliest boot stage |
| `/boot/postshare.sh` | SSH key, persistent Wi-Fi, Bluetooth tuning, storage guard — runs as root |
| `/userdata/beatify/` | app, Python bundle, go-librespot, Piper voice, tokens |
| `/userdata/system/services/beatify` | the autostart service |

`/userdata` is currently the **SD card's** `SHARE` partition (`/dev/mmcblk0p2`),
rolled back there while the item below is open. Do not point EmulationStation's
STORAGE DEVICE menu at another drive: that menu moves no data, so the box comes
back up looking wiped while everything sits safe where it was. It has happened
once; `postshare.sh` now catches it and reboots itself back. See
`docs/HARDWARE-FINDINGS.md` §4.

## Open, and where it was left

**/userdata belongs on the SSD, and the move itself works.** SD cards die of
writes and every write this box makes goes to `/userdata`; `/boot` is read-only,
so moving it takes the card out of the wear path entirely. Measured on the box:
64 MB written into `/userdata` produced 153 MB of sectors on the SSD and **zero**
on the card. `deploy/move-userdata-to-ssd.sh` did the migration and verified all
9113 files by checksum before switching anything; the SSD is one GPT partition
labelled `BEATIFY` (`849820dc-…`), holds a complete copy, and passes `e2fsck`
after a journal replay. The card still holds the pre-migration original.

It is **not** switched on, because the box then became unreachable and needed a
card reader to recover. Root cause of that is known and fixed: a storage guard
was briefly run from `/boot/preshare.sh`, which executes inside `S11share` while
init is still waiting for it, and rebooting from there produced unclean reboots.
That hook is gone; the guard is back in `postshare.sh`, where it was already
proven, and now refuses to reboot unless it can read its own fix back from
`/boot` first.

What is **not** yet established is why the box kept dropping off Wi-Fi
afterwards — it did so again once while running normally on the card, so it may
be a separate fault rather than fallout. Settle that before switching `/userdata`
back to the SSD, since a box that vanishes is a box that needs a screwdriver.

To resume: the intended target is preserved on the boot partition as
`beatify-storage.conf.pending` — rename it back and set
`sharedevice=DEV 849820dc-…` in `batocera-boot.conf`.
`deploy/rescue-boot-partition.sh` reverses that from a card reader.

## Picking it up again

    .venv/bin/python -m pytest              # 201 tests
    .venv/bin/python tools/check_ha_surface.py   # before any upstream bump
    BEATIFY_PI=<ip> bin/pi <command>        # ssh to the box

Deploying a change is: tar the tree, copy it over, replace
`/userdata/beatify/app`, restart the service. There is no build step.
