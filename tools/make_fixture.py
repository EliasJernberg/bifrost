#!/usr/bin/env python3
"""Generate a synthetic spacenavd capture for --replay testing.

The frames use exactly the format spacenavd v1.3.1 writes (see the module
docstring in daemon/bifrost_daemon.py): 32 bytes, eight native little endian
int32, data[0] = UEV_*. Period is fixed at 16 ms, which is what the real device
produced in the live capture taken while building this.

The script writes three fixtures into tests/fixtures/:

* synthetic_orbit.bin: a yaw sweep, a pan sweep, a dolly sweep, a button press
  and release, and a final centred frame.
* steady_yaw.bin: six seconds of constant yaw, for watching Fusion move.
* axis_matrix.bin: twelve single axis bursts, plus and minus on each of the six
  axes, each held for half a second and separated by silence. That is the
  fixture docs/AXELMATRIS.md was measured with.

    python3 tools/make_fixture.py                  # write all three
    python3 tools/make_fixture.py --axis rx --sign -1 --out /tmp/rx.bin
"""

import argparse
import os
import struct
import sys

FRAME = "<8i"
PERIOD_MS = 16
UEV_MOTION = 0
UEV_PRESS = 1
UEV_RELEASE = 2

AXES = ("x", "y", "z", "rx", "ry", "rz")

# Held at well under full deflection so a rotation burst turns about 21 degrees,
# which is large enough to measure and small enough to stay clear of the
# turntable pitch limit from a standard home view.
MATRIX_COUNTS = 115
MATRIX_HOLD_FRAMES = 31  # about 0.5 s at 16 ms
MATRIX_GAP_FRAMES = 75  # about 1.2 s, longer than the add-in's idle_gap_seconds


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


def hold(axis, counts, frames):
    """Hold one axis at a constant deflection, then let go."""
    return motion(**{axis: counts}) * frames


def gap(frames=MATRIX_GAP_FRAMES):
    return motion() * frames


def axis_burst(axis, sign, counts=MATRIX_COUNTS):
    """One measurable burst: silence, a steady hold, silence again."""
    return gap() + hold(axis, sign * counts, MATRIX_HOLD_FRAMES) + gap()


def axis_matrix():
    """Every axis, both signs, one at a time. 12 bursts, about 33 s of replay."""
    blob = b""
    for axis in AXES:
        for sign in (1, -1):
            blob += axis_burst(axis, sign)
    return blob


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--axis", choices=AXES, help="write a single axis burst")
    parser.add_argument("--sign", type=int, default=1, choices=(1, -1))
    parser.add_argument("--counts", type=int, default=MATRIX_COUNTS)
    parser.add_argument("--out", help="where the single axis burst goes")
    args = parser.parse_args(argv)

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "tests", "fixtures")
    os.makedirs(out_dir, exist_ok=True)

    if args.axis:
        path = args.out or os.path.join(
            out_dir,
            "axis_%s_%s.bin" % (args.axis, "plus" if args.sign > 0 else "minus"),
        )
        blob = axis_burst(args.axis, args.sign, args.counts)
        with open(path, "wb") as handle:
            handle.write(blob)
        print("%s: %d bytes, %d frames" % (path, len(blob), len(blob) // 32))
        return 0

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

    # A second fixture for watching Fusion itself move: six seconds of steady
    # yaw, then centre. Long enough to see several full turns on screen.
    steady_path = os.path.join(out_dir, "steady_yaw.bin")
    steady = b""
    for _ in range(int(6.0 * 1000 / PERIOD_MS)):
        steady += motion(ry=300)
    steady += motion()
    with open(steady_path, "wb") as handle:
        handle.write(steady)
    print("%s: %d bytes, %d frames" % (steady_path, len(steady), len(steady) // 32))

    # The axis matrix: one axis at a time, both signs, with enough silence
    # between bursts for the add-in to close one burst and open the next.
    matrix_path = os.path.join(out_dir, "axis_matrix.bin")
    matrix = axis_matrix()
    with open(matrix_path, "wb") as handle:
        handle.write(matrix)
    print(
        "%s: %d bytes, %d frames, %d bursts, about %.0f s of replay"
        % (
            matrix_path,
            len(matrix),
            len(matrix) // 32,
            len(AXES) * 2,
            len(matrix) / 32.0 * PERIOD_MS / 1000.0,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
