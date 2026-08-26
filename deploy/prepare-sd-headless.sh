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

# Parsed rather than sourced. Sourcing would make the file shell code, and an
# unquoted `SSID=iPhone André` then means "run the command André" — the variable
# silently ends up empty. Wi-Fi names contain spaces, accents and apostrophes as
# a matter of course, so the format has to tolerate them without the person
# editing the file having to know any shell quoting rules.
read_setting() {
    # $1 = key. Prints the value verbatim: everything after the first '=', with
    # one optional layer of surrounding quotes removed. Only whole-line comments
    # are stripped, so a '#' inside a password survives.
    sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" "$CRED_FILE" \
        | head -1 \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

SSID=$(read_setting SSID)
PASSWORD=$(read_setting PASSWORD)
COUNTRY=$(read_setting COUNTRY); COUNTRY=${COUNTRY:-DE}
HOTSPOT_SSID=$(read_setting HOTSPOT_SSID)
HOTSPOT_PASSWORD=$(read_setting HOTSPOT_PASSWORD)

[ -n "$SSID" ]     || die "SSID is empty in $CRED_FILE"
[ -n "$PASSWORD" ] || die "PASSWORD is empty in $CRED_FILE"
# Half-filled is a typo, not an intention: fail rather than quietly skip it.
if [ -n "$HOTSPOT_SSID" ] && [ -z "$HOTSPOT_PASSWORD" ]; then
    die "HOTSPOT_SSID is set but HOTSPOT_PASSWORD is empty in $CRED_FILE"
fi
if [ -z "$HOTSPOT_SSID" ] && [ -n "$HOTSPOT_PASSWORD" ]; then
    die "HOTSPOT_PASSWORD is set but HOTSPOT_SSID is empty in $CRED_FILE"
fi

# batocera.conf's own template says to escape these three in the Wi-Fi key.
escape_conf() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/\([#;$]\)/\\\1/g'; }
SSID_ESC=$(escape_conf "$SSID")
PASSWORD_ESC=$(escape_conf "$PASSWORD")
HOTSPOT_SSID_ESC=$(escape_conf "$HOTSPOT_SSID")
HOTSPOT_PASSWORD_ESC=$(escape_conf "$HOTSPOT_PASSWORD")

# BEATIFY_EMIT_DIR writes the same files into a directory instead of onto a
# card, so an already-running box can be updated over ssh without being opened
# up and having its SD card pulled.
if [ -n "${BEATIFY_EMIT_DIR:-}" ]; then
    mkdir -p "$BEATIFY_EMIT_DIR"
    BOOT_MP="$BEATIFY_EMIT_DIR"
    EMIT_ONLY=1
    log "emit mode: writing into $BOOT_MP (no card touched)"
else
    EMIT_ONLY=0
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
fi

# --- early-boot Wi-Fi -------------------------------------------------------
BOOTCONF="$BOOT_MP/batocera-boot.conf"
if [ "$EMIT_ONLY" = "1" ] && [ ! -f "$BOOTCONF" ]; then
    # Nothing to patch in emit mode; the running box already has its own.
    log "no batocera-boot.conf here — emitting postshare.sh only"
    BOOTCONF=/dev/null
fi
[ "$BOOTCONF" = /dev/null ] || [ -f "$BOOTCONF" ] || die "batocera-boot.conf missing — is this really a Batocera card?"

# Idempotent: drop any previous block of ours before appending a new one.
if [ "$BOOTCONF" != /dev/null ] && grep -qF "$MARKER" "$BOOTCONF"; then
    log "replacing previous beatify block in batocera-boot.conf"
    sed -i "/$MARKER/,/$MARKER end/d" "$BOOTCONF"
fi

[ "$BOOTCONF" = /dev/null ] || log "adding Wi-Fi to batocera-boot.conf (used on the first boot only)"
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

if [ -n "$HOTSPOT_SSID" ] && [ "$BOOTCONF" != /dev/null ]; then
    # S08connman writes a connman profile with Autoconnect=true for wifi,
    # wifi2, wifi3 and wifi.hidden alike, so a second network is simply joined
    # whenever it is the one in range. That is what lets the box follow a phone
    # hotspot anywhere without a screen or keyboard.
    log "adding the phone hotspot as a second network"
    sed -i "/$MARKER end/i wifi2.ssid=${HOTSPOT_SSID_ESC}\nwifi2.key=${HOTSPOT_PASSWORD_ESC}" "$BOOTCONF"
fi

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
           -e '/^#*wifi2\.ssid=/d'   -e '/^#*wifi2\.key=/d' \\
           -e '/^#*system\.power\.switch=/d' "\$CONF" 2>/dev/null || true
    {
        echo ""
        echo "### beatify-headless ###"
        echo "wifi.enabled=1"
        echo "wifi.country=${COUNTRY}"
        echo "wifi.ssid=${SSID_ESC}"
        echo "wifi.key=${PASSWORD_ESC}"
        [ -n "${HOTSPOT_SSID_ESC}" ] && echo "wifi2.ssid=${HOTSPOT_SSID_ESC}"
        [ -n "${HOTSPOT_SSID_ESC}" ] && echo "wifi2.key=${HOTSPOT_PASSWORD_ESC}"
        echo "system.power.switch=RETROFLAG"
    } >> "\$CONF"
    say "batocera.conf seeded with wifi + retroflag power switch"
fi

# --- 3. one-shot Wi-Fi from the boot partition ---
# The rescue path for a network nobody planned for. /boot is vfat, so the file
# can be written from any laptop -- Windows and macOS included -- with no screen
# or keyboard attached to the Pi. Applied once and then renamed, so it never
# fights a change made later in the Batocera UI.
NEWNET=/boot/beatify-wifi.conf
if [ -f "\$NEWNET" ]; then
    NEW_SSID=""; NEW_PASSWORD=""; NEW_SLOT="3"
    . "\$NEWNET" 2>/dev/null || true
    if [ -n "\$NEW_SSID" ]; then
        case "\$NEW_SLOT" in 1) P="wifi" ;; *) P="wifi\$NEW_SLOT" ;; esac
        mount -o remount,rw /boot 2>/dev/null || true
        sed -i -e "/^\$P\.ssid=/d" -e "/^\$P\.key=/d" "\$CONF" 2>/dev/null || true
        printf '%s.ssid=%s\n%s.key=%s\n' "\$P" "\$NEW_SSID" "\$P" "\$NEW_PASSWORD" >> "\$CONF"
        mv "\$NEWNET" "\$NEWNET.applied" 2>/dev/null || rm -f "\$NEWNET"
        mount -o remount,ro /boot 2>/dev/null || true
        say "applied one-shot network '\$NEW_SSID' to \$P (rebooting to connect)"
        sync
        reboot
    fi
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
