# Hardware findings — Raspberry Pi 4B in a Retroflag NESPi 4 case

Everything measured on the real box, so a rebuild from a blank SD card does not
have to rediscover it. Each entry says what was observed, what it means, and
where the fix now lives so it is applied automatically.

Measured 2026-08-26 against Batocera **43.1** (`batocera-bcm2711-43.1-20260530`),
kernel `6.12.62-v8`, on a **Raspberry Pi 4 Model B Rev 1.4, 8 GB**.

---

## 1. The SATA-USB bridge kills the SSD under load — the big one

**Observed.** On the very first boot the SSD dropped off the bus 55 seconds in
and took its ext4 journal with it:

```
[  1.392295] usb 2-1: idVendor=152d, idProduct=0562  Manufacturer: JMicron
[  1.405094] scsi host0: uas
[ 17.226648] FAT-fs (sda1): Volume was not properly unmounted.      <- had happened before
[ 50.178720] sd 0:0:0:0: [sda] uas_eh_abort_handler ... inflight: CMD IN
[ 55.518861] usb 1-1: USB disconnect, device number 2
[ 55.546701] sd 0:0:0:0: Device offlined - not ready after error recovery
[ 55.550913] EXT4-fs error (device sda2): Cannot read block bitmap
```

The trigger was `ext4lazyinit` — the routine post-mount housekeeping. Not even a
real workload. The case's VIA Labs USB hub (`2109:3431`) disconnected alongside
the bridge, so the whole USB path in the case reset, not just the drive.

**Meaning.** The bridge is a **JMicron JMS567 (`152d:0562`)** and its UAS
implementation is broken on this kernel. `Volume was not properly unmounted` on
the *first* mount proves this had been happening before, unnoticed.

> The project plan guessed `152d:0578` for this chip. That guess was wrong, and a
> wrong quirk string is silently ignored by the kernel — it would have looked
> like "the fix does not work". Always read the ID off `lsusb` / `dmesg`.

**Fix.** Force the device onto bulk-only transport by disabling UAS for that one
ID, in `/boot/cmdline.txt`:

```
usb-storage.quirks=152d:0562:u
```

Verified afterwards:

```
usb 2-1: UAS is ignored for this device, using usb-storage instead
usb-storage 2-1:1.0: Quirks match for vid 152d pid 0562: 800000
scsi host0: usb-storage
```

Applied automatically by `deploy/install-batocera.sh`, which detects a JMicron
bridge via `lsusb` and writes the matching quirk. `/boot` is mounted read-only —
remount it `rw` first. A backup is kept at `/boot/cmdline.txt.beatify-backup`.

**Verified after the quirk.** Both paths, with real I/O:

| Run | Load | Result |
|---|---|---|
| 15 min, write path | 130 x 512 MiB `conv=fsync`, ~68 GB, ~85 MB/s | 0 kernel errors |
| 10 min, read path | 168 dd runs, **90.2 GB**, raw-device reads at **298-305 MB/s** | 0 kernel errors |

The read rates cluster tightly in the 300 MB/s range with no gigabyte-per-second
outliers, which is how you can tell they came off the platter rather than the
page cache. The drive stayed on the bus throughout.

**Losing UAS costs almost nothing here.** 300 MB/s over bulk-only transport is
the USB 3 link talking, not the protocol. The throughput argument against the
quirk does not survive measurement.

> **Two traps in writing that test**, both of which made the first run claim more
> than it proved:
> * `iflag=count_bytes` makes `count` a *byte* count. `bs=4M count=1024` with it
>   reads **1024 bytes**, not 4 GB — a read test that silently tests nothing.
> * Reading back a file just written measures the page cache, not the disk. The
>   first run reported 1.4 GB/s on the verify read, which was RAM.
>
> This matters here specifically: the original failure aborted on `opcode=0x28`,
> i.e. **READ(10)**. A write-only test does not exercise the path that broke.
> `deploy/ssd-stress.sh` now reads from varying raw-device offsets and drops
> caches before every verify read.

**Still open.** The bootloader EEPROM on this board dates from **16 Feb 2021**
(`version d6d82cf9…`, timestamp 1613481816), which is old enough to be part of
the picture. If the quirk ever proves insufficient, the escalation is: Retroflag
NO_UAS bridge firmware → replace the bridge with a third-party adapter.

**Unrelated but worth knowing.** The SSD currently holds a **RetroPie**
installation (`sda1` = `boot`, `sda2` = `retropie`, 223 GB). Moving `/userdata`
onto it would destroy that.

---

## 2. Audio: Batocera 43 runs PipeWire, not raw ALSA

**Observed.** Three ALSA cards exist:

| Card | Device string | Note |
|---|---|---|
| `Headphones` | `plughw:CARD=Headphones` | 3.5 mm jack — **verified working** |
| `vc4hdmi0` | `plughw:CARD=vc4hdmi0` | error 524 with no display attached |
| `vc4hdmi1` | `plughw:CARD=vc4hdmi1` | second HDMI port |

`pipewire` and `wireplumber` hold the cards. `aplay -D default` fails from a
plain SSH session with **`Host is down`**, but succeeds with
`XDG_RUNTIME_DIR=/var/run` — that is where Batocera's PipeWire keeps its socket.

**Meaning.** This resolves the plan's worry that EmulationStation would hold the
audio device exclusively: it does not, PipeWire mixes. But any process without a
session environment — a Batocera service, for instance — cannot reach PipeWire
unless `XDG_RUNTIME_DIR` is set.

**Fix.** `deploy/batocera/beatify` exports `XDG_RUNTIME_DIR=/var/run`. Set
`librespot_device` to `default` to go through PipeWire; fall back to
`plughw:CARD=Headphones` (direct, exclusive) if PipeWire misbehaves.

**HDMI audio verified**, with a real Spotify track through the driver, on a TV.

**The two HDMI sockets are not interchangeable, and they are unlabelled.** The
DRM connector `HDMI-A-1` is ALSA card `vc4hdmi0`; `HDMI-A-2` is `vc4hdmi1`. On
the test box the cable was in the second socket, so a hardcoded `vc4hdmi0` sent
the audio to an empty port and nothing was heard while everything reported
success. `deploy/batocera/beatify-audio` reads
`/sys/class/drm/card*-HDMI-A-N/status` and picks the socket that actually has a
screen on it.

Note that selecting an HDMI card directly bypasses PipeWire, so PipeWire's own
HDMI sink volume no longer applies — level is then set by Spotify and by the
television.

`speaker-test` is **not** installed on Batocera. Generate a WAV with the bundled
Python and play it with `aplay` (see `docs/` history or just synthesise a sine).

---

## 3. Python: the bundle is required, and it works

**Observed.** Batocera ships `python3` **3.12.8** but **no pip**
(`No module named pip`). glibc is **Buildroot 2.40**.

The `aarch64-unknown-linux-gnu` build of python-build-standalone works, and —
the actual question — the compiled C extension loads:

```
python   : 3.13.15 aarch64
aiohttp  : 3.14.3
C-Parser : _http_parser.cpython-313-aarch64-linux-gnu.so -> loaded
OpenSSL  : OpenSSL 3.5.8
```

**Meaning.** The musl fallback contemplated in the plan is **not needed**.
Batocera's glibc 2.40 is new enough for manylinux wheels.

---

## 4. Batocera mechanics worth not rediscovering

All verified against upstream source, not folklore.

| Fact | Where it matters |
|---|---|
| `/boot` is mounted **read-only** | `mount -o remount,rw /boot` before editing `cmdline.txt` |
| `/boot/postshare.sh` runs **as root** at every boot, called at the end of `S12populateshare` | The only way to create root-owned files from a card prepared on a desktop |
| `S12populateshare` special-cases `system/batocera.conf` and creates it **only if absent** | A pre-seeded config survives first boot |
| ...but it creates it from a template with `wifi.enabled=0` | Which would kill Wi-Fi from the **second** boot on. `postshare.sh` rewrites it once. |
| `S08connman` falls back to `/boot/batocera-boot.conf` when `/userdata/system/batocera.conf` does not exist | This is how Wi-Fi comes up on the *first* boot, before userdata exists |
| SSH is on by default: `root` / `linux`, home is `/userdata/system` | Keys go to `/userdata/system/.ssh/authorized_keys`, root-owned, `0600` |
| `system.power.switch=RETROFLAG` | NESPi 4 power button / safe shutdown |
| Services live in `/userdata/system/services/<name>`, enabled with `batocera-services enable` | Must have **LF** line endings or they silently never launch |
| `S08connman` runs **before** `S11share` mounts /userdata | So at boot `/userdata/system/batocera.conf` does not exist yet and Wi-Fi is read from `/boot/batocera-boot.conf` |
| `S65values4boot` syncs Wi-Fi keys between the two — but opens with `[ "$1" = "stop" ] \|\| exit 0` | **Only on a clean shutdown.** Pull the plug after changing a network and the change is silently forgotten |
| EmulationStation runs scripts in `/userdata/system/scripts/` with `gameStart`/`gameStop` as `$1` | The hook point for pausing things while a game runs |
| `batocera-services stop` waits up to 10 s for the process to die | A test that checks after 3 s reports a false failure |
| Boot partition is 6 GB; the rest becomes `SHARE` (`/userdata`) on first boot | A 16 GB card leaves ~8.5 GB for userdata |

---

## 5. Spotify Web API quirks, measured against a live account

None of these are documented clearly enough to have been predicted. All were
found by driving the real API from the Pi.

**`play` with `uris` is refused; with `context_uri` it works.** Starting an
exact track via `PUT /me/player/play {"uris": [...]}` answers

```
403 {"error": {"status": 403, "message": "Player command failed: Restriction violated"}}
```

while the same track started as `{"context_uri": "<album>", "offset": {"uri": "<track>"}}`
returns 204 and plays exactly that track. The device was active and unrestricted,
`actions.disallows` was empty, and the track reported `is_playable: true` — so
none of the obvious explanations applied. This is a long-standing API quirk that
others report too. `MediaPlayerDriver._resolve_context` therefore looks up each
track's album (cached, since a playlist replays the same songs) and plays it as
a context.

**An idle Connect device refuses commands.** `play` against a device that is
merely *visible* fails the same way. It has to be made active first with
`PUT /me/player {"device_ids": [...]}`. A party box is idle between rounds, so
this is the normal path, not an edge case.

**Player commands answer 200 with plain text, not JSON.** `seek` and `pause`
return a bare request id such as `72iqe6X9om_ZNEUkXZB8DLUcPG0` with
`Content-Type: text/plain`. Parsing that as JSON and warning would put a false
alarm in the log on every single round.

**Never decide on `Content-Length`.** aiohttp reports it as `None` for chunked
and compressed responses, and Spotify sends both. An early version returned
`None` whenever it was absent, which silently discarded valid 200s — the device
list came back empty and `current_playback()` reported idle forever, which in
turn would have made upstream score **every track as a playback failure**.

**`/me` cannot confirm Premium** without the `user-read-private` scope, which
this project does not request. `product` comes back `None`. Working playback is
the only proof available.

**Wi-Fi changes need a clean reboot, or they must be written twice.** Because
of the two rows above, `beatify_standalone.wifi_setup.apply_network` writes the
network into `/boot/batocera-boot.conf` as well as into `batocera.conf`. A party
box gets unplugged rather than shut down, and losing the venue's Wi-Fi that way
would strand it.

**PipeWire and direct ALSA both mix concurrent streams.** Two `aplay` processes
on `default`, and two on `plughw:CARD=Headphones`, all play together. So Beatify
and an emulator do *not* fight over the audio device, and the game hook in
`deploy/batocera/scripts/` is optional rather than necessary. An earlier test
suggested otherwise; it had started the second stream 0.3 s after the first,
which was not long enough to be sure the first had opened at all.

**Idle cost is not an argument for stopping anything.** Beatify idles at ~74 MB
RSS and go-librespot at ~21 MB — together 1.2 % of an 8 GB Pi, about a second of
CPU per minute.

**go-librespot re-registers by itself.** With `persist_credentials: true` the
device is back in the account's Connect list about five seconds after a service
restart, with no phone interaction. That is what makes the box usable on the
road.

---

## 6. Bugs in *this* project that only hardware found

Documented because they are the class of thing that unit tests do not catch.

1. **`-config_dir` vs `--config_dir`.** go-librespot uses pflag, where `-c` is
   the short form of `--conf`. A single dash is parsed as `-c onfig_dir` and the
   daemon dies instantly with `invalid config override format: onfig_dir`.
   Regression test: `tests/test_librespot.py`.

2. **Every log line appeared twice.** The app added a `FileHandler` while the
   service script *also* redirected stdout into the same file. Regression test:
   `tests/test_logging_setup.py`.

3. **`Content-Length` was used to decide whether a response had a body** —
   see section 5. The most dangerous bug of the three: it failed silently and
   would have made every round look like a playback failure.

4. **A failing audio daemon was invisible.** The supervisor logged the daemon's
   output at `DEBUG`, so the restart loop printed "exited (1), restarting" with
   no reason. It now keeps the last ten lines and logs them at `ERROR` on a
   non-zero exit — a silently restarting audio daemon is the worst failure mode
   this project has.

---

## Rebuilding from a blank card

```sh
# 1. flash (guards against non-removable targets, verifies the gzip CRC)
sudo deploy/flash-batocera.sh batocera-bcm2711-<version>.img.gz /dev/disk/by-id/usb-…

# 2. pre-seed Wi-Fi + SSH key, no root needed
#    fill in ~/.beatify-wifi first (SSID / PASSWORD / COUNTRY)
deploy/prepare-sd-headless.sh

# 3. put the payload on the boot partition
tar -czf /run/media/$USER/BATOCERA/beatify/beatify-standalone.tar.gz \
    --exclude=.venv --exclude=.git --exclude=data --exclude=__pycache__ \
    --transform='s|^\.|beatify-standalone|' .

# 4. boot the Pi, then from the desktop:
BEATIFY_PI=<ip> bin/pi 'tar -xzf /boot/beatify/beatify-standalone.tar.gz -C /tmp \
    && mv /tmp/beatify-standalone /userdata/beatify/app \
    && sh /userdata/beatify/app/deploy/install-batocera.sh'
```

`bin/pi` wraps ssh with the right key and host options.
