"""Sensor-PC health reporter: answers GET /diagnostics for the base station.

DROP-IN for PitFusion3D's `landfill_camera_sim/scripts/` (next to
fusion_web_streamer.py). It watches the rig's ROS topics and nodes, plus
the Pi's own vitals, and renders one JSON document the base station polls.
See sensor_side/README.md for the two-line hook into fusion_web_streamer.

What it measures, and how:

  * Per-sensor / per-stage frame rate and "age of last frame" come from
    raw subscriptions (raw=True): rclpy hands over the serialized bytes
    without building Python message objects, so even 1280x720 images cost
    one memcpy, not a full deserialize. Nothing is decoded or stored.
  * "Connected" = someone is publishing on the topic AND the owning node
    is alive. A camera that is unplugged shows up as a missing publisher or
    a stalled stream, not as a silent zero.
  * System vitals come from /proc, /sys and `vcgencmd get_throttled`
    (under-voltage is the known failure mode on this rig's power bank), all
    sampled on a background thread so an HTTP request never blocks on them.

Schema (all fields optional except the section names; the base station is
tolerant of extras and shows them generically):

    {
      "schema": 1, "timestamp": <unix s>, "hostname": "...", "uptime_s": N,
      "sensors":   { "<name>": ENTRY, ... },
      "pipelines": { "<name>": ENTRY, ... },
      "system":    { "cpu_pct", "mem_pct", "temp_c", "disk_pct",
                     "load_1m", "under_voltage_now", "under_voltage_since_boot",
                     "throttled_now", "throttled_since_boot" }
    }
    ENTRY = { "state": "streaming|running|waiting|stalled|disconnected|not_running",
              "connected": bool, "fps": float, "target_fps": float,
              "last_frame_age_s": float, "frames": int, "mb_per_s": float,
              "topic": str, "node": str, "message": str }
"""

import collections
import os
import shutil
import socket
import subprocess
import threading
import time

SCHEMA_VERSION = 1
RATE_WINDOW_S = 5.0     # fps is measured over this trailing window
STALL_AFTER_S = 3.0     # no message for this long => "stalled"


class RateTracker:
    """Thread-safe frame-rate / freshness tracker for one topic."""

    def __init__(self, window_s: float = RATE_WINDOW_S, clock=time.monotonic):
        self._clock = clock
        self._window = window_s
        self._lock = threading.Lock()
        self._stamps = collections.deque()   # (t, nbytes)
        self.total = 0
        self._last_t = None

    def tick(self, nbytes: int = 0) -> None:
        now = self._clock()
        with self._lock:
            self._stamps.append((now, nbytes))
            self.total += 1
            self._last_t = now
            self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while self._stamps and self._stamps[0][0] < cutoff:
            self._stamps.popleft()

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            self._trim(now)
            n = len(self._stamps)
            if n >= 2:
                span = self._stamps[-1][0] - self._stamps[0][0]
                fps = (n - 1) / span if span > 0 else 0.0
                mb_s = sum(b for _, b in self._stamps) / max(span, 1e-6) / 1e6
            else:
                fps, mb_s = 0.0, 0.0
            age = None if self._last_t is None else now - self._last_t
            return {"fps": fps, "mb_per_s": mb_s, "age": age, "frames": self.total}


class SystemSampler:
    """Background sampler for Pi vitals; read via .latest (never blocks)."""

    def __init__(self, interval_s: float = 2.0):
        self.latest: dict = {}
        self._interval = interval_s
        self._prev_cpu = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.latest = self._sample()
            except Exception:
                pass  # diagnostics must never take the streamer down
            self._stop.wait(self._interval)

    def _sample(self) -> dict:
        out: dict = {}
        # CPU %, from the delta between two reads of /proc/stat.
        try:
            with open("/proc/stat") as fh:
                parts = [float(x) for x in fh.readline().split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
            total = sum(parts)
            if self._prev_cpu is not None:
                d_total = total - self._prev_cpu[0]
                d_idle = idle - self._prev_cpu[1]
                if d_total > 0:
                    out["cpu_pct"] = round(100.0 * (1.0 - d_idle / d_total), 1)
            self._prev_cpu = (total, idle)
        except OSError:
            pass
        try:
            info = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    key, val = line.split(":", 1)
                    info[key] = float(val.split()[0])  # kB
            total_kb, avail_kb = info["MemTotal"], info["MemAvailable"]
            out["mem_pct"] = round(100.0 * (1.0 - avail_kb / total_kb), 1)
            out["mem_total_mb"] = round(total_kb / 1024.0)
        except (OSError, KeyError, ValueError):
            pass
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as fh:
                out["temp_c"] = round(int(fh.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            pass
        try:
            out["load_1m"] = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError):
            pass
        try:
            usage = shutil.disk_usage("/")
            out["disk_pct"] = round(100.0 * usage.used / usage.total, 1)
        except OSError:
            pass
        out.update(self._throttle_flags())
        return out

    @staticmethod
    def _throttle_flags() -> dict:
        """Pi firmware power flags -- the under-voltage alarm for this rig."""
        try:
            res = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                                 text=True, timeout=1.5)
            bits = int(res.stdout.strip().split("=")[1], 16)
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            return {}
        return {
            "under_voltage_now": bool(bits & (1 << 0)),
            "throttled_now": bool(bits & ((1 << 1) | (1 << 2) | (1 << 3))),
            "under_voltage_since_boot": bool(bits & (1 << 16)),
            "throttled_since_boot": bool(bits & ((1 << 17) | (1 << 18) | (1 << 19))),
        }


# name -> (topic, owning ROS node, target fps, message type name)
# Targets mirror real_sensors.launch.py / sensor_fusion.launch.py defaults.
DEFAULT_SENSORS = {
    "camera":  ("/landfill/rgb/image_raw",     "rgb_camera_node",       15.0, "Image"),
    "thermal": ("/landfill/thermal/image_raw", "thermal_camera_node",   30.0, "Image"),
    "lidar":   ("livox/lidar",                 "livox_lidar_publisher", 10.0, "PointCloud2"),
}
DEFAULT_PIPELINES = {
    "livox_restamp":      ("/landfill/livox/points",       "livox_restamp_node",       10.0, "PointCloud2"),
    "sensor_fusion":      ("/landfill/fusion/points_rgbt", "sensor_fusion_node",       10.0, "PointCloud2"),
    "registration_merge": ("/landfill/fusion/points_merged", "registration_merge_node", 1.0, "PointCloud2"),
}


def classify(publishers: int, node_alive, snap: dict, *, is_sensor: bool) -> tuple[str, str]:
    """(state, human message) for one sensor/stage from raw observations."""
    down = "disconnected" if is_sensor else "not_running"
    if node_alive is False:
        return down, "ROS node is not running"
    if publishers == 0:
        return down, "No publisher on topic"
    if snap["age"] is None:
        return "waiting", "Publisher present, no frames received yet"
    if snap["age"] > STALL_AFTER_S:
        return "stalled", f"No frame for {snap['age']:.1f}s"
    return ("streaming" if is_sensor else "running"), ""


class DiagnosticsCollector:
    def __init__(self, sensors=None, pipelines=None, clock=time.monotonic):
        self._specs = {"sensors": dict(sensors or DEFAULT_SENSORS),
                       "pipelines": dict(pipelines or DEFAULT_PIPELINES)}
        self._trackers: dict[str, RateTracker] = {}
        for section in self._specs.values():
            for name, (topic, *_rest) in section.items():
                # Topics can repeat across sections; share one tracker.
                self._trackers.setdefault(topic, RateTracker(clock=clock))
        self._web = RateTracker(clock=clock)
        self._system = SystemSampler()
        self._node = None
        self._boot = time.time()

    # -- ROS wiring (lazy imports so the pure logic is testable anywhere) --

    def attach(self, node) -> None:
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import Image, PointCloud2
        types = {"Image": Image, "PointCloud2": PointCloud2}
        self._node = node
        self._subs = []
        seen = set()
        for section in self._specs.values():
            for topic, _node_name, _target, type_name in section.values():
                if topic in seen:
                    continue
                seen.add(topic)
                tracker = self._trackers[topic]
                self._subs.append(node.create_subscription(
                    types[type_name], topic,
                    lambda data, t=tracker: t.tick(len(data)),
                    qos_profile_sensor_data, raw=True))
        self._system.start()

    def note_web_request(self) -> None:
        """Call once per /latest.bin (or /live.bin) request served."""
        self._web.tick()

    # -- report ----------------------------------------------------------

    def report(self) -> dict:
        names = set()
        if self._node is not None:
            try:
                names = set(self._node.get_node_names())
            except Exception:
                pass
        out = {
            "schema": SCHEMA_VERSION,
            "timestamp": time.time(),
            "hostname": socket.gethostname(),
            "uptime_s": self._uptime(),
            "sensors": {},
            "pipelines": {},
            "system": dict(self._system.latest),
        }
        for section, is_sensor in (("sensors", True), ("pipelines", False)):
            for name, (topic, node_name, target, _t) in self._specs[section].items():
                snap = self._trackers[topic].snapshot()
                pubs = 0
                if self._node is not None:
                    try:
                        pubs = self._node.count_publishers(topic)
                    except Exception:
                        pass
                alive = (node_name in names) if self._node is not None else None
                state, message = classify(pubs, alive, snap, is_sensor=is_sensor)
                entry = {
                    "state": state,
                    "connected": state not in ("disconnected", "not_running"),
                    "fps": round(snap["fps"], 2),
                    "target_fps": target,
                    "last_frame_age_s": None if snap["age"] is None else round(snap["age"], 2),
                    "frames": snap["frames"],
                    "mb_per_s": round(snap["mb_per_s"], 2),
                    "topic": topic,
                    "node": node_name,
                    "message": message,
                }
                out[section][name] = entry

        web = self._web.snapshot()
        out["pipelines"]["web_stream"] = {
            "state": "running",
            "connected": True,
            "fps": round(web["fps"], 2),   # requests served per second
            "frames": web["frames"],
            "last_frame_age_s": None if web["age"] is None else round(web["age"], 2),
            "message": "" if web["age"] is not None else "No viewer has fetched a frame yet",
            "unit": "requests/s",
        }
        return out

    @staticmethod
    def _uptime() -> float | None:
        try:
            with open("/proc/uptime") as fh:
                return float(fh.read().split()[0])
        except (OSError, ValueError):
            return None
