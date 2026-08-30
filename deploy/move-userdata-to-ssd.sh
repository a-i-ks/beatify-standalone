#!/bin/bash
# Move /userdata off the SD card onto an external disk. Runs ON the box, as root:
#
#   bin/pi --copy deploy/move-userdata-to-ssd.sh /tmp/move.sh
#   bin/pi 'bash /tmp/move.sh /dev/sda --yes'
#
# Why: /boot is mounted read-only, so once /userdata is elsewhere the SD card is
# written to essentially never. All the churn — logs, saves, gamelists, scraped
# media, Spotify tokens — lives in /userdata, and cards die of writes.
#
# The whole disk is repartitioned into one Batocera volume. Anything already on
# it is destroyed, which is the point: a disk carrying another distribution's
# leftover partitions keeps offering them to Batocera's STORAGE DEVICE menu.
#
# Batocera's own mechanics, read out of /etc/init.d/S11share rather than assumed:
#
#   * `sharedevice=DEV <fsuuid>` selects the partition, by filesystem UUID.
#   * That partition is mounted at /var/batocerafs and its **`batocera/`
#     subdirectory** is bind-mounted onto /userdata. Data copied to the
#     partition root would simply be ignored.
#   * If the mount fails, S11share falls back to the internal SD partition and
#     then to a tmpfs. A dead disk therefore degrades to stale data rather than
#     to no boot — worth knowing, and worth detecting afterwards (postshare.sh).
#   * `sharewait=` is a budget in seconds for the disk to appear, spent in
#     4-second tries. USB disks need more than the default 15.
#
# Nothing is deleted from the SD card: it keeps a complete copy, so rolling back
# is one line — sharedevice=INTERNAL in /boot/batocera-boot.conf.

set -euo pipefail

DISK=${1:-}
CONFIRM=${2:-}
SHAREWAIT=${SHAREWAIT:-60}
LABEL=${LABEL:-BEATIFY}

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*"; }

[ -n "$DISK" ] || die "usage: $0 /dev/sdX --yes   (the WHOLE DISK to take over)"
[ "$(id -u)" = 0 ] || die "must run as root, on the box"
[ -b "$DISK" ] || die "$DISK is not a block device"

# --- refuse to eat anything we depend on ------------------------------------
case "$DISK" in
    /dev/mmcblk*) die "$DISK is the SD card — that is what we are moving away from" ;;
    *[0-9])       die "$DISK looks like a partition; pass the whole disk (e.g. /dev/sda)" ;;
esac
BOOT_SRC=$(awk '$2=="/boot"{print $1; exit}' /proc/mounts)
case "$BOOT_SRC" in
    "$DISK"*) die "$DISK carries /boot ($BOOT_SRC) — refusing to repartition what we boot from" ;;
esac
USERDATA_SRC=$(awk '$2=="/userdata"{print $1; exit}' /proc/mounts)
case "$USERDATA_SRC" in
    "$DISK"*) die "$DISK already carries /userdata ($USERDATA_SRC); nothing to do" ;;
esac

USED_KB=$(df -k --output=used /userdata | tail -1 | tr -d ' ')
SIZE_KB=$(( $(blockdev --getsize64 "$DISK") / 1024 ))
[ "$SIZE_KB" -gt $(( USED_KB * 2 )) ] \
    || die "$DISK holds ${SIZE_KB}K, /userdata uses ${USED_KB}K — want at least double for headroom"

# --- say exactly what is about to be destroyed ------------------------------
echo
log "source /userdata: $USERDATA_SRC  ($(( USED_KB / 1024 )) MB in use)"
log "target disk:      $DISK  ($(( SIZE_KB / 1024 / 1024 )) GB) — EVERYTHING ON IT WILL BE ERASED:"
echo
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS "$DISK" | sed 's/^/    /'
echo
[ "$CONFIRM" = "--yes" ] || die "refusing without --yes as the second argument"

# --- quiesce ----------------------------------------------------------------
# A copy taken while Beatify writes its log and ES rewrites gamelists is a copy
# of a torn state. Stop them; the reboot at the end brings them back.
log "stopping Beatify and EmulationStation so nothing writes mid-copy"
batocera-services stop beatify >/dev/null 2>&1 || true
# The init script's own stop, not a kill: it asks ES to shut down, takes the
# compositor with it, and does not respawn. A bare pkill leaves the launcher to
# start ES straight back up, which would then write gamelists and es_log into
# the tree being copied and fail the verification pass for no good reason.
/etc/init.d/S31emulationstation stop >/dev/null 2>&1 || pkill -f '[e]mulationstation' || true
sleep 5
sync

# --- one clean partition ----------------------------------------------------
log "clearing every partition table on $DISK and creating a single volume"
for part in "$DISK"?*; do umount "$part" 2>/dev/null || true; done
sgdisk --zap-all "$DISK" >/dev/null          # removes GPT *and* the legacy MBR
sgdisk -n 1:0:0 -t 1:8300 -c 1:"$LABEL" "$DISK" >/dev/null
partprobe "$DISK"
sleep 2

PART="${DISK}1"
[ -b "$PART" ] || die "$PART did not appear after partitioning"

# The JMS567 bridge corrupted data on this disk before its quirk was applied
# (HARDWARE-FINDINGS section 1), and the filesystem it carried predates that
# fix. A new one costs seconds and removes the entire question.
log "creating a fresh ext4 on $PART"
mkfs.ext4 -F -L "$LABEL" -m 0 "$PART" >/dev/null
UUID=$(blkid -s UUID -o value "$PART")
[ -n "$UUID" ] || die "could not read the new filesystem's UUID"
log "new filesystem: $PART  label=$LABEL  uuid=$UUID"

SCRATCH=$(mktemp -d)
cleanup() { umount "$SCRATCH" 2>/dev/null || true; rmdir "$SCRATCH" 2>/dev/null || true; }
trap cleanup EXIT

mount "$PART" "$SCRATCH"
mkdir -p "$SCRATCH/batocera"   # S11share bind-mounts this, not the partition root

# --- copy -------------------------------------------------------------------
log "copying /userdata (hardlinks, ACLs, xattrs and sparseness preserved)"
rsync -aHAX --numeric-ids /userdata/ "$SCRATCH/batocera/"
sync

# --- verify -----------------------------------------------------------------
# rsync's checksum pass re-reads both trees byte for byte and itemises anything
# that still differs. An empty report is the proof, not an assumption.
log "verifying every file by checksum (re-reads both copies)"
DIFF=$(rsync -aHAXn --checksum --itemize-changes /userdata/ "$SCRATCH/batocera/" | grep -v '^\.' || true)
if [ -n "$DIFF" ]; then
    printf '%s\n' "$DIFF" | head -20
    die "the copy does not match the source — nothing switched over, /userdata untouched"
fi
SRC_FILES=$(find /userdata -xdev -type f | wc -l)
DST_FILES=$(find "$SCRATCH/batocera" -xdev -type f | wc -l)
[ "$SRC_FILES" = "$DST_FILES" ] || die "file count differs: $SRC_FILES source, $DST_FILES target"
log "verified: $SRC_FILES files identical by checksum"

umount "$SCRATCH"

# --- switch over ------------------------------------------------------------
mount -o remount,rw /boot
CONF=/boot/batocera-boot.conf

sed -i "s|^sharedevice=.*|sharedevice=DEV $UUID|" "$CONF"
grep -q '^sharedevice=' "$CONF" || echo "sharedevice=DEV $UUID" >> "$CONF"

# A USB disk enumerates well after the SD card does. Without a budget S11share
# gives up and silently falls back to the card, so the box boots on stale data
# and looks perfectly fine — the worst kind of wrong.
sed -i -e '/^#*sharewait=/d' "$CONF"
sed -i "/^sharedevice=/a sharewait=$SHAREWAIT" "$CONF"

# What the boot hooks compare reality against. Absent, they fall back to
# "keep /userdata on the card", which is right for a fresh build with no disk.
cat > /boot/beatify-storage.conf <<EOF
# Where /userdata is supposed to live. Written by move-userdata-to-ssd.sh.
# preshare.sh restores sharedevice= to this before the share is mounted;
# postshare.sh then checks that this is really what got mounted.
# Delete this file to go back to keeping /userdata on the SD card.
userdata_uuid=$UUID
EOF

rm -f /boot/beatify-storage-guard
sync
mount -o remount,ro /boot

echo
log "switched over:"
grep -E '^(sharedevice|sharewait)=' "$CONF" | sed 's/^/    /'
echo
log "the SD card still holds the complete original, untouched."
log "roll back by putting sharedevice=INTERNAL in /boot/batocera-boot.conf"
log "and deleting /boot/beatify-storage.conf. Reboot to come up on $PART."
