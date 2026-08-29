"""Tell real gamepad input apart from the burst a joystick device emits on open.

The joystick API replays the current value of every axis and every button the
moment the device is opened, flagged as ``JS_EVENT_INIT``. An Xbox pad produces
19 of those, exactly 152 bytes. Read the device, count the bytes, and it looks
indistinguishable from someone playing — which is how this project once
concluded a dead controller was working. Only events *without* the flag mean a
human touched something.

Run it directly to watch a device for a while::

    python3 tools/js_probe.py /dev/input/js0 --seconds 10
"""

from __future__ import annotations

import argparse
import select
import struct
import sys
import time

# struct js_event { __u32 time; __s16 value; __u8 type; __u8 number; }
EVENT_SIZE = 8
_EVENT = struct.Struct("<IhBB")

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80


class Event:
    """One decoded joystick event."""

    __slots__ = ("timestamp", "value", "kind", "number", "synthetic")

    def __init__(self, timestamp: int, value: int, raw_type: int, number: int) -> None:
        self.timestamp = timestamp
        self.value = value
        self.kind = raw_type & ~JS_EVENT_INIT
        self.number = number
        self.synthetic = bool(raw_type & JS_EVENT_INIT)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        name = "button" if self.kind == JS_EVENT_BUTTON else "axis"
        tag = " (init)" if self.synthetic else ""
        return f"{name} {self.number} = {self.value}{tag}"


def decode(buffer: bytes) -> tuple[list[Event], bytes]:
    """Decode whole events out of ``buffer``, returning them and the remainder.

    A read can split an event across two chunks, so the tail is handed back to
    be prepended to the next read rather than discarded.
    """
    events = []
    offset = 0
    while len(buffer) - offset >= EVENT_SIZE:
        timestamp, value, raw_type, number = _EVENT.unpack_from(buffer, offset)
        events.append(Event(timestamp, value, raw_type, number))
        offset += EVENT_SIZE
    return events, buffer[offset:]


def summarise(events: list[Event]) -> dict[str, int]:
    """Count what arrived, keeping the synthetic burst strictly separate."""
    real = [event for event in events if not event.synthetic]
    return {
        "total": len(events),
        "synthetic": len(events) - len(real),
        "real": len(real),
        "real_buttons": sum(1 for event in real if event.kind == JS_EVENT_BUTTON),
        "real_axes": sum(1 for event in real if event.kind == JS_EVENT_AXIS),
    }


def watch(path: str, seconds: float, out=sys.stdout) -> dict[str, int]:
    """Read ``path`` for ``seconds`` and report what genuinely arrived."""
    events: list[Event] = []
    pending = b""
    deadline = time.monotonic() + seconds
    with open(path, "rb", buffering=0) as device:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([device], [], [], min(remaining, 0.5))
            if not ready:
                continue
            chunk = device.read(EVENT_SIZE * 64)
            if not chunk:
                # The device vanished mid-read: the link dropped.
                print("device disappeared while reading", file=out)
                break
            decoded, pending = decode(pending + chunk)
            events.extend(decoded)
    return summarise(events)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", nargs="?", default="/dev/input/js0")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument(
        "--quiet", action="store_true", help="print only the verdict line"
    )
    args = parser.parse_args(argv)

    if not args.quiet:
        print(f"reading {args.device} for {args.seconds:.0f}s — press buttons now")
    try:
        counts = watch(args.device, args.seconds)
    except FileNotFoundError:
        print(f"no such device: {args.device}")
        return 2
    except OSError as err:
        print(f"cannot read {args.device}: {err}")
        return 2

    print(
        f"events: {counts['total']} total, "
        f"{counts['synthetic']} synthetic (open-time burst), "
        f"{counts['real']} real "
        f"({counts['real_buttons']} button, {counts['real_axes']} axis)"
    )
    if counts["real"]:
        print("VERDICT: input works — real events arrived")
        return 0
    print("VERDICT: no input — only the open-time burst, the pad sent nothing")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
