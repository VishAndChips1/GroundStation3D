# Ground Station 3D

Desktop base-station app for a mine/landfill survey rig. It polls the
rover's HTTP point-cloud endpoint over WiFi and renders the live cloud with
camera-RGB or thermal colouring, a metre-graduated scale grid, and an
origin marker.

Built on Qt (PySide6) + PyVista/VTK.

## Features

- **Live 3D point cloud**, updating as frames arrive
- **Any sender, any network** — the rover IP/port are entered in the app
  itself, so it works with a rig on WiFi, a WSL instance, or localhost
  without touching the command line
- **Colour overlays** — camera RGB, or thermal colour-mapped (`inferno`)
  and auto-scaled to each frame's min/max, with points lacking a thermal
  reading drawn grey
- **Coordinate references** — corner orientation axes that rotate with the
  camera, an origin triad at (0, 0, 0), and a graduated bounding box whose
  metre tick labels rescale as you zoom
- **Camera** — left-drag orbit, middle-drag pan, scroll/right-drag zoom,
  plus Reset view and Top-down buttons
- **Capture** — save the current frame as a timestamped `.pcd`, or save a
  PNG of the viewport
- **Live stats** — data rate, points/sec, points in frame, HTTP fetch time,
  frames OK/failed
- **Clear connection feedback** — connection refused / timed out / bad
  frame are reported in plain language instead of failing silently

## Requirements

- Python 3.10+
- See `requirements.txt`

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

## Running

**Windows:** double-click `Run Base Station.bat`.

**Any platform:**

```bash
python main.py
```

Then either:

1. Press **Scan for senders** — it probes this machine, any running WSL
   distro, and every host on your local subnets, and lists the ones
   actually serving point clouds (with their point count, so it's obvious
   which is the rig). Pick one and press **Connect**.
2. Or type the IP straight into **Rover host** and press **Connect**.

Hosts you've connected to before are remembered and reappear in the
dropdown next launch.

Optional flags (all also editable in the GUI):

```bash
python main.py --rover-host 192.168.1.50 --rover-port 8080 --connect
```

- `--rover-host` / `--rover-port` — pre-fill the connection fields
- `--poll-interval` — minimum seconds between requests (default `0.2`)
- `--timeout` — HTTP request timeout (default `10`); raise it for large
  frames over a slow link
- `--connect` — connect immediately on startup

### Finding the rover's IP

**Scan for senders** in the app usually answers this for you. If you'd
rather look it up manually:

- **Rig on WiFi:** whatever address the rover reports on the shared network.
  Nothing here is WiFi- or WSL-specific — the app is a plain HTTP client, so
  any reachable host works.
- **Sender running in WSL on this machine:** run `wsl hostname -I` in
  PowerShell and use that address (e.g. `172.18.213.120`). It changes when
  WSL restarts. `127.0.0.1` also works, since WSL2 forwards localhost.
- **Same machine:** `127.0.0.1`.

> **Note on WSL and other devices:** WSL2 sits behind NAT, so a *different*
> machine on the WiFi cannot reach a WSL-hosted server without explicit
> port forwarding (`netsh interface portproxy`). A real rover on the WiFi
> has its own address and needs none of that.

### Sender requirements

The sender must bind `0.0.0.0` (not `127.0.0.1`) to be reachable from
another machine. The rig's `fusion_web_streamer.py` already does.

### Testing without the rover

`tools/mock_rover_server.py` serves a synthetic spinning cloud in the same
binary format:

```bash
python tools/mock_rover_server.py --port 8080
python main.py --rover-host 127.0.0.1 --connect
```

## Protocol / adapting to the real rover

All parsing lives in `protocol.py`, isolated from the GUI. The rover serves:

```
GET http://<rover-host>:<port>/latest.bin

Binary body (little-endian):
    uint32       N   -- point count
    float32[N][7]    -- x, y, z, r, g, b, thermal
        xyz      metres
        rgb      already normalised 0-1
        thermal  Kelvin, or NaN where unavailable
```

Each request returns one complete frame — no reassembly needed, since TCP
guarantees the body arrives whole. If the format changes, `protocol.py` is
the only file that should need editing; everything else depends solely on
the `Frame` dataclass (`xyz`, `rgb`, `temp_c`) that `parse_frame()` returns.

The endpoint is currently capped at ~20k points (stride-sampled) while the
ROS side carries the full ~250k+ cloud. The app doesn't assume a point
count, so an uncapped endpoint renders as-is — expect a heavier payload per
frame and a correspondingly lower practical poll rate over WiFi.

## Project layout

| File | Purpose |
| --- | --- |
| `main.py` | Qt window, 3D view, polling thread, stats |
| `protocol.py` | Wire format — the only file to touch if it changes |
| `discovery.py` | Network scan that finds senders on the local subnets |
| `theme.py` | Dark theme stylesheet |
| `tools/mock_rover_server.py` | Synthetic sender for testing without hardware |

## Non-goals

- No accumulation across frames, SLAM, or registration — only the latest
  frame is shown.
- No authentication or encryption — trusted LAN only.
