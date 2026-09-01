"""Dev-only mock rover server for testing the base station without hardware.

Serves a spinning, colored sphere of points with a fake thermal field at
GET /latest.bin, using the same binary layout as the real rover endpoint
(see protocol.py): uint32 point count, then N x 7 float32 (x,y,z,r,g,b,
thermal-Kelvin, occasionally NaN to exercise that path).

Usage:
    python tools/test_sender.py --port 8080
"""

import argparse
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import COUNT_STRUCT, POINT_DTYPE


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


class Handler(BaseHTTPRequestHandler):
    points_per_frame = 20000

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def do_GET(self):
        if self.path != "/latest.bin":
            self.send_response(404)
            self.end_headers()
            return

        points = make_frame_points(self.points_per_frame, time.time())
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
    args = parser.parse_args()

    Handler.points_per_frame = args.points
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving synthetic point clouds at http://{args.host}:{args.port}/latest.bin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
