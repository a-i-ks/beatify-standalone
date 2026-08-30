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
| `sharedevice=` in `/boot/batocera-boot.conf` decides which partition becomes `/userdata` | The STORAGE DEVICE menu writes it. **It moves nothing** — see below |

### The STORAGE DEVICE menu looks like data loss

Picking a drive in EmulationStation's STORAGE DEVICE menu rewrites `sharedevice=`
and mounts that partition as `/userdata` on the next boot. It does **not** copy
anything across, and it does not warn. The box then comes up with Batocera's
empty skeleton directories: no Beatify, no web UI, no ROMs, no controller
pairings — while all of it sits untouched on the card.

It reads exactly like a wiped disk. It is not. Before assuming the worst:

```sh
awk '$2=="/userdata"{print $1}' /proc/mounts   # what is mounted now
blkid -L SHARE                                 # where the data actually is
grep ^sharedevice= /boot/batocera-boot.conf    # what asked for it
```

Here the SSD held an old partition labelled `retropie`, so the menu offered it
as a plausible target and Batocera mounted it without complaint. Two telltales,
both of which read as catastrophic and mean nothing of the kind: `df` shows a
near-empty `/userdata`, and the **SSH host key changes**, because host keys live
in `/userdata/system/ssh` and the other partition has its own set.

`postshare.sh` now guards against this (section 5 of the generated hook): when
`/userdata` is not the partition labelled `SHARE`, it puts `sharedevice=INTERNAL`
back and reboots once. A marker file on `/boot` makes that a one-shot, so a share
that genuinely cannot be mounted records the fact rather than rebooting forever,
and `/boot/beatify-allow-storage-move` stands the guard down for a deliberate
migration. Verified by injecting the fault on the real box: it healed itself and
came back complete, without anyone touching it.

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
and an emulator do *not* fight over the audio device, and there is no need to
stop Beatify while a game runs — an EmulationStation `gameStart` hook that did
that was removed for exactly this reason. An earlier test suggested the
opposite; it had started the second stream 0.3 s after the first, which was not
long enough to be sure the first had opened at all.

**Idle cost is not an argument for stopping anything.** Beatify idles at ~74 MB
RSS and go-librespot at ~21 MB — together 1.2 % of an 8 GB Pi, about a second of
CPU per minute.

**go-librespot re-registers by itself.** With `persist_credentials: true` the
device is back in the account's Connect list about five seconds after a service
restart, with no phone interaction. That is what makes the box usable on the
road.

---

## 6. Bluetooth controllers

An **Xbox Wireless Controller (045E:0B13)** pairs and works, driven by
`xpadneo` v6, which Batocera ships. ERTM is already disabled
(`/sys/module/bluetooth/parameters/disable_ertm = Y`), so the usual Xbox-on-Linux
fix is not needed here.

**The Xbox button keeps blinking, and that is not a failure.** The controller
connects over HID-over-GATT, so the kernel names it `bluez-hog-device`. On a real
console the host assigns a player slot and the LED then goes solid; nothing on
Linux sends that assignment, so it blinks forever. It cost an evening to chase.

Judge success by these instead:

| Signal | Meaning |
|---|---|
| The controller rumbles twice on connect | `xpadneo_welcome_rumble` — the driver bound. This is the reliable one. |
| `/dev/input/js0` exists | An input device was actually created |
| `ls /sys/bus/hid/drivers/xpadneo/` lists a device | The HID profile is bound, not just the ACL link |
| `cat /dev/input/js0` produces bytes while you press | Input genuinely arrives |

`bluetoothctl` reporting `Connected: yes` proves **nothing** on its own: an
earlier attempt showed exactly that while `xpadneo` had nothing bound and no
input device existed at all. A Bluetooth link without a bound HID profile looks
identical to a working controller from that command's point of view.

A failed attempt leaves a broken pairing that BlueZ keeps retrying in the wrong
transport (`le-connection-abort-by-local`). Remove the device before pairing
again rather than retrying on top of it.

### Confirmed: it was the firmware

`xpadneo` said the firmware was old, and it said so before anything else went
wrong, which is why it was the first place to look:

```
xpadneo 0005:045E:0B13.000A: BLE firmware version 5.09, please upgrade for better stability
Bluetooth: hci0: Bad flag given (0x1) vs supported (0x0)
```

The controller paired, `xpadneo` bound, the welcome rumble fired — and the link
died somewhere between 9 and 69 seconds later having delivered **zero** input
events.

Batocera's own wiki said the same thing for exactly this model, under
*Xbox Core/Series S/Series X controllers*:

> If the controller is not pairing correctly, it may need to have its firmware
> updated via a Windows 10+ PC or an Xbox One/Series console.

**Updating the pad through the Xbox Accessories app on Windows fixed it.**
Firmware only updates that way, or through an Xbox console; there is no Linux
path. After the update the controller connects and stays connected — the
9-to-69-second disconnect is gone.

That settles the question the first version of this section left open: a
second, untested explanation looked just as plausible from the evidence alone —

* The Pi 4's Bluetooth is a **Cypress CYW43455 on a UART**, sharing silicon and
  antenna with Wi-Fi. `hciconfig` reports `Bus: UART`. It looked like the
  weakest link in this box by a distance, and BLE HID is the traffic pattern
  that would expose it.
* The controller had **only ever been tried on this one host**, so a
  misbehaving adapter could not be ruled out from here alone.

— but the firmware update alone made the disconnects stop, on the same host,
with nothing else changed. The Cypress radio was never the problem.

### Diagnosing it

`deploy/bt-controller-probe.sh` runs one pairing attempt end to end and answers
the four questions in order: does it appear in a scan, does a driver bind, does
pressing buttons produce events, and — if the link drops — what reason the
controller gives for leaving. That last one is the fact no earlier attempt
captured, and it is the one that separates the two theories:

| Disconnect reason | Points at |
|---|---|
| `0x08` supervision timeout | the radio: interference, range, connection interval — an adapter problem |
| `0x13` remote user ended it | the controller decided to leave — firmware or standby |
| `0x22` LMP response timeout | the controller stopped answering — radio or firmware |
| `0x3b` unacceptable connection parameters | the pad rejected our intervals — tunable in `main.conf` |

The script runs on any Linux box with bluez, which is the point: run it on the
Pi, then on a machine with a different adapter, and compare. It needs root, and
traces HCI with `btmon` where available and `hcidump` — what Batocera ships —
otherwise.

### Paths that do not depend on the theory being right

* **USB cable.** Works today, costs nothing. The wiki is blunt about it:
  "Xbox controllers always work if connected via USB cable."
* **Xbox Wireless Adapter.** Microsoft's own dongle speaks a proprietary
  2.4 GHz protocol, not Bluetooth, so it sidesteps the Cypress radio and the
  pad's BLE firmware in one move. Batocera already ships the driver —
  `xone_dongle.ko` is present in `/lib/modules/6.12.62-v8/updates/`. The wiki
  also reports the third-party Cipon adapter working with these pads.

**Ruled out along the way**, so nobody repeats them:

* *Wi-Fi/Bluetooth coexistence.* Real on a Pi 4, but not this: the box is on
  5 GHz (5500 MHz), so the shared 2.4 GHz front end is idle.
* *ERTM.* Already disabled by Batocera.
* *`ControllerMode = bredr`.* Actively harmful here. Product `0x0B13` is an
  Xbox Series X|S controller, and those connect over BLE by design — the
  `bluez-hog-device` name is normal for them, not a symptom. Forcing classic
  Bluetooth removes the only transport the controller has, and it stops being
  discoverable at all.
* *Batocera's LE tuning.* `main.conf` ships a commented `[LE]` block labelled
  "for Xbox X|S controllers". Enabling it changed the reported device name from
  `bluez-hog-device` to `Xbox Wireless Controller`, so it does something — but
  it did not stop the disconnects.

### Two traps when testing this remotely

**`bluetoothctl scan on` stops scanning the moment it exits.** Run
non-interactively it connects, starts discovery, prints `Discovery started`, and
returns — and discovery ends with the client, while `bluetoothctl show` says
`Discovering: no`. Hold stdin open for the whole window instead:
`( echo "scan on"; sleep 300 ) | bluetoothctl &`. A search that silently is not
searching looks exactly like a controller that will not pair.

**Reading `/dev/input/js0` always yields a burst that is not input.** The
joystick API emits one synthetic event per axis and per button on open — for an
Xbox pad, 19 events, 152 bytes. Measuring that and concluding "input works" is a
mistake this project made. Always take a no-press baseline first, and compare.

---

## 7. Bugs in *this* project that only hardware found

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

## 8. Switching audio output live, through PipeWire directly

Picking an output used to mean rewriting go-librespot's `audio_device` and
restarting the daemon — a few seconds of silence, awkward mid-round. Measured
on the box instead of assumed: PipeWire can be told directly which card is the
default output, live, with no daemon involved at all.

**`pactl`, `wpctl`, `pw-play` and `pw-cat` are all present** on Batocera 43,
reachable the same way `aplay -D default` already needed to be —
`XDG_RUNTIME_DIR=/var/run`.

**go-librespot does not hold the device open while idle.** A `pactl list
sink-inputs` right after starting it, connected but not playing, shows nothing
at all — it opens PipeWire only for the duration of a track. So most output
switches have no live stream to move; setting the default sink is enough for
whatever plays next. If a track *is* playing, `pactl move-sink-input <id>
<sink>` re-routes it immediately, with no restart and no audible gap — verified
with a 5-second tone kept running across the move.

**Forcing a profile on a card with nothing physically attached "succeeds" and
produces no sink.** `pactl set-card-profile … pro-audio` on `vc4hdmi0` (no
display attached during the test) returned exit 0 and changed
`active_profile`, but no sink ever appeared in `pactl list sinks`. This is
section 2's `vc4hdmi0 | error 524 with no display attached` finding, seen from
the PipeWire side instead of the ALSA side — same underlying limitation, not a
new one. The fix here is the same shape as there: check for the sink
afterwards rather than trusting the exit code.

**None of this survives a reboot on its own.** `/var` is a `tmpfs` on
Batocera, and there is no `/var/lib/wireplumber` — WirePlumber has nowhere to
remember a default sink across a restart, and comes back at its own priority
order every time. Whatever was pinned has to be reapplied once PipeWire is
back up; `reapply_pipewire_output_at_boot` in `audio_setup.py` does this from
Beatify's own startup, retrying for a few seconds in case it wins the race
against PipeWire the way go-librespot itself sometimes does at boot.

> **A trap while measuring this remotely.** `pkill -f beatify-standalone` run
> over `ssh host '…'` can kill the *ssh session itself*: the remote shell's own
> command line is `sh -c 'pkill -f beatify-standalone; …'`, which contains the
> literal pattern being searched for, so `pkill -f` matches its own parent
> shell and the connection dies mid-script with no further output. Happened
> twice while testing this. The fix is the classic `ps`/`grep` self-exclusion
> trick applied to `pkill`: `pkill -f '[b]eatify-standalone'` — a bracket
> expression that still matches the literal text in another process's command
> line, but not in the pattern argument that spells it with brackets.

---

## 9. The case fan keeps spinning after shutdown — open

The Pi halts, but the NESPi 4's board never cuts power: LEDs stay lit and the
fan runs until the plug comes out. What is *established* on this box:

* `system.power.switch=RETROFLAG` is set, `/etc/init.d/S92switch` starts
  `rpi_gpioswitch`, and `rpi-retroflag-AdvancedSafeShutdown` is running.
* `dtoverlay=RetroFlag_pw_io.dtbo` is present in `/boot/config.txt`. The actual
  power cut is that overlay's job, not the Python daemon's.
* Batocera's own `config.txt` ships `# enable UART (required for for retroflag)`
  directly above a commented-out `enable_uart=1`, and it was **not** enabled.
  It is now. Bluetooth is unaffected (it uses the PL011, not the mini-UART) —
  confirmed after the change: `hci0` comes up and the pad still pairs.
* **Enabling UART does not fix it.** Tested the only way it can be: a real
  shutdown from the menu, watched at the machine. The fan kept running. So the
  UART line is a prerequisite Batocera documents, not the cause.

That leaves the daemon. Upstream has this open for exactly this case and Pi
model — [batocera-linux#13725][fanbug], with [PR #13789][fanpr] unmerged — and
the thread's conclusion is that `rpi-retroflag-AdvancedSafeShutdown` never
drives `POWEREN_PIN` low, so the board is never told to cut power. Two candidate
fixes, in the order worth trying:

1. Replace the daemon's `shutdown -h` path with `shutdown -r`. Counter-intuitive,
   but it is what the official Retroflag script does: the overlay cuts power
   during the reboot sequence, before the system comes back up.
2. Drive `POWEREN_PIN` low from the daemon at shutdown. A maintainer warns this
   cuts power *immediately* and can lose metadata, so it is the second choice,
   not the first.

Note that the daemon lives in the read-only squashfs at
`/usr/bin/rpi-retroflag-AdvancedSafeShutdown`, so a fix has to be a copy started
from somewhere persistent rather than an edit in place.

[fanbug]: https://github.com/batocera-linux/batocera.linux/issues/13725
[fanpr]: https://github.com/batocera-linux/batocera.linux/pull/13789

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
