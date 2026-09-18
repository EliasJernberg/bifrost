#!/usr/bin/env python3
"""Exercise the add-in's camera maths outside Fusion.

Bifrost.py imports adsk.core, which only exists inside Fusion, so this test
installs a small stand-in first: Point3D, Vector3D, Point2D, CameraTypes,
CustomEventHandler and an Application whose activeViewport hands out a fake
orthographic camera. The add-in then runs against it unmodified, which lets the
orbit, pan, zoom and fit logic be checked on plain Linux.

The cross talk test goes one step further and pulls in the daemon's shaping, so
it runs raw spacenavd counts through the whole chain, exactly as the hardware
does: counts in, camera out.

    python3 tests/test_camera_math.py
"""

import math
import os
import random
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

sys.path.insert(0, os.path.join(REPO, "daemon"))
import bifrost_daemon  # noqa: E402

failures = []


def check(condition, message):
    print("  [%s] %s" % ("PASS" if condition else "FAIL", message))
    if not condition:
        failures.append(message)


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol


AXES_INDEX = dict((name, index) for index, name in enumerate(bifrost_daemon.AXES))


def axis_of(motion):
    """Index into acc[] for whatever raw axis the default map gives a motion."""
    return Bifrost.AXES.index(Bifrost.DEFAULT_ADDIN_CONFIG["map"][motion])


# -- the fake Fusion API ---------------------------------------------------


class FakePoint3D(object):
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    @staticmethod
    def create(x, y, z):
        return FakePoint3D(x, y, z)


class FakeVector3D(FakePoint3D):
    @staticmethod
    def create(x, y, z):
        return FakeVector3D(x, y, z)


class FakePoint2D(object):
    def __init__(self, x, y):
        self.x, self.y = x, y

    @staticmethod
    def create(x, y):
        return FakePoint2D(x, y)


class FakeCameraTypes(object):
    OrthographicCameraType = 0
    PerspectiveCameraType = 1


class FakeCamera(object):
    def __init__(self):
        self.eye = FakePoint3D(0.0, -100.0, 0.0)
        self.target = FakePoint3D(0.0, 0.0, 0.0)
        self.upVector = FakeVector3D(0.0, 0.0, 1.0)
        self.viewExtents = 50.0
        self.cameraType = FakeCameraTypes.OrthographicCameraType
        self.isSmoothTransition = True


class FakeViewport(object):
    def __init__(self):
        self._camera = FakeCamera()
        self.width = 1600
        self.height = 900
        self.fit_calls = 0
        self.refresh_calls = 0

    @property
    def camera(self):
        copy = FakeCamera()
        copy.eye = FakePoint3D(
            self._camera.eye.x, self._camera.eye.y, self._camera.eye.z
        )
        copy.target = FakePoint3D(
            self._camera.target.x, self._camera.target.y, self._camera.target.z
        )
        copy.upVector = FakeVector3D(
            self._camera.upVector.x, self._camera.upVector.y, self._camera.upVector.z
        )
        copy.viewExtents = self._camera.viewExtents
        copy.cameraType = self._camera.cameraType
        return copy

    @camera.setter
    def camera(self, value):
        self._camera = value

    def viewToModelSpace(self, point2d):
        """Pretend the view is 2 * sqrt(viewExtents) wide in world units."""
        half = math.sqrt(self._camera.viewExtents)
        frac = (point2d.x / float(self.width)) * 2.0 - 1.0
        return FakePoint3D(frac * half, self._camera.target.y, self._camera.target.z)

    def fit(self):
        self.fit_calls += 1

    def refresh(self):
        self.refresh_calls += 1


class FakeBoundingBox(object):
    def __init__(self, low, high):
        self.minPoint = FakePoint3D(*low)
        self.maxPoint = FakePoint3D(*high)


class FakeBody(object):
    def __init__(self, box, visible=True):
        self.boundingBox = box
        self.isVisible = visible


class FakeCollection(object):
    def __init__(self, items):
        self.items = items

    @property
    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]


class FakeComponent(object):
    def __init__(self, box, bodies=None):
        self.boundingBox = box
        if bodies is not None:
            self.bRepBodies = FakeCollection(bodies)


class FakeProduct(object):
    """Stand-in for adsk.fusion.Design, which is what activeProduct returns."""

    def __init__(self, box, bodies=None):
        self.rootComponent = FakeComponent(box, bodies)


class FakeApplication(object):
    _instance = None

    def __init__(self):
        self.activeViewport = FakeViewport()
        self.activeProduct = None
        self.fired = 0

    @staticmethod
    def get():
        if FakeApplication._instance is None:
            FakeApplication._instance = FakeApplication()
        return FakeApplication._instance

    def fireCustomEvent(self, event_id, info):
        self.fired += 1

    def registerCustomEvent(self, event_id):
        return types.SimpleNamespace(add=lambda h: None, remove=lambda h: None)

    def unregisterCustomEvent(self, event_id):
        pass


def install_fake_adsk():
    core = types.ModuleType("adsk.core")
    core.Point3D = FakePoint3D
    core.Vector3D = FakeVector3D
    core.Point2D = FakePoint2D
    core.CameraTypes = FakeCameraTypes
    core.Application = FakeApplication
    core.CustomEventHandler = object
    adsk = types.ModuleType("adsk")
    adsk.core = core
    sys.modules["adsk"] = adsk
    sys.modules["adsk.core"] = core


install_fake_adsk()
sys.path.insert(0, os.path.join(REPO, "fusion_addin", "Bifrost"))
import Bifrost  # noqa: E402


# -- tests ------------------------------------------------------------------


def test_vector_helpers():
    print("vector helpers")
    rotated = Bifrost.v_rotate((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), math.pi / 2.0)
    check(
        close(rotated[0], 0.0) and close(rotated[1], 1.0),
        "90 degree rotation around Z maps +X to +Y",
    )
    back = Bifrost.v_rotate(rotated, (0.0, 0.0, 1.0), -math.pi / 2.0)
    check(
        close(back[0], 1.0) and close(back[1], 0.0),
        "the inverse rotation gets back to the start",
    )
    check(
        close(Bifrost.v_len(Bifrost.v_norm((3.0, 4.0, 0.0))), 1.0),
        "v_norm returns a unit vector",
    )
    check(
        Bifrost.v_norm((0.0, 0.0, 0.0)) == (0.0, 0.0, 0.0),
        "v_norm survives the zero vector",
    )


def new_state():
    """Fresh add-in state on a fresh viewport, so tests cannot leak into each other."""
    Bifrost._state = None
    FakeApplication._instance = None
    state = Bifrost.BifrostState()
    state.log.path = os.path.join(HERE, "fixtures", "addin_test.log")
    state.apply_config(dict(Bifrost.DEFAULT_ADDIN_CONFIG))
    return state


def test_orbit_returns_home():
    print("orbit")
    state = new_state()
    viewport = state.app.activeViewport
    start = (viewport.camera.eye.x, viewport.camera.eye.y, viewport.camera.eye.z)
    steps = 90
    for _ in range(steps):
        state.extra_yaw = (2.0 * math.pi) / steps
        state.apply()
    end = (viewport.camera.eye.x, viewport.camera.eye.y, viewport.camera.eye.z)
    drift = Bifrost.v_len(Bifrost.v_sub(end, start))
    check(
        drift < 1e-6,
        "a full 360 degree orbit returns to the start (drift %.2e cm)" % drift,
    )
    check(
        state.camera_sets == steps,
        "one camera update per step (%d)" % state.camera_sets,
    )

    # A quarter turn from -Y must land on -X or +X depending on direction.
    state = new_state()
    viewport = state.app.activeViewport
    state.extra_yaw = math.pi / 2.0
    state.apply()
    eye = viewport.camera.eye
    radius = math.sqrt(eye.x**2 + eye.y**2)
    check(
        close(radius, 100.0, 1e-6) and abs(eye.y) < 1e-6 and abs(eye.x) > 99.0,
        "a quarter turn lands on the X axis at the same radius",
    )
    check(
        close(viewport.camera.upVector.z, 1.0, 1e-6),
        "world up is preserved through a turntable yaw",
    )


def camera_tuple(viewport):
    camera = viewport.camera
    return (
        (camera.eye.x, camera.eye.y, camera.eye.z),
        (camera.target.x, camera.target.y, camera.target.z),
        (camera.upVector.x, camera.upVector.y, camera.upVector.z),
    )


def roll_of(viewport, world_up=(0.0, 0.0, 1.0)):
    """How far off level the view is. Zero is a perfect turntable."""
    eye, target, up = camera_tuple(viewport)
    return Bifrost.roll_error(eye, target, up, world_up)


def elevation_of(viewport, world_up=(0.0, 0.0, 1.0)):
    eye, target, _up = camera_tuple(viewport)
    basis = Bifrost.basis_from_up(world_up)
    direction = Bifrost.v_norm(Bifrost.v_sub(eye, target))
    _azimuth, elevation = Bifrost.turntable_angles(direction, basis)
    return math.degrees(elevation)


def azimuth_of(viewport, world_up=(0.0, 0.0, 1.0)):
    eye, target, _up = camera_tuple(viewport)
    basis = Bifrost.basis_from_up(world_up)
    direction = Bifrost.v_norm(Bifrost.v_sub(eye, target))
    azimuth, _elevation = Bifrost.turntable_angles(direction, basis)
    return math.degrees(azimuth) if azimuth is not None else 0.0


def test_pitch_limit():
    print("pitch limit")
    limit = Bifrost.DEFAULT_ADDIN_CONFIG["pitch_limit_deg"]
    state = new_state()
    viewport = state.app.activeViewport
    for _ in range(200):
        with state.lock:
            state.acc[axis_of("pitch")] += 0.05
        state.apply()
    eye = viewport.camera.eye
    offset = Bifrost.v_norm((eye.x, eye.y, eye.z))
    angle = math.degrees(math.acos(max(-1.0, min(1.0, offset[2]))))
    check(
        limit - 1e-6 <= angle <= 180.0 - limit + 1e-6,
        "turntable pitch never crosses the pole (%.2f degrees from world up, "
        "limit %.1f)" % (angle, limit),
    )
    check(
        close(abs(elevation_of(viewport)), 90.0 - limit, 1e-6),
        "pitch parks exactly on the elevation clamp (%.4f degrees)"
        % elevation_of(viewport),
    )
    check(
        close(Bifrost.v_len((eye.x, eye.y, eye.z)), 100.0, 1e-6),
        "pitch keeps the orbit radius",
    )
    check(roll_of(viewport) < 1e-9, "the view is still level at the clamp")


def test_turntable_invariants():
    """A thousand random movements must not tumble, drift or leave the dome."""
    print("turntable invariants")
    limit = Bifrost.DEFAULT_ADDIN_CONFIG["pitch_limit_deg"]
    state = new_state()
    # An off centre model, so the orbit runs around a pivot that is not the
    # target: that is the case the old code got wrong.
    state.app.activeProduct = FakeProduct(FakeBoundingBox((5, -8, -3), (25, 12, 17)))
    viewport = state.app.activeViewport
    random.seed(20260918)

    worst_roll = 0.0
    worst_elevation = 0.0
    worst_radius = 0.0
    bursts = 1000
    for index in range(bursts):
        # Every so often, pretend Fusion or the mouse moved the camera behind
        # the add-in's back, which is what the log from the physical test is
        # full of: wheel zooms, view cube clicks, re-fits while loading.
        if index % 97 == 0:
            camera = viewport._camera
            camera.eye = FakePoint3D(
                camera.eye.x + random.uniform(-20, 20),
                camera.eye.y + random.uniform(-20, 20),
                camera.eye.z + random.uniform(-20, 20),
            )
            state.last_motion_at = 0.0  # a gap, so the next input opens a burst

        before = camera_tuple(viewport)
        before_radius = Bifrost.v_len(Bifrost.v_sub(before[0], before[1]))
        with state.lock:
            state.acc[axis_of("yaw")] = random.uniform(-0.4, 0.4)
            state.acc[axis_of("pitch")] = random.uniform(-0.4, 0.4)
        state.apply()

        worst_roll = max(worst_roll, roll_of(viewport))
        elevation = elevation_of(viewport)
        worst_elevation = max(worst_elevation, abs(elevation))
        after = camera_tuple(viewport)
        after_radius = Bifrost.v_len(Bifrost.v_sub(after[0], after[1]))
        worst_radius = max(worst_radius, abs(after_radius - before_radius))

    check(
        worst_roll < 1e-6,
        "up never leaves the world up plane over %d bursts (worst %.2e)"
        % (bursts, worst_roll),
    )
    check(
        worst_elevation <= 90.0 - limit + 1e-6,
        "elevation stays inside the clamp (worst %.4f, limit %.4f degrees)"
        % (worst_elevation, 90.0 - limit),
    )
    check(
        worst_radius < 1e-6,
        "a pure orbit keeps the eye to target distance (worst drift %.2e cm)"
        % worst_radius,
    )

    # The same run, but with the input coming in one frame at a time rather
    # than as whole bursts, which is how the add-in really sees it.
    state = new_state()
    viewport = state.app.activeViewport
    worst_roll = 0.0
    for _ in range(2000):
        with state.lock:
            state.acc[axis_of("yaw")] = random.uniform(-0.02, 0.02)
            state.acc[axis_of("pitch")] = random.uniform(-0.02, 0.02)
        state.apply()
        worst_roll = max(worst_roll, roll_of(viewport))
    check(
        worst_roll < 1e-6,
        "2000 small steps do not accumulate roll either (worst %.2e)" % worst_roll,
    )

    # Roll that is already in the camera when a burst opens gets cleaned up,
    # rather than being taken as the new level.
    state = new_state()
    viewport = state.app.activeViewport
    viewport._camera.upVector = FakeVector3D(0.6, 0.0, 0.8)
    check(roll_of(viewport) > 0.1, "the test camera really is rolled to start with")
    with state.lock:
        state.acc[axis_of("yaw")] = 0.01
    state.apply()
    check(
        roll_of(viewport) < 1e-9,
        "a rolled camera is levelled by the first orbit (%.2e)" % roll_of(viewport),
    )


def test_roll_is_ignored_in_turntable():
    print("roll")
    state = new_state()
    state.config["roll_speed"] = 0.5
    viewport = state.app.activeViewport
    before = camera_tuple(viewport)
    with state.lock:
        state.acc[axis_of("roll")] = 0.5
    state.apply()
    after = camera_tuple(viewport)
    check(
        before == after,
        "turntable mode ignores roll even when roll_speed is on",
    )

    state = new_state()
    state.config["roll_speed"] = 0.5
    state.config["orbit_mode"] = "free"
    viewport = state.app.activeViewport
    with state.lock:
        state.acc[axis_of("roll")] = 0.5
    state.apply()
    check(
        roll_of(viewport) > 0.1,
        "free mode still rolls (%.3f)" % roll_of(viewport),
    )


def test_pan_and_zoom():
    print("pan and zoom")
    state = new_state()
    viewport = state.app.activeViewport
    before_target = viewport.camera.target.x
    with state.lock:
        state.acc[axis_of("pan_x")] = 0.1
    state.apply()
    after = viewport.camera
    moved = abs(after.target.x - before_target)
    check(moved > 0.0, "pan moves the target (%.3f cm)" % moved)
    check(
        close(after.target.x - before_target, after.eye.x - 0.0, 1e-9),
        "pan moves eye and target by the same amount",
    )
    check(
        close(after.eye.x - after.target.x, 0.0, 1e-6),
        "pan keeps the view direction unchanged",
    )

    state = new_state()
    viewport = state.app.activeViewport
    extents_before = viewport.camera.viewExtents
    dolly_in = 0.5 if not Bifrost.DEFAULT_ADDIN_CONFIG["invert"]["dolly"] else -0.5
    with state.lock:
        state.acc[axis_of("dolly")] = dolly_in
    state.apply()
    extents_after = viewport.camera.viewExtents
    expected = extents_before * math.exp(
        -0.5 * Bifrost.DEFAULT_ADDIN_CONFIG["zoom_speed"]
    )
    check(
        close(extents_after, expected, 1e-6),
        "orthographic zoom scales viewExtents as exp(-dolly) (%.4f -> %.4f)"
        % (extents_before, extents_after),
    )
    check(
        close(
            Bifrost.v_len(
                (viewport.camera.eye.x, viewport.camera.eye.y, viewport.camera.eye.z)
            ),
            100.0,
            1e-6,
        ),
        "orthographic zoom leaves the eye where it was",
    )

    # Perspective cameras dolly instead.
    state = new_state()
    viewport = state.app.activeViewport
    viewport._camera.cameraType = FakeCameraTypes.PerspectiveCameraType
    with state.lock:
        state.acc[axis_of("dolly")] = dolly_in
    state.apply()
    distance = Bifrost.v_len(
        (viewport.camera.eye.x, viewport.camera.eye.y, viewport.camera.eye.z)
    )
    check(distance < 99.0, "perspective zoom moves the eye closer (%.2f cm)" % distance)


def test_button_fit():
    print("buttons")
    state = new_state()
    viewport = state.app.activeViewport
    state.handle_line(b'{"t":"b","n":2,"p":1}')
    check(state.fit_button == 2, "the first button seen becomes the fit button")
    state.apply()
    check(viewport.fit_calls == 1, "that button calls viewport.fit()")
    state.handle_line(b'{"t":"b","n":5,"p":1}')
    state.apply()
    check(viewport.fit_calls == 1, "other buttons are ignored")


def test_message_handling():
    print("message handling")
    state = new_state()
    state.handle_line(b'{"t":"m","v":[1,0,0,0,0,0],"dt":0.5}')
    state.handle_line(b'{"t":"m","v":[1,0,0,0,0,0],"dt":0.5}')
    check(close(state.acc[0], 1.0), "motion frames integrate value times dt")
    state.handle_line(b"not json at all")
    check(close(state.acc[0], 1.0), "garbage lines are ignored")
    state.handle_line(b'{"t":"hello","addin":{"orbit_speed":9.5}}')
    check(state.config["orbit_speed"] == 9.5, "hello updates the config")
    check(
        state.config["pan_speed"] == Bifrost.DEFAULT_ADDIN_CONFIG["pan_speed"],
        "hello keeps defaults for keys it does not mention",
    )
    state = new_state()
    state.handle_line(b'{"t":"m","v":[0,0,0,0,0,0],"dt":0.0}')
    check(all(v == 0.0 for v in state.acc), "a zero dt frame changes nothing")


def test_orbit_pivot():
    print("orbit pivot")
    # The model sits well off the camera target: centre (20, 20, 5).
    state = new_state()
    state.app.activeProduct = FakeProduct(FakeBoundingBox((10, 10, 0), (30, 30, 10)))
    viewport = state.app.activeViewport
    state.config["orbit_pivot"] = "target"
    state.extra_yaw = math.pi / 2.0
    state.apply()
    check(
        close(viewport.camera.target.x, 0.0, 1e-9)
        and close(viewport.camera.target.y, 0.0, 1e-9),
        "pivot=target leaves the target where it was",
    )

    state = new_state()
    state.app.activeProduct = FakeProduct(FakeBoundingBox((10, 10, 0), (30, 30, 10)))
    viewport = state.app.activeViewport
    state.config["orbit_pivot"] = "auto"
    state.extra_yaw = math.pi / 2.0
    state.apply()
    eye = viewport.camera.eye
    target = viewport.camera.target
    check(
        state.pivot_valid and state.pivot_source == "model",
        "auto picks the model centre as pivot (%s)" % (state.pivot,),
    )
    check(
        close(state.pivot[0], 20.0) and close(state.pivot[1], 20.0),
        "the pivot is the bounding box centre",
    )
    # eye (0,-100,0) and target (0,0,0) both turn 90 degrees around (20,20,5).
    check(
        close(eye.x, 140.0, 1e-6) and close(eye.y, 0.0, 1e-6),
        "the eye turns around the model, not the target (%.3f, %.3f)" % (eye.x, eye.y),
    )
    check(
        close(target.x, 40.0, 1e-6) and close(target.y, 0.0, 1e-6),
        "the target travels with it (%.3f, %.3f)" % (target.x, target.y),
    )
    check(
        close(
            Bifrost.v_len(
                Bifrost.v_sub((eye.x, eye.y, eye.z), (target.x, target.y, target.z))
            ),
            100.0,
            1e-6,
        ),
        "the eye to target distance survives a pivot orbit",
    )

    # A model centred on the target must behave exactly like the old code.
    state = new_state()
    state.app.activeProduct = FakeProduct(FakeBoundingBox((-5, -5, -5), (5, 5, 5)))
    viewport = state.app.activeViewport
    state.extra_yaw = math.pi / 2.0
    state.apply()
    check(
        close(viewport.camera.target.x, 0.0, 1e-6)
        and close(viewport.camera.eye.x, 100.0, 1e-6),
        "a centred model gives the same orbit as pivot=target",
    )

    # No design open: fall back to the target instead of blowing up.
    state = new_state()
    state.app.activeProduct = None
    viewport = state.app.activeViewport
    state.extra_yaw = math.pi / 2.0
    state.apply()
    check(
        state.pivot_source == "target" and close(viewport.camera.target.x, 0.0, 1e-6),
        "no design open falls back to the target",
    )

    # What is hidden must not drag the pivot away from what is on screen.
    state = new_state()
    state.app.activeProduct = FakeProduct(
        FakeBoundingBox((-100, -100, -100), (10, 10, 10)),
        bodies=[
            FakeBody(FakeBoundingBox((0, 0, 0), (10, 10, 10)), visible=True),
            FakeBody(FakeBoundingBox((-100, -100, -100), (-90, -90, -90)), False),
        ],
    )
    state.extra_yaw = 0.01
    state.apply()
    check(
        state.pivot is not None and close(state.pivot[0], 5.0),
        "a hidden body far away does not move the pivot (%s)" % (state.pivot,),
    )

    # Nothing visible at all: fall back to the whole design's box.
    state = new_state()
    state.app.activeProduct = FakeProduct(
        FakeBoundingBox((0, 0, 0), (10, 10, 10)),
        bodies=[FakeBody(FakeBoundingBox((0, 0, 0), (10, 10, 10)), visible=False)],
    )
    state.extra_yaw = 0.01
    state.apply()
    check(
        state.pivot is not None and close(state.pivot[0], 5.0),
        "with nothing visible it falls back to the design box (%s)" % (state.pivot,),
    )

    # Pan drags the pivot along, so the next orbit still turns around the same
    # point of the model.
    state = new_state()
    state.app.activeProduct = FakeProduct(FakeBoundingBox((10, 10, 0), (30, 30, 10)))
    state.extra_yaw = 0.01
    state.apply()
    before = state.pivot
    with state.lock:
        state.acc[axis_of("pan_x")] = 0.1
    state.apply()
    moved = Bifrost.v_len(Bifrost.v_sub(state.pivot, before))
    check(moved > 1e-6, "panning moves the pivot with the view (%.3f cm)" % moved)


def test_bursts():
    print("bursts")
    state = new_state()
    log_path = os.path.join(HERE, "fixtures", "addin_burst_test.log")
    if os.path.exists(log_path):
        os.remove(log_path)
    state.log.path = log_path
    state.log.set_level("debug")
    with state.lock:
        state.acc[axis_of("yaw")] = 0.1
    state.apply()
    check(state.burst_open, "motion opens a burst")
    with state.lock:
        state.acc[axis_of("yaw")] = 0.1
    state.apply()
    check(
        close(state.burst_input[axis_of("yaw")], 0.2),
        "the burst sums the input it saw (%.3f)" % state.burst_input[axis_of("yaw")],
    )
    sets_before = state.camera_sets
    with state.lock:
        state.close_burst_requested = True
    state.apply()
    check(
        not state.burst_open and state.camera_sets == sets_before,
        "closing a burst reads the camera without moving it",
    )
    text = open(log_path).read()
    check("burst start" in text, "the burst start line is logged")
    check("burst end" in text, "the burst end line is logged")
    check(
        "%s=+0.2000" % Bifrost.DEFAULT_ADDIN_CONFIG["map"]["yaw"] in text,
        "the end line carries the integrated input",
    )
    check("d_eye=" in text, "the end line carries the camera delta")

    # A gap longer than idle_gap_seconds starts a new burst.
    state.last_motion_at = time.time() - 5.0
    state.burst_open = True
    with state.lock:
        state.acc[axis_of("yaw")] = 0.1
    state.apply()
    check(
        close(state.burst_input[axis_of("yaw")], 0.1),
        "a gap resets the burst integral (%.3f)" % state.burst_input[axis_of("yaw")],
    )


class StubConfig(object):
    """Just enough of daemon.Config for the Bridge's shaping to run."""

    def __init__(self):
        self.data = bifrost_daemon.DEFAULTS

    def section(self, name):
        return dict(self.data.get(name, {}))


def feed(state, raw, seconds=0.8, dt=1.0 / 60.0):
    """Hold raw spacenavd counts on the puck and let the whole chain run.

    Counts go through the daemon's deadzone, response curve, dominant group
    gating and smoothing, out as the JSON the add-in reads, and into the
    accumulator. One apply() at the end stands in for the camera update.
    """
    bridge = bifrost_daemon.Bridge(StubConfig())
    daemon_cfg = bifrost_daemon.DEFAULTS["daemon"]
    addin_cfg = bifrost_daemon.DEFAULTS["addin"]
    seen = [0.0] * 6
    for _ in range(int(seconds / dt)):
        values = bridge.shape(list(raw), daemon_cfg, addin_cfg, dt)
        for index in range(6):
            seen[index] += abs(values[index]) * dt
        with state.lock:
            for index in range(6):
                state.acc[index] += values[index] * dt
    state.apply()
    return seen


def test_cross_talk():
    """One deliberate push plus three stray axes must move one thing only.

    The hardware recording in tests/fixtures/hardware shows every push leaking
    into every axis: lifting the puck puts 52 counts on rz, pushing right puts
    42 on z. Before the response curve and the group gating, a single nudge
    orbited, panned and zoomed at the same time, which is exactly what made the
    view feel like it was tumbling.
    """
    print("cross talk")
    dominant = 350
    stray = 60

    # Rotation wins: only the elevation may change.
    state = new_state()
    viewport = state.app.activeViewport
    before = camera_tuple(viewport)
    before_extents = viewport.camera.viewExtents
    raw = [stray, 0, stray, dominant, stray, 0]  # x, y, z, rx, ry, rz
    seen = feed(state, raw)
    check(
        seen[AXES_INDEX["rx"]] > 0.5,
        "the dominant axis carries the movement (%.3f unit seconds)"
        % seen[AXES_INDEX["rx"]],
    )
    check(
        all(seen[AXES_INDEX[name]] == 0.0 for name in ("x", "y", "z", "ry", "rz")),
        "the three stray axes reach the add-in as exactly zero (%s)"
        % ", ".join(
            "%s=%.4f" % (name, seen[AXES_INDEX[name]]) for name in ("x", "z", "ry")
        ),
    )
    expected = -math.degrees(
        seen[AXES_INDEX["rx"]] * Bifrost.DEFAULT_ADDIN_CONFIG["orbit_speed"]
    )
    check(
        close(elevation_of(viewport), expected, 1e-6),
        "the camera turned exactly input times orbit_speed (%.3f degrees, "
        "expected %.3f)" % (elevation_of(viewport), expected),
    )
    check(
        close(azimuth_of(viewport), -90.0, 1e-9),
        "no yaw leaked in (azimuth %.9f degrees)" % azimuth_of(viewport),
    )
    check(
        close(viewport.camera.viewExtents, before_extents, 1e-12),
        "no zoom leaked in",
    )
    after = camera_tuple(viewport)
    check(
        close(
            Bifrost.v_len(Bifrost.v_sub(after[0], after[1])),
            Bifrost.v_len(Bifrost.v_sub(before[0], before[1])),
            1e-9,
        ),
        "no dolly leaked in",
    )
    check(roll_of(viewport) < 1e-9, "and the view is still level")

    # Translation wins: only the target may move, sideways.
    state = new_state()
    viewport = state.app.activeViewport
    before = camera_tuple(viewport)
    before_extents = viewport.camera.viewExtents
    seen = feed(state, [dominant, stray, 0, stray, stray, 0])
    check(
        seen[AXES_INDEX["x"]] > 0.5
        and all(seen[AXES_INDEX[n]] == 0.0 for n in ("y", "z", "rx", "ry", "rz")),
        "a sideways push arrives on x alone",
    )
    after = camera_tuple(viewport)
    check(
        abs(after[1][0] - before[1][0]) > 1.0,
        "the pan moved the view (%.3f cm)" % (after[1][0] - before[1][0]),
    )
    check(
        close(azimuth_of(viewport), -90.0, 1e-9)
        and close(elevation_of(viewport), 0.0, 1e-9),
        "no rotation leaked into a pan",
    )
    check(
        close(viewport.camera.viewExtents, before_extents, 1e-12),
        "no zoom leaked into a pan",
    )

    # Zoom wins: only viewExtents may change.
    state = new_state()
    viewport = state.app.activeViewport
    before = camera_tuple(viewport)
    before_extents = viewport.camera.viewExtents
    seen = feed(state, [stray, dominant, stray, stray, 0, 0])
    check(
        seen[AXES_INDEX["y"]] > 0.5
        and all(seen[AXES_INDEX[n]] == 0.0 for n in ("x", "z", "rx", "ry", "rz")),
        "a lift arrives on y alone",
    )
    after = camera_tuple(viewport)
    check(
        abs(viewport.camera.viewExtents - before_extents) > 1.0,
        "the zoom moved the view (%.4f -> %.4f)"
        % (before_extents, viewport.camera.viewExtents),
    )
    check(
        before[0] == after[0] and before[1] == after[1],
        "an orthographic zoom moved nothing else at all",
    )


def test_speed_defaults():
    """Full deflection has to land on numbers a hand can live with."""
    print("speeds")
    config = Bifrost.DEFAULT_ADDIN_CONFIG
    orbit_deg = math.degrees(config["orbit_speed"])
    check(
        85.0 <= orbit_deg <= 95.0,
        "a second of full deflection orbits about 90 degrees (%.1f)" % orbit_deg,
    )
    check(
        0.9 <= config["pan_speed"] <= 1.1,
        "a second of full deflection pans about one viewport width (%.2f)"
        % config["pan_speed"],
    )
    zoom_factor = math.exp(config["zoom_speed"])
    check(
        1.9 <= zoom_factor <= 2.1,
        "a second of full deflection zooms about a factor two (%.3f)" % zoom_factor,
    )
    check(
        close(90.0 - config["pitch_limit_deg"], 89.0),
        "elevation is clamped at 89 degrees",
    )


def test_viewport_refresh():
    """Moving the camera and drawing the result are two different things."""
    print("repaint")
    state = new_state()
    viewport = state.app.activeViewport
    with state.lock:
        state.acc[axis_of("yaw")] = 0.1
    state.apply()
    check(
        viewport.refresh_calls == 1,
        "a camera update asks Fusion to repaint (%d)" % viewport.refresh_calls,
    )
    before = viewport.refresh_calls
    for _ in range(5):
        state.apply()
    check(
        viewport.refresh_calls == before,
        "an idle frame does not ask for a repaint",
    )

    state = new_state()
    state.config["refresh_viewport"] = False
    viewport = state.app.activeViewport
    with state.lock:
        state.acc[axis_of("yaw")] = 0.1
    state.apply()
    check(
        viewport.refresh_calls == 0 and state.camera_sets == 1,
        "refresh_viewport false still moves the camera, just does not repaint",
    )


def test_idle_is_free():
    print("idle")
    state = new_state()
    viewport = state.app.activeViewport
    before = state.camera_sets
    for _ in range(10):
        state.apply()
    check(state.camera_sets == before, "an empty accumulator never touches the camera")


def main():
    test_vector_helpers()
    test_orbit_returns_home()
    test_pitch_limit()
    test_turntable_invariants()
    test_roll_is_ignored_in_turntable()
    test_pan_and_zoom()
    test_orbit_pivot()
    test_cross_talk()
    test_speed_defaults()
    test_bursts()
    test_viewport_refresh()
    test_button_fit()
    test_message_handling()
    test_idle_is_free()
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
