"""Networking and recording: polling the rover, scanning for senders, and
saving frames. Qt threads only -- no widgets are touched from here."""

import threading
import time
import urllib.error
import urllib.request
from collections import deque

from PySide6.QtCore import QThread, Signal

from basestation.core import discovery
from basestation.core.protocol import MalformedFrameError, parse_frame

# Generous by default: over real WiFi a full-resolution frame can be several
# MB, and a tight timeout would fail every request on a marginal link.
DEFAULT_FETCH_TIMEOUT = 10.0



class NetworkStats:
    """Rolling-window rate/fetch-time/frame counters, shared across threads."""

    def __init__(self, window_seconds: float = 1.0):
        self._lock = threading.Lock()
        self._window = window_seconds
        self._byte_log = deque()   # (t, nbytes) per HTTP response
        self._point_log = deque()  # (t, npoints) per decoded frame
        self.frames_received = 0
        self.frames_failed = 0
        self.last_fetch_seconds = None
        self.last_point_count = 0
        self.last_frame_time = None

    def record_frame(self, nbytes: int, npoints: int, fetch_seconds: float) -> None:
        now = time.time()
        with self._lock:
            self._byte_log.append((now, nbytes))
            self._point_log.append((now, npoints))
            self._trim(self._byte_log, now)
            self._trim(self._point_log, now)
            self.frames_received += 1
            self.last_fetch_seconds = fetch_seconds
            self.last_point_count = npoints
            self.last_frame_time = now

    def record_failure(self) -> None:
        with self._lock:
            self.frames_failed += 1

    def reset(self) -> None:
        with self._lock:
            self._byte_log.clear()
            self._point_log.clear()
            self.frames_received = 0
            self.frames_failed = 0
            self.last_fetch_seconds = None
            self.last_point_count = 0
            self.last_frame_time = None

    def _trim(self, log: deque, now: float) -> None:
        cutoff = now - self._window
        while log and log[0][0] < cutoff:
            log.popleft()

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._trim(self._byte_log, now)
            self._trim(self._point_log, now)
            return {
                "kbps": sum(n for _, n in self._byte_log) / 1024.0 / self._window,
                "pts_per_sec": sum(n for _, n in self._point_log) / self._window,
                "fps": len(self._byte_log) / self._window,
                "last_frame_time": self.last_frame_time,
                "frames_received": self.frames_received,
                "frames_failed": self.frames_failed,
                "fetch_seconds": self.last_fetch_seconds,
                "point_count": self.last_point_count,
            }


class PollWorker(QThread):
    """Polls the rover endpoint on a background thread.

    Frames are handed to the GUI thread through a queued Qt signal, so no
    GUI object is ever touched from here.
    """

    frame_ready = Signal(object)
    state_changed = Signal(str, str)  # state ("live"/"connecting"/"error"), detail

    def __init__(self, url: str, poll_interval: float, stats: NetworkStats,
                 timeout: float = DEFAULT_FETCH_TIMEOUT):
        super().__init__()
        self._url = url
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._stats = stats
        self._stop_event = threading.Event()
        self._last_state = None

    def stop(self) -> None:
        self._stop_event.set()

    def _emit_state(self, state: str, detail: str) -> None:
        # Only emit on change, so the GUI isn't spammed while idling.
        if (state, detail) != self._last_state:
            self._last_state = (state, detail)
            self.state_changed.emit(state, detail)

    def run(self) -> None:
        frame_id = 0
        self._emit_state("connecting", "Connecting...")

        while not self._stop_event.is_set():
            start = time.time()
            try:
                with urllib.request.urlopen(self._url, timeout=self._timeout) as resp:
                    data = resp.read()
            except urllib.error.HTTPError as exc:
                self._stats.record_failure()
                self._emit_state("error", f"HTTP {exc.code} from server")
                self._stop_event.wait(self._poll_interval)
                continue
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                self._stats.record_failure()
                self._emit_state("error", self._describe_error(exc))
                self._stop_event.wait(self._poll_interval)
                continue

            recv_time = time.time()
            fetch_seconds = recv_time - start

            try:
                frame = parse_frame(data, frame_id, recv_time, fetch_seconds)
            except MalformedFrameError as exc:
                self._stats.record_failure()
                self._emit_state("error", f"Bad frame: {exc}")
                self._stop_event.wait(self._poll_interval)
                continue

            frame_id += 1
            self._stats.record_frame(len(data), frame.num_points, fetch_seconds)
            self._emit_state("live", f"Live - {frame.num_points:,} points")
            self.frame_ready.emit(frame)

            elapsed = time.time() - start
            if elapsed < self._poll_interval:
                self._stop_event.wait(self._poll_interval - elapsed)

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        if "refused" in text.lower():
            return "Connection refused - is the rover server running?"
        if "timed out" in text.lower() or isinstance(exc, TimeoutError):
            return "Timed out - check the IP and that you're on the same network"
        if "unreachable" in text.lower():
            return "Host unreachable - check the IP"
        return text[:80]


class DiscoveryWorker(QThread):
    """Scans the local network for point-cloud senders, off the GUI thread."""

    found = Signal(str, int)      # host, point count
    progress = Signal(str)
    done = Signal(int)            # number of senders found

    def __init__(self, port: int):
        super().__init__()
        self._port = port
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.progress.emit("Scanning local network...")
        results = discovery.scan(
            self._port,
            on_found=lambda host, n: self.found.emit(host, n),
            should_stop=self._stop_event.is_set,
        )
        self.done.emit(len(results))


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

class FrameRecorder:
    """Saves incoming frames to disk on an interval, for a fixed duration."""

    def __init__(self):
        self.active = False
        self.folder = ""
        self.interval = 1.0
        self.duration = 30.0
        self.saved = 0
        self.failed = 0
        self._started_at = 0.0
        self._last_saved_at = 0.0

    def start(self, folder: str, interval: float, duration: float) -> None:
        self.folder = folder
        self.interval = interval
        self.duration = duration
        self.saved = 0
        self.failed = 0
        self._started_at = time.time()
        # Zero means "save the first frame that arrives".
        self._last_saved_at = 0.0
        self.active = True

    def stop(self) -> None:
        self.active = False

    def elapsed(self) -> float:
        return time.time() - self._started_at if self.active else 0.0

    def expired(self) -> bool:
        return self.active and self.elapsed() >= self.duration

    def should_save(self) -> bool:
        if not self.active:
            return False
        return (time.time() - self._last_saved_at) >= self.interval

    def note_saved(self, ok: bool) -> None:
        self._last_saved_at = time.time()
        if ok:
            self.saved += 1
        else:
            self.failed += 1




# --------------------------------------------------------------------------
# Sensor-PC power / control
# --------------------------------------------------------------------------

from basestation.core import control  # noqa: E402  (kept next to its only users)


class ControlWorker(QThread):
    """Polls the control daemon's /status, independent of the point-cloud
    stream, so the power button knows the pipeline state even when no
    frames are flowing (that is exactly when you want to start it)."""

    status = Signal(object)    # control.ControlStatus

    SLOW, FAST = 2.0, 0.7      # seconds between polls; fast while starting/stopping

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._target = None
        self._fast = False
        self._wake = threading.Event()
        self._stop_event = threading.Event()

    def set_target(self, host: str, port: int) -> None:
        with self._lock:
            self._target = (host, port) if host else None
        self._wake.set()

    def set_fast(self, fast: bool) -> None:
        self._fast = fast

    def poll_now(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                target = self._target
            if target is not None:
                self.status.emit(control.get_status(*target))
            self._wake.wait(self.FAST if self._fast else self.SLOW)
            self._wake.clear()


class ActionWorker(QThread):
    """Sends one command (can block for seconds while systemd stops nodes)."""

    done = Signal(str, bool, str)   # action, ok, message

    def __init__(self, host: str, port: int, action: str, token: str):
        super().__init__()
        self._args = (host, port, action, token)

    def run(self) -> None:
        ok, message = control.send_action(*self._args)
        self.done.emit(self._args[2], ok, message)
