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
| `verbose` | `false` | log every button and device event |

### addin section

| key | default | meaning |
| --- | --- | --- |
| `max_fire_hz` | `30` | upper bound on camera updates per second |
| `map` | see below | which spacenavd axis drives which camera motion |
| `invert` | `dolly`, `yaw` true | per motion sign flip |
| `orbit_speed` | `2.5` | radians per unit of accumulated input |
| `pan_speed` | `1.0` | viewport widths per unit of input |
| `zoom_speed` | `1.2` | e-folds of view scale per unit of input |
| `roll_speed` | `0.0` | roll is off by default, Fusion has no roll of its own |
| `orbit_mode` | `turntable` | `turntable` keeps world up level, `free` is a true 6DoF orbit |
| `orbit_pivot` | `auto` | `auto` turns around the model's centre, `target` around the camera target |
| `idle_gap_seconds` | `0.5` | a pause this long ends one movement and starts the next |
| `world_up` | `[0, 0, 1]` | up axis for turntable mode |
| `pitch_limit_deg` | `2.0` | how close to the pole turntable pitch may get |
| `min_distance` | `0.01` | closest a perspective camera may dolly to its target |
| `fit_button` | `5` | button number for fit, `"first"` learns it, `-1` disables |
| `selftest` | `false` | run a scripted 360 degree orbit once a design is open |
| `selftest_seconds` | `3.0` | how long that orbit takes |
| `selftest_pan` | `0.0` | viewport widths to pan before that orbit |
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
was measured with.

Measured results for all of it, including what happens inside Fusion, are in
[docs/TESTRESULTAT.md](docs/TESTRESULTAT.md) (Swedish), and the axis by axis
measurement behind the default mapping is in
[docs/AXELMATRIS.md](docs/AXELMATRIS.md) (Swedish).

Setting `"selftest": true` makes the add-in drive a scripted 360 degree orbit
as soon as a design is open, and render the viewport at 0, 90, 180, 270 and 360
degrees to PNG files next to the log. That is the quickest way to prove the
camera path works on a machine where taking a screenshot is awkward. Adding
`"selftest_pan": 1.0` shoves the model a full viewport width off centre first,
which is how the auto pivot is shown to work: the model stays put through the
orbit instead of sweeping across the view.

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
* A sensitivity curve, so small deflections are finer than a straight line makes
  them.

## Known limits

* Only the fit button is mapped. The other SpaceMouse buttons are logged and
  ignored.
* Roll is wired up but off by default (`roll_speed: 0.0`).
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
  camera and the integrated input. That is how the axis matrix was measured, and
  it is the first thing to turn on when a motion behaves oddly.

## Licence

MIT.
