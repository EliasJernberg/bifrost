#!/usr/bin/env python3
"""Exercise the calibration tool's detection logic without a SpaceMouse.

Two halves: synthetic streams that poke at each rule in turn, and the real
recording in tests/fixtures/hardware/, taken with a hand on the puck, which the
tool has to read back as the mapping Bifrost ships.

    python3 tests/test_calibrate.py
"""

import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))

import calibrate  # noqa: E402

AXES = calibrate.AXES
CAPTURE = os.path.join(HERE, "fixtures", "hardware", "calibration_capture.bin")

failures = []


def check(condition, message):
    print("  [%s] %s" % ("PASS" if condition else "FAIL", message))
    if not condition:
        failures.append(message)


def frames(axis, counts, n=60, noise=None):
    """n frames holding one axis at `counts`, with optional cross talk."""
    out = []
    for _ in range(n):
        values = [0] * 6
        values[AXES.index(axis)] = counts
        for other, amount in (noise or {}).items():
            values[AXES.index(other)] += amount
        out.append(tuple(values))
    return out


def quiet(n=30):
    return [(0, 0, 0, 0, 0, 0)] * n


def test_detection():
    print("detection")
    for axis in AXES:
        for sign in (1, -1):
            index, got, report = calibrate.dominant_axis(frames(axis, sign * 300))
            check(
                index == AXES.index(axis) and got == sign,
                "a clean %s%s push is read as %s%s"
                % (
                    axis,
                    "+" if sign > 0 else "-",
                    report.get("axis"),
                    "+" if got > 0 else "-",
                ),
            )

    index, sign, report = calibrate.dominant_axis(frames("x", 40))
    check(
        index is None and "too small" in report["quality"],
        "a 40 count nudge is rejected (%s)" % report["quality"],
    )

    index, sign, report = calibrate.dominant_axis(frames("x", 200, noise={"z": 190}))
    check(
        index is None and "too close" in report["quality"],
        "two axes at once are rejected (%s)" % report["quality"],
    )

    wobble = frames("rx", 300, n=30) + frames("rx", -300, n=30)
    index, sign, report = calibrate.dominant_axis(wobble)
    check(
        index is None and "wobbled" in report["quality"],
        "a movement that goes both ways is rejected (%s)" % report["quality"],
    )

    index, sign, report = calibrate.dominant_axis(
        frames("z", 300, noise={"rx": 60, "rz": -40})
    )
    check(
        index == AXES.index("z") and sign == 1,
        "normal cross talk still resolves (%.1fx clear of %s)"
        % (report["ratio"], report["runner_up"]),
    )

    index, sign, report = calibrate.dominant_axis([])
    check(index is None, "an empty window is rejected, not crashed on")


def test_segmentation():
    print("segmentation")
    stream = quiet(20) + frames("x", 300) + quiet(40) + frames("ry", -300) + quiet(20)
    windows = calibrate.segment_stream(stream)
    check(
        len(windows) == 2, "silence between movements splits them (%d)" % len(windows)
    )

    # The case that matters for the real capture: no silence in between.
    stream = frames("x", 300) + frames("z", 300) + frames("y", 300)
    windows = calibrate.segment_stream(stream)
    axes = [AXES[calibrate.dominant_axis(w)[0]] for w in windows]
    check(
        axes == ["x", "z", "y"],
        "movements that run together split on the dominant axis (%s)" % axes,
    )

    # Repeats of the same movement belong together.
    stream = (
        frames("x", 300) + quiet(10) + frames("x", 320) + quiet(10) + frames("x", 290)
    )
    groups = calibrate.group_windows(calibrate.segment_stream(stream))
    check(len(groups) == 1, "three pushes the same way become one (%d)" % len(groups))

    stream = frames("x", 300) + quiet(10) + frames("x", -300)
    groups = calibrate.group_windows(calibrate.segment_stream(stream))
    check(len(groups) == 2, "the same axis the other way is a new movement")


def test_mapping():
    print("mapping")
    # What the hardware actually reports, from docs/AXELMATRIS.md.
    results = {
        "pan_x": (AXES.index("x"), +1),
        "pan_y": (AXES.index("z"), +1),
        "dolly": (AXES.index("y"), +1),
        "pitch": (AXES.index("rx"), -1),
        "yaw": (AXES.index("ry"), -1),
    }
    mapping, invert = calibrate.build_mapping(results)
    check(
        mapping
        == {
            "pan_x": "x",
            "pan_y": "z",
            "dolly": "y",
            "pitch": "rx",
            "yaw": "ry",
            "roll": "rz",
        },
        "the map comes out as the one Bifrost ships (%s)" % mapping,
    )
    check(
        invert
        == {
            "pan_x": False,
            "pan_y": False,
            "dolly": True,
            "pitch": False,
            "yaw": True,
            "roll": False,
        },
        "so do the sign flips (%s)" % invert,
    )

    mapping_in, invert_in = calibrate.build_mapping(results, zoom_in=True)
    check(
        invert_in["dolly"] is False
        and all(invert_in[k] == invert[k] for k in invert if k != "dolly"),
        "--zoom-in flips dolly and nothing else",
    )

    clash = dict(results)
    clash["pan_y"] = (AXES.index("x"), +1)
    try:
        calibrate.build_mapping(clash)
        check(False, "two movements on one axis are refused")
    except ValueError as exc:
        check("both came out as x" in str(exc), "two movements on one axis are refused")


def test_against_the_real_capture():
    print("the recorded hand on the puck")
    if not os.path.exists(CAPTURE):
        check(False, "the capture fixture is missing")
        return
    results, fit_button = calibrate.run_offline(CAPTURE, zoom_in=False)
    check(len(results) == 5, "all five movements were found (%d)" % len(results))
    expected = [
        ("pan_x", "x", +1),
        ("pan_y", "z", +1),
        ("dolly", "y", +1),
        ("pitch", "rx", -1),
        ("yaw", "ry", -1),
    ]
    for motion, axis, sign in expected:
        got = results.get(motion)
        check(
            got == (AXES.index(axis), sign),
            "%s came out as %s%s" % (motion, axis, "+" if sign > 0 else "-"),
        )
    check(fit_button == 5, "the fit button is 5 (%s)" % fit_button)

    mapping, invert = calibrate.build_mapping(results)
    shipped = os.path.join(REPO, "config.default.json")
    import json

    with open(shipped) as handle:
        addin = json.load(handle)["addin"]
    check(mapping == addin["map"], "it reproduces the shipped map")
    check(invert == addin["invert"], "it reproduces the shipped sign flips")
    check(fit_button == addin["fit_button"], "it reproduces the shipped fit button")


def test_frame_decoding():
    print("frame decoding")
    if not os.path.exists(CAPTURE):
        check(False, "the capture fixture is missing")
        return
    blob = open(CAPTURE, "rb").read()
    check(
        len(blob) % calibrate.FRAME_SIZE == 0,
        "the capture is a whole number of 32 byte frames (%d)"
        % (len(blob) // calibrate.FRAME_SIZE),
    )
    types = set()
    for i in range(0, len(blob), calibrate.FRAME_SIZE):
        types.add(struct.unpack(calibrate.FRAME_FMT, blob[i : i + 32])[0])
    check(
        {0, 1, 2} <= types,
        "it holds motion, press and release frames (%s)" % sorted(types),
    )


def main():
    test_detection()
    test_segmentation()
    test_mapping()
    test_frame_decoding()
    test_against_the_real_capture()
    print()
    if failures:
        print("%d check(s) FAILED:" % len(failures))
        for item in failures:
            print("  - %s" % item)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
