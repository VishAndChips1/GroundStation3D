"""Dev-only mock rover server for testing the base station without hardware.

Serves a spinning, colored sphere of points with a fake thermal field at
GET /latest.bin, using the same binary layout as the real rover endpoint
(see protocol.py): uint32 point count, then N x 7 float32 (x,y,z,r,g,b,
thermal-Kelvin, occasionally NaN to exercise that path).

It also serves a simulated GET /diagnostics so the Diagnostics window can
be tried without the rig. The thermal camera drops out for a few seconds
every minute and the fusion stage runs a little slow, so the warning and
error states are easy to see. Pass --no-diagnostics to get a 404 instead
(what a sensor PC without the diagnostics module does).

It also runs the REAL control daemon (sensor_side/pitfusion_control.py) on
--control-port, with a fake systemd behind it, so the power button can be
tried end to end: start / stop / restart move through activating /
deactivating, and "power off" really takes both servers down (the app then
sees an unreachable Pi, as it would after a real shutdown). The control
token is --token (default "demo"). Use --control-port 0 to turn it off.

Usage:
    python tools/mock_rover_server.py --port 8080
"""

import argparse
import json
import math
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sensor_side"))
import threading

import pitfusion_control as pc
from basestation.core.protocol import COUNT_STRUCT, POINT_DTYPE


def make_frame_points(n: int, t: float) -> np.ndarray:
    phi = np.random.uniform(0, np.pi, n)
    theta = np.random.uniform(0, 2 * np.pi, n) + t
    r = 5.0

    arr = np.zeros(n, dtype=POINT_DTYPE)
    arr["xyz"][:, 0] = r * np.sin(phi) * np.cos(theta)
    arr["xyz"][:, 1] = r * np.sin(phi) * np.sin(theta)
    arr["xyz"][:, 2] = r * np.cos(phi)

    arr["rgb"][:, 0] = (np.sin(theta) + 1) / 2
    arr["rgb"][:, 1] = (np.cos(phi) + 1) / 2
    arr["rgb"][:, 2] = (np.cos(theta) + 1) / 2

    thermal_c = 20.0 + 15.0 * np.sin(phi) + np.random.normal(0, 0.5, n)
    arr["thermal_k"] = thermal_c + 273.15
    nan_mask = np.random.random(n) < 0.02  # sprinkle in some missing readings
    arr["thermal_k"][nan_mask] = np.nan
    return arr


START = time.time()
SERVERS: list = []


def make_terrain_points(n: int, t: float) -> np.ndarray:
    """A survey-like scene: rolling ground, a mound, a pit, two warm patches."""
    rng = np.random.default_rng(7)               # fixed: the scene holds still
    x = rng.uniform(-6, 6, n)
    y = rng.uniform(-6, 6, n)
    z = (0.5 * np.sin(0.55 * x) + 0.4 * np.cos(0.45 * y)
         + 1.7 * np.exp(-((x - 2.0) ** 2 + (y + 1.0) ** 2) / 2.2)       # mound
         - 1.5 * np.exp(-((x + 2.5) ** 2 + (y - 2.0) ** 2) / 1.6)       # pit
         + rng.normal(0, 0.03, n))
    h = np.clip((z + 1.6) / 3.4, 0, 1)
    arr = np.zeros(n, dtype=POINT_DTYPE)
    arr["xyz"][:, 0], arr["xyz"][:, 1], arr["xyz"][:, 2] = x, y, z
    # three-stop ground colour: dark soil (low) -> sand (mid) -> green (high)
    low, mid, high = (np.array(c)[None, :] for c in ((0.30, 0.21, 0.15), (0.70, 0.58, 0.38), (0.34, 0.56, 0.26)))
    k = h[:, None]
    rgb = np.where(k < 0.5, low + (mid - low) * (k / 0.5), mid + (high - mid) * ((k - 0.5) / 0.5))
    arr["rgb"] = np.clip(rgb + rng.normal(0, 0.025, (n, 1)), 0, 1)
    hot = (38 * np.exp(-((x - 2.0) ** 2 + (y + 1.0) ** 2) / 0.9)          # hot spot on the mound
           + 22 * np.exp(-((x + 3.5) ** 2 + (y + 3.0) ** 2) / 0.6))
    arr["thermal_k"] = 291.0 + hot + rng.normal(0, 0.4, n) + 1.5 * h
    return arr


class FakeSystemd:
    """Stands in for `systemctl --user` / `sudo systemctl poweroff`."""

    def __init__(self):
        self.state = "active"
        self.since = time.strftime("%a %Y-%m-%d %H:%M:%S")

    def _later(self, delay, state):
        def settle():
            self.state = state
            self.since = time.strftime("%a %Y-%m-%d %H:%M:%S")
        t = threading.Timer(delay, settle)
        t.daemon = True
        t.start()

    def __call__(self, cmd, timeout=20):
        if cmd[:2] == ["systemctl", "--user"]:
            verb = cmd[2]
            if verb == "show":
                return 0, (f"ActiveState={self.state}\nSubState=running\n"
                           f"ActiveEnterTimestamp={self.since}")
            if verb == "stop":
                self.state = "deactivating"
                self._later(1.5, "inactive")
                return 0, ""
            if verb in ("start", "restart"):
                self.state = "activating"
                self._later(2.0, "active")
                return 0, ""
        if cmd[0] == "sudo":
            if "-l" in cmd:
                return 0, ""
            if cmd[-1] == "poweroff":
                print("[mock] POWER OFF - both servers going down", flush=True)
                for srv in SERVERS:
                    threading.Thread(target=srv.shutdown, daemon=True).start()
                return 0, ""
        return 1, "unsupported"


SYSTEMD = FakeSystemd()


def make_diagnostics() -> dict:
    """Plausible sensor-PC report; faults come and go on a 60 s cycle."""
    t = time.time() - START
    phase = t % 60.0
    thermal_down = 40.0 <= phase < 48.0
    jitter = lambda base, amp: round(base + amp * math.sin(t * 1.7), 2)
    return {
        "schema": 1, "timestamp": time.time(), "hostname": "pitfusion-mock",
        "uptime_s": 3600 + t,
        "sensors": {
            "camera": {"state": "streaming", "connected": True, "fps": jitter(14.8, 0.2),
                        "target_fps": 15, "last_frame_age_s": 0.05, "frames": int(t * 15),
                        "mb_per_s": 38.2, "topic": "/landfill/rgb/image_raw",
                        "node": "rgb_camera_node", "message": ""},
            "thermal": ({"state": "disconnected", "connected": False, "fps": 0.0,
                          "target_fps": 30, "last_frame_age_s": None,
                          "topic": "/landfill/thermal/image_raw",
                          "node": "thermal_camera_node",
                          "message": "ROS node is not running"} if thermal_down else
                         {"state": "streaming", "connected": True, "fps": jitter(29.6, 0.3),
                          "target_fps": 30, "last_frame_age_s": 0.03, "frames": int(t * 30),
                          "mb_per_s": 19.7, "topic": "/landfill/thermal/image_raw",
                          "node": "thermal_camera_node", "message": ""}),
            "lidar": {"state": "streaming", "connected": True, "fps": jitter(10.0, 0.1),
                       "target_fps": 10, "last_frame_age_s": 0.04, "frames": int(t * 10),
                       "mb_per_s": 4.1, "topic": "livox/lidar", "node": "livox_lidar_publisher",
                       "message": ""},
        },
        "pipelines": {
            "livox_restamp": {"state": "running", "connected": True, "fps": jitter(10.0, 0.1),
                               "target_fps": 10, "last_frame_age_s": 0.04},
            "sensor_fusion": {"state": "running", "connected": True, "fps": jitter(7.6, 0.4),
                               "target_fps": 10, "last_frame_age_s": 0.12,
                               "message": "Running below target rate"},
            "registration_merge": {"state": "running", "connected": True, "fps": jitter(0.98, 0.02),
                                    "target_fps": 1, "last_frame_age_s": 0.4},
            "web_stream": {"state": "running", "fps": 4.9, "unit": "requests/s",
                            "last_frame_age_s": 0.1},
        },
        "system": {"cpu_pct": jitter(62, 6), "mem_pct": 41.5, "temp_c": jitter(61, 2),
                   "disk_pct": 38.0, "load_1m": 2.1, "under_voltage_now": False,
                   "under_voltage_since_boot": phase > 20, "throttled_now": False,
                   "throttled_since_boot": False},
    }


class Handler(BaseHTTPRequestHandler):
    points_per_frame = 20000
    scene = "sphere"
    diagnostics_enabled = True

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def do_GET(self):
        if SYSTEMD.state != "active":      # pipeline stopped: nothing is served
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/diagnostics" and self.diagnostics_enabled:
            body = json.dumps(make_diagnostics()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/latest.bin":
            self.send_response(404)
            self.end_headers()
            return

        points = (make_terrain_points if self.scene == "terrain" else make_frame_points)(
            self.points_per_frame, time.time())
        body = COUNT_STRUCT.pack(len(points)) + points.tobytes()

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Mock rover HTTP point-cloud server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--points", type=int, default=20000)
    parser.add_argument("--scene", choices=("sphere", "terrain"), default="sphere",
                        help="synthetic scene to serve (terrain is handy for screenshots)")
    parser.add_argument("--no-diagnostics", action="store_true",
                        help="answer /diagnostics with 404, like an un-upgraded sensor PC")
    parser.add_argument("--control-port", type=int, default=8081,
                        help="port for the power/control daemon (0 = off)")
    parser.add_argument("--token", default="demo", help="control token the app must send")
    args = parser.parse_args()

    Handler.points_per_frame = args.points
    Handler.scene = args.scene
    Handler.diagnostics_enabled = not args.no_diagnostics
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    SERVERS.append(server)
    if args.control_port:
        pc.shutil.which = lambda _name: "/usr/bin/systemctl"   # pretend we're on a Pi
        control = ThreadingHTTPServer(
            (args.host, args.control_port),
            pc.make_handler(lambda: args.token, runner=SYSTEMD))
        control.daemon_threads = True
        SERVERS.append(control)
        threading.Thread(target=control.serve_forever, daemon=True).start()
        print(f"Control daemon on port {args.control_port}, token '{args.token}'")
    print(f"Serving synthetic point clouds at http://{args.host}:{args.port}/latest.bin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
