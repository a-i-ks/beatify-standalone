#!/bin/bash
# Sustained load test for a USB-attached disk, for validating a UAS quirk.
#
#   deploy/ssd-stress.sh [seconds] [device] [scratch-dir]
#
# Exercises BOTH paths on purpose. The NESPi 4's JMS567 bridge failed with
# `uas_eh_abort_handler` on `opcode=0x28` — READ(10) — so a write-only test
# proves nothing about the failure that was actually observed.
#
# Two mistakes this script exists to avoid making again:
#   * `iflag=count_bytes` makes `count` a BYTE count. `bs=4M count=1024` with it
#     reads 1024 bytes, not 4 GB — a read test that silently tests nothing.
#   * Reading back a file just written measures the page cache, not the disk.
#     Caches are dropped before every verify read.

set -u

DURATION="${1:-600}"
DEVICE="${2:-/dev/sda}"
SCRATCH_DIR="${3:-}"
LOG=/tmp/ssd-stress.log
SCRATCH=""

[ -b "$DEVICE" ] || { echo "not a block device: $DEVICE" >&2; exit 1; }
if [ -n "$SCRATCH_DIR" ]; then
    SCRATCH="$SCRATCH_DIR/.ssd-stress.tmp"
    mkdir -p "$SCRATCH_DIR" || exit 1
fi
cleanup() { [ -n "$SCRATCH" ] && rm -f "$SCRATCH"; }
trap cleanup EXIT INT TERM

DEV_BYTES=$(blockdev --getsize64 "$DEVICE" 2>/dev/null || echo 0)
READ_CHUNK_MB=512
END=$(( $(date +%s) + DURATION ))
ROUND=0
: > "$LOG"
echo "start $(date -Is) device=$DEVICE size=${DEV_BYTES}B duration=${DURATION}s" >> "$LOG"

while [ "$(date +%s)" -lt "$END" ]; do
    ROUND=$((ROUND + 1))

    # 1. Real sequential read straight off the raw device, from a different
    #    offset each round so the drive cannot serve it from its own cache.
    if [ "$DEV_BYTES" -gt 0 ]; then
        MAX_SKIP=$(( DEV_BYTES / 1048576 - READ_CHUNK_MB - 1 ))
        [ "$MAX_SKIP" -lt 1 ] && MAX_SKIP=1
        SKIP=$(( (ROUND * 4096) % MAX_SKIP ))
        dd if="$DEVICE" of=/dev/null bs=1M count="$READ_CHUNK_MB" skip="$SKIP" \
           2>>"$LOG" || echo "READ FAIL round $ROUND" >> "$LOG"
    fi

    # 2. Write + flush, the path ext4lazyinit was on when the drive first died.
    if [ -n "$SCRATCH" ]; then
        dd if=/dev/zero of="$SCRATCH" bs=1M count=512 conv=fsync \
           2>>"$LOG" || echo "WRITE FAIL round $ROUND" >> "$LOG"
        # 3. Verify read from the platter, not from RAM.
        sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
        dd if="$SCRATCH" of=/dev/null bs=1M \
           2>>"$LOG" || echo "VERIFY FAIL round $ROUND" >> "$LOG"
    fi

    echo "round $ROUND ok $(date -Is)" >> "$LOG"
done

echo "done $(date -Is) rounds=$ROUND" >> "$LOG"
awk '/bytes.*copied/ {gsub(/,/,"",$1); total += $1} END {
    printf "transferred %.1f GB\n", total/1e9
}' "$LOG" >> "$LOG"
tail -2 "$LOG"
