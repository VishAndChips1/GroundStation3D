# Sensor-PC side (PitFusion3D): diagnostics and power control

Makes the rig answer `GET /diagnostics` so the base station's **Diagnostics**
window can show what is happening on the Pi: camera / thermal / lidar
connection and frame rate, whether each pipeline stage is running and
keeping up, and the Pi's own health (CPU, memory, temperature, and the
**under-voltage** flag that matters on a power bank).

It also adds a small **control daemon** so the base station's power button can
start, stop and restart the sensor pipeline, and power the Pi off. See
[Power control](#power-control) below.

Nothing here changes the point-cloud stream. If the module is missing,
`fusion_web_streamer.py` runs exactly as before.

## Install on the sensor PC

From the PitFusion3D repo root:

```bash
git apply /path/to/pitfusion3d-sensor-side.patch
colcon build --packages-select landfill_camera_sim
```

The patch adds `scripts/sensor_diagnostics.py`, installs it next to the
streamer in `CMakeLists.txt`, and adds ~30 lines to `fusion_web_streamer.py`
(an optional import, a `/diagnostics` route, and an `enable_diagnostics`
parameter, default on). To switch it off: `-p enable_diagnostics:=false`.

Check it from any machine on the network:

```bash
curl http://<rig-ip>:8080/diagnostics
```

## What it measures

| Section | Entries | How |
| --- | --- | --- |
| `sensors` | `camera` (`/landfill/rgb/image_raw`), `thermal` (`/landfill/thermal/image_raw`), `lidar` (`livox/lidar`) | rate and freshness from raw topic subscriptions; connected = a publisher exists **and** its node is alive |
| `pipelines` | `livox_restamp`, `sensor_fusion`, `registration_merge`, `web_stream` | output-topic rate vs target, node alive; `web_stream` is requests/s served |
| `system` | CPU, memory, SoC temperature, disk, load, power flags | `/proc`, `/sys`, `vcgencmd get_throttled` (sampled on a background thread) |

Raw subscriptions (`raw=True`) hand over serialized bytes without building
Python message objects, so even the 1280x720 camera costs a copy, not a
deserialize. Target rates live in `DEFAULT_SENSORS` / `DEFAULT_PIPELINES` at
the top of `sensor_diagnostics.py`; change them if the launch rates change.

## Schema

```json
{
  "schema": 1, "timestamp": 1731330000.0, "hostname": "pitfusion", "uptime_s": 3600,
  "sensors": {
    "camera": {"state": "streaming", "connected": true, "fps": 14.8, "target_fps": 15,
               "last_frame_age_s": 0.05, "frames": 5515, "mb_per_s": 38.2,
               "topic": "/landfill/rgb/image_raw", "node": "rgb_camera_node", "message": ""}
  },
  "pipelines": {"sensor_fusion": {"state": "running", "fps": 9.8, "target_fps": 10}},
  "system": {"cpu_pct": 62, "mem_pct": 41.5, "temp_c": 61, "disk_pct": 38,
             "under_voltage_now": false, "under_voltage_since_boot": false}
}
```

The sensor PC reports raw facts. The **base station** decides healthy /
degraded / problem (state, `connected`, fps against `target_fps`, frame age,
temperature, power flags), so thresholds can change without touching the
rig. Extra fields on an entry are shown automatically. `state` is one of
`streaming`/`running`, `waiting`, `stalled`, `disconnected`/`not_running`.

## Try it without the rig

`python tools/mock_rover_server.py --port 8080` (in the base-station repo)
serves a simulated `/diagnostics` with the thermal camera dropping out for a
few seconds every minute. `--no-diagnostics` serves a 404 instead, to see
how the app reports a sensor PC that has not been upgraded.


## Power control

The base station's power icon (title bar) talks to `pitfusion_control.py`, a
tiny stdlib-only daemon that runs as **its own** systemd user service. It has
to be separate: stopping the pipeline also stops `fusion_web_streamer.py`, so
nothing inside the pipeline could ever receive the following "start".

| Icon state | Meaning | Click |
| --- | --- | --- |
| green | pipeline running | menu: Restart sensors, Stop sensors, **Power off Raspberry Pi...** |
| grey | pipeline stopped | starts it |
| amber, blinking | starting or stopping | (wait) |
| red | pipeline failed | tries to start it again |
| faint | Pi unreachable / no control service | explains why on the Rover tab |

**Power off** always shows a warning first: the Pi cannot be switched back on
from the app. Someone has to power-cycle it (disconnect and reconnect its
power). After a power-off the app shows "powered off" and keeps saying so
until the Pi answers again.

### Install

On the sensor PC, after applying the patch:

```bash
src/pitfusion3d_bringup/scripts/install_control.sh
```

It installs `pitfusion3d-control.service`, enables lingering (so it runs at
boot with no login), creates a random token in
`~/.config/pitfusion3d/control_token`, starts the daemon on port 8081 and
prints the token. Paste that token into the base station: **Rover tab >
Sensor PC power > Control token**. (Control port defaults to 8081.)

The script also prints a one-line sudoers rule for the optional power-off
command. Run it once, as root, if you want **Power off Raspberry Pi...**:

```bash
echo 'vish ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff' | sudo tee /etc/sudoers.d/pitfusion3d-poweroff
```

(Use your own username and the path `command -v systemctl` reports.) Without
it, the menu item is greyed out as "not permitted"; start, stop and restart
work regardless. That rule is the only place a Pi password is ever involved,
and it is typed by you on the Pi -- the app never stores or sends one.

### Safety

- **Off by default.** With no token configured the daemon answers
  `503 control disabled` and changes nothing.
- Commands need the token (`X-Control-Token`), compared in constant time. A
  wrong token gets `403` after a short delay.
- Only `start`, `stop`, `restart`, `shutdown` exist. Nothing the client sends
  reaches a shell or a command line.
- **Plain HTTP, no encryption**: meant for a trusted LAN or the rig's own
  hotspot. Anyone who can sniff that network can read the token. The token is
  kept in the base station's local settings (not in the repo or any log).
- `GET /status` is read-only and needs no token.

### Try it without the rig

`python tools/mock_rover_server.py --control-port 8081 --token demo` runs the
**real** daemon against a fake systemd. Enter `demo` as the token and the whole
flow works: start/stop move through starting/stopping, and Power off really
takes the mock down so the app sees an unreachable Pi.
