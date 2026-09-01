# Ground Station 3D

Desktop base-station app for a mine/landfill survey rig. It polls the
rover's HTTP point-cloud endpoint over WiFi, renders the live point cloud
(camera RGB or a thermal overlay), and lets you save frames to disk.

## Features

- Live point cloud view (Open3D), updating as new frames arrive
- Mouse controls: left-drag orbit, right/middle-drag pan, scroll to zoom
- Coordinate axes and an optional reference grid, plus a one-click "Reset
  View" to re-fit the camera to the current cloud
- Point size slider
- RGB / Thermal color overlay, auto-scaled to each frame's min/max
  temperature
- Save the current frame to a timestamped `.pcd` file
- Live stats: data rate, points/sec, HTTP fetch time, frames received/dropped

## Requirements

- Python 3.10+
- See `requirements.txt` (Open3D, NumPy, Matplotlib)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

## Running

**Windows:** double-click `Run Base Station.bat`, enter the rover's IP when
prompted, and it launches with no console window.

**Any platform / from source:**

```bash
python main.py --rover-host <rover-ip>
```

`--rover-host` is required -- it's the IP (or hostname) of the machine
running the rover's HTTP server. Optional flags:

- `--rover-port` (default `8080`)
- `--poll-interval` (default `0.1`s) -- minimum time between
  `GET /latest.bin` requests

### Testing without the rover

`tools/mock_rover_server.py` serves a synthetic spinning point cloud on the
same endpoint shape, so you can exercise the whole app before hardware is
available:

```bash
python tools/mock_rover_server.py --port 8080
python main.py --rover-host 127.0.0.1 --rover-port 8080
```

## Using the app

- **Orbit / pan / zoom**: standard Open3D `SceneWidget` mouse controls --
  nothing custom here, these come from Open3D itself.
- **Point size**: slider in the side panel, applied live via the render
  material.
- **Color overlay**: RGB uses the camera color carried in each frame;
  Thermal colormaps temperature (matplotlib `inferno`), auto-scaled to the
  current frame's min/max (shown under the dropdown). Points with no
  thermal reading render mid-gray.
- **View**: toggle the axes triad or reference grid, or hit Reset View to
  re-fit the camera to the current cloud.
- **Save PCD**: writes the currently displayed frame to
  `./captures/frame_<timestamp>.pcd` and shows a brief confirmation message.
- **Live stats**: rolling ~1s data rate (KB/s and points/sec), HTTP fetch
  time (how long each `GET /latest.bin` round-trip took), and frames
  received/dropped (a "dropped" frame is a failed or malformed HTTP
  response, not a decode of a partial one).

## Protocol / adapting to the real rover

All parsing lives in `protocol.py`, isolated from rendering/GUI code in
`main.py`. The rover currently serves:

```
GET http://<rover-host>:<port>/latest.bin

Binary body (little-endian):
    uint32       N   -- point count
    float32[N][7]    -- x, y, z, r, g, b, thermal
        xyz      meters
        rgb      already normalized 0-1
        thermal  Kelvin, or NaN where unavailable
```

Each request returns one complete frame -- no reassembly needed, since TCP
already guarantees the body arrives whole. If the endpoint's layout changes
again, `protocol.py` is the only file that should need editing; the rest of
the app only depends on the `Frame` dataclass (`xyz`, `rgb`, `temp_c`)
`parse_frame()` returns.

The endpoint is currently capped at ~20k points (stride-sampled) for
browser bandwidth. The rover's ROS side carries the full ~250k+ point cloud
if that cap ever gets lifted -- this app doesn't assume a point count, so a
larger frame from an uncapped endpoint should render as-is (expect a
heavier per-frame payload and correspondingly lower achievable poll rate
over WiFi).

## Non-goals

- No accumulation across frames or SLAM/registration -- only the latest
  complete frame is shown.
- No authentication or encryption -- trusted LAN only.
