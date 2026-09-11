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


def run_daemon_test():
    print("daemon replay")
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
                }
            },
            handle,
        )

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
    run_daemon_test()
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
