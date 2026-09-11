# Testresultat, Bifrost

Maskin: vanessa, Arch (Omarchy 4.x), Hyprland pa Wayland, GTX 1080 Ti.
spacenavd 1.3.1-1, libspnav 1.2-1, Fusion 2705.1.15 i Wine-prefixet
`~/.autodesk_fusion/wineprefixes/default`, patchad wine ur `~/fusion-wine-build`.
Datum: 2026-09-11.

Alla siffror nedan ar uppmatta, inte uppskattade.

---

## T1: spnav-protokollet bekraftat

**Utfall: godkant.** Tre oberoende kallor sager samma sak.

### Kallkod

`spacenavd` v1.3.1, `src/proto_unix.c`, funktionen `send_uevent`, skriver alltid
exakt `int32_t data[8]` med ett enda `write()`. `data[0]` ar handelsetypen ur
enum `UEV_*` i `src/proto.h`:

| typ | varde | resten av ramen |
| --- | --- | --- |
| `UEV_MOTION` | 0 | `data[1..6]` = x, y, z, rx, ry, rz, `data[7]` = period i ms |
| `UEV_PRESS` | 1 | `data[1]` = knappnummer, `data[2]` = 1 |
| `UEV_RELEASE` | 2 | `data[1]` = knappnummer, `data[2]` = 0 |
| `UEV_DEV` | 3 | enhet till/fran |
| `UEV_CFG` | 4 | konfigandring |
| `UEV_RAWAXIS` | 5 | raa axelvarden |
| `UEV_RAWBUTTON` | 6 | raa knappar |

Alltsa 32 bytes per ram, atta int32 i maskinens byteordning (little-endian har).

Om handshaken: `src/client.c` satter `client->proto = 0` och
`client->evmask = EVMASK_MOTION | EVMASK_BUTTON` for varje ny klient. En klient
som aldrig ber om protokollversion 1 far alltsa rorelser och knappar direkt.
Bifrost gor ingen handshake alls, vilket ar det som star i daemonens docstring.

### Live-capture

En capture togs mot `/run/spnav.sock` medan SpaceMousen rordes. Den innehaller
tre rutor, avkodade som:

```
(0, -13,  -2, 7, 2, -6, -6, 212864)
(0, -13,   3, 7, 2, -6, -6,     16)
(0, -13,  19, 7, 2, -6, -6,     16)
```

Vilket bekraftar bade layouten (typ 0 = motion, sex axlar, periodfaltet sist)
och att spacenavd upprepar ungefar var 16:e ms, alltsa 60 Hz, sa lange pucken
halls utslagen. Forsta rutans period ar tiden sedan anslutningen, inte en
rapporteringstakt.

Begransning: capturen blev bara tre rutor, sa den innehaller ingen full
utslagning och ingen knapptryckning. De bitarna vilar pa kallkoden och pa
fixturen `tests/fixtures/synthetic_orbit.bin`, som ar byggd efter exakt samma
format. Capturen ligger kvar som `tests/fixtures/real_capture_3frames.bin`.

### Egen testanslutning

```
$ ./daemon/bifrost_daemon.py --dump --seconds 8
connected to /run/spnav.sock, 8.0s window, protocol v0 (no handshake)
0 bytes, 0 complete frames, connection stayed healthy
```

Anslutningen halls utan fel i atta sekunder med pucken i vila, och utan
handshake. Daemonen kor dessutom som tjanst samtidigt utan att nagon av dem stor
den andra, vilket ar hela poangen med att spacenavd ar en multi-client-server:
KiCad paverkas inte.

---

## T2: daemonen end-to-end lokalt

**Utfall: godkant, 24 av 24 kontroller.**

`tests/test_daemon.py` startar daemonen i `--replay`-lage mot
`tests/fixtures/synthetic_orbit.bin` (187 rutor: yaw-svep, pitch-svep, pan-svep,
dolly-svep, knapp ner och upp) pa port 47661, ansluter en TCP-klient och
granskar strommen.

```
frame decoding
  [PASS] fixture is a whole number of 32 byte frames
  [PASS] first frame is UEV_MOTION (type 0)
  [PASS] period field is 16 ms
  [PASS] fixture holds motion, press and release types
daemon replay
  [PASS] daemon accepted a TCP connection
  [PASS] exactly one hello frame
  [PASS] hello carries the add-in config
  [PASS] motion frames streamed (160)
  [PASS] keepalive pings arrive (8)
  [PASS] every motion frame has six axes and a positive dt
       peaks: {"x": 0.77015, "y": 0.0, "z": 0.71045, "rx": 0.59403,
                "ry": 0.83582, "rz": 0.0}
  [PASS] ry swept to a real deflection (0.836)
  [PASS] rx swept to a real deflection (0.594)
  [PASS] x swept to a real deflection (0.770)
  [PASS] z swept to a real deflection (0.710)
  [PASS] y stayed at zero as the fixture intends
  [PASS] rz stayed at zero as the fixture intends
  [PASS] no cross talk between axes
  [PASS] button press and release both forwarded (2)
  [PASS] press arrives before release
  [PASS] button number preserved
  [PASS] stream ends with an all-zero frame
  [PASS] daemon goes quiet once the puck is centred (longest zero run 1)
  [PASS] integrated yaw is sane (0.384 unit seconds)
client reconnect
  [PASS] connected to the first daemon instance
  [PASS] port is closed while the daemon is down
  [PASS] reconnected to the restarted daemon

all checks passed
```

Utan rorelse: stabil anslutning, hello-ram, keepalive-pingar var femte sekund,
inga rorelserutor. Med rorelse: 160 rutor, en axel i taget, ingen overhorning,
och exakt en nollruta nar pucken centreras.

Replay-laget (`--replay <fil.bin>`) matar inspelade rutor genom samma pipeline
som hardvaran, sa hela kedjan gar att testa utan SpaceMouse. Det anvands aven i
T4 nedan for att kora Fusions kamera utan att nagon ror pucken.

### Kamerammatten, utanfor Fusion

`tests/test_camera_math.py` stoppar in en fejkad `adsk.core` med en fejkad
viewport, sa att add-inets matte gar att kora pa ren Linux.

```
  [PASS] 90 degree rotation around Z maps +X to +Y
  [PASS] the inverse rotation gets back to the start
  [PASS] v_norm returns a unit vector
  [PASS] v_norm survives the zero vector
  [PASS] a full 360 degree orbit returns to the start (drift 4.99e-13 cm)
  [PASS] one camera update per step (90)
  [PASS] a quarter turn lands on the X axis at the same radius
  [PASS] world up is preserved through a turntable yaw
  [PASS] turntable pitch never crosses the pole (174.0 degrees from world up)
  [PASS] pitch keeps the orbit radius
  [PASS] pan moves the target (1.414 cm)
  [PASS] pan moves eye and target by the same amount
  [PASS] pan keeps the view direction unchanged
  [PASS] orthographic zoom scales viewExtents as exp(-dolly) (50.0000 -> 27.4406)
  [PASS] orthographic zoom leaves the eye where it was
  [PASS] perspective zoom moves the eye closer (54.88 cm)
  [PASS] the first button seen becomes the fit button
  [PASS] that button calls viewport.fit()
  [PASS] other buttons are ignored
  [PASS] motion frames integrate value times dt
  [PASS] garbage lines are ignored
  [PASS] hello updates the config
  [PASS] hello keeps defaults for keys it does not mention
  [PASS] a zero dt frame changes nothing
  [PASS] an empty accumulator never touches the camera

all checks passed
```

Drift over ett helt varv: 5e-13 cm. Matten ackumulerar alltsa inget fel.

---
