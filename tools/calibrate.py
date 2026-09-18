#!/usr/bin/env python3
"""Learn the axis map from the puck itself.

Which physical push a SpaceMouse reports as x versus z, and which way round the
rotations run, depends on the device and on how spacenavd is set up. Rather than
guessing, this walks through six movements, watches the raw spacenavd stream,
and writes the map, the sign flips and the fit button into
~/.config/bifrost/config.json.

    python3 tools/calibrate.py               # at the puck
    python3 tools/calibrate.py --dry-run     # detect and print, write nothing
    python3 tools/calibrate.py --replay tests/fixtures/hardware/calibration_capture.bin

It opens its own connection to /run/spnav.sock. spacenavd serves several clients
at once, so the daemon, KiCad and this tool can all watch the device together.

The signs follow one rule: the model follows the puck on screen. Push the puck
right and the model goes right, push it away from you and the model goes up,
tilt the front edge down and the model tips its front edge down. Zoom is the one
choice that is a matter of taste, so --zoom-in flips it.

Standard library only, like the rest of Bifrost.
"""

import argparse
import datetime
import json
import os
import shutil
import socket
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "daemon"))

import bifrost_daemon as daemon  # noqa: E402

AXES = daemon.AXES
FRAME_SIZE = daemon.FRAME_SIZE
FRAME_FMT = daemon.FRAME_FMT

# A movement has to reach this many raw counts to count as a real deflection.
MIN_PEAK = 80
# The winning axis must carry this many times the integral of the runner up.
MIN_RATIO = 2.5
# ... and it must lean one way, rather than wobble back and forth.
MIN_CONSISTENCY = 0.75
# Below this a frame counts as the puck sitting still.
QUIET = 25


class Movement(object):
    """One calibration step: a motion to name, and the sign it wants.

    `want` is the sign the add-in's motion value must end up with when the user
    performs `prompt`. Bifrost's camera maths is written so that a positive
    pan_x moves the model right on screen, a positive pan_y moves it up, a
    positive dolly zooms in, a positive pitch drops the camera under the model
    and a positive yaw swings the camera counterclockwise seen from above, which
    the model answers by turning clockwise. See docs/AXELMATRIS.md.
    """

    def __init__(self, motion, prompt, want, why):
        self.motion = motion
        self.prompt = prompt
        self.want = want
        self.why = why


def movements(zoom_in=False):
    return [
        Movement(
            "pan_x",
            "Push the puck to the RIGHT",
            +1,
            "the model should follow to the right",
        ),
        Movement(
            "pan_y",
            "Push the puck FORWARD, away from you",
            +1,
            "the model should move up the screen",
        ),
        Movement(
            "dolly",
            "Pull the puck STRAIGHT UP",
            +1 if zoom_in else -1,
            "lifting should zoom %s" % ("in" if zoom_in else "out"),
        ),
        Movement(
            "pitch",
            "Tilt the front edge of the puck DOWN",
            -1,
            "the model should tip its front edge down",
        ),
        Movement(
            "yaw",
            "Twist the puck CLOCKWISE, seen from above",
            +1,
            "the model should turn clockwise seen from above",
        ),
    ]


# ------------------------------------------------------------- detection ---


def integrate(samples):
    """Sum each axis over a window of raw frames. Returns six floats."""
    totals = [0.0] * 6
    for frame in samples:
        for index in range(6):
            totals[index] += frame[index]
    return totals


def peaks(samples):
    out = [0.0] * 6
    for frame in samples:
        for index in range(6):
            if abs(frame[index]) > abs(out[index]):
                out[index] = frame[index]
    return out


def dominant_axis(samples, min_peak=MIN_PEAK, ratio=MIN_RATIO):
    """Which axis did this movement drive, and which way?

    Returns (axis index, sign, report dict). The report always carries a
    "quality" string, which is "ok" when the movement is usable and otherwise
    says what was wrong, so the caller can ask for a retry.
    """
    report = {"frames": len(samples)}
    if not samples:
        report["quality"] = "nothing was recorded"
        return None, 0, report

    totals = integrate(samples)
    tops = peaks(samples)
    travel = [sum(abs(frame[i]) for frame in samples) for i in range(6)]
    # The candidate is the axis that moved the most, not the one with the
    # largest integral: an axis rocked hard both ways has to be caught by the
    # consistency rule below, not quietly ignored for having a small integral.
    order = sorted(range(6), key=lambda i: travel[i], reverse=True)
    best, second = order[0], order[1]
    report["axis"] = AXES[best]
    report["integral"] = totals[best]
    report["peak"] = tops[best]
    report["runner_up"] = AXES[second]
    report["runner_up_integral"] = totals[second]
    report["ratio"] = (
        abs(totals[best]) / abs(totals[second]) if abs(totals[second]) > 1e-9 else 999.0
    )
    report["consistency"] = (
        abs(totals[best]) / travel[best] if travel[best] > 1e-9 else 0.0
    )

    if abs(tops[best]) < min_peak:
        report["quality"] = "too small a movement, peak %d counts (want %d)" % (
            abs(tops[best]),
            min_peak,
        )
        return None, 0, report
    if report["ratio"] < ratio:
        report["quality"] = "%s and %s came out too close (%.1fx, want %.1fx)" % (
            AXES[best],
            AXES[second],
            report["ratio"],
            ratio,
        )
        return None, 0, report
    if report["consistency"] < MIN_CONSISTENCY:
        report["quality"] = "%s wobbled both ways (%.0f%% one way)" % (
            AXES[best],
            report["consistency"] * 100.0,
        )
        return None, 0, report

    report["quality"] = "ok"
    return best, (1 if totals[best] > 0 else -1), report


def segment_stream(samples, quiet=QUIET, min_frames=20, min_peak=MIN_PEAK):
    """Cut a long recording into one window per movement.

    A capture taken while someone works through the movements does not
    necessarily fall silent between them, so this splits on the dominant axis
    changing as well as on silence. Returns a list of windows, each a list of
    frames.
    """
    windows = []
    current = []
    current_axis = None
    for frame in samples:
        loudest = max(range(6), key=lambda i: abs(frame[i]))
        if abs(frame[loudest]) <= quiet:
            if len(current) >= min_frames:
                windows.append(current)
            current = []
            current_axis = None
            continue
        if current_axis is None:
            current_axis = loudest
        elif loudest != current_axis:
            # A different axis has taken over: close the window if the one that
            # ran so far was a real movement, otherwise keep collecting.
            if (
                len(current) >= min_frames
                and abs(peaks(current)[current_axis]) >= min_peak
            ):
                windows.append(current)
                current = []
            current_axis = loudest
        current.append(frame)
    if len(current) >= min_frames:
        windows.append(current)
    return windows


def group_windows(windows):
    """Merge neighbouring windows that drove the same axis the same way.

    People repeat a movement two or three times before they are happy with it,
    and a single push can break into several windows if the puck passes near
    centre on the way. Both cases are the same movement, so they belong in one
    window.
    """
    groups = []
    last_key = None
    for window in windows:
        axis, sign, report = dominant_axis(window)
        key = (axis, sign) if axis is not None else None
        if key is not None and key == last_key:
            groups[-1] = groups[-1] + window
        else:
            groups.append(list(window))
            last_key = key
    return groups


def build_mapping(results, zoom_in=False):
    """Turn {motion: (axis index, sign)} into a map and an invert table.

    Raises ValueError when two movements landed on the same axis, which means
    one of them was performed badly.
    """
    mapping = {}
    invert = {}
    wanted = dict((m.motion, m.want) for m in movements(zoom_in))
    seen = {}
    for motion, (index, sign) in results.items():
        axis = AXES[index]
        if axis in seen:
            raise ValueError(
                "%s and %s both came out as %s, so one of them was performed "
                "wrong" % (seen[axis], motion, axis)
            )
        seen[axis] = motion
        mapping[motion] = axis
        # The add-in computes value = raw * (-1 if invert else 1). The user's
        # movement produced `sign` on that axis and we want `wanted`.
        invert[motion] = sign != wanted[motion]

    # Roll is not calibrated by a movement of its own: it gets whichever
    # rotation axis is left over, and is off by default anyway.
    used = set(mapping.values())
    spare = [axis for axis in ("rx", "ry", "rz") if axis not in used]
    mapping["roll"] = spare[0] if spare else "rz"
    invert["roll"] = False
    return mapping, invert


# ----------------------------------------------------------------- input ---


class Puck(object):
    """Raw spacenavd frames, either live or from a recorded capture."""

    def __init__(self, socket_path=None, replay=None):
        self.replay = None
        self.sock = None
        if replay:
            with open(replay, "rb") as handle:
                blob = handle.read()
            self.replay = [
                struct.unpack(FRAME_FMT, blob[i : i + FRAME_SIZE])
                for i in range(0, len(blob) - FRAME_SIZE + 1, FRAME_SIZE)
            ]
            self.replay_at = 0
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect(socket_path or "/run/spnav.sock")
            self.sock.settimeout(0.2)
            self.buffer = b""

    def frames(self):
        """Yield decoded frames, blocking on the live socket."""
        if self.replay is not None:
            while self.replay_at < len(self.replay):
                frame = self.replay[self.replay_at]
                self.replay_at += 1
                yield frame
            return
        while True:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                yield None
                continue
            if not chunk:
                return
            self.buffer += chunk
            while len(self.buffer) >= FRAME_SIZE:
                frame = struct.unpack(FRAME_FMT, self.buffer[:FRAME_SIZE])
                self.buffer = self.buffer[FRAME_SIZE:]
                yield frame

    def drain(self):
        if self.replay is not None:
            return
        self.sock.settimeout(0.05)
        try:
            while True:
                if not self.sock.recv(65536):
                    break
        except socket.timeout:
            pass
        except OSError:
            pass
        self.sock.settimeout(0.2)
        self.buffer = b""

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


def record_movement(puck, quiet=QUIET, start_timeout=30.0, max_seconds=8.0):
    """Wait for the puck to move, then record until it comes to rest."""
    import time

    motion = []
    started = None
    idle_since = None
    deadline = time.time() + start_timeout
    for frame in puck.frames():
        if frame is None:
            if started is None and time.time() > deadline:
                return motion
            continue
        if frame[0] != daemon.UEV_MOTION:
            continue
        values = frame[1:7]
        loud = max(abs(v) for v in values) > quiet
        now = time.time()
        if started is None:
            if not loud:
                if now > deadline:
                    return motion
                continue
            started = now
        motion.append(values)
        if loud:
            idle_since = None
        else:
            if idle_since is None:
                idle_since = now
            elif now - idle_since >= 0.4:
                break
        if now - started > max_seconds:
            break
    return motion


def record_button(puck, timeout=30.0):
    """Wait for a button press and return its number, or None."""
    import time

    deadline = time.time() + timeout
    for frame in puck.frames():
        if frame is None:
            if time.time() > deadline:
                return None
            continue
        if frame[0] == daemon.UEV_PRESS:
            return frame[1]
        if time.time() > deadline:
            return None
    return None


# ------------------------------------------------------------------ main ---


def load_config(path):
    if os.path.exists(path):
        with open(path, "r") as handle:
            return json.load(handle)
    default = os.path.join(REPO, "config.default.json")
    if os.path.exists(default):
        with open(default, "r") as handle:
            return json.load(handle)
    return {"daemon": {}, "addin": {}}


def backup(path):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = "%s.bak-%s" % (path, stamp)
    shutil.copy2(path, target)
    return target


def describe(mapping, invert, fit_button):
    lines = ["", "  motion   axis   direction", "  ------   ----   ---------"]
    for motion in ("pan_x", "pan_y", "dolly", "pitch", "yaw", "roll"):
        lines.append(
            "  %-8s %-6s %s"
            % (
                motion,
                mapping.get(motion, "?"),
                "flipped" if invert.get(motion) else "as reported",
            )
        )
    lines.append("  fit      button %s" % fit_button)
    return "\n".join(lines)


def run_offline(path, zoom_in):
    """Detect the movements in a recorded capture, in the order they happen."""
    puck = Puck(replay=path)
    frames = [f[1:7] for f in puck.frames() if f[0] == daemon.UEV_MOTION]
    buttons = []
    puck = Puck(replay=path)
    for frame in puck.frames():
        if frame[0] == daemon.UEV_PRESS:
            buttons.append(frame[1])
    windows = group_windows(segment_stream(frames))
    steps = movements(zoom_in)
    print("%d movements found in %s" % (len(windows), os.path.basename(path)))
    results = {}
    for index, window in enumerate(windows):
        axis, sign, report = dominant_axis(window)
        label = steps[index].motion if index < len(steps) else "extra"
        print(
            "  %-6s %-6s %-6s peak %+5d  ratio %5.1f  %s"
            % (
                label,
                report.get("axis", "?"),
                "+" if sign > 0 else "-" if sign < 0 else "?",
                report.get("peak", 0),
                report.get("ratio", 0),
                report["quality"],
            )
        )
        if axis is not None and index < len(steps):
            results[steps[index].motion] = (axis, sign)
    if buttons:
        print("  fit    button %d (%d presses seen)" % (buttons[0], len(buttons)))
    return results, (buttons[0] if buttons else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=daemon.DEFAULT_CONFIG_PATH)
    parser.add_argument("--socket", default="/run/spnav.sock")
    parser.add_argument(
        "--replay", metavar="FILE", help="read a recorded capture instead of the device"
    )
    parser.add_argument(
        "--zoom-in",
        action="store_true",
        help="lifting the puck zooms in instead of out",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="detect and print, write nothing"
    )
    parser.add_argument(
        "--no-restart", action="store_true", help="do not restart bifrost.service"
    )
    args = parser.parse_args(argv)

    if args.replay:
        results, fit_button = run_offline(args.replay, args.zoom_in)
    else:
        try:
            puck = Puck(socket_path=args.socket)
        except OSError as exc:
            print("cannot reach spacenavd at %s: %s" % (args.socket, exc))
            print("is it running?  systemctl status spacenavd")
            return 1
        print("Connected to spacenavd. Six movements, one at a time.")
        print("Press Enter, then make the movement, then let the puck go.\n")
        results = {}
        fit_button = None
        for step in movements(args.zoom_in):
            for attempt in range(3):
                try:
                    input("  %s, then Enter to arm > " % step.prompt)
                except (EOFError, KeyboardInterrupt):
                    print("\naborted, nothing written")
                    return 1
                puck.drain()
                print("    go ...", end="", flush=True)
                window = record_movement(puck)
                axis, sign, report = dominant_axis(window)
                if axis is None:
                    print(" %s, try again" % report["quality"])
                    continue
                print(
                    " %s%s, peak %d counts, %.0fx clear of %s"
                    % (
                        "+" if sign > 0 else "-",
                        AXES[axis],
                        abs(report["peak"]),
                        report["ratio"],
                        report["runner_up"],
                    )
                )
                results[step.motion] = (axis, sign)
                break
            else:
                print("  giving up on %s, nothing written" % step.motion)
                puck.close()
                return 1

        try:
            input("  Press the button you want as FIT, then Enter to arm > ")
        except (EOFError, KeyboardInterrupt):
            print("\naborted, nothing written")
            return 1
        puck.drain()
        print("    press it now ...", end="", flush=True)
        fit_button = record_button(puck)
        print(" button %s" % fit_button if fit_button is not None else " nothing seen")
        puck.close()

    if len(results) < 5:
        print("\nonly %d of 5 movements came through, nothing written" % len(results))
        return 1
    try:
        mapping, invert = build_mapping(results, args.zoom_in)
    except ValueError as exc:
        print("\n%s" % exc)
        return 1

    print(describe(mapping, invert, fit_button if fit_button is not None else "first"))

    if args.dry_run:
        print("\n--dry-run, nothing written")
        return 0

    config = load_config(args.config)
    addin = config.setdefault("addin", {})
    addin["map"] = mapping
    addin["invert"] = invert
    if fit_button is not None:
        addin["fit_button"] = fit_button
    folder = os.path.dirname(args.config)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    if os.path.exists(args.config):
        print("\nbacked up the old config to %s" % backup(args.config))
    with open(args.config, "w") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    print("wrote %s" % args.config)

    if args.no_restart:
        print("run  systemctl --user restart bifrost.service  to apply it")
        return 0
    try:
        subprocess.check_call(
            ["systemctl", "--user", "restart", "bifrost.service"],
            stdout=subprocess.DEVNULL,
        )
        print("restarted bifrost.service, the add-in picks it up within seconds")
    except Exception as exc:
        print("could not restart bifrost.service (%s), do it by hand" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
