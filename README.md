# Bifrost

SpaceMouse navigation for Autodesk Fusion running under Wine on Linux.

Fusion does not read 3Dconnexion devices as raw HID. It talks to 3DxWare, the
vendor driver, which has no Linux build and does not run inside a Wine prefix.
So Bifrost stops trying to be a HID device at all. It splits the problem across
the Wine boundary instead:

```
  SpaceMouse (USB HID)
        |
        v
   spacenavd            native Linux daemon, already packaged in Arch
        |  AF_UNIX /run/spnav.sock, 32 byte binary frames
        v
   bifrost_daemon.py    native Linux, deadzone + gain + invert + rate shaping
        |  TCP 127.0.0.1:47653, newline delimited JSON
        v
   Bifrost add-in       Python, runs inside Fusion inside the Wine prefix
        |  adsk.core Viewport.camera
        v
   Fusion viewport
```

TCP over loopback crosses the Wine boundary in both directions, which is what
makes the split work. The add-in never touches USB, HID or 3DxWare.

Tested on Arch (Omarchy), Hyprland on Wayland, spacenavd 1.3.1, Fusion 2705.1.15
in the standard `~/.autodesk_fusion` Wine prefix.

## Install

```bash
git clone https://github.com/EliasJernberg/bifrost ~/dev/bifrost
cd ~/dev/bifrost
./install.sh
```

`install.sh` is idempotent, so run it again after every pull. It

1. writes `~/.config/bifrost/config.json` if you do not have one yet,
2. links the daemon into `~/.local/bin/bifrost-daemon`,
3. installs and enables the `bifrost.service` systemd user unit,
4. links the add-in into the prefix at
   `.../Autodesk Fusion 360/API/AddIns/Bifrost`,
5. registers the add-in for autostart in Fusion's script registry.

Options: `--copy` copies the add-in instead of symlinking it, `--prefix <dir>`
points at a different Wine prefix, `--no-service` skips systemd.

Requirements: `spacenavd` running (`systemctl enable --now spacenavd`), Python 3
on the Linux side, and a Fusion install under Wine. No Python packages, no pip,
standard library only.

### Turning the add-in on inside Fusion

`install.sh` writes the entry Fusion uses for "Run on Startup", but Fusion only
reads that file at startup and rewrites it at shutdown, so the sequence matters:

* quit Fusion, run `./install.sh`, start Fusion. The add-in then loads by itself.
* If it does not, open **UTILITIES > ADD-INS > Scripts and Add-Ins**, find
  **Bifrost** under the Add-Ins tab, tick **Run on Startup** and press **Run**.

You can confirm which one happened by looking at the log, see Troubleshooting.

## Daily use

The daemon runs as a user service and needs no attention:

```bash
systemctl --user status bifrost.service
systemctl --user restart bifrost.service
journalctl --user -u bifrost.service -f
```

Move the puck, the Fusion viewport moves:

| Puck | Viewport |
| --- | --- |
| push left or right | the model follows left or right |
| push forward or back | the model moves up or down |
| lift or press the cap | zoom out or in |
| tilt the front edge down or up | the model tips its front edge down or up |
| twist clockwise or counterclockwise | the model turns the same way, seen from above |
| button 5 | `viewport.fit()` |

The model follows the puck, in other words, and the view turns around the centre
of the model rather than around whatever the camera happened to be aimed at.
Which physical push a device reports as which axis is not something you can look
up, so all of it was measured: see [docs/AXELMATRIS.md](docs/AXELMATRIS.md).
Different puck, or something feels backwards? Run the calibration, below.

Two things shape how that feels, and both are on by default:

* **One motion at a time.** A SpaceMouse reports all six axes on every push, so
  a straight mapping turns one nudge into a simultaneous orbit, pan and zoom.
  Bifrost keeps only the strongest of turning, sliding and zooming each frame.
* **A square response curve.** Full deflection is unchanged, half deflection is
  a quarter of the speed, and the handful of counts that leak onto the other
  axes are under one percent. Fine work near the centre, full speed at the edge.

At full deflection the defaults give a quarter turn a second, one viewport width
of pan a second, and a factor two of zoom a second. See
[Response and cross talk](#response-and-cross-talk) to change any of it.

The view is a turntable: the horizon stays level, the model never tips over
sideways, and the camera stops one degree short of straight above and straight
below. `orbit_mode: "free"` gives a true trackball with roll instead.

Stopping the daemon does not break Fusion. The add-in keeps retrying and picks
the daemon back up within a couple of seconds of it returning.

## Calibration

With the puck in front of you:

```bash
python3 tools/calibrate.py
```

It asks for six things in turn, one line at a time: push right, push forward,
pull up, tilt the front edge down, twist clockwise, press the button you want as
Fit. It watches the raw spacenavd stream on its own connection, works out which
axis each movement drove and which way, and writes `map`, `invert` and
`fit_button` into `~/.config/bifrost/config.json`. The old config is copied to
`config.json.bak-<timestamp>` first, and the service is restarted at the end, so
the new mapping is live within a few seconds. Nothing inside the Wine prefix is
touched.

It refuses to guess: a movement that is too small, that drives two axes at once,
or that wobbles both ways is rejected on the spot and asked for again.

```bash
python3 tools/calibrate.py --dry-run     # detect and print, write nothing
python3 tools/calibrate.py --zoom-in     # lifting the puck zooms in, not out
python3 tools/calibrate.py --no-restart  # write the config, restart it yourself
python3 tools/calibrate.py --replay tests/fixtures/hardware/calibration_capture.bin
```

That last one runs the whole detection over a recording instead of the device,
which is how it is tested: the recording is a real hand on a real SpaceMouse Pro,
and the mapping it comes back with is the one this repo ships.

## Configuration

Everything lives in `~/.config/bifrost/config.json`. The daemon watches the
file's mtime and reloads within a second, so deadzone and sensitivity changes
take effect immediately, with no Fusion restart.

The `addin` section is different: it is sent to the add-in in the hello frame
when it connects, so camera tuning takes effect on the next connection. The
quickest way to apply it is `systemctl --user restart bifrost.service`, which
the add-in follows within a few seconds. That way nothing inside the Wine prefix
ever has to be edited.

### daemon section

| key | default | meaning |
| --- | --- | --- |
| `spnav_socket` | `/run/spnav.sock` | spacenavd socket |
| `listen_host`, `listen_port` | `127.0.0.1`, `47653` | where the add-in connects |
| `emit_hz` | `60` | motion frames per second while the puck is deflected |
| `ping_seconds` | `5.0` | keepalive interval, 0 disables |
| `deadzone` | `30` | raw counts below this count as zero |
| `full_scale` | `350` | raw count that maps to a normalised 1.0 |
| `sensitivity` | `1.0` | global multiplier |
| `axis_gain` | all `1.0` | per axis multiplier, keys `x y z rx ry rz` |
| `axis_invert` | all `false` | per axis sign flip |
| `response` | see below | the curve, the gating and the smoothing |
| `verbose` | `false` | log every button and device event |

### daemon.response section

| key | default | meaning |
| --- | --- | --- |
| `exponent` | `2.0` | `1.0` is a straight line, `2.0` squares, `3.0` cubes |
| `axis_cut` | `0.1` | drop an axis carrying less than this share of the leading one in its group |
| `dominant_group` | `true` | keep only one of rotate, translate and zoom per frame |
| `dominant_hysteresis` | `1.25` | how far another group must beat the current winner to take over |
| `smoothing_seconds` | `0.05` | exponential smoothing time constant, `0` disables it |

### addin section

| key | default | meaning |
| --- | --- | --- |
| `max_fire_hz` | `30` | upper bound on camera updates per second |
| `map` | see below | which spacenavd axis drives which camera motion |
| `invert` | `dolly`, `yaw` true | per motion sign flip |
| `orbit_speed` | `1.5708` | radians per unit of input, so rad/s at full deflection |
| `pan_speed` | `1.0` | viewport widths per unit of input |
| `zoom_speed` | `0.6931` | e-folds of view scale per unit of input, `ln 2` is a factor two |
| `roll_speed` | `0.0` | roll is off by default, and ignored entirely in turntable mode |
| `orbit_mode` | `turntable` | `turntable` keeps world up level, `free` is a true 6DoF orbit |
| `orbit_pivot` | `auto` | `auto` turns around the model's centre, `target` around the camera target |
| `idle_gap_seconds` | `0.5` | a pause this long ends one movement and starts the next |
| `world_up` | `[0, 0, 1]` | up axis for turntable mode |
| `pitch_limit_deg` | `1.0` | how close to the pole turntable pitch may get, so elevation is clamped to 89 degrees |
| `min_distance` | `0.01` | closest a perspective camera may dolly to its target |
| `fit_button` | `5` | button number for fit, `"first"` learns it, `-1` disables |
| `selftest` | `false` | run a scripted pan, orbit and zoom once a design is open |
| `selftest_seconds` | `3.0` | sets the number of orbit steps, 30 per second |
| `selftest_pan` | `0.0` | viewport widths to pan before that orbit |
| `selftest_zoom` | `0.0` | e-folds to zoom in and back out after it, `0.6931` is a factor two |
| `selftest_fit` | `true` | fit the view first, so the frames are framed the same every run |
| `selftest_wait_seconds` | `600` | how long to wait for a design to appear |
| `selftest_settle_seconds` | `3.0` | how long the camera must hold still first |
| `selftest_image_dir` | `""` | where the rendered orbit frames go, empty means next to the log |
| `log_path` | `""` | empty means Fusion's temp folder, see Troubleshooting |
| `log_level` | `info` | `debug`, `info`, `warn`, `error` |

Default axis map, measured on a SpaceMouse Pro through spacenavd 1.3.1 with
close to stock settings:

```json
"map": {
  "pan_x": "x", "pan_y": "z", "dolly": "y",
  "pitch": "rx", "yaw": "ry", "roll": "rz"
},
"invert": {
  "pan_x": false, "pan_y": false, "dolly": true,
  "pitch": false, "yaw": true, "roll": false
}
```

If a motion runs the wrong way, flip its entry in `invert`. If two motions are
swapped, for instance yaw and roll, swap their entries in `map`. Or just run
`tools/calibrate.py`, which writes both tables for you. Which physical push a
SpaceMouse reports as `y` versus `z`, and which way round the rotations run,
depends on the device and on how spacenavd is set up: on the device this was
built for, translation and rotation do not even share a handedness. That is
exactly why this is a config table and not hard-coded, and the whole measurement
is in [docs/AXELMATRIS.md](docs/AXELMATRIS.md).

### Response and cross talk

A SpaceMouse has no isolated axes. The hardware recording in this repo shows
every push leaking into all six: pushing right puts about 42 counts on `z`,
lifting puts about 52 on `rz`, and a hand that means to twist is also tilting a
little. Fed straight through, one nudge orbits, pans and zooms at once, the view
ends up somewhere nobody asked for, and the puck feels broken rather than
imprecise. Three cheap stages in the daemon fix it, all under `daemon.response`:

1. **The curve.** The deadzoned magnitude is raised to `exponent` before it
   becomes speed. Full deflection still means full speed, half deflection means
   a quarter of it, and 60 counts of leakage next to a 350 count push drops from
   19 percent of full speed to 0.9 percent. This is also what makes slow, precise
   work possible at all: near the centre the puck is four times finer than a
   straight line makes it.
2. **One group per frame.** The six motions fall into three groups, rotate
   (`pitch`, `yaw`, `roll`), translate (`pan_x`, `pan_y`) and zoom (`dolly`).
   The strongest group wins the frame and the other two are set to zero, so
   going from an orbit into a pan stops the orbit instead of blending the two.
   `dominant_hysteresis` keeps a gesture from flickering between groups, and the
   groups are built from your own `map`, so a recalibrated puck groups itself.
   Inside the winning group, `axis_cut` drops any axis carrying less than a
   tenth of the leading one: a deliberate diagonal orbit survives, a stray
   sixtieth does not.
3. **Smoothing.** A 50 ms exponential filter takes the hand tremor out without
   any perceptible lag. It settles to exactly zero, so the stream still goes
   quiet when you let go.

Set `exponent` to `1.0`, `dominant_group` to `false` and `smoothing_seconds` to
`0` to get the raw stream back, which is what the plumbing tests use. All of it
lives in the `daemon` section, so the file is re-read within a second and you
can tune it with Fusion running.

### The turntable

`orbit_mode: "turntable"` means the camera has exactly two degrees of freedom
around its pivot: azimuth, how far round it has been turned, and elevation, how
far above or below the horizon it sits. The eye and the up vector are rebuilt
from those two numbers and `world_up` on every single frame, never nudged a
little further from wherever they happened to be. That has three consequences
worth knowing:

* **The view cannot roll.** Not after a thousand movements, not after a pitch
  near the pole. The up vector is constructed level, so roll is not something
  that is corrected, it is something that cannot be expressed.
* **Elevation is clamped for real,** at `90 - pitch_limit_deg` degrees, because
  the clamp is applied to the number the camera is built from rather than to a
  rotation that has already happened.
* **A mouse orbit, a view cube click or a wheel zoom in between is fine.** The
  angles are read back off the camera when a movement starts, and again whenever
  the camera turns out to have moved behind the add-in's back.

Roll is ignored in turntable mode however `roll_speed` is set. Use
`orbit_mode: "free"` for a true trackball where the up vector goes where the
puck puts it.

### The orbit pivot

With `orbit_pivot: "auto"`, the first orbit after a pause of `idle_gap_seconds`
unions the bounding boxes of everything visible in the design and turns around
the centre of that. Visible is the operative word: the whole design's box, which
is one cheap call, includes hidden bodies, and a hidden body parked far from
what you are working on drags the pivot off the model. That box is only the
fallback for when nothing visible can be found.
Panning drags that pivot along, so orbiting after a pan still turns around the
same point of the model. It falls back to the camera target, which is what the
first versions always did, when no design is open or nothing in it is visible.
Set `orbit_pivot: "target"` to get the old behaviour back.

## Tools and tests

```bash
# decode raw spacenavd events, to confirm the device and the wire format
./daemon/bifrost_daemon.py --dump --seconds 10

# watch what the daemon actually serves
./daemon/bifrost_daemon.py --tail --seconds 10

# run the daemon against a recorded capture instead of hardware
./daemon/bifrost_daemon.py --replay tests/fixtures/synthetic_orbit.bin

# ... and hold that capture until the Fusion add-in has connected
./daemon/bifrost_daemon.py --replay tests/fixtures/axis_matrix.bin --replay-wait

# the three test suites, no hardware and no Fusion needed
python3 tests/test_daemon.py
python3 tests/test_camera_math.py
python3 tests/test_calibrate.py
```

`tests/test_camera_math.py` stubs out `adsk.core` with a fake viewport, so the
orbit, pan, zoom, pivot and fit logic can be checked on plain Linux before it
ever reaches Fusion. `tests/test_calibrate.py` runs the calibration detection
over synthetic streams and over the real recording in
`tests/fixtures/hardware/`, which is a hand on an actual SpaceMouse Pro.

`tools/make_fixture.py` writes the fixtures, including `axis_matrix.bin`: twelve
single axis bursts, plus and minus on all six axes, which is what the axis matrix
was measured with. It also turns a real recording into a replayable one:

```bash
python3 tools/make_fixture.py --from-capture tests/fixtures/hardware/calibration_capture.bin
```

A raw capture holds no frames at all while the puck is still, so the pauses the
hand made are simply missing and `--replay` runs the whole thing together as one
movement. That splits it on the quiet frames and splices real silence back in,
giving one burst per movement with the hand's own cross talk intact.

Measured results for all of it, including what happens inside Fusion, are in
[docs/TESTRESULTAT.md](docs/TESTRESULTAT.md) (Swedish), and the axis by axis
measurement behind the default mapping is in
[docs/AXELMATRIS.md](docs/AXELMATRIS.md) (Swedish).

Setting `"selftest": true` makes the add-in fit the view as soon as a design is
open and then drive a scripted pan, a 360 degree orbit and a zoom, rendering the
viewport to PNG files next to the log at every step. That is the quickest way to
prove the camera path works on a machine where taking a screenshot is awkward.
Adding `"selftest_pan": 0.25` shoves the model off centre first, which is how the
auto pivot is shown to work: the model stays put through the orbit instead of
sweeping across the view. `"selftest_zoom": 0.6931` adds a factor two in and back
out at the end.

Each step waits for Fusion's event queue to drain rather than for a wall clock,
so the frames land on exact angles however slowly the viewport is redrawing. The
proof that it works is that the 0 degree and the 360 degree frame come out byte
identical.

## Troubleshooting

**Nothing moves.** Check the three links in turn:

```bash
systemctl --user status bifrost.service          # daemon alive?
./daemon/bifrost_daemon.py --dump --seconds 5    # does spacenavd see the puck?
./daemon/bifrost_daemon.py --tail --seconds 5    # does the daemon serve frames?
tail -f ~/.autodesk_fusion/wineprefixes/default/drive_c/users/$USER/AppData/Local/Temp/bifrost.log
```

That last path is where Fusion's embedded Python puts its temp files. If yours
differs, `find ~/.autodesk_fusion -name bifrost.log` will find it, or set
`log_path` in the config to somewhere you pick.

The add-in log is a plain file on the Linux side, so it can be tailed while
Fusion runs. It records startup, every connect and disconnect, the config it
received, and the camera update rate once per second while you are moving.

**The add-in never logs anything.** It is not loaded. Turn it on by hand under
UTILITIES > ADD-INS, see above.

**It drifts when I let go.** Raise `deadzone`. The default was 15 until the
hardware recording showed the puck resting up to about 20 counts off centre on
several axes, and cross talk of 50 counts on `rz` while lifting, so it is now 30.
Full deflection is around 350, so you lose very little range. If a view still
creeps, 40 is fine.

**Nothing moves the way I expect.** Run `python3 tools/calibrate.py` rather than
editing `map` and `invert` by hand.

**It is too fast or too slow.** `sensitivity` for everything at once,
`orbit_speed`, `pan_speed` and `zoom_speed` for one motion at a time.

**KiCad stopped seeing the SpaceMouse.** It should not: spacenavd serves several
clients at once and Bifrost is just one more. If it does, restart spacenavd.

## Backlog

* **A visible pivot marker.** The orbit already turns around the centre of the
  model, but nothing on screen says where that point is. Fusion's API has no
  transient overlay for this, so it would have to be a custom graphics entity
  drawn while a movement is running and cleared when it ends.
* Per button actions beyond Fit, so the rest of the SpaceMouse Pro keypad does
  something useful.
* A pan that recentres the auto pivot. Pan far enough and the orbit still turns
  around the model you left behind, which is correct and occasionally surprising.
  Fit puts it right.

## Known limits

* Only the fit button is mapped. The other SpaceMouse buttons are logged and
  ignored.
* Roll is wired up but ignored in turntable mode, which is the default, and off
  by default in free mode too (`roll_speed: 0.0`).
* Only one motion group happens per frame by default, so a deliberate pan while
  orbiting is not possible. `dominant_group: false` turns that off.
* Turntable elevation stops one degree short of the pole and stays there: push
  further and nothing moves, which is the clamp doing its job.
* The camera update rate is capped at 30 Hz by `max_fire_hz`. Fusion's own
  custom event queue is the bottleneck, not the daemon, which runs at 60 Hz.
* Changing `listen_port` means editing the constant in `Bifrost.py` too, since
  the add-in has to know where to connect before it has any config.
* Panning scales with how much world the viewport shows, which is read through
  `Viewport.viewToModelSpace`. If a future Fusion drops that call, the add-in
  falls back to the eye-to-target distance, and panning in an orthographic view
  will no longer track the zoom level.
* The auto pivot covers everything visible in the design, not only what is
  inside the viewport. Zoom into a corner of a big assembly and the orbit still
  turns around the centre of the whole visible model.
* `log_level: "debug"` adds two lines per movement, before and after, with the
  camera, the integrated input, the azimuth, the elevation, the roll error and
  the world width panning was scaled against. That is how the axis matrix was
  measured, and it is the first thing to turn on when a motion behaves oddly.
* Fusion recomputes the relationship between `viewExtents` and the visible world
  width the first time anything writes to `viewExtents`, by a factor of about
  1.31 on this setup. Panning follows, since the width is measured live, but two
  rendered frames taken on either side of the first zoom are not comparable.

## Licence

MIT.
