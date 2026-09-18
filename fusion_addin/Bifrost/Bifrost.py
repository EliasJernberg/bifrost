# Bifrost: SpaceMouse navigation for Autodesk Fusion inside a Wine prefix.
#
# The add-in keeps a background thread connected to the Bifrost daemon running
# natively on Linux (TCP 127.0.0.1:47653, newline delimited JSON). The thread
# only accumulates deltas under a lock. All camera work happens on Fusion's main
# thread, inside a custom event handler, because the Fusion API is not thread
# safe.
#
# Tuning lives in ~/.config/bifrost/config.json on the Linux side and arrives in
# the daemon's hello frame, so nothing inside the Wine prefix has to be edited.

import json
import math
import os
import socket
import tempfile
import threading
import time
import traceback

import adsk.core

CUSTOM_EVENT_ID = "BifrostMotionEvent"
AXES = ("x", "y", "z", "rx", "ry", "rz")

DEFAULT_ADDIN_CONFIG = {
    "max_fire_hz": 30,
    "map": {
        "pan_x": "x",
        "pan_y": "z",
        "dolly": "y",
        "pitch": "rx",
        "yaw": "ry",
        "roll": "rz",
    },
    "invert": {
        "pan_x": False,
        "pan_y": False,
        "dolly": True,
        "pitch": False,
        "yaw": True,
        "roll": False,
    },
    "orbit_speed": 2.5,
    "pan_speed": 1.0,
    "zoom_speed": 1.2,
    "roll_speed": 0.0,
    "orbit_mode": "turntable",
    "orbit_pivot": "auto",
    "idle_gap_seconds": 0.5,
    "world_up": [0.0, 0.0, 1.0],
    "pitch_limit_deg": 2.0,
    "min_distance": 0.01,
    "fit_button": "first",
    "selftest": False,
    "selftest_seconds": 3.0,
    "selftest_pan": 0.0,
    "selftest_wait_seconds": 600.0,
    "selftest_image_dir": "",
    "selftest_settle_seconds": 3.0,
    "log_path": "",
    "log_level": "info",
}

_state = None


# ---------------------------------------------------------------- logging ---


class Logger(object):
    LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

    def __init__(self, path=None, level="info"):
        if not path:
            path = os.path.join(tempfile.gettempdir(), "bifrost.log")
        self.path = path
        self.level = self.LEVELS.get(level, 20)
        self._lock = threading.Lock()

    def set_level(self, level):
        self.level = self.LEVELS.get(level, 20)

    def write(self, level, message):
        if self.LEVELS.get(level, 20) < self.level:
            return
        line = "%s %-5s %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"),
            level.upper(),
            message,
        )
        try:
            with self._lock:
                folder = os.path.dirname(self.path)
                if folder and not os.path.isdir(folder):
                    os.makedirs(folder)
                with open(self.path, "a") as handle:
                    handle.write(line)
        except Exception:
            pass

    def debug(self, message):
        self.write("debug", message)

    def info(self, message):
        self.write("info", message)

    def warn(self, message):
        self.write("warn", message)

    def error(self, message):
        self.write("error", message)


# ------------------------------------------------------------ vector math ---


def v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def v_len(a):
    return math.sqrt(v_dot(a, a))


def v_norm(a):
    length = v_len(a)
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (a[0] / length, a[1] / length, a[2] / length)


def v_rotate(v, axis, angle):
    """Rodrigues rotation of v around the unit vector axis by angle radians."""
    if abs(angle) < 1e-12:
        return v
    axis = v_norm(axis)
    if axis == (0.0, 0.0, 0.0):
        return v
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    return v_add(
        v_add(v_scale(v, cos_a), v_scale(v_cross(axis, v), sin_a)),
        v_scale(axis, v_dot(axis, v) * (1.0 - cos_a)),
    )


# --------------------------------------------------------------- add-in ----


class BifrostState(object):
    def __init__(self):
        self.app = adsk.core.Application.get()
        self.log = Logger()
        self.config = dict(DEFAULT_ADDIN_CONFIG)
        self.running = True
        self.connected = False

        self.lock = threading.Lock()
        self.acc = [0.0] * 6  # integral of normalised input, per axis
        self.extra_yaw = 0.0  # radians injected by the self test
        self.buttons = []  # pending (bnum, pressed)
        self.pending = False

        # A burst is one continuous movement: it opens on the first input after
        # a quiet gap and closes once the puck has been still for
        # idle_gap_seconds. The pivot is chosen once per burst, and the debug
        # log gets one before line and one after line per burst, which is what
        # makes the axis matrix measurable.
        self.burst_open = False
        self.burst_started_at = 0.0
        self.last_motion_at = 0.0
        self.burst_input = [0.0] * 6
        self.burst_start_cam = None
        self.close_burst_requested = False

        self.pivot = None
        self.pivot_valid = False
        self.pivot_source = "target"

        self.custom_event = None
        self.handler = None
        self.threads = []

        self.fit_button = None
        self.camera_sets = 0
        self.fires = 0
        self.in_flight = False
        self.fired_at = 0.0
        self.shot_queue = []
        self._rate_window_start = time.time()
        self._rate_window_sets = 0
        self._logged_view_scale = False

    # -- config --------------------------------------------------------

    def apply_config(self, incoming):
        if not isinstance(incoming, dict):
            return
        merged = dict(DEFAULT_ADDIN_CONFIG)
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                sub = dict(merged[key])
                sub.update(value)
                merged[key] = sub
            else:
                merged[key] = value
        self.config = merged
        if merged.get("log_path"):
            self.log.path = merged["log_path"]
        self.log.set_level(merged.get("log_level", "info"))
        fit = merged.get("fit_button", "first")
        if isinstance(fit, int) and fit >= 0:
            self.fit_button = fit
        elif fit == "first":
            pass
        else:
            self.fit_button = -1
        self.log.info(
            "config applied: orbit=%s pan=%s zoom=%s mode=%s pivot=%s map=%s invert=%s"
            % (
                merged.get("orbit_speed"),
                merged.get("pan_speed"),
                merged.get("zoom_speed"),
                merged.get("orbit_mode"),
                merged.get("orbit_pivot"),
                merged.get("map"),
                merged.get("invert"),
            )
        )

    # -- network thread ------------------------------------------------

    def reader_loop(self):
        host = "127.0.0.1"
        port = 47653
        backoff = 1.0
        while self.running:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((host, port))
                sock.settimeout(1.0)
                self.connected = True
                backoff = 1.0
                self.log.info("connected to daemon %s:%d" % (host, port))
                buffer = b""
                while self.running:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise IOError("daemon closed the connection")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.strip():
                            self.handle_line(line)
            except Exception as exc:
                if self.running:
                    self.log.warn(
                        "daemon link down (%s), retry in %.0f s" % (exc, backoff)
                    )
            finally:
                self.connected = False
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                with self.lock:
                    self.acc = [0.0] * 6
            if self.running:
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 5.0)

    def handle_line(self, raw):
        try:
            message = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        kind = message.get("t")
        if kind == "m":
            values = message.get("v") or []
            dt = float(message.get("dt", 0.0))
            if len(values) < 6 or dt <= 0.0:
                return
            with self.lock:
                for index in range(6):
                    self.acc[index] += float(values[index]) * dt
                self.pending = True
        elif kind == "b":
            bnum = int(message.get("n", -1))
            pressed = int(message.get("p", 0))
            if self.fit_button is None and pressed:
                self.fit_button = bnum
                self.log.info("learned fit button: %d" % bnum)
            with self.lock:
                self.buttons.append((bnum, pressed))
                self.pending = True
        elif kind == "hello":
            self.apply_config(message.get("addin"))
        elif kind == "ping":
            # Keepalive. Deliberately not logged: debug level is there to show
            # what each movement did to the camera, and a line every five
            # seconds would bury exactly that.
            pass

    # -- pacing thread -------------------------------------------------

    def pacer_loop(self):
        """Fire the custom event, but never faster than Fusion can drain it.

        Fusion delivers custom events on its own main loop, and each camera
        update forces a redraw. On a real assembly that settles somewhere around
        10 Hz, well under max_fire_hz. Firing regardless would just pile up a
        queue and add latency, so a new event only goes out once the previous
        one has been handled. Nothing is lost either way: the handler drains the
        whole accumulator, so a slower rate means bigger steps, not slower
        motion.
        """
        while self.running:
            hz = float(self.config.get("max_fire_hz", 30)) or 30.0
            period = 1.0 / hz
            idle_gap = float(self.config.get("idle_gap_seconds", 0.5))
            fire = False
            with self.lock:
                # A burst that has gone quiet needs one more trip through the
                # main thread to read the camera it ended on.
                if (
                    self.burst_open
                    and not self.pending
                    and not self.close_burst_requested
                    and (time.time() - self.last_motion_at) >= idle_gap
                ):
                    self.close_burst_requested = True
                    self.pending = True
                busy = self.in_flight and (time.time() - self.fired_at) < 1.0
                if (self.pending or self.extra_yaw != 0.0) and not busy:
                    self.pending = False
                    self.in_flight = True
                    self.fired_at = time.time()
                    fire = True
            if fire:
                try:
                    self.app.fireCustomEvent(CUSTOM_EVENT_ID, "")
                    self.fires += 1
                except Exception as exc:
                    with self.lock:
                        self.in_flight = False
                    self.log.error("fireCustomEvent failed: %s" % exc)
            time.sleep(period)

    # -- self test -----------------------------------------------------

    def camera_state(self):
        """A comparable snapshot of the current camera, or None."""
        try:
            viewport = self.app.activeViewport
            if viewport is None:
                return None
            camera = viewport.camera
            eye = (camera.eye.x, camera.eye.y, camera.eye.z)
            target = (camera.target.x, camera.target.y, camera.target.z)
            if v_len(v_sub(eye, target)) <= 1e-9:
                return None
            return (eye, target, round(camera.viewExtents, 6))
        except Exception:
            return None

    def wait_for_viewport(self, timeout, settle=3.0):
        """Block until a design is open and its camera has stopped moving.

        A camera exists a good while before the document has finished loading,
        and Fusion fits the view once it has. Starting the self test on the
        half-loaded view would measure the wrong thing, so wait until the same
        camera comes back unchanged for `settle` seconds.
        """
        deadline = time.time() + timeout
        last = None
        stable_since = None
        while time.time() < deadline and self.running:
            state = self.camera_state()
            if state is not None:
                if state == last:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= settle:
                        return True
                else:
                    last = state
                    stable_since = None
            time.sleep(0.5)
        return False

    def selftest_loop(self):
        seconds = float(self.config.get("selftest_seconds", 3.0))
        timeout = float(self.config.get("selftest_wait_seconds", 600.0))
        settle = float(self.config.get("selftest_settle_seconds", 3.0))
        self.log.info(
            "selftest: waiting up to %.0f s for a design whose camera has been "
            "still for %.1f s" % (timeout, settle)
        )
        if not self.wait_for_viewport(timeout, settle):
            self.log.warn("selftest: no viewport appeared, skipping")
            return
        camera = self.app.activeViewport.camera
        self.log.info(
            "selftest: start eye=(%.3f, %.3f, %.3f) target=(%.3f, %.3f, %.3f) "
            "up=(%.3f, %.3f, %.3f) extents=%.4f ortho=%s"
            % (
                camera.eye.x,
                camera.eye.y,
                camera.eye.z,
                camera.target.x,
                camera.target.y,
                camera.target.z,
                camera.upVector.x,
                camera.upVector.y,
                camera.upVector.z,
                camera.viewExtents,
                camera.cameraType == adsk.core.CameraTypes.OrthographicCameraType,
            )
        )
        pan = float(self.config.get("selftest_pan", 0.0))
        if abs(pan) > 1e-9:
            # Shove the model off to one side first. With orbit_pivot "auto" the
            # orbit that follows still has to turn around the model, which is
            # exactly what the rendered frames are there to show.
            axis = self.config.get("map", {}).get("pan_x")
            index = AXES.index(axis) if axis in AXES else 0
            speed = float(self.config.get("pan_speed", 1.0)) or 1.0
            amount = pan / speed
            if self.config.get("invert", {}).get("pan_x", False):
                amount = -amount
            self.log.info("selftest: panning %.2f viewport widths first" % pan)
            for _ in range(20):
                with self.lock:
                    self.acc[index] += amount / 20.0
                    self.pending = True
                time.sleep(0.05)
            time.sleep(1.0)
            camera = self.app.activeViewport.camera
            self.log.info(
                "selftest: after pan eye=(%.3f, %.3f, %.3f) target=(%.3f, %.3f, %.3f)"
                % (
                    camera.eye.x,
                    camera.eye.y,
                    camera.eye.z,
                    camera.target.x,
                    camera.target.y,
                    camera.target.z,
                )
            )

        self.log.info("selftest: scripted 360 degree orbit over %.1f s" % seconds)
        steps = max(1, int(seconds * 30))
        per_step = (2.0 * math.pi) / steps
        shot_dir = self.config.get("selftest_image_dir") or os.path.dirname(
            self.log.path
        )
        shot_at = dict(
            (max(0, int(steps * fraction / 4.0) - 1), fraction)
            for fraction in (1, 2, 3, 4)
        )
        self.request_shot(os.path.join(shot_dir, "bifrost-selftest-0deg.png"))
        start = time.time()
        sets_before = self.camera_sets
        for index in range(steps):
            if not self.running:
                return
            with self.lock:
                self.extra_yaw += per_step
                self.pending = True
            if index in shot_at:
                self.request_shot(
                    os.path.join(
                        shot_dir,
                        "bifrost-selftest-%ddeg.png" % (shot_at[index] * 90),
                    )
                )
            time.sleep(seconds / steps)
        time.sleep(0.5)
        elapsed = time.time() - start
        sets = self.camera_sets - sets_before
        camera = self.app.activeViewport.camera
        self.log.info(
            "selftest: end   eye=(%.3f, %.3f, %.3f) target=(%.3f, %.3f, %.3f) "
            "up=(%.3f, %.3f, %.3f)"
            % (
                camera.eye.x,
                camera.eye.y,
                camera.eye.z,
                camera.target.x,
                camera.target.y,
                camera.target.z,
                camera.upVector.x,
                camera.upVector.y,
                camera.upVector.z,
            )
        )
        self.log.info(
            "selftest: done in %.2f s, %d camera updates, %.1f camera sets/s"
            % (elapsed, sets, sets / elapsed if elapsed > 0 else 0.0)
        )

    # -- camera --------------------------------------------------------

    def view_scale(self, viewport, camera, distance):
        """World-space width of the viewport, used to scale panning."""
        try:
            width = viewport.width
            height = viewport.height
            if width > 1 and height > 1:
                left = viewport.viewToModelSpace(
                    adsk.core.Point2D.create(0.0, height / 2.0)
                )
                right = viewport.viewToModelSpace(
                    adsk.core.Point2D.create(float(width), height / 2.0)
                )
                span = v_len(
                    v_sub((right.x, right.y, right.z), (left.x, left.y, left.z))
                )
                if span > 1e-9:
                    if not self._logged_view_scale:
                        self._logged_view_scale = True
                        self.log.info(
                            "view scale: viewport %dx%d, world width %.4f cm, "
                            "eye-target %.4f cm, viewExtents %.6f"
                            % (width, height, span, distance, camera.viewExtents)
                        )
                    return span
        except Exception as exc:
            self.log.debug("viewToModelSpace unavailable (%s), using distance" % exc)
        return max(distance, 1e-6)

    def request_shot(self, path):
        """Ask the main thread to render the viewport to a file.

        Used by the self test. Saving has to happen on Fusion's own thread, so
        the request is queued and picked up by the next apply().
        """
        with self.lock:
            self.shot_queue.append(path)
            self.pending = True

    def take_shots(self, viewport, shots):
        for path in shots:
            try:
                ok = viewport.saveAsImageFile(path, 1200, 800)
                self.log.info("selftest: saved %s (%s)" % (path, ok))
            except Exception as exc:
                self.log.warn("selftest: could not save %s: %s" % (path, exc))

    # -- pivot ---------------------------------------------------------

    @staticmethod
    def box_centre(box):
        """Centre of an adsk BoundingBox3D, or None if it is unusable."""
        try:
            low = box.minPoint
            high = box.maxPoint
            centre = (
                (low.x + high.x) / 2.0,
                (low.y + high.y) / 2.0,
                (low.z + high.z) / 2.0,
            )
        except Exception:
            return None
        for value in centre:
            if value != value or abs(value) > 1e12:  # NaN or nonsense
                return None
        return centre

    def visible_centre(self, root):
        """Centre of what is actually visible in the root component.

        Component.boundingBox is one native call, but it covers everything the
        design holds, hidden bodies and switched off occurrences included. On a
        design where something invisible sits far from the part being worked on,
        that box centre is nowhere near what is on screen, and orbiting around
        it throws the model off the view. So the visible items are unioned by
        hand, and the whole design box is only the fallback.
        """
        low = None
        high = None
        count = 0
        for collection_name in ("bRepBodies", "meshBodies", "occurrences"):
            try:
                collection = getattr(root, collection_name)
            except Exception:
                continue
            try:
                total = collection.count
            except Exception:
                continue
            for index in range(min(total, 500)):
                try:
                    item = collection.item(index)
                    if hasattr(item, "isVisible") and not item.isVisible:
                        continue
                    if hasattr(item, "isLightBulbOn") and not item.isLightBulbOn:
                        continue
                    box = item.boundingBox
                    if box is None:
                        continue
                    mins = (box.minPoint.x, box.minPoint.y, box.minPoint.z)
                    maxs = (box.maxPoint.x, box.maxPoint.y, box.maxPoint.z)
                except Exception:
                    continue
                count += 1
                if low is None:
                    low, high = list(mins), list(maxs)
                else:
                    for axis in range(3):
                        low[axis] = min(low[axis], mins[axis])
                        high[axis] = max(high[axis], maxs[axis])
        if low is None:
            return None
        self.log.debug(
            "pivot: unioned %d visible items, box (%.2f, %.2f, %.2f) to "
            "(%.2f, %.2f, %.2f)"
            % (count, low[0], low[1], low[2], high[0], high[1], high[2])
        )
        return tuple((low[axis] + high[axis]) / 2.0 for axis in range(3))

    def model_centre(self):
        """Centre of the visible model's bounding box, in world coordinates.

        None when no design is open, when it holds nothing visible, or when the
        API refuses the call. The caller then falls back to the camera target,
        which is what every version before orbit_pivot did.
        """
        try:
            product = self.app.activeProduct
        except Exception as exc:
            self.log.debug("pivot: activeProduct unavailable (%s)" % exc)
            return None
        if product is None:
            self.log.debug("pivot: no active product, using target")
            return None
        root = None
        try:
            root = product.rootComponent
        except Exception as exc:
            self.log.debug("pivot: no rootComponent (%s), using target" % exc)
            return None
        if root is None:
            return None
        started = time.time()
        centre = self.visible_centre(root)
        source = "visible"
        if centre is None:
            # Nothing visible to union, or the collections were not there. Fall
            # back to the whole design's box, which at least exists.
            source = "whole design"
            try:
                centre = self.box_centre(root.boundingBox)
            except Exception as exc:
                self.log.debug("pivot: rootComponent.boundingBox failed (%s)" % exc)
        if centre is None:
            self.log.debug("pivot: nothing to centre on, using target")
            return None
        self.log.debug(
            "pivot: model centre (%.3f, %.3f, %.3f) from the %s in %.0f ms"
            % (
                centre[0],
                centre[1],
                centre[2],
                source,
                (time.time() - started) * 1000.0,
            )
        )
        return centre

    def resolve_pivot(self, target):
        """Pivot for this burst's orbit. Computed once, then reused."""
        if self.config.get("orbit_pivot", "auto") != "auto":
            return target, "target"
        if not self.pivot_valid:
            centre = self.model_centre()
            if centre is None:
                self.pivot = target
                self.pivot_source = "target"
            else:
                self.pivot = centre
                self.pivot_source = "model"
            self.pivot_valid = True
        return self.pivot, self.pivot_source

    # -- bursts --------------------------------------------------------

    @staticmethod
    def fmt_vec(vector):
        return "(%.3f, %.3f, %.3f)" % (vector[0], vector[1], vector[2])

    def open_burst(self, eye, target, up, extents, is_ortho):
        self.burst_start_cam = (eye, target, up, extents)
        self.pivot_valid = False
        self.log.debug(
            "burst start eye=%s target=%s up=%s dist=%.4f extents=%.4f ortho=%s"
            % (
                self.fmt_vec(eye),
                self.fmt_vec(target),
                self.fmt_vec(up),
                v_len(v_sub(eye, target)),
                extents,
                is_ortho,
            )
        )

    def close_burst(self, viewport):
        """Log where the camera ended up, once the puck has gone still."""
        start = self.burst_start_cam
        self.burst_open = False
        self.burst_start_cam = None
        with self.lock:
            burst_input = list(self.burst_input)
            self.burst_input = [0.0] * 6
        if start is None:
            return
        try:
            camera = viewport.camera
            eye = (camera.eye.x, camera.eye.y, camera.eye.z)
            target = (camera.target.x, camera.target.y, camera.target.z)
            up = (camera.upVector.x, camera.upVector.y, camera.upVector.z)
            extents = camera.viewExtents
        except Exception as exc:
            self.log.debug("burst end: camera unreadable (%s)" % exc)
            return
        self.log.debug(
            "burst end   eye=%s target=%s up=%s dist=%.4f extents=%.4f "
            "input=[%s] d_eye=%s d_target=%s d_dist=%+.4f d_extents=%+.4f "
            "pivot=%s"
            % (
                self.fmt_vec(eye),
                self.fmt_vec(target),
                self.fmt_vec(up),
                v_len(v_sub(eye, target)),
                extents,
                ", ".join("%s=%+.4f" % (AXES[i], burst_input[i]) for i in range(6)),
                self.fmt_vec(v_sub(eye, start[0])),
                self.fmt_vec(v_sub(target, start[1])),
                v_len(v_sub(eye, target)) - v_len(v_sub(start[0], start[1])),
                extents - start[3],
                self.pivot_source if self.pivot_valid else "none",
            )
        )

    def apply(self):
        with self.lock:
            acc = self.acc
            self.acc = [0.0] * 6
            extra_yaw = self.extra_yaw
            self.extra_yaw = 0.0
            buttons = self.buttons
            self.buttons = []
            shots = self.shot_queue[:1]
            self.shot_queue = self.shot_queue[1:]
            if self.shot_queue:
                self.pending = True
            closing = self.close_burst_requested
            self.close_burst_requested = False

        config = self.config
        mapping = config.get("map", {})
        invert = config.get("invert", {})

        def pick(name):
            axis = mapping.get(name)
            if axis not in AXES:
                return 0.0
            value = acc[AXES.index(axis)]
            if invert.get(name, False):
                value = -value
            return value

        orbit_speed = float(config.get("orbit_speed", 2.5))
        pan_speed = float(config.get("pan_speed", 1.0))
        zoom_speed = float(config.get("zoom_speed", 1.2))
        roll_speed = float(config.get("roll_speed", 0.0))

        yaw = pick("yaw") * orbit_speed + extra_yaw
        pitch = pick("pitch") * orbit_speed
        roll = pick("roll") * roll_speed
        pan_x = pick("pan_x") * pan_speed
        pan_y = pick("pan_y") * pan_speed
        dolly = pick("dolly") * zoom_speed

        moved = any(abs(v) > 1e-9 for v in (yaw, pitch, roll, pan_x, pan_y, dolly))
        if not moved and not buttons and not shots and not closing:
            return

        try:
            viewport = self.app.activeViewport
        except Exception:
            viewport = None
        if viewport is None:
            # No document, so nothing to read the burst off. Close it anyway,
            # or the pacer keeps asking for a camera that is not there.
            self.burst_open = False
            self.burst_start_cam = None
            return

        if closing and not moved:
            self.close_burst(viewport)

        if buttons:
            for bnum, pressed in buttons:
                if pressed and self.fit_button is not None and bnum == self.fit_button:
                    try:
                        viewport.fit()
                        self.log.info("button %d: viewport.fit()" % bnum)
                    except Exception as exc:
                        self.log.warn("fit failed: %s" % exc)
                elif pressed:
                    self.log.debug("button %d pressed, unmapped" % bnum)
        if not moved:
            self.take_shots(viewport, shots)
            return

        camera = viewport.camera
        eye = (camera.eye.x, camera.eye.y, camera.eye.z)
        target = (camera.target.x, camera.target.y, camera.target.z)
        up = (camera.upVector.x, camera.upVector.y, camera.upVector.z)

        offset = v_sub(eye, target)
        distance = v_len(offset)
        if distance < 1e-9:
            return

        is_ortho = False
        try:
            is_ortho = camera.cameraType == adsk.core.CameraTypes.OrthographicCameraType
        except Exception:
            pass

        # Burst bookkeeping. The first input after a quiet gap opens a burst,
        # which is also when the orbit pivot is allowed to move.
        now = time.time()
        idle_gap = float(config.get("idle_gap_seconds", 0.5))
        with self.lock:
            fresh = (not self.burst_open) or (now - self.last_motion_at) >= idle_gap
            self.last_motion_at = now
            self.burst_open = True
            if fresh:
                self.burst_input = [0.0] * 6
                self.burst_started_at = now
            for index in range(6):
                self.burst_input[index] += acc[index]
        if fresh:
            self.open_burst(eye, target, up, camera.viewExtents, is_ortho)

        forward = v_norm(v_scale(offset, -1.0))
        right = v_norm(v_cross(forward, up))
        if right == (0.0, 0.0, 0.0):
            right = (1.0, 0.0, 0.0)
        up = v_norm(v_cross(right, forward))

        # Orbit. Pitch always turns around the camera's right vector. Yaw turns
        # around the world up axis in turntable mode, around the camera up
        # vector in free mode. Both turn eye and target around the pivot, which
        # is the camera target in "target" mode and the centre of the model in
        # "auto" mode, so the model stays put even when it sits off target.
        pivot = target
        if abs(yaw) > 1e-9 or abs(pitch) > 1e-9:
            pivot, _source = self.resolve_pivot(target)
        eye_off = v_sub(eye, pivot)
        target_off = v_sub(target, pivot)

        if abs(pitch) > 1e-9:
            new_eye_off = v_rotate(eye_off, right, pitch)
            new_target_off = v_rotate(target_off, right, pitch)
            new_up = v_rotate(up, right, pitch)
            if config.get("orbit_mode", "turntable") == "turntable":
                world_up = v_norm(tuple(config.get("world_up", [0.0, 0.0, 1.0])))
                limit = math.radians(float(config.get("pitch_limit_deg", 2.0)))
                angle = math.acos(
                    max(
                        -1.0,
                        min(
                            1.0,
                            v_dot(v_norm(v_sub(new_eye_off, new_target_off)), world_up),
                        ),
                    )
                )
                if angle < limit or angle > math.pi - limit:
                    new_eye_off = eye_off
                    new_target_off = target_off
                    new_up = up
            eye_off = new_eye_off
            target_off = new_target_off
            up = new_up

        if abs(yaw) > 1e-9:
            if config.get("orbit_mode", "turntable") == "turntable":
                yaw_axis = v_norm(tuple(config.get("world_up", [0.0, 0.0, 1.0])))
            else:
                yaw_axis = up
            eye_off = v_rotate(eye_off, yaw_axis, yaw)
            target_off = v_rotate(target_off, yaw_axis, yaw)
            up = v_rotate(up, yaw_axis, yaw)

        eye = v_add(pivot, eye_off)
        target = v_add(pivot, target_off)
        offset = v_sub(eye, target)

        if abs(roll) > 1e-9:
            forward = v_norm(v_scale(offset, -1.0))
            up = v_rotate(up, forward, roll)

        forward = v_norm(v_scale(offset, -1.0))
        right = v_norm(v_cross(forward, up))
        up = v_norm(v_cross(right, forward))
        distance = v_len(offset)

        scale = self.view_scale(viewport, camera, distance)

        # Zoom. A perspective camera dollies along the view direction, an
        # orthographic one changes viewExtents, which is what actually controls
        # its zoom level.
        if abs(dolly) > 1e-9:
            factor = math.exp(-dolly)
            if is_ortho:
                try:
                    extents = camera.viewExtents
                    if extents > 0:
                        camera.viewExtents = max(extents * factor, 1e-9)
                except Exception as exc:
                    self.log.debug("viewExtents update failed: %s" % exc)
            else:
                min_distance = float(config.get("min_distance", 0.01))
                new_distance = max(distance * factor, min_distance)
                offset = v_scale(v_norm(offset), new_distance)
                distance = new_distance

        eye = v_add(target, offset)

        # Pan. Both eye and target slide along the camera plane, scaled by how
        # much world the viewport currently shows. The camera moves against the
        # input, which is what makes the model follow the puck on screen. An
        # auto pivot rides along, so orbiting after a pan still turns around the
        # same point of the model.
        if abs(pan_x) > 1e-9 or abs(pan_y) > 1e-9:
            delta = v_add(v_scale(right, -pan_x * scale), v_scale(up, -pan_y * scale))
            eye = v_add(eye, delta)
            target = v_add(target, delta)
            if self.pivot_valid and self.pivot is not None:
                self.pivot = v_add(self.pivot, delta)

        try:
            camera.eye = adsk.core.Point3D.create(eye[0], eye[1], eye[2])
            camera.target = adsk.core.Point3D.create(target[0], target[1], target[2])
            camera.upVector = adsk.core.Vector3D.create(up[0], up[1], up[2])
            camera.isSmoothTransition = False
            viewport.camera = camera
            self.camera_sets += 1
            self._rate_window_sets += 1
        except Exception as exc:
            self.log.warn("camera update rejected: %s" % exc)
            return

        self.take_shots(viewport, shots)

        now = time.time()
        if now - self._rate_window_start >= 1.0:
            elapsed = now - self._rate_window_start
            if self._rate_window_sets >= 3:
                self.log.info(
                    "camera update rate: %.1f Hz (%d sets in %.2f s), "
                    "eye=(%.3f, %.3f, %.3f) dist=%.3f extents=%.4f"
                    % (
                        self._rate_window_sets / elapsed,
                        self._rate_window_sets,
                        elapsed,
                        eye[0],
                        eye[1],
                        eye[2],
                        v_len(v_sub(eye, target)),
                        camera.viewExtents,
                    )
                )
            self._rate_window_start = now
            self._rate_window_sets = 0


class BifrostEventHandler(adsk.core.CustomEventHandler):
    def __init__(self):
        super(BifrostEventHandler, self).__init__()

    def notify(self, args):
        state = _state
        if state is None:
            return
        try:
            state.apply()
        except Exception:
            state.log.error("apply() blew up:\n%s" % traceback.format_exc())
        finally:
            # Let the pacer send the next one now that this one is done.
            with state.lock:
                state.in_flight = False


def run(context):
    global _state
    try:
        _state = BifrostState()
        _state.log.info("=" * 60)
        _state.log.info(
            "Bifrost add-in starting (pid %d, python %s)"
            % (os.getpid(), os.sys.version.split()[0])
        )

        try:
            _state.app.unregisterCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            pass
        _state.custom_event = _state.app.registerCustomEvent(CUSTOM_EVENT_ID)
        _state.handler = BifrostEventHandler()
        _state.custom_event.add(_state.handler)
        _state.log.info("custom event %s registered" % CUSTOM_EVENT_ID)

        for target, name in (
            (_state.reader_loop, "bifrost-reader"),
            (_state.pacer_loop, "bifrost-pacer"),
        ):
            thread = threading.Thread(target=target, name=name)
            thread.daemon = True
            thread.start()
            _state.threads.append(thread)

        def maybe_selftest():
            time.sleep(3.0)
            if _state.config.get("selftest"):
                _state.selftest_loop()

        thread = threading.Thread(target=maybe_selftest, name="bifrost-selftest")
        thread.daemon = True
        thread.start()
        _state.threads.append(thread)

        _state.log.info("Bifrost add-in running")
    except Exception:
        message = "Bifrost failed to start:\n%s" % traceback.format_exc()
        try:
            Logger().error(message)
        except Exception:
            pass
        try:
            adsk.core.Application.get().userInterface.messageBox(message)
        except Exception:
            pass


def stop(context):
    global _state
    state = _state
    _state = None
    if state is None:
        return
    try:
        state.running = False
        if state.custom_event is not None and state.handler is not None:
            try:
                state.custom_event.remove(state.handler)
            except Exception:
                pass
        try:
            state.app.unregisterCustomEvent(CUSTOM_EVENT_ID)
        except Exception:
            pass
        state.log.info(
            "Bifrost add-in stopped (%d camera updates total)" % state.camera_sets
        )
    except Exception:
        try:
            state.log.error("stop() failed:\n%s" % traceback.format_exc())
        except Exception:
            pass
