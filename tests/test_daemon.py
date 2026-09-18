#!/usr/bin/env python3
"""End to end test of the Bifrost daemon without any hardware.

Starts the daemon in --replay mode against tests/fixtures/synthetic_orbit.bin,
connects a TCP client, and checks the JSON-lines stream: hello frame, one axis
at a time moving, the expected peak magnitudes, a button press and release, a
final all-zero frame, and a clean disconnect. Exit status 0 means everything
passed.

    python3 tests/test_daemon.py
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DAEMON = os.path.join(REPO, "daemon", "bifrost_daemon.py")
FIXTURE = os.path.join(HERE, "fixtures", "synthetic_orbit.bin")
AXES = ("x", "y", "z", "rx", "ry", "rz")
PORT = 47661  # not the production port, so a running service is not disturbed

failures = []


def check(condition, message):
    status = "PASS" if condition else "FAIL"
    print("  [%s] %s" % (status, message))
    if not condition:
        failures.append(message)


def test_frame_decoding():
    """The fixture must decode exactly as spacenavd v1.3.1 writes events."""
    print("frame decoding")
    with open(FIXTURE, "rb") as handle:
        blob = handle.read()
    check(len(blob) % 32 == 0, "fixture is a whole number of 32 byte frames")
    first = struct.unpack("<8i", blob[:32])
    check(first[0] == 0, "first frame is UEV_MOTION (type 0)")
    check(first[7] == 16, "period field is 16 ms")
    types = set(
        struct.unpack("<8i", blob[i : i + 32])[0] for i in range(0, len(blob), 32)
    )
    check(types == {0, 1, 2}, "fixture holds motion, press and release types")


# The plumbing checks below want the stream the daemon served before the feel
# work: a straight line from deadzone to full scale, one axis at a time, no
# smoothing. The shaping itself is checked separately, both offline and over the
# same replay.
LINEAR = {
    "exponent": 1.0,
    "axis_cut": 0.0,
    "dominant_group": False,
    "smoothing_seconds": 0.0,
}


def write_config(response):
    config = os.path.join(HERE, "fixtures", "test_config.json")
    with open(config, "w") as handle:
        json.dump(
            {
                "daemon": {
                    "listen_port": PORT,
                    "deadzone": 15,
                    "full_scale": 350,
                    "emit_hz": 60,
                    "ping_seconds": 1.0,
                    "response": response,
                }
            },
            handle,
        )
    return config


def collect(config, seconds=8.0):
    """Run the daemon over the fixture and return every message it served."""
    proc = subprocess.Popen(
        [sys.executable, DAEMON, "--replay", FIXTURE, "--config", config],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        sock = None
        for _ in range(40):
            try:
                sock = socket.create_connection(("127.0.0.1", PORT), timeout=2.0)
                break
            except OSError:
                time.sleep(0.1)
        if sock is None:
            return None
        sock.settimeout(1.0)
        buffer = b""
        messages = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    messages.append(json.loads(line.decode("utf-8")))
        sock.close()
        return messages
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_shaping():
    """The response curve, the group gating and the smoothing, offline."""
    print("input shaping")
    sys.path.insert(0, os.path.join(REPO, "daemon"))
    import bifrost_daemon

    class StubConfig(object):
        def __init__(self):
            self.data = bifrost_daemon.DEFAULTS

        def section(self, name):
            return dict(self.data.get(name, {}))

    daemon_cfg = bifrost_daemon.DEFAULTS["daemon"]
    addin_cfg = bifrost_daemon.DEFAULTS["addin"]
    bridge = bifrost_daemon.Bridge(StubConfig())

    curve = dict(
        (counts, bridge._normalise([counts, 0, 0, 0, 0, 0], daemon_cfg)[0])
        for counts in (30, 60, 190, 350, 500)
    )
    check(curve[30] == 0.0, "the deadzone still swallows a resting puck")
    check(
        abs(curve[350] - 1.0) < 1e-9 and abs(curve[500] - 1.0) < 1e-9,
        "full deflection is 1.0 and does not run away past it (%.4f, %.4f)"
        % (curve[350], curve[500]),
    )
    check(
        abs(curve[190] - 0.25) < 1e-3,
        "half deflection is a quarter of full speed (%.4f)" % curve[190],
    )
    check(
        curve[60] < 0.01,
        "a 60 count stray axis is under one percent of full speed (%.4f)" % curve[60],
    )

    groups = bifrost_daemon.Bridge.axis_groups(addin_cfg)
    check(
        groups.get("rotate") == [3, 4]
        and groups.get("translate") == [0, 2]
        and groups.get("zoom") == [1],
        "the groups follow the measured axis map (%s)" % groups,
    )
    check(
        5 not in sum(groups.values(), []),
        "rz is left out while roll is off in turntable mode",
    )
    free = dict(addin_cfg)
    free["orbit_mode"] = "free"
    free["roll_speed"] = 0.5
    check(
        5 in bifrost_daemon.Bridge.axis_groups(free).get("rotate", []),
        "and joins the rotate group once free mode turns roll on",
    )

    # One real push plus three stray axes: nothing but the push survives.
    shaped = None
    for _ in range(200):
        shaped = bridge.shape([60, 0, 60, 350, 60, 0], daemon_cfg, addin_cfg, 1 / 60.0)
    check(
        abs(shaped[3] - 1.0) < 1e-3 and all(shaped[i] == 0.0 for i in (0, 1, 2, 4, 5)),
        "350 counts with three 60 count strays comes out as one axis (%s)" % shaped,
    )

    # The measured worst case from the hardware recording: lifting the puck
    # leaks about 52 counts onto rz.
    bridge = bifrost_daemon.Bridge(StubConfig())
    for _ in range(200):
        shaped = bridge.shape([0, 350, 0, 0, 0, 52], daemon_cfg, addin_cfg, 1 / 60.0)
    check(
        abs(shaped[1] - 1.0) < 1e-3 and shaped[5] == 0.0,
        "a lift with its measured rz cross talk is a clean zoom (%s)" % shaped,
    )

    # Release: the smoothing has to reach exactly zero, or the stream never
    # goes quiet and the add-in's burst never closes.
    frames = 0
    while frames < 600:
        shaped = bridge.shape([0, 0, 0, 0, 0, 0], daemon_cfg, addin_cfg, 1 / 60.0)
        frames += 1
        if all(v == 0.0 for v in shaped):
            break
    check(
        all(v == 0.0 for v in shaped) and frames < 60,
        "the smoothing settles to exact zero after release (%d frames, %.2f s)"
        % (frames, frames / 60.0),
    )


def run_daemon_test():
    print("daemon replay")
    config = write_config(LINEAR)

    proc = subprocess.Popen(
        [sys.executable, DAEMON, "--replay", FIXTURE, "--config", config],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        sock = None
        for _ in range(40):
            try:
                sock = socket.create_connection(("127.0.0.1", PORT), timeout=2.0)
                break
            except OSError:
                time.sleep(0.1)
        check(sock is not None, "daemon accepted a TCP connection")
        if sock is None:
            return

        sock.settimeout(1.0)
        buffer = b""
        messages = []
        deadline = time.time() + 8.0
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    messages.append(json.loads(line.decode("utf-8")))
        sock.close()

        hellos = [m for m in messages if m.get("t") == "hello"]
        motions = [m for m in messages if m.get("t") == "m"]
        buttons = [m for m in messages if m.get("t") == "b"]
        pings = [m for m in messages if m.get("t") == "ping"]

        check(len(hellos) == 1, "exactly one hello frame")
        if hellos:
            addin = hellos[0].get("addin", {})
            check(
                "orbit_speed" in addin and "map" in addin,
                "hello carries the add-in config",
            )
        check(len(motions) > 120, "motion frames streamed (%d)" % len(motions))
        check(len(pings) >= 1, "keepalive pings arrive (%d)" % len(pings))

        check(
            all(len(m["v"]) == 6 and m["dt"] > 0 for m in motions),
            "every motion frame has six axes and a positive dt",
        )

        peaks = dict((name, 0.0) for name in AXES)
        for message in motions:
            for index, name in enumerate(AXES):
                if abs(message["v"][index]) > abs(peaks[name]):
                    peaks[name] = message["v"][index]
        print("       peaks: %s" % json.dumps(peaks))
        # The fixture sweeps ry, rx, x and z, and never touches y or rz.
        for name in ("ry", "rx", "x", "z"):
            check(
                peaks[name] > 0.5,
                "%s swept to a real deflection (%.3f)" % (name, peaks[name]),
            )
        for name in ("y", "rz"):
            check(peaks[name] == 0.0, "%s stayed at zero as the fixture intends" % name)

        # Axis isolation: while ry sweeps, nothing else may move.
        cross_talk = [
            m
            for m in motions
            if m["v"][4] != 0.0 and any(m["v"][i] != 0.0 for i in (0, 1, 2, 3, 5))
        ]
        check(not cross_talk, "no cross talk between axes")

        check(
            len(buttons) == 2,
            "button press and release both forwarded (%d)" % len(buttons),
        )
        if len(buttons) == 2:
            check(
                buttons[0]["p"] == 1 and buttons[1]["p"] == 0,
                "press arrives before release",
            )
            check(buttons[0]["n"] == 0, "button number preserved")

        check(
            motions and all(v == 0.0 for v in motions[-1]["v"]),
            "stream ends with an all-zero frame",
        )

        # Every time the puck centres, exactly one zero frame goes out and then
        # the daemon stays quiet until it moves again.
        longest_zero_run = 0
        run = 0
        for message in motions:
            if all(v == 0.0 for v in message["v"]):
                run += 1
                longest_zero_run = max(longest_zero_run, run)
            else:
                run = 0
        check(
            longest_zero_run == 1,
            "daemon goes quiet once the puck is centred "
            "(longest zero run %d)" % longest_zero_run,
        )

        # Integrated yaw should be close to the ideal triangle integral.
        integral = sum(m["v"][4] * m["dt"] for m in motions)
        check(
            0.3 < integral < 1.2,
            "integrated yaw is sane (%.3f unit seconds)" % integral,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_shaped_replay():
    """The same fixture through the shaping the daemon actually ships with."""
    print("shaped replay")
    messages = collect(write_config({}))
    check(messages is not None, "daemon accepted a TCP connection")
    if messages is None:
        return
    motions = [m for m in messages if m.get("t") == "m"]
    check(len(motions) > 120, "motion frames streamed (%d)" % len(motions))

    peaks = dict((name, 0.0) for name in AXES)
    for message in motions:
        for index, name in enumerate(AXES):
            peaks[name] = max(peaks[name], abs(message["v"][index]))
    print("       peaks: %s" % json.dumps(peaks))
    check(
        all(peaks[name] > 0.25 for name in ("ry", "rx", "x", "z")),
        "the four swept axes still reach the add-in",
    )
    check(
        peaks["y"] == 0.0 and peaks["rz"] == 0.0,
        "the two untouched axes stay at zero",
    )

    # Whatever the smoothing does at a sweep boundary, a rotation frame and a
    # translation frame may never be nonzero at the same time: they are in
    # different groups, and only one group survives a frame.
    both = [
        m
        for m in motions
        if any(m["v"][i] != 0.0 for i in (3, 4))
        and any(m["v"][i] != 0.0 for i in (0, 2))
    ]
    check(not both, "rotation and translation never arrive in the same frame")
    zoom_and_rest = [
        m
        for m in motions
        if m["v"][1] != 0.0 and any(m["v"][i] != 0.0 for i in (0, 2, 3, 4))
    ]
    check(not zoom_and_rest, "zoom never shares a frame with anything else")

    check(
        motions and all(v == 0.0 for v in motions[-1]["v"]),
        "the shaped stream still ends with an all-zero frame",
    )
    longest = 0
    run = 0
    for message in motions:
        if all(v == 0.0 for v in message["v"]):
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    check(longest == 1, "and still goes quiet once (longest zero run %d)" % longest)


def test_reconnect():
    """A client must survive the daemon restarting under it."""
    print("client reconnect")
    config = os.path.join(HERE, "fixtures", "test_config.json")
    proc = subprocess.Popen(
        [
            sys.executable,
            DAEMON,
            "--replay",
            FIXTURE,
            "--replay-loop",
            "--config",
            config,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    connected_first = False
    for _ in range(40):
        try:
            sock = socket.create_connection(("127.0.0.1", PORT), timeout=2.0)
            connected_first = True
            sock.close()
            break
        except OSError:
            time.sleep(0.1)
    check(connected_first, "connected to the first daemon instance")
    proc.terminate()
    proc.wait(timeout=5)

    refused = False
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=1.0).close()
    except OSError:
        refused = True
    check(refused, "port is closed while the daemon is down")

    proc = subprocess.Popen(
        [sys.executable, DAEMON, "--replay", FIXTURE, "--config", config],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    connected_again = False
    for _ in range(40):
        try:
            sock = socket.create_connection(("127.0.0.1", PORT), timeout=2.0)
            connected_again = True
            sock.close()
            break
        except OSError:
            time.sleep(0.1)
    check(connected_again, "reconnected to the restarted daemon")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    test_frame_decoding()
    test_shaping()
    run_daemon_test()
    test_shaped_replay()
    test_reconnect()
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
