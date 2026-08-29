#!/bin/sh
# One controlled pairing attempt with an Xbox controller, start to finish,
# leaving evidence behind instead of impressions.
#
# It answers the four questions that actually matter, in order:
#   1. does the pad show up in a scan?
#   2. does a driver bind, or is it only a Bluetooth link?
#   3. does a human pressing buttons produce events?  (not the open-time burst)
#   4. if the link drops, what reason does the controller give?
#
# Question 4 is the one no previous attempt captured, and it is the one that
# separates a radio problem from a controller that decides to leave.
#
# Runs on the Pi and on any Linux box with bluez — use a second host to find
# out whether the pad or the adapter is at fault.
#
#   sh deploy/bt-controller-probe.sh                 # full attempt
#   sh deploy/bt-controller-probe.sh --mac AA:BB:..  # skip the search
#   sh deploy/bt-controller-probe.sh --forget        # drop pairings and exit
set -eu

SCAN_WINDOW=90      # seconds to keep looking for the pad
PRESS_WINDOW=12     # seconds to measure real input
HOLD_WINDOW=120     # seconds to watch whether the link survives
MAC=""
FORGET_ONLY=0
OUT="${BT_PROBE_OUT:-/tmp/bt-probe}"

while [ $# -gt 0 ]; do
    case "$1" in
        --mac) MAC="$2"; shift 2 ;;
        --scan-window) SCAN_WINDOW="$2"; shift 2 ;;
        --press-window) PRESS_WINDOW="$2"; shift 2 ;;
        --hold-window) HOLD_WINDOW="$2"; shift 2 ;;
        --forget) FORGET_ONLY=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "must run as root (pairing and HCI tracing need it)" >&2; exit 1; }
command -v bluetoothctl >/dev/null || { echo "bluetoothctl not found" >&2; exit 1; }

mkdir -p "$OUT"
TRACE="$OUT/hci.log"
SCAN_LOG="$OUT/scan.log"
DMESG_MARK="$OUT/dmesg.mark"

say() { echo "== $*"; }
note() { echo "   $*"; }

# ---------------------------------------------------------------- reason codes
# The Bluetooth spec's disconnect reasons, narrowed to the ones that come up
# with gamepads. Each points somewhere different, which is the whole point.
reason_meaning() {
    case "$1" in
        0x08) echo "supervision timeout — the link went quiet: radio, range or connection interval" ;;
        0x13) echo "remote user ended it — the controller chose to leave (idle, or its own firmware)" ;;
        0x14) echo "remote device low on resources" ;;
        0x15) echo "remote device powering off — the pad went to standby" ;;
        0x16) echo "local host ended it — something on this box dropped the link" ;;
        0x22) echo "LMP response timeout — the controller stopped answering: radio or firmware" ;;
        0x28) echo "instant passed — timing negotiation failed" ;;
        0x3b) echo "unacceptable connection parameters — the pad rejected our intervals" ;;
        0x3e) echo "connection failed to be established — never really came up" ;;
        *)    echo "see the Bluetooth core spec, Vol 1 Part F" ;;
    esac
}

# --------------------------------------------------------------------- tracing
# btmon decodes properly; hcidump is what Batocera ships. Either beats nothing.
TRACER=""
if command -v btmon >/dev/null; then
    TRACER="btmon"
elif command -v hcidump >/dev/null; then
    TRACER="hcidump"
fi

TRACE_PID=""
start_trace() {
    [ -n "$TRACER" ] || { note "no btmon or hcidump — running without HCI evidence"; return; }
    : > "$TRACE"
    "$TRACER" -t >> "$TRACE" 2>&1 &
    TRACE_PID=$!
    note "tracing HCI with $TRACER into $TRACE"
}

stop_trace() {
    [ -n "$TRACE_PID" ] || return 0
    kill "$TRACE_PID" 2>/dev/null || true
    wait "$TRACE_PID" 2>/dev/null || true
    TRACE_PID=""
}

SCAN_PID=""
cleanup() {
    stop_trace
    [ -n "$SCAN_PID" ] && kill "$SCAN_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ------------------------------------------------------------------- pairings
forget_pads() {
    bluetoothctl devices 2>/dev/null \
        | awk '/[Xx]box|bluez-hog/ {print $2}' \
        | while read -r old; do
            note "removing stale pairing $old"
            bluetoothctl remove "$old" >/dev/null 2>&1 || true
        done
}

if [ "$FORGET_ONLY" = 1 ]; then
    say "forgetting every Xbox pairing"
    forget_pads
    say "done"
    exit 0
fi

# ============================================================================
say "0. starting clean"
bluetoothctl power on >/dev/null 2>&1 || true
# A half-finished pairing from a previous attempt makes bluez retry on the
# wrong transport forever. Always start from nothing.
[ -n "$MAC" ] || forget_pads
dmesg | wc -l > "$DMESG_MARK"
start_trace

# ============================================================================
if [ -z "$MAC" ]; then
    say "1. searching — put the pad in pairing mode (hold the small button on top"
    note "   until the Xbox button flashes *fast*), then leave it alone"
    # Scanning stops the moment the client exits, and bluetoothctl exits as soon
    # as it has read its input. Hold stdin open for the whole window, or the
    # search silently is not searching.
    ( echo "scan on"; sleep "$SCAN_WINDOW" ) | bluetoothctl > "$SCAN_LOG" 2>&1 &
    SCAN_PID=$!

    waited=0
    while [ "$waited" -lt "$SCAN_WINDOW" ]; do
        MAC=$(bluetoothctl devices 2>/dev/null | awk '/[Xx]box/ {print $2; exit}')
        [ -n "$MAC" ] && break
        sleep 2
        waited=$((waited + 2))
        [ $((waited % 10)) = 0 ] && note "still looking (${waited}s)"
    done

    if [ -z "$MAC" ]; then
        say "FAIL: nothing calling itself an Xbox controller appeared in ${SCAN_WINDOW}s"
        note "if the pad stopped flashing fast, its pairing window closed — start it again"
        exit 1
    fi
fi
say "found $MAC"

# ============================================================================
say "2. pairing"
bluetoothctl --timeout 25 pair "$MAC" 2>&1 | sed 's/^/   /' || true
bluetoothctl trust "$MAC" >/dev/null 2>&1 || true
bluetoothctl --timeout 25 connect "$MAC" 2>&1 | sed 's/^/   /' || true

connected=$(bluetoothctl info "$MAC" 2>/dev/null | awk '/Connected:/ {print $2}')
note "bluez says Connected: ${connected:-unknown}"
note "(which proves only that a link exists — not that anything is driving it)"

# ============================================================================
say "3. waiting for a driver to bind"
JS=""
waited=0
while [ "$waited" -lt 20 ]; do
    for candidate in /dev/input/js0 /dev/input/js1 /dev/input/js2; do
        [ -e "$candidate" ] && JS="$candidate"
    done
    [ -n "$JS" ] && break
    sleep 1
    waited=$((waited + 1))
done

if [ -d /sys/bus/hid/drivers/xpadneo ] && [ -n "$(ls -A /sys/bus/hid/drivers/xpadneo 2>/dev/null)" ]; then
    note "xpadneo has a device bound: $(ls /sys/bus/hid/drivers/xpadneo | tr '\n' ' ')"
else
    note "xpadneo has nothing bound"
fi

if [ -z "$JS" ]; then
    say "FAIL: no joystick device appeared — a Bluetooth link, but no input device"
    stop_trace
    tail -n +"$(( $(cat "$DMESG_MARK") + 1 ))" /dev/null 2>/dev/null || true
    dmesg | tail -20 | sed 's/^/   /'
    exit 1
fi
note "input device: $JS"

# ============================================================================
say "4. measuring real input for ${PRESS_WINDOW}s"
note "PRESS BUTTONS AND MOVE BOTH STICKS NOW"
PY=""
for p in /userdata/beatify/python/bin/python3 python3; do
    command -v "$p" >/dev/null 2>&1 && { PY="$p"; break; }
done
HERE=$(dirname "$0")
INPUT_OK=1
if [ -n "$PY" ] && [ -f "$HERE/../tools/js_probe.py" ]; then
    "$PY" "$HERE/../tools/js_probe.py" "$JS" --seconds "$PRESS_WINDOW" --quiet \
        | sed 's/^/   /' && INPUT_OK=0 || INPUT_OK=1
else
    note "js_probe.py not reachable — falling back to a raw byte count"
    note "(remember: the first 152 bytes are the open-time burst, not input)"
    timeout "$PRESS_WINDOW" cat "$JS" | wc -c | sed 's/^/   bytes: /'
fi

# ============================================================================
say "5. watching the link for up to ${HOLD_WINDOW}s"
start=$(date +%s)
dropped=0
while [ $(( $(date +%s) - start )) -lt "$HOLD_WINDOW" ]; do
    state=$(bluetoothctl info "$MAC" 2>/dev/null | awk '/Connected:/ {print $2}')
    if [ "$state" != "yes" ]; then
        dropped=$(( $(date +%s) - start ))
        break
    fi
    sleep 2
done
stop_trace

# ============================================================================
say "verdict"
if [ "$dropped" -gt 0 ]; then
    note "link dropped after ${dropped}s"
else
    note "link survived the full ${HOLD_WINDOW}s"
fi

if [ "$INPUT_OK" = 0 ]; then
    note "input: real events arrived"
else
    note "input: nothing but the open-time burst"
fi

if [ -s "$TRACE" ]; then
    code=$(grep -iE "reason" "$TRACE" | grep -oiE "0x[0-9a-f]{2}" | tail -1 || true)
    if [ -n "$code" ]; then
        note "last disconnect reason $code: $(reason_meaning "$(echo "$code" | tr 'A-F' 'a-f')")"
    else
        note "no disconnect event in the trace"
    fi
    note "full trace: $TRACE"
fi

say "kernel messages since the attempt started"
dmesg | tail -n "$(( $(dmesg | wc -l) - $(cat "$DMESG_MARK") ))" 2>/dev/null \
    | grep -iE "xpadneo|bluetooth|hid|input" | tail -25 | sed 's/^/   /' || true
