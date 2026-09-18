#!/usr/bin/env python3
"""Bifrost daemon: bridges spacenavd to a TCP JSON-lines stream.

Fusion 360 running under Wine cannot talk to 3Dconnexion HID hardware, because
3DxWare does not run there. Bifrost sidesteps HID completely: this daemon runs
natively on Linux, reads 6DoF events from the spacenavd UNIX socket, applies
deadzone, a response curve, gain, inversion, dominant group gating and light
smoothing, and serves the result as newline delimited JSON over TCP on
127.0.0.1. The Fusion add-in connects to that port from inside the Wine prefix
(TCP over loopback crosses the Wine boundary fine) and drives the viewport
camera.

The shaping matters as much as the plumbing: a SpaceMouse reports all six axes
on every push, so a linear map turns one nudge into a simultaneous orbit, pan
and zoom. Bifrost squares the normalised magnitude, keeps only the strongest of
rotate / translate / zoom per frame, and drops the axes inside that group that
are carrying a fraction of the leading one.

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
import math
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

# Which camera motions belong together. A SpaceMouse leaks a little of every
# push into all six axes, so the shaping stage picks one group per frame and
# drops the rest: you are either turning the model, sliding it or zooming, never
# all three at once by accident. Measured cross talk on this hardware is in
# docs/AXELMATRIS.md.
MOTION_GROUPS = (
    ("rotate", ("pitch", "yaw", "roll")),
    ("translate", ("pan_x", "pan_y")),
    ("zoom", ("dolly",)),
)

# The add-in config key that decides whether a motion does anything at all. A
# motion with speed zero must never win the dominance contest.
MOTION_SPEED_KEY = {
    "pitch": "orbit_speed",
    "yaw": "orbit_speed",
    "roll": "roll_speed",
    "pan_x": "pan_speed",
    "pan_y": "pan_speed",
    "dolly": "zoom_speed",
}

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
        "deadzone": 30,
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
        # Feel. This is what keeps a single nudge from driving all six axes at
        # once, see README "Response and cross talk".
        "response": {
            # 1.0 is a straight line from deadzone to full scale, 2.0 squares
            # it, 3.0 cubes it. Higher means fine control around centre and the
            # same top speed at full deflection.
            "exponent": 2.0,
            # Inside the winning group, an axis carrying less than this fraction
            # of the leading axis is dropped.
            "axis_cut": 0.1,
            # Only one of rotate / translate / zoom survives each frame.
            "dominant_group": True,
            # ... and the group that is already winning keeps winning until
            # another one beats it by this factor, so a gesture cannot flicker.
            "dominant_hysteresis": 1.25,
            # Exponential smoothing time constant in seconds, 0 disables it.
            "smoothing_seconds": 0.05,
        },
        "verbose": False,
    },
    # Everything below is passed straight through to the Fusion add-in in the
    # hello frame, so camera tuning never needs a file inside the Wine prefix.
    "addin": {
        "max_fire_hz": 30,
        # Which spacenavd axis drives which camera motion. Measured against the
        # hardware, see docs/AXELMATRIS.md: push right is x+, push away is z+,
        # lift is y+, tilting the front edge down is rx-, twisting clockwise
        # seen from above is ry-.
        "map": {
            "pan_x": "x",
            "pan_y": "z",
            "dolly": "y",
            "pitch": "rx",
            "yaw": "ry",
            "roll": "rz",
        },
        # Signs chosen so the model follows the puck on screen.
        "invert": {
            "pan_x": False,
            "pan_y": False,
            "dolly": True,
            "pitch": False,
            "yaw": True,
            "roll": False,
        },
        # Radians per unit of accumulated normalised input, so also radians per
        # second at full deflection: pi/2 is a quarter turn a second.
        "orbit_speed": 1.5708,
        # Fractions of the visible viewport width per unit of input, so one
        # full viewport width a second at full deflection.
        "pan_speed": 1.0,
        # e-folds of view scale per unit of input. ln(2) is a factor two a
        # second at full deflection.
        "zoom_speed": 0.6931,
        "roll_speed": 0.0,
        # "free" orbits around the camera up vector, "turntable" around world_up.
        "orbit_mode": "turntable",
        # "auto" turns around the centre of the visible model, "target" around
        # the camera target, which is what every version before this did.
        "orbit_pivot": "auto",
        # A pause this long ends one movement and starts the next, which is when
        # the auto pivot is allowed to move.
        "idle_gap_seconds": 0.5,
        "world_up": [0.0, 0.0, 1.0],
        # Keep at least this many degrees between the view direction and
        # world_up in turntable mode, so elevation is clamped to +/- 89 degrees.
        "pitch_limit_deg": 1.0,
        "min_distance": 0.01,
        # Button number that triggers viewport.fit(). -1 disables, "first" learns
        # the first button ever seen.
        "fit_button": "first",
        # Run a scripted 360 degree orbit right after the add-in starts.
        "selftest": False,
        "selftest_seconds": 3.0,
        # Viewport widths of pan before the scripted orbit, so the rendered
        # frames show whether the orbit still turns around the model.
        "selftest_pan": 0.0,
        # e-folds of zoom the self test applies after the orbit, rendered both
        # ways, so the pictures cover all three motions.
        "selftest_zoom": 0.0,
        # Fit the view before the self test runs, so its rendered frames are
        # framed the same way whatever camera Fusion restored.
        "selftest_fit": True,
        "selftest_wait_seconds": 600.0,
        "selftest_image_dir": "",
        "selftest_settle_seconds": 15.0,
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
    def __init__(self, config, replay=None, replay_loop=False, replay_wait=False):
        self.config = config
        self.replay = replay
        self.replay_loop = replay_loop
        self.replay_wait = replay_wait
        self.hub = ClientHub()
        self.running = True
        self._state_lock = threading.Lock()
        self._raw = [0] * 6
        self._last_nonzero = False
        self._fit_button_seen = None
        self._threads = []
        self.stats_frames = 0
        self.stats_motion_in = 0
        # Shaping state, emitter thread only.
        self._ema = [0.0] * 6
        self._dominant = None

    # -- normalisation -----------------------------------------------------

    def _normalise(self, raw, cfg):
        """Raw counts to a signed magnitude, deadzoned and put through the curve.

        The curve is the whole point: a straight line makes every stray count a
        camera movement, while squaring the normalised magnitude leaves full
        deflection untouched and pushes the cross talk near the deadzone down
        into nothing. 60 counts next to a 350 count push goes from 19 percent of
        full speed to under 1 percent.
        """
        deadzone = float(cfg.get("deadzone", 0))
        full_scale = float(cfg.get("full_scale", 350))
        sensitivity = float(cfg.get("sensitivity", 1.0))
        gains = cfg.get("axis_gain", {})
        inverts = cfg.get("axis_invert", {})
        response = cfg.get("response", {}) or {}
        try:
            exponent = float(response.get("exponent", 2.0))
        except (TypeError, ValueError):
            exponent = 2.0
        if exponent < 1.0:
            exponent = 1.0
        span = max(1.0, full_scale - deadzone)
        out = []
        for index, name in enumerate(AXES):
            value = float(raw[index])
            magnitude = abs(value)
            if magnitude <= deadzone:
                out.append(0.0)
                continue
            scaled = (magnitude - deadzone) / span
            if scaled > 1.0:
                scaled = 1.0
            if exponent != 1.0:
                scaled = scaled**exponent
            if value < 0:
                scaled = -scaled
            scaled *= float(gains.get(name, 1.0)) * sensitivity
            if inverts.get(name, False):
                scaled = -scaled
            out.append(round(scaled, 5))
        return out

    # -- shaping -----------------------------------------------------------

    @staticmethod
    def axis_groups(addin):
        """Axis indices per motion group, skipping motions that do nothing.

        Built from the add-in's own map, so a recalibrated puck groups itself
        correctly. Roll is left out in turntable mode and any motion whose speed
        is zero is left out too: an axis that cannot move the camera must never
        win the dominance contest and silence the axis that can.
        """
        mapping = addin.get("map", {}) or {}
        turntable = addin.get("orbit_mode", "turntable") == "turntable"
        groups = {}
        for group, motions in MOTION_GROUPS:
            indices = []
            for motion in motions:
                if motion == "roll" and turntable:
                    continue
                try:
                    speed = float(addin.get(MOTION_SPEED_KEY[motion], 0.0))
                except (TypeError, ValueError):
                    speed = 0.0
                if speed == 0.0:
                    continue
                axis = mapping.get(motion)
                if axis in AXES:
                    index = AXES.index(axis)
                    if index not in indices:
                        indices.append(index)
            if indices:
                groups[group] = indices
        return groups

    def _gate(self, values, groups, response):
        """Keep one motion group, and inside it only the axes that carry it."""
        if not groups:
            self._dominant = None
            return list(values)
        out = [0.0] * 6
        strength = dict(
            (name, max(abs(values[i]) for i in indices))
            for name, indices in groups.items()
        )
        if max(strength.values()) <= 0.0:
            self._dominant = None
            return out
        if response.get("dominant_group", True):
            winner = max(strength, key=lambda name: (strength[name], name))
            previous = self._dominant
            try:
                hysteresis = float(response.get("dominant_hysteresis", 1.25))
            except (TypeError, ValueError):
                hysteresis = 1.25
            if hysteresis < 1.0:
                hysteresis = 1.0
            if (
                previous is not None
                and previous in strength
                and previous != winner
                and strength[previous] > 0.0
                and strength[winner] < strength[previous] * hysteresis
            ):
                winner = previous
            self._dominant = winner
            winners = [winner]
        else:
            self._dominant = None
            winners = list(groups)
        try:
            cut = float(response.get("axis_cut", 0.1))
        except (TypeError, ValueError):
            cut = 0.1
        for name in winners:
            top = strength[name]
            if top <= 0.0:
                continue
            for index in groups[name]:
                if abs(values[index]) >= cut * top:
                    out[index] = values[index]
        return out

    def _smooth(self, values, response, dt):
        """Exponential smoothing, so the puck's own jitter is not a camera move."""
        try:
            tau = float(response.get("smoothing_seconds", 0.05))
        except (TypeError, ValueError):
            tau = 0.05
        if tau <= 0.0:
            self._ema = list(values)
            return list(values)
        alpha = 1.0 - math.exp(-dt / tau)
        out = []
        for index in range(6):
            level = self._ema[index] + (values[index] - self._ema[index]) * alpha
            # An exponential never actually reaches zero, and a stream that
            # never goes quiet would keep the add-in's burst open forever.
            if values[index] == 0.0 and abs(level) < 1e-3:
                level = 0.0
            self._ema[index] = level
            out.append(round(level, 5))
        return out

    def shape(self, raw, cfg, addin, dt):
        """Raw counts to the values that go on the wire."""
        values = self._normalise(raw, cfg)
        response = cfg.get("response", {}) or {}
        gated = self._gate(values, self.axis_groups(addin), response)
        smoothed = self._smooth(gated, response, dt)
        if any(value != 0.0 for value in values):
            # While the puck is deflected, nothing outside the winning group
            # leaves the daemon, not even a smoothing tail. Going from an orbit
            # straight into a pan has to stop the orbit, not blend the two, and
            # the tail has to be forgotten rather than surface again on release.
            for index in range(6):
                if gated[index] == 0.0:
                    self._ema[index] = 0.0
                    smoothed[index] = 0.0
        return smoothed

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
        if self.replay_wait:
            # Fusion's add-in reconnects with a backoff of up to five seconds,
            # so without this the first bursts of a capture play to nobody.
            deadline = time.monotonic() + 60.0
            while self.running and self.hub.count() == 0:
                if time.monotonic() > deadline:
                    log("replay: no client after 60 s, playing anyway")
                    break
                time.sleep(0.25)
            if self.hub.count():
                log("replay: client is listening, starting in 1 s")
                time.sleep(1.0)
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
            values = self.shape(raw, cfg, self.config.section("addin"), dt)
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
        "--replay-wait",
        action="store_true",
        help="hold the replay until a client (the Fusion add-in) has connected",
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
    bridge = Bridge(
        config,
        replay=args.replay,
        replay_loop=args.replay_loop,
        replay_wait=args.replay_wait,
    )
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
