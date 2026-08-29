#!/bin/sh
# Install the Beatify standalone runtime on Batocera (Raspberry Pi 4, aarch64).
#
# Everything lands under /userdata, which is the only persistently writable
# place on Batocera — the root filesystem is a read-only SquashFS with a
# tmpfs overlay, so anything installed into / is gone on reboot unless it is
# explicitly saved into the overlay. Nothing here touches the overlay.
#
# Run it on the Pi over SSH:
#     sh /userdata/beatify/app/deploy/install-batocera.sh
#
# POSIX sh on purpose: Batocera ships busybox, not bash.

set -eu

PREFIX="${BEATIFY_PREFIX:-/userdata/beatify}"
APP_DIR="$PREFIX/app"
PY_DIR="$PREFIX/python"
BIN_DIR="$PREFIX/bin"
DATA_DIR="$PREFIX/data"

PYTHON_RELEASE="${BEATIFY_PYTHON_RELEASE:-20260825}"
PYTHON_VERSION="${BEATIFY_PYTHON_VERSION:-3.13.15}"
GO_LIBRESPOT_VERSION="${BEATIFY_GO_LIBRESPOT_VERSION:-v0.9.0}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(uname -m)" = "aarch64" ] || die "expected aarch64, got $(uname -m). Batocera on a Pi 4 must be the 64-bit build."
command -v curl >/dev/null 2>&1 || die "curl not found"

mkdir -p "$PY_DIR" "$BIN_DIR" "$DATA_DIR"

# --- Python -----------------------------------------------------------------
# Batocera's own Python is part of the read-only firmware and has no usable pip,
# and a system upgrade would replace it underneath us. A self-contained build
# keeps the runtime independent of the distro entirely.
if [ ! -x "$PY_DIR/bin/python3" ]; then
  log "Installing CPython $PYTHON_VERSION (python-build-standalone $PYTHON_RELEASE)"
  PY_TARBALL="cpython-${PYTHON_VERSION}+${PYTHON_RELEASE}-aarch64-unknown-linux-gnu-install_only.tar.gz"
  PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_RELEASE}/${PY_TARBALL}"
  curl -fsSL "$PY_URL" -o "$PREFIX/python.tar.gz" || die "download failed: $PY_URL"
  # The tarball unpacks into a top-level `python/` directory.
  tar -xzf "$PREFIX/python.tar.gz" -C "$PREFIX"
  rm -f "$PREFIX/python.tar.gz"
  [ -x "$PY_DIR/bin/python3" ] || die "python bundle did not unpack as expected"
else
  log "CPython already installed, skipping"
fi

log "Python reports: $("$PY_DIR/bin/python3" --version)"

# --- Python dependencies ----------------------------------------------------
log "Installing Python dependencies"
"$PY_DIR/bin/python3" -m pip install --quiet --upgrade pip
"$PY_DIR/bin/python3" -m pip install --quiet -r "$APP_DIR/requirements.txt" \
  || die "pip install failed — if this is a wheel/glibc mismatch, try the musl python-build-standalone variant"

# --- go-librespot -----------------------------------------------------------
# The Rust librespot publishes no release binaries at all, so a prebuilt static
# Go binary is the only thing that installs without a toolchain on this box.
if [ ! -x "$BIN_DIR/go-librespot" ]; then
  log "Installing go-librespot $GO_LIBRESPOT_VERSION"
  GL_URL="https://github.com/devgianlu/go-librespot/releases/download/${GO_LIBRESPOT_VERSION}/go-librespot_linux_arm64.tar.gz"
  curl -fsSL "$GL_URL" -o "$PREFIX/go-librespot.tar.gz" || die "download failed: $GL_URL"
  tar -xzf "$PREFIX/go-librespot.tar.gz" -C "$BIN_DIR"
  rm -f "$PREFIX/go-librespot.tar.gz"
  chmod +x "$BIN_DIR/go-librespot"
else
  log "go-librespot already installed, skipping"
fi


# --- USB-SATA bridge quirk --------------------------------------------------
# The Retroflag NESPi 4 ships a JMicron JMS567 bridge whose UAS implementation
# drops the drive off the bus under load and corrupts the filesystem with it —
# observed within 55 s of a first boot, triggered by nothing worse than
# ext4lazyinit. See docs/HARDWARE-FINDINGS.md. Disabling UAS for that one device
# fixes it at the cost of throughput.
#
# The ID is read from lsusb rather than hardcoded: this project's plan guessed
# 152d:0578 and the real chip is 152d:0562, and a wrong quirk string is silently
# ignored by the kernel — which looks exactly like "the fix does not work".
apply_usb_quirk() {
  command -v lsusb >/dev/null 2>&1 || { log "lsusb missing, skipping bridge quirk"; return 0; }

  BRIDGE_ID=$(lsusb | grep -i 'JMicron\|ASMedia.*SATA' | head -1 \
              | sed -n 's/.*ID \([0-9a-f]\{4\}:[0-9a-f]\{4\}\).*/\1/p')
  [ -n "$BRIDGE_ID" ] || { log "no known-problematic USB-SATA bridge detected"; return 0; }

  if grep -q "usb-storage.quirks=.*${BRIDGE_ID}" /boot/cmdline.txt 2>/dev/null; then
    log "UAS quirk for $BRIDGE_ID already present"
    return 0
  fi

  log "found USB-SATA bridge $BRIDGE_ID — disabling UAS for it"
  mount -o remount,rw /boot || { log "could not remount /boot rw, skipping"; return 0; }
  cp -n /boot/cmdline.txt /boot/cmdline.txt.beatify-backup 2>/dev/null || true
  sed -i "s/\$/ usb-storage.quirks=${BRIDGE_ID}:u/" /boot/cmdline.txt
  sync
  mount -o remount,ro /boot || true
  QUIRK_APPLIED=1
  log "quirk written — takes effect after a reboot (backup: /boot/cmdline.txt.beatify-backup)"
}

QUIRK_APPLIED=0
apply_usb_quirk

# --- service ----------------------------------------------------------------
log "Installing the Batocera service"
mkdir -p /userdata/system/services
# Strip CR: a service script with CRLF endings silently never launches.
sed 's/\r$//' "$APP_DIR/deploy/batocera/beatify" > /userdata/system/services/beatify
chmod +x /userdata/system/services/beatify

# --- helper commands --------------------------------------------------------
log "Installing helper commands"
install -m 0755 "$APP_DIR/deploy/batocera/beatify-audio" "$BIN_DIR/beatify-audio"

# Drop a game hook from an older install: Beatify is meant to keep running
# (and reachable on the website) while a game runs, not get stopped by one.
if [ -f /userdata/system/scripts/beatify-gamehook ]; then
  rm -f /userdata/system/scripts/beatify-gamehook
  log "removed the old game hook (Beatify no longer stops when a game starts)"
fi

# --- default config ---------------------------------------------------------
if [ ! -f "$DATA_DIR/beatify_standalone.json" ]; then
  log "Writing a starter config"
  cat > "$DATA_DIR/beatify_standalone.json" <<JSON
{
  "port": 8123,
  "country": "DE",
  "spotify_client_id": "",
  "librespot_flavor": "go",
  "librespot_binary": "$BIN_DIR/go-librespot",
  "librespot_name": "Beatify",
  "librespot_device": "default",
  "librespot_bitrate": 320
}
JSON
fi

cat <<DONE

==> Installed.
$( [ "$QUIRK_APPLIED" = "1" ] && printf '\n  !! A USB-SATA bridge quirk was written to /boot/cmdline.txt.\n     REBOOT before trusting the SSD.\n' )

Next steps:

  1. Pick the ALSA output and put it in $DATA_DIR/beatify_standalone.json
     as "librespot_device". List the options with:  aplay -l
     Default is "default", which routes through PipeWire (Batocera 43 runs it)
     so the Connect daemon and EmulationStation do not fight over the device.
     Direct alternatives: plughw:CARD=Headphones (3.5 mm, verified working) or
     plughw:CARD=vc4hdmi0 (HDMI, needs a display actually attached).

  2. Create a Spotify app at https://developer.spotify.com/dashboard
     Redirect URI must be exactly:  http://127.0.0.1:8123/beatify/spotify/callback
     Put its client id into "spotify_client_id".

  3. Enable and start the service:
       batocera-services enable beatify
       batocera-services start beatify

  4. Authorise Spotify once, from a machine with a browser. Spotify only
     accepts http:// redirects for 127.0.0.1, so tunnel in:
       ssh -N -L 8123:127.0.0.1:8123 root@<this-pi>
     then open  http://127.0.0.1:8123/beatify/spotify/login

  5. The admin PIN is printed in $PREFIX/beatify.log on first start.
     Host the game at  http://<this-pi>:8123/beatify/admin

DONE
