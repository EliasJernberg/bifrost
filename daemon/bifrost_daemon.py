#!/usr/bin/env python3
"""Bifrost daemon: bridges spacenavd to a TCP JSON-lines stream.

Fusion 360 running under Wine cannot talk to 3Dconnexion HID hardware, because
3DxWare does not run there. Bifrost sidesteps HID completely: this daemon runs
natively on Linux, reads 6DoF events from the spacenavd UNIX socket, applies
deadzone, gain and inversion, and serves the result as newline delimited JSON
over TCP on 127.0.0.1. The Fusion add-in connects to that port from inside the
Wine prefix (TCP over loopback crosses the Wine boundary fine) and drives the
viewport camera.

Standard library only. Tested on Python 3.9 and newer.

Wire protocol (server to client, one JSON object per line, UTF-8):

    {"t":"hello","v":1,"addin":{...}}      sent once per connection
    {"t":"m","v":[x,y,z,rx,ry,rz],"dt":s}  motion, normalised to about -1..1
    {"t":"b","n":<button>,"p":0|1}         button release / press
    {"t":"ping","ts":<monotonic>}          keepalive

Motion frames are emitted at a fixed rate (emit_hz) for as long as the puck is
deflected, which makes the stream independent of how often spacenavd decides to
report. Exactly one all-zero frame is sent when the puck returns to centre, and
then the stream goes quiet until it moves again. "dt" is the wall time the frame
covers, so a client that accumulates v * dt gets a rate independent integral.

spacenavd wire format (verified against spacenavd v1.3.1 src/proto_unix.c,
function send_uevent, and src/proto.h): every event is exactly 32 bytes, eight
native little endian int32. data[0] is the event type from enum UEV_*:

    UEV_MOTION    = 0   ->  [0, x, y, z, rx, ry, rz, period_ms]
    UEV_PRESS     = 1   ->  [1, button, 1, 0, 0, 0, 0, 0]
    UEV_RELEASE   = 2   ->  [2, button, 0, 0, 0, 0, 0, 0]
    UEV_DEV       = 3, UEV_CFG = 4, UEV_RAWAXIS = 5, UEV_RAWBUTTON = 6

Protocol version 0 is the default for a fresh connection (src/client.c sets
evmask = EVMASK_MOTION | EVMASK_BUTTON for v0 clients), so no handshake is
needed and none is performed. spacenavd is happy with several clients at once,
so running this alongside KiCad is fine.
"""

import argparse
import errno
import json
import os
import socket
import struct
import sys
import threading
import time

APP_NAME = "bifrost"
PROTO_VERSION = 1

FRAME_SIZE = 32
FRAME_FMT = "<8i"

UEV_MOTION = 0
UEV_PRESS = 1
UEV_RELEASE = 2
UEV_DEV = 3
UEV_CFG = 4
UEV_RAWAXIS = 5
UEV_RAWBUTTON = 6

AXES = ("x", "y", "z", "rx", "ry", "rz")

DEFAULT_CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    APP_NAME,
    "config.json",
)

DEFAULTS = {
    "daemon": {
        "spnav_socket": "/run/spnav.sock",
        "listen_host": "127.0.0.1",
        "listen_port": 47653,
        "emit_hz": 60,
        "ping_seconds": 5.0,
        # Raw spacenavd counts below this are treated as zero.
        "deadzone": 15,
        # Raw count that maps to a normalised magnitude of 1.0.
        "full_scale": 350,
        "sensitivity": 1.0,
        "axis_gain": {"x": 1.0, "y": 1.0, "z": 1.0, "rx": 1.0, "ry": 1.0, "rz": 1.0},
        "axis_invert": {
            "x": False,
            "y": False,
            "z": False,
            "rx": False,
            "ry": False,
            "rz": False,
        },
        "verbose": False,
    },
    # Everything below is passed straight through to the Fusion add-in in the
    # hello frame, so camera tuning never needs a file inside the Wine prefix.
    "addin": {
        "max_fire_hz": 30,
        # Which spacenavd axis drives which camera motion.
        "map": {
            "pan_x": "x",
            "pan_y": "y",
            "dolly": "z",
            "pitch": "rx",
            "yaw": "ry",
            "roll": "rz",
        },
        "invert": {
            "pan_x": False,
            "pan_y": True,
            "dolly": False,
            "pitch": False,
            "yaw": False,
            "roll": False,
        },
        # Radians per unit of accumulated normalised input.
        "orbit_speed": 2.5,
        # Fractions of the visible viewport width per unit of input.
        "pan_speed": 1.0,
        # e-folds of view scale per unit of input.
        "zoom_speed": 1.2,
        "roll_speed": 0.0,
        # "free" orbits around the camera up vector, "turntable" around world_up.
        "orbit_mode": "turntable",
        "world_up": [0.0, 0.0, 1.0],
        # Keep at least this many degrees between the view direction and world_up
        # in turntable mode.
        "pitch_limit_deg": 2.0,
        "min_distance": 0.01,
        # Button number that triggers viewport.fit(). -1 disables, "first" learns
        # the first button ever seen.
        "fit_button": "first",
        # Run a scripted 360 degree orbit right after the add-in starts.
        "selftest": False,
        "selftest_seconds": 3.0,
        "selftest_wait_seconds": 600.0,
        "log_path": "",
        "log_level": "info",
    },
}


def deep_merge(base, override):
    """Return a copy of base with override recursively laid on top."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config(object):
    """Config file wrapper that reloads itself when the mtime changes."""

    def __init__(self, path):
        self.path = path
        self._mtime = None
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        self.reload(force=True)

    def reload(self, force=False):
        """Re-read the file if it changed. Returns True if data changed."""
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        loaded = {}
        if mtime is not None:
            try:
                with open(self.path, "r") as handle:
                    loaded = json.load(handle)
            except Exception as exc:  # noqa: BLE001 - never die on bad config
                log(
                    "config: could not read %s: %s (keeping previous)"
                    % (self.path, exc)
                )
                return False
            if not isinstance(loaded, dict):
                log("config: %s is not a JSON object, ignoring" % self.path)
                loaded = {}
        merged = deep_merge(DEFAULTS, loaded)
        with self._lock:
            changed = merged != self.data
            self.data = merged
        if changed and not force:
            log("config: reloaded %s" % self.path)
        return changed

    def section(self, name):
        with self._lock:
            return dict(self.data.get(name, {}))


_log_lock = threading.Lock()


def log(message):
    stamp = time.strftime("%H:%M:%S")
    with _log_lock:
        sys.stderr.write("[%s] bifrost: %s\n" % (stamp, message))
        sys.stderr.flush()


class ClientHub(object):
    """Keeps the set of connected TCP clients and fans messages out to them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients = []

    def add(self, sock, addr):
        sock.settimeout(2.0)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        with self._lock:
            self._clients.append((sock, addr))
        log("client connected: %s (%d total)" % (addr, self.count()))

    def count(self):
        with self._lock:
            return len(self._clients)

    def remove(self, sock, addr, reason):
        with self._lock:
            self._clients = [c for c in self._clients if c[0] is not sock]
        try:
            sock.close()
        except OSError:
            pass
        log("client disconnected: %s (%s)" % (addr, reason))

    def send_one(self, sock, addr, payload):
        try:
            sock.sendall(payload)
            return True
        except Exception as exc:  # noqa: BLE001
            self.remove(sock, addr, str(exc))
            return False

    def broadcast(self, obj):
        with self._lock:
            targets = list(self._clients)
        if not targets:
            return
        payload = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        for sock, addr in targets:
            self.send_one(sock, addr, payload)

    def close_all(self):
        with self._lock:
            targets = list(self._clients)
            self._clients = []
        for sock, _addr in targets:
            try:
                sock.close()
            except OSError:
                pass


class Bridge(object):
    def __init__(self, config, replay=None, replay_loop=False):
        self.config = config
        self.replay = replay
        self.replay_loop = replay_loop
        self.hub = ClientHub()
        self.running = True
        self._state_lock = threading.Lock()
        self._raw = [0] * 6
        self._last_nonzero = False
        self._fit_button_seen = None
        self._threads = []
        self.stats_frames = 0
        self.stats_motion_in = 0

    # -- normalisation -----------------------------------------------------

    def _normalise(self, raw, cfg):
        deadzone = float(cfg.get("deadzone", 0))
        full_scale = float(cfg.get("full_scale", 350))
        sensitivity = float(cfg.get("sensitivity", 1.0))
        gains = cfg.get("axis_gain", {})
        inverts = cfg.get("axis_invert", {})
        span = max(1.0, full_scale - deadzone)
        out = []
        for index, name in enumerate(AXES):
            value = float(raw[index])
            magnitude = abs(value)
            if magnitude <= deadzone:
                out.append(0.0)
                continue
            scaled = (magnitude - deadzone) / span
            if scaled > 1.5:
                scaled = 1.5
            if value < 0:
                scaled = -scaled
            scaled *= float(gains.get(name, 1.0)) * sensitivity
            if inverts.get(name, False):
                scaled = -scaled
            out.append(round(scaled, 5))
        return out

    # -- spacenavd side ----------------------------------------------------

    def handle_frame(self, frame):
        values = struct.unpack(FRAME_FMT, frame)
        etype = values[0]
        if etype == UEV_MOTION:
            self.stats_motion_in += 1
            with self._state_lock:
                self._raw = list(values[1:7])
        elif etype in (UEV_PRESS, UEV_RELEASE):
            bnum = values[1]
            pressed = 1 if etype == UEV_PRESS else 0
            if self._fit_button_seen is None and pressed:
                self._fit_button_seen = bnum
            self.hub.broadcast({"t": "b", "n": bnum, "p": pressed})
            if self.config.section("daemon").get("verbose"):
                log("button %d %s" % (bnum, "press" if pressed else "release"))
        elif etype in (UEV_DEV, UEV_CFG, UEV_RAWAXIS, UEV_RAWBUTTON):
            if self.config.section("daemon").get("verbose"):
                log("spnav event type %d: %s" % (etype, values[1:]))
        else:
            log("unknown spnav event type %d, ignoring" % etype)

    def spnav_reader(self):
        while self.running:
            path = self.config.section("daemon").get("spnav_socket", "/run/spnav.sock")
            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect(path)
                sock.settimeout(1.0)
                log("connected to spacenavd at %s" % path)
                buffer = b""
                while self.running:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        if exc.errno == errno.EINTR:
                            continue
                        raise
                    if not chunk:
                        raise OSError("spacenavd closed the connection")
                    buffer += chunk
                    while len(buffer) >= FRAME_SIZE:
                        self.handle_frame(buffer[:FRAME_SIZE])
                        buffer = buffer[FRAME_SIZE:]
            except Exception as exc:  # noqa: BLE001
                if self.running:
                    log("spacenavd link lost (%s), retrying in 2 s" % exc)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                with self._state_lock:
                    self._raw = [0] * 6
            if self.running:
                time.sleep(2.0)

    def replay_reader(self):
        """Feed a recorded .bin capture through the same pipeline."""
        with open(self.replay, "rb") as handle:
            data = handle.read()
        count = len(data) // FRAME_SIZE
        log("replay: %s, %d frames" % (self.replay, count))
        while self.running:
            for index in range(count):
                if not self.running:
                    break
                frame = data[index * FRAME_SIZE : (index + 1) * FRAME_SIZE]
                values = struct.unpack(FRAME_FMT, frame)
                self.handle_frame(frame)
                delay = 0.01
                if values[0] == UEV_MOTION:
                    period = values[7]
                    if 1 <= period <= 200:
                        delay = period / 1000.0
                time.sleep(delay)
            if not self.replay_loop:
                break
            time.sleep(0.5)
        # Leave the puck centred when the capture ends.
        with self._state_lock:
            self._raw = [0] * 6
        log("replay finished")

    # -- emitter -----------------------------------------------------------

    def emitter(self):
        last = time.monotonic()
        last_ping = last
        last_config_check = last
        while self.running:
            cfg = self.config.section("daemon")
            hz = float(cfg.get("emit_hz", 60)) or 60.0
            period = 1.0 / hz
            now = time.monotonic()
            dt = now - last
            last = now
            if dt < 0.0005:
                dt = 0.0005
            elif dt > 0.2:
                dt = 0.2

            if now - last_config_check >= 1.0:
                last_config_check = now
                self.config.reload()

            with self._state_lock:
                raw = list(self._raw)
            values = self._normalise(raw, cfg)
            nonzero = any(v != 0.0 for v in values)
            if nonzero or self._last_nonzero:
                self.hub.broadcast({"t": "m", "v": values, "dt": round(dt, 5)})
                self.stats_frames += 1
            self._last_nonzero = nonzero

            ping_seconds = float(cfg.get("ping_seconds", 5.0))
            if ping_seconds > 0 and now - last_ping >= ping_seconds:
                last_ping = now
                self.hub.broadcast({"t": "ping", "ts": round(now, 3)})

            sleep_for = period - (time.monotonic() - now)
            if sleep_for > 0:
                time.sleep(sleep_for)

    # -- TCP side ----------------------------------------------------------

    def acceptor(self):
        cfg = self.config.section("daemon")
        host = cfg.get("listen_host", "127.0.0.1")
        port = int(cfg.get("listen_port", 47653))
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(8)
        server.settimeout(1.0)
        log("listening on %s:%d" % (host, port))
        self._server = server
        while self.running:
            try:
                sock, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            hello = {
                "t": "hello",
                "v": PROTO_VERSION,
                "addin": self.config.section("addin"),
            }
            payload = (json.dumps(hello, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                sock.sendall(payload)
            except OSError as exc:
                log("client %s died during hello: %s" % (addr, exc))
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            self.hub.add(sock, addr)
        try:
            server.close()
        except OSError:
            pass

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        targets = [self.acceptor, self.emitter]
        targets.append(self.replay_reader if self.replay else self.spnav_reader)
        for target in targets:
            thread = threading.Thread(target=target, name=target.__name__)
            thread.daemon = True
            thread.start()
            self._threads.append(thread)

    def stop(self):
        self.running = False
        self.hub.close_all()


# -- standalone helper modes ----------------------------------------------


def mode_dump(args):
    """Print decoded spacenavd events. Used to verify the wire format."""
    path = args.socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect(path)
    print(
        "connected to %s, %ss window, protocol v0 (no handshake)" % (path, args.seconds)
    )
    sock.settimeout(1.0)
    start = time.monotonic()
    buffer = b""
    total = 0
    names = {
        0: "MOTION",
        1: "PRESS",
        2: "RELEASE",
        3: "DEV",
        4: "CFG",
        5: "RAWAXIS",
        6: "RAWBUTTON",
    }
    while time.monotonic() - start < args.seconds:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            print("socket closed by spacenavd")
            break
        buffer += chunk
        total += len(chunk)
        while len(buffer) >= FRAME_SIZE:
            values = struct.unpack(FRAME_FMT, buffer[:FRAME_SIZE])
            buffer = buffer[FRAME_SIZE:]
            print(
                "%7.3f  %-9s %s"
                % (
                    time.monotonic() - start,
                    names.get(values[0], "?%d" % values[0]),
                    values[1:],
                )
            )
    print(
        "%d bytes, %d complete frames, connection stayed healthy"
        % (total, total // FRAME_SIZE)
    )
    sock.close()
    return 0


def mode_tail(args):
    """Connect to a running daemon and print the JSON lines it serves."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect((args.host, args.port))
    print("connected to %s:%d" % (args.host, args.port))
    sock.settimeout(1.0)
    start = time.monotonic()
    buffer = b""
    lines = 0
    while time.monotonic() - start < args.seconds:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            print("daemon closed the connection")
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            lines += 1
            print("%7.3f  %s" % (time.monotonic() - start, line.decode("utf-8")))
    print("%d lines" % lines)
    sock.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bifrost: spacenavd to TCP JSON-lines bridge for Fusion 360 under Wine."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="config file (default: %s)" % DEFAULT_CONFIG_PATH,
    )
    parser.add_argument(
        "--replay",
        metavar="FILE",
        help="feed a recorded raw spnav capture instead of the device",
    )
    parser.add_argument(
        "--replay-loop", action="store_true", help="loop the replay file forever"
    )
    parser.add_argument(
        "--dump", action="store_true", help="decode spacenavd events to stdout and exit"
    )
    parser.add_argument(
        "--tail",
        action="store_true",
        help="connect to a running daemon and print its JSON lines",
    )
    parser.add_argument(
        "--socket", default="/run/spnav.sock", help="spacenavd socket for --dump"
    )
    parser.add_argument("--host", default="127.0.0.1", help="host for --tail")
    parser.add_argument("--port", type=int, default=47653, help="port for --tail")
    parser.add_argument(
        "--seconds", type=float, default=10.0, help="duration for --dump and --tail"
    )
    args = parser.parse_args(argv)

    if args.dump:
        return mode_dump(args)
    if args.tail:
        return mode_tail(args)

    config = Config(args.config)
    bridge = Bridge(config, replay=args.replay, replay_loop=args.replay_loop)
    bridge.start()
    log("started (config: %s)" % args.config)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log("shutting down")
        bridge.stop()
        time.sleep(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
