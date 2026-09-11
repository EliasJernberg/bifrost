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
    "orbit_speed": 2.5,
    "pan_speed": 1.0,
    "zoom_speed": 1.2,
    "roll_speed": 0.0,
    "orbit_mode": "turntable",
    "world_up": [0.0, 0.0, 1.0],
    "pitch_limit_deg": 2.0,
    "min_distance": 0.01,
    "fit_button": "first",
    "selftest": False,
    "selftest_seconds": 3.0,
    "selftest_wait_seconds": 600.0,
    "selftest_image_dir": "",
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
            "config applied: orbit=%s pan=%s zoom=%s mode=%s map=%s"
            % (
                merged.get("orbit_speed"),
                merged.get("pan_speed"),
                merged.get("zoom_speed"),
                merged.get("orbit_mode"),
                merged.get("map"),
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
            self.log.debug("ping")

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
            fire = False
            with self.lock:
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

    def wait_for_viewport(self, timeout):
        """Block until a design with a usable camera is open, or give up."""
        deadline = time.time() + timeout
        while time.time() < deadline and self.running:
            try:
                viewport = self.app.activeViewport
                if viewport is not None:
                    camera = viewport.camera
                    offset = v_sub(
                        (camera.eye.x, camera.eye.y, camera.eye.z),
                        (camera.target.x, camera.target.y, camera.target.z),
                    )
                    if v_len(offset) > 1e-9:
                        return True
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def selftest_loop(self):
        seconds = float(self.config.get("selftest_seconds", 3.0))
        timeout = float(self.config.get("selftest_wait_seconds", 600.0))
        self.log.info("selftest: waiting up to %.0f s for an open design" % timeout)
        if not self.wait_for_viewport(timeout):
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

    def apply(self):
        with self.lock:
            acc = self.acc
            self.acc = [0.0] * 6
            extra_yaw = self.extra_yaw
            self.extra_yaw = 0.0
            buttons = self.buttons
            self.buttons = []
            shots = self.shot_queue
            self.shot_queue = []

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
        if not moved and not buttons and not shots:
            return

        try:
            viewport = self.app.activeViewport
        except Exception:
            return
        if viewport is None:
            return

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

        forward = v_norm(v_scale(offset, -1.0))
        right = v_norm(v_cross(forward, up))
        if right == (0.0, 0.0, 0.0):
            right = (1.0, 0.0, 0.0)
        up = v_norm(v_cross(right, forward))

        # Orbit. Pitch always turns around the camera's right vector. Yaw turns
        # around the world up axis in turntable mode, around the camera up
        # vector in free mode.
        if abs(pitch) > 1e-9:
            new_offset = v_rotate(offset, right, pitch)
            new_up = v_rotate(up, right, pitch)
            if config.get("orbit_mode", "turntable") == "turntable":
                world_up = v_norm(tuple(config.get("world_up", [0.0, 0.0, 1.0])))
                limit = math.radians(float(config.get("pitch_limit_deg", 2.0)))
                angle = math.acos(
                    max(-1.0, min(1.0, v_dot(v_norm(new_offset), world_up)))
                )
                if angle < limit or angle > math.pi - limit:
                    new_offset = offset
                    new_up = up
            offset = new_offset
            up = new_up

        if abs(yaw) > 1e-9:
            if config.get("orbit_mode", "turntable") == "turntable":
                yaw_axis = v_norm(tuple(config.get("world_up", [0.0, 0.0, 1.0])))
            else:
                yaw_axis = up
            offset = v_rotate(offset, yaw_axis, yaw)
            up = v_rotate(up, yaw_axis, yaw)

        if abs(roll) > 1e-9:
            forward = v_norm(v_scale(offset, -1.0))
            up = v_rotate(up, forward, roll)

        forward = v_norm(v_scale(offset, -1.0))
        right = v_norm(v_cross(forward, up))
        up = v_norm(v_cross(right, forward))
        distance = v_len(offset)

        is_ortho = False
        try:
            is_ortho = camera.cameraType == adsk.core.CameraTypes.OrthographicCameraType
        except Exception:
            pass

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
        # much world the viewport currently shows.
        if abs(pan_x) > 1e-9 or abs(pan_y) > 1e-9:
            delta = v_add(v_scale(right, -pan_x * scale), v_scale(up, -pan_y * scale))
            eye = v_add(eye, delta)
            target = v_add(target, delta)

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
