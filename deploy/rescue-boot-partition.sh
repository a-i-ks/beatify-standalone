#!/usr/bin/env bash
# Repair the box's boot partition from a desktop, with the SD card in a reader.
#
#   deploy/rescue-boot-partition.sh
#
# For when the Pi will not come up far enough to be reached over ssh. Only the
# vfat BATOCERA partition is touched, so no root is needed and the card can come
# from a box that is otherwise completely unresponsive.
#
# What it fixes, in order of how likely it is to be the problem:
#
#   1. /boot/preshare.sh — anything here runs inside S11share, *while init is
#      still waiting for it*. A reboot from there deadlocks the boot before the
#      network exists, which looks exactly like a dead box. Always removed.
#   2. sharedevice= — set back to whatever /boot/beatify-storage.conf says the
#      userdata filesystem is meant to be, so the box comes up on its real data.
#   3. the storage-guard marker — cleared, so the guard is armed again.

set -euo pipefail

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*"; }

BOOT_DEV=$(lsblk -nrpo NAME,LABEL | awk '$2=="BATOCERA"{print $1; exit}')
[ -n "$BOOT_DEV" ] || die "no partition labelled BATOCERA found — is the card in the reader?"

BOOT_MP=$(findmnt -n -o TARGET "$BOOT_DEV" 2>/dev/null || true)
if [ -z "$BOOT_MP" ]; then
    udisksctl mount -b "$BOOT_DEV" >/dev/null
    BOOT_MP=$(findmnt -n -o TARGET "$BOOT_DEV")
fi
[ -w "$BOOT_MP" ] || die "$BOOT_MP is not writable"
log "BATOCERA partition at $BOOT_MP"

trap 'sync; udisksctl unmount -b "$BOOT_DEV" >/dev/null 2>&1 || true' EXIT

# --- 1. the hook that cannot safely exist ----------------------------------
if [ -f "$BOOT_MP/preshare.sh" ]; then
    rm -f "$BOOT_MP/preshare.sh"
    log "removed preshare.sh — a reboot from there hangs the boot before networking"
else
    log "no preshare.sh present"
fi

# --- 2. point userdata back at the right filesystem ------------------------
CONF="$BOOT_MP/batocera-boot.conf"
[ -f "$CONF" ] || die "batocera-boot.conf missing — is this really a Batocera card?"

WANT_UUID=$(sed -n 's/^userdata_uuid=//p' "$BOOT_MP/beatify-storage.conf" 2>/dev/null | head -1)
if [ -n "$WANT_UUID" ]; then
    WANT="DEV $WANT_UUID"
else
    WANT="INTERNAL"
fi

HAVE=$(sed -n 's/^[ ]*sharedevice=//p' "$CONF" | head -1)
if [ "$HAVE" = "$WANT" ]; then
    log "sharedevice is already '$WANT'"
else
    sed -i "s|^[ ]*sharedevice=.*|sharedevice=$WANT|" "$CONF"
    log "sharedevice: '$HAVE' -> '$WANT'"
fi

# --- 3. re-arm the guard ----------------------------------------------------
if [ -f "$BOOT_MP/beatify-storage-guard" ]; then
    rm -f "$BOOT_MP/beatify-storage-guard"
    log "cleared the storage-guard marker"
fi

sync
echo
log "card repaired. Put it back in the Pi and power it on."
grep -E '^(sharedevice|sharewait)=' "$CONF" | sed 's/^/    /'
