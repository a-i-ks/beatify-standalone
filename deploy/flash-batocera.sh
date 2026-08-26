#!/usr/bin/env bash
# Write a Batocera image to an SD card, with the guards a bare `dd` does not have.
#
#   sudo deploy/flash-batocera.sh <image.img.gz> <device>
#
# Refuses to run unless the target is a removable, non-system disk, shows you
# exactly what is about to be destroyed, and makes you type the device name to
# confirm. A mistyped `of=` here costs a system disk.

set -euo pipefail

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 2 ] || die "usage: $0 <image.img.gz> <device>   (e.g. /dev/disk/by-id/usb-…)"
IMAGE="$1"
TARGET="$2"

[ "$(id -u)" -eq 0 ] || die "must run as root (writing to a raw block device)"
[ -f "$IMAGE" ] || die "image not found: $IMAGE"

# Resolve by-id symlinks to the real node so the checks below see the truth.
DEVICE="$(readlink -f "$TARGET")"
[ -b "$DEVICE" ] || die "not a block device: $TARGET"

NAME="$(basename "$DEVICE")"
[[ "$NAME" =~ [0-9]$ && "$NAME" =~ ^(sd|nvme|mmcblk) ]] && die "$DEVICE looks like a partition — pass the whole disk"

read -r REMOVABLE < "/sys/block/$NAME/removable" 2>/dev/null || REMOVABLE=0
[ "$REMOVABLE" = "1" ] || die "$DEVICE is not removable. Refusing — this guard exists to protect your system disks."

# Anything mounted from this disk means it is in use, possibly by the OS.
if lsblk -nro MOUNTPOINT "$DEVICE" | grep -q .; then
    echo "Mounted partitions found on $DEVICE:"
    lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEVICE"
    die "unmount them first (udisksctl unmount -b /dev/…)"
fi

echo
echo "About to COMPLETELY OVERWRITE this device:"
echo
lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MODEL "$DEVICE"
echo
echo "  image:  $IMAGE  ($(du -h "$IMAGE" | cut -f1))"
echo "  target: $DEVICE"
echo
echo "Everything on it will be lost and cannot be recovered."
printf 'Type the device name (%s) to confirm: ' "$NAME"
read -r CONFIRM
[ "$CONFIRM" = "$NAME" ] || die "confirmation did not match — nothing was written"

echo
echo "==> Verifying the archive before touching the card"
# gzip carries a CRC32 of the payload, so this catches a truncated or corrupted
# download. It says nothing about authenticity — Batocera publishes no checksums.
gzip -t "$IMAGE" || die "archive is corrupt — re-download it"

echo "==> Writing"
gunzip -c "$IMAGE" | dd of="$DEVICE" bs=4M status=progress conv=fsync
sync

echo "==> Re-reading the partition table"
partprobe "$DEVICE" 2>/dev/null || blockdev --rereadpt "$DEVICE" 2>/dev/null || true
sleep 2
lsblk -o NAME,SIZE,FSTYPE,LABEL "$DEVICE"

echo
echo "Done. Eject with:  udisksctl power-off -b $DEVICE"
