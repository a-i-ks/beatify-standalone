#!/usr/bin/env bash
# Repair the box's boot partition from a desktop, with the SD card in a reader.
#
#   deploy/rescue-boot-partition.sh
#
# For when the Pi will not come up far enough to be reached over ssh. Only the
# vfat BATOCERA partition is touched, so no root is needed and the card can come
# from a box that is otherwise completely unresponsive.
#
# The goal is a box that boots, not a box with its preferences intact. Those are
# different jobs, and confusing them costs another round trip with a screwdriver.
# So this aims at the most conservative configuration that is known to work, and
# leaves restoring the intended one to a machine you can already log into.
#
#   1. /boot/preshare.sh is removed. Anything there runs inside S11share, *while
#      init is still waiting for it*, and S11share has already read sharedevice=
#      by then — so it cannot redirect the mount it precedes, and rebooting from
#      it interferes with a boot that is only half done.
#   2. sharedevice= goes to INTERNAL: the SD card, which carries a complete
#      install by construction and needs no external disk to enumerate.
#   3. /boot/beatify-storage.conf is moved aside to .pending rather than
#      deleted, so the intended external target is not lost — postshare.sh then
#      has nothing to correct, and the box boots on the card without a fight.
#      Point it back at the disk from a running box, where a wrong guess costs
#      an ssh command instead of a disassembly.
#   4. the storage-guard marker is cleared, so the guard starts armed again.

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

# --- 2. boot from the card, which needs nothing else to show up -------------
CONF="$BOOT_MP/batocera-boot.conf"
[ -f "$CONF" ] || die "batocera-boot.conf missing — is this really a Batocera card?"

HAVE=$(sed -n 's/^[ ]*sharedevice=//p' "$CONF" | head -1)
if [ "$HAVE" = "INTERNAL" ]; then
    log "sharedevice is already INTERNAL"
else
    sed -i "s|^[ ]*sharedevice=.*|sharedevice=INTERNAL|" "$CONF"
    log "sharedevice: '$HAVE' -> 'INTERNAL' (the card's own SHARE partition)"
fi

# --- 3. keep the intended target, but stop it fighting the boot -------------
if [ -f "$BOOT_MP/beatify-storage.conf" ]; then
    mv -f "$BOOT_MP/beatify-storage.conf" "$BOOT_MP/beatify-storage.conf.pending"
    log "moved beatify-storage.conf aside to .pending — postshare now has nothing to correct"
    log "  (its target is preserved; re-apply it over ssh once the box is up)"
fi

# --- 4. re-arm the guard ----------------------------------------------------
if [ -f "$BOOT_MP/beatify-storage-guard" ]; then
    rm -f "$BOOT_MP/beatify-storage-guard"
    log "cleared the storage-guard marker"
fi

sync
echo
log "card repaired. Put it back in the Pi and power it on."
grep -E '^(sharedevice|sharewait)=' "$CONF" | sed 's/^/    /'
[ -f "$BOOT_MP/beatify-storage.conf.pending" ] && \
    sed -n 's/^userdata_uuid=/    pending userdata target: /p' "$BOOT_MP/beatify-storage.conf.pending"
