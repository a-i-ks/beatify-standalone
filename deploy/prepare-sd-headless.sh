#!/usr/bin/env bash
# Pre-configure a freshly flashed Batocera card for headless first boot.
#
#   deploy/prepare-sd-headless.sh [credentials-file]
#
# Writes only to the vfat BATOCERA partition, so it needs no root. Three
# Batocera behaviours make that work, all verified in upstream's source rather
# than assumed:
#
#   * `S08connman` falls back to `/boot/batocera-boot.conf` when
#     `/userdata/system/batocera.conf` does not exist yet — which is exactly the
#     case on a first boot. That is how Wi-Fi comes up before userdata exists.
#   * `S12populateshare` runs `/boot/postshare.sh` AS ROOT at the end of every
#     boot. Anything needing root ownership is done from there, because a card
#     mounted on a desktop cannot create root-owned files.
#   * That same script special-cases `system/batocera.conf` and only creates it
#     when absent — but it creates it from a template with `wifi.enabled=0`,
#     which would silently kill Wi-Fi from the SECOND boot onwards. postshare.sh
#     therefore rewrites it once, on the first boot, and leaves it alone after.

set -euo pipefail

CRED_FILE="${1:-$HOME/.beatify-wifi}"
PUBKEY="${BEATIFY_PUBKEY:-$HOME/.ssh/beatify_pi.pub}"
MARKER="### beatify-headless ###"

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*"; }

[ -f "$CRED_FILE" ] || die "credentials file not found: $CRED_FILE"
[ -f "$PUBKEY" ]    || die "public key not found: $PUBKEY"

SSID=""; PASSWORD=""; COUNTRY="DE"
# shellcheck disable=SC1090
. "$CRED_FILE"
[ -n "$SSID" ]     || die "SSID is empty in $CRED_FILE"
[ -n "$PASSWORD" ] || die "PASSWORD is empty in $CRED_FILE"

# batocera.conf's own template says to escape these three in the Wi-Fi key.
escape_conf() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/\([#;$]\)/\\\1/g'; }
SSID_ESC=$(escape_conf "$SSID")
PASSWORD_ESC=$(escape_conf "$PASSWORD")

BOOT_DEV=$(lsblk -nrpo NAME,LABEL | awk '$2=="BATOCERA"{print $1; exit}')
[ -n "$BOOT_DEV" ] || die "no partition labelled BATOCERA found — is the card in the reader?"

BOOT_MP=$(findmnt -n -o TARGET "$BOOT_DEV" 2>/dev/null || true)
if [ -z "$BOOT_MP" ]; then
    udisksctl mount -b "$BOOT_DEV" >/dev/null
    BOOT_MP=$(findmnt -n -o TARGET "$BOOT_DEV")
fi
[ -w "$BOOT_MP" ] || die "$BOOT_MP is not writable"
log "BATOCERA partition at $BOOT_MP"

cleanup() { sync; udisksctl unmount -b "$BOOT_DEV" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# --- early-boot Wi-Fi -------------------------------------------------------
BOOTCONF="$BOOT_MP/batocera-boot.conf"
[ -f "$BOOTCONF" ] || die "batocera-boot.conf missing — is this really a Batocera card?"

# Idempotent: drop any previous block of ours before appending a new one.
if grep -qF "$MARKER" "$BOOTCONF"; then
    log "replacing previous beatify block in batocera-boot.conf"
    sed -i "/$MARKER/,/$MARKER end/d" "$BOOTCONF"
fi

log "adding Wi-Fi to batocera-boot.conf (used on the first boot only)"
cat >> "$BOOTCONF" <<CONF

$MARKER
## Read by S08connman on the first boot, before /userdata exists.
wifi.enabled=1
wifi.country=${COUNTRY}
wifi.ssid=${SSID_ESC}
wifi.key=${PASSWORD_ESC}
system.hostname=BATOCERA
$MARKER end
CONF

# --- root-side setup --------------------------------------------------------
log "writing postshare.sh (runs as root on every boot)"
KEY_CONTENT=$(cat "$PUBKEY")
cat > "$BOOT_MP/postshare.sh" <<HOOK
#!/bin/bash
# Executed as root by /etc/init.d/S12populateshare. Two jobs:
#   1. install an authorized_keys dropbear will accept (needs root ownership)
#   2. make the Wi-Fi settings permanent, because the batocera.conf that
#      S12populateshare just created from its template has wifi.enabled=0 and
#      takes precedence over batocera-boot.conf from the second boot onwards.

[ "\$1" = "start" ] || exit 0

LOG=/userdata/system/logs/beatify-postshare.log
mkdir -p /userdata/system/logs
say() { echo "\$(date -u '+%Y-%m-%dT%H:%M:%SZ') \$*" >> "\$LOG"; }

# --- 1. SSH key ---
KEY='${KEY_CONTENT}'
AUTH=/userdata/system/.ssh/authorized_keys
mkdir -p -m 0700 /userdata/system/.ssh
grep -qxF "\$KEY" "\$AUTH" 2>/dev/null || printf '%s\n' "\$KEY" >> "\$AUTH"
chown -R root:root /userdata/system/.ssh
chmod 0700 /userdata/system/.ssh
chmod 0600 "\$AUTH"
say "ssh key installed"

# --- 2. persistent Wi-Fi ---
CONF=/userdata/system/batocera.conf
if ! grep -q '^wifi.ssid=' "\$CONF" 2>/dev/null || grep -q '^wifi.enabled=0' "\$CONF" 2>/dev/null; then
    sed -i -e '/^#*wifi\.enabled=/d' -e '/^#*wifi\.ssid=/d' \\
           -e '/^#*wifi\.key=/d'     -e '/^#*wifi\.country=/d' \\
           -e '/^#*system\.power\.switch=/d' "\$CONF" 2>/dev/null || true
    {
        echo ""
        echo "### beatify-headless ###"
        echo "wifi.enabled=1"
        echo "wifi.country=${COUNTRY}"
        echo "wifi.ssid=${SSID_ESC}"
        echo "wifi.key=${PASSWORD_ESC}"
        echo "system.power.switch=RETROFLAG"
    } >> "\$CONF"
    say "batocera.conf seeded with wifi + retroflag power switch"
fi

exit 0
HOOK

# --- report -----------------------------------------------------------------
echo
log "card ready for headless first boot"
echo
printf "  %-14s %s\n" "SSID:"      "$SSID"
printf "  %-14s %s\n" "country:"   "$COUNTRY"
printf "  %-14s %s\n" "hostname:"  "BATOCERA  (mDNS: BATOCERA.local)"
printf "  %-14s %s\n" "ssh key:"   "$(ssh-keygen -lf "$PUBKEY" | awk '{print $2}')"
printf "  %-14s %s\n" "ssh login:" "root@<ip>  (password 'linux' also still works)"
printf "  %-14s %s\n" "payload:"   "$(ls -1 "$BOOT_MP/beatify" 2>/dev/null | tr '\n' ' ')"
