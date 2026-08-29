"""The open-time burst must never be counted as input.

This is a regression test for a wrong conclusion, not for a crash: 152 bytes
out of `/dev/input/js0` were once read as proof that a controller worked, when
every one of those bytes was the kernel replaying the pad's resting state.
"""

from __future__ import annotations

import struct
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))

from js_probe import (  # noqa: E402
    JS_EVENT_AXIS,
    JS_EVENT_BUTTON,
    JS_EVENT_INIT,
    decode,
    summarise,
)

_EVENT = struct.Struct("<IhBB")


def _event(kind: int, number: int, value: int, *, init: bool = False) -> bytes:
    return _EVENT.pack(0, value, kind | (JS_EVENT_INIT if init else 0), number)


def _xbox_open_burst() -> bytes:
    """What an Xbox pad emits the instant the device is opened: 19 events."""
    burst = b"".join(_event(JS_EVENT_BUTTON, n, 0, init=True) for n in range(11))
    burst += b"".join(_event(JS_EVENT_AXIS, n, 0, init=True) for n in range(8))
    return burst


def test_the_open_time_burst_is_exactly_the_152_bytes_that_fooled_us() -> None:
    burst = _xbox_open_burst()
    assert len(burst) == 152

    events, rest = decode(burst)
    assert rest == b""
    counts = summarise(events)

    assert counts["total"] == 19
    assert counts["synthetic"] == 19
    assert counts["real"] == 0


def test_a_real_press_is_counted_even_amid_the_burst() -> None:
    stream = _xbox_open_burst() + _event(JS_EVENT_BUTTON, 0, 1)
    events, _ = decode(stream)
    counts = summarise(events)

    assert counts["real"] == 1
    assert counts["real_buttons"] == 1
    assert counts["real_axes"] == 0
    assert counts["synthetic"] == 19


def test_stick_movement_counts_as_axis_input() -> None:
    stream = _event(JS_EVENT_AXIS, 1, -32768) + _event(JS_EVENT_AXIS, 1, 0)
    counts = summarise(decode(stream)[0])

    assert counts["real_axes"] == 2
    assert counts["real_buttons"] == 0


def test_a_split_read_keeps_the_partial_event_for_next_time() -> None:
    stream = _event(JS_EVENT_BUTTON, 3, 1) + _event(JS_EVENT_BUTTON, 4, 1)
    head, tail = stream[:12], stream[12:]

    events, pending = decode(head)
    assert len(events) == 1
    assert pending == head[8:]

    events, pending = decode(pending + tail)
    assert len(events) == 1
    assert pending == b""


def test_the_init_flag_is_stripped_from_the_event_kind() -> None:
    (event,), _ = decode(_event(JS_EVENT_BUTTON, 0, 1, init=True))

    assert event.synthetic is True
    assert event.kind == JS_EVENT_BUTTON
