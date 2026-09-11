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

Move the puck, the Fusion viewport moves. The first SpaceMouse button you press
is learned as the Fit button and calls `viewport.fit()`; the rest are ignored in
this version.

Stopping the daemon does not break Fusion. The add-in keeps retrying and picks
the daemon back up within a couple of seconds of it returning.

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
| `deadzone` | `15` | raw counts below this count as zero |
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
| `invert` | `pan_y` true | per motion sign flip |
| `orbit_speed` | `2.5` | radians per unit of accumulated input |
| `pan_speed` | `1.0` | viewport widths per unit of input |
| `zoom_speed` | `1.2` | e-folds of view scale per unit of input |
| `roll_speed` | `0.0` | roll is off by default, Fusion has no roll of its own |
| `orbit_mode` | `turntable` | `turntable` keeps world up level, `free` is a true 6DoF orbit |
| `world_up` | `[0, 0, 1]` | up axis for turntable mode |
| `pitch_limit_deg` | `2.0` | how close to the pole turntable pitch may get |
| `min_distance` | `0.01` | closest a perspective camera may dolly to its target |
| `fit_button` | `"first"` | button number for fit, `"first"` learns it, `-1` disables |
| `selftest` | `false` | run a scripted 360 degree orbit once a design is open |
| `log_path` | `""` | empty means `C:\users\<you>\Temp\bifrost.log` |
| `log_level` | `info` | `debug`, `info`, `warn`, `error` |

Default axis map:

```json
"map": {
  "pan_x": "x", "pan_y": "y", "dolly": "z",
  "pitch": "rx", "yaw": "ry", "roll": "rz"
}
```

If an axis moves the wrong way, flip the matching entry in `invert`. If two
motions are swapped, for instance yaw and roll, swap their entries in `map`.
Which physical axis a SpaceMouse reports as `ry` versus `rz` depends on how
spacenavd is configured, which is exactly why this is a config table and not
hard-coded.

## Tools and tests

```bash
# decode raw spacenavd events, to confirm the device and the wire format
./daemon/bifrost_daemon.py --dump --seconds 10

# watch what the daemon actually serves
./daemon/bifrost_daemon.py --tail --seconds 10

# run the daemon against a recorded capture instead of hardware
./daemon/bifrost_daemon.py --replay tests/fixtures/synthetic_orbit.bin

# the two test suites, no hardware and no Fusion needed
python3 tests/test_daemon.py
python3 tests/test_camera_math.py
```

`tests/test_camera_math.py` stubs out `adsk.core` with a fake viewport, so the
orbit, pan, zoom and fit logic can be checked on plain Linux before it ever
reaches Fusion.

## Troubleshooting

**Nothing moves.** Check the three links in turn:

```bash
systemctl --user status bifrost.service          # daemon alive?
./daemon/bifrost_daemon.py --dump --seconds 5    # does spacenavd see the puck?
./daemon/bifrost_daemon.py --tail --seconds 5    # does the daemon serve frames?
tail -f ~/.autodesk_fusion/wineprefixes/default/drive_c/users/$USER/Temp/bifrost.log
```

The add-in log is a plain file on the Linux side, so it can be tailed while
Fusion runs. It records startup, every connect and disconnect, the config it
received, and the camera update rate once per second while you are moving.

**The add-in never logs anything.** It is not loaded. Turn it on by hand under
UTILITIES > ADD-INS, see above.

**It drifts when I let go.** Raise `deadzone`. A SpaceMouse that has warmed up
can sit at 10 to 20 counts off centre.

**It is too fast or too slow.** `sensitivity` for everything at once,
`orbit_speed`, `pan_speed` and `zoom_speed` for one motion at a time.

**KiCad stopped seeing the SpaceMouse.** It should not: spacenavd serves several
clients at once and Bifrost is just one more. If it does, restart spacenavd.

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

## Licence

MIT.
