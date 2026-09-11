#!/usr/bin/env python3
"""Generate a synthetic spacenavd capture for --replay testing.

The frames use exactly the format spacenavd v1.3.1 writes (see the module
docstring in daemon/bifrost_daemon.py): 32 bytes, eight native little endian
int32, data[0] = UEV_*. Period is fixed at 16 ms, which is what the real device
produced in the live capture taken while building this.

The script writes tests/fixtures/synthetic_orbit.bin: a yaw sweep, a pan sweep,
a dolly sweep, a button press and release, and a final centred frame.
"""

import os
import struct
import sys

FRAME = "<8i"
PERIOD_MS = 16
UEV_MOTION = 0
UEV_PRESS = 1
UEV_RELEASE = 2


def motion(x=0, y=0, z=0, rx=0, ry=0, rz=0):
    return struct.pack(FRAME, UEV_MOTION, x, y, z, rx, ry, rz, PERIOD_MS)


def button(bnum, pressed):
    return struct.pack(
        FRAME,
        UEV_PRESS if pressed else UEV_RELEASE,
        bnum,
        1 if pressed else 0,
        0,
        0,
        0,
        0,
        0,
    )


def sweep(axis, peak, frames):
    """Ramp an axis up to peak and back down, mimicking a real push."""
    out = b""
    for index in range(frames):
        phase = index / float(frames - 1)
        # triangle: 0 -> 1 -> 0
        amount = 2.0 * phase if phase < 0.5 else 2.0 * (1.0 - phase)
        out += motion(**{axis: int(round(peak * amount))})
    return out


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "tests", "fixtures")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "synthetic_orbit.bin")

    blob = b""
    blob += sweep("ry", 300, 60)  # yaw
    blob += motion()
    blob += sweep("rx", 220, 40)  # pitch
    blob += motion()
    blob += sweep("x", 280, 40)  # pan sideways
    blob += motion()
    blob += sweep("z", 260, 40)  # dolly
    blob += motion()
    blob += button(0, True)
    blob += button(0, False)
    blob += motion()

    with open(out_path, "wb") as handle:
        handle.write(blob)
    print("%s: %d bytes, %d frames" % (out_path, len(blob), len(blob) // 32))
    return 0


if __name__ == "__main__":
    sys.exit(main())
