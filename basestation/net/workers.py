"""Networking and recording: polling the rover, scanning for senders, and
saving frames. Qt threads only -- no widgets are touched from here."""

import http.client
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from PySide6.QtCore import QThread, Signal

from basestation.core import discovery
from basestation.core.protocol import MalformedFrameError, parse_frame
from basestation.core.quality import AdaptiveQuality

# Generous by default: over real WiFi a full-resolution frame can be several
# MB, and a tight timeout would fail every request on a marginal link.
DEFAULT_FETCH_TIMEOUT = 10.0
MAX_BODY_BYTES = 512 * 1024 * 1024    # refuse an absurd response rather than exhaust memory



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


class _NotModified(Exception):
    """The server answered 304: the frame has not changed since the last fetch."""


class _Stopped(Exception):
    """stop() was called while a download was in progress."""


class PollWorker(QThread):
    """Polls the rover endpoint on a background thread.

    Frames do NOT travel through the signal queue. The worker keeps only the
    newest frame in a one-slot mailbox and emits a bare `frame_ready` when the
    slot goes from empty to full; the GUI calls take_frame(). If the GUI is
    slower than the network (a heavy filter, a big cloud) frames are dropped
    instead of piling up in an unbounded queue -- which at several MB a frame
    used to grow memory and lag without limit.

    The thread never dies silently: every failure becomes an "error" state and
    a retry with growing back-off, including dropped connections mid-body
    (http.client.IncompleteRead is not an OSError) and anything unexpected.
    """

    frame_ready = Signal()
    state_changed = Signal(str, str)  # state ("live"/"connecting"/"error"), detail

    MAX_BACKOFF = 2.0

    def __init__(self, url: str, poll_interval: float, stats: NetworkStats,
                 timeout: float = DEFAULT_FETCH_TIMEOUT, adaptive: bool = True, compact: bool = True):
        super().__init__()
        self._url = url
        self._compact = compact              # ask for the smaller frame format once the rig says it has one
        self._rig_has_compact = False
        self._fetched_fmt = 0                # the format the most recent request asked for
        self._conn = None                    # one connection kept open between polls (a new one every poll restarts TCP's slow start)
        self._target = "/"
        self._adaptive = adaptive
        self._quality = AdaptiveQuality()
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._stats = stats
        self._stop_event = threading.Event()
        self._last_state = None
        self._lock = threading.Lock()
        self._latest = None
        self._signalled = False
        self._etag = None

    # -- called from the GUI thread ----------------------------------------

    def take_frame(self):
        """The newest unconsumed frame, or None."""
        with self._lock:
            frame, self._latest, self._signalled = self._latest, None, False
        return frame

    def stop(self) -> None:
        """Ask the thread to finish. A download in progress notices within
        ~0.5 s (see _read_body); only the connect phase has to run out its
        timeout, which is why callers retire a worker that is slow to stop."""
        self._stop_event.set()

    # -- worker thread ------------------------------------------------------

    def _post(self, frame) -> None:
        with self._lock:
            self._latest = frame            # a newer frame replaces an unconsumed one
            notify = not self._signalled
            self._signalled = True
        if notify:
            self.frame_ready.emit()

    def _emit_state(self, state: str, detail: str) -> None:
        # Only emit on change, so the GUI isn't spammed while idling.
        if (state, detail) != self._last_state:
            self._last_state = (state, detail)
            self.state_changed.emit(state, detail)

    def _connection(self):
        """The open connection to the rig, created on first use."""
        if self._conn is None:
            parts = urllib.parse.urlsplit(self._url)            # ValueError for an unusable address
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("unusable address")
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            self._conn = cls(parts.hostname, parts.port, timeout=self._timeout)
            self._target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return self._conn

    def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _request_target(self) -> str:
        """The path to ask for, with ?n=<points> once the rig has said it supports sizes."""
        n = self._quality.requested() if self._adaptive else None
        extras = []
        if self._compact and self._rig_has_compact:
            extras.append("fmt=2")
        if n is not None and "n=" not in self._target:
            extras.append(f"n={n}")
        if not extras:
            self._fetched_fmt = 0
            return self._target
        self._fetched_fmt = 2 if "fmt=2" in extras else 0
        return f"{self._target}{'&' if '?' in self._target else '?'}{'&'.join(extras)}"

    def _fetch(self) -> bytes:
        headers = {"Accept-Encoding": "gzip"}
        if self._etag:
            headers["If-None-Match"] = self._etag      # lets the rig answer 304
        resp = None
        for attempt in (0, 1):
            reused = self._conn is not None
            conn = self._connection()
            try:
                conn.request("GET", self._request_target(), headers=headers)
                if conn.sock is not None:
                    conn.sock.settimeout(self._timeout)   # _read_body shortens it; a reused socket must start fresh
                resp = conn.getresponse()
                break
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                self._drop_connection()
                if reused and attempt == 0:
                    continue                              # the rig closed an idle connection: reconnect once, quietly
                raise
            except BaseException:
                self._drop_connection()
                raise
        try:
            if resp.status == 304:
                resp.read()
                if resp.will_close:
                    self._drop_connection()
                raise _NotModified()
            if resp.status != 200:
                resp.read()
                raise urllib.error.HTTPError(self._url, resp.status, resp.reason, resp.headers, None)
            data = self._read_body(resp)
            self._etag = resp.headers.get("ETag") or self._etag
            self._rig_has_compact = "2" in [x.strip() for x in resp.headers.get("X-Formats", "").split(",")]
            if self._adaptive:
                levels = resp.headers.get("X-Quality-Levels", "")
                try:
                    self._quality.set_levels([int(x) for x in levels.split(",") if x.strip()])
                except ValueError:
                    pass                                  # a malformed header just means fixed quality
        except _NotModified:
            raise
        except BaseException:
            self._drop_connection()                       # state unknown after any failure
            raise
        if resp.will_close:
            self._drop_connection()
        return data

    def _read_body(self, resp) -> bytes:
        """Read the body in short slices so stop() is honoured within ~0.5 s.

        (Cutting the socket from another thread does not wake a blocked recv()
        on Windows, so the reader wakes itself instead.) read1() is used rather
        than read(n): on a timeout it has consumed nothing, so no bytes are lost.
        """
        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            try:
                sock.settimeout(0.5)
            except OSError:
                sock = None
        declared = resp.headers.get("Content-Length")
        chunks, total, last_progress = [], 0, time.time()
        while True:
            if self._stop_event.is_set():
                raise _Stopped()
            try:
                chunk = resp.read1(1 << 16)
            except TimeoutError:
                if sock is None:
                    raise
                if time.time() - last_progress > self._timeout:
                    raise TimeoutError("timed out waiting for the rest of the frame") from None
                continue
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            last_progress = time.time()
            if total > MAX_BODY_BYTES:
                raise MalformedFrameError("response is implausibly large")
        data = b"".join(chunks)
        if declared and declared.isdigit() and len(data) < int(declared):
            raise http.client.IncompleteRead(data, int(declared) - len(data))
        return data

    def _rest(self, started: float, failures: int) -> None:
        wait = self._poll_interval - (time.time() - started)
        if failures:
            # Back off while the link is down instead of hammering it.
            wait = max(wait, min(self._poll_interval * (2 ** min(failures, 6)), self.MAX_BACKOFF))
        if wait > 0:
            self._stop_event.wait(wait)

    def run(self) -> None:
        frame_id = 0
        failures = 0
        self._emit_state("connecting", "Connecting...")

        while not self._stop_event.is_set():
            start = time.time()
            try:
                data = self._fetch()
                recv_time = time.time()
                fetch_seconds = recv_time - start
                self._quality.report(fetch_seconds, recv_time)
                frame = parse_frame(data, frame_id, recv_time, fetch_seconds, fmt=self._fetched_fmt)
            except _Stopped:
                break
            except _NotModified:
                failures = 0                 # reachable, simply nothing new yet
                self._rest(start, 0)
                continue
            except urllib.error.HTTPError as exc:
                failures += 1
                self._fail(f"HTTP {exc.code} from server")
            except MalformedFrameError as exc:
                failures += 1
                self._etag = None            # don't ask "unchanged?" about a bad frame
                self._fail(f"Bad frame: {exc}"[:90])
            except (urllib.error.URLError, OSError, TimeoutError,
                    http.client.HTTPException, ValueError) as exc:
                if self._stop_event.is_set():
                    break                    # stop() closed the socket: not an error
                self._quality.report(0.0, time.time(), failed=True)
                failures += 1
                self._fail(self._describe_error(exc))
            except Exception as exc:         # last resort: never die silently
                failures += 1
                self._fail(f"Unexpected error: {exc!r}"[:90])
            else:
                failures = 0
                frame_id += 1
                self._stats.record_frame(len(data), frame.num_points, fetch_seconds)
                self._emit_state("live", f"Live - {frame.num_points:,} points")
                self._post(frame)
            self._rest(start, failures)
        self._drop_connection()

    def _fail(self, detail: str) -> None:
        self._stats.record_failure()
        self._emit_state("error", detail)

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        if isinstance(exc, http.client.IncompleteRead):
            return "Connection dropped mid-frame - weak WiFi?"
        if isinstance(exc, http.client.RemoteDisconnected):
            return "The rover closed the connection"
        if isinstance(exc, ValueError):
            return "That address can't be used as a rover URL"
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
    POLL_TIMEOUT = 6.0         # a busy WiFi link can delay one reply by several seconds
    UNREACHABLE_AFTER = 3      # consecutive failed polls before the screen says "unreachable"

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._target = None
        self._fast = False
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._misses = 0
        self._seen_ok = False

    def set_target(self, host: str, port: int) -> None:
        with self._lock:
            self._target = (host, port) if host else None
            self._misses = 0
            self._seen_ok = False
        self._wake.set()

    def set_fast(self, fast: bool) -> None:
        self._fast = fast

    def poll_now(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def _debounce(self, st) -> bool:
        """True if this reply should reach the screen.

        One slow reply on a busy WiFi link must not flip a working rig to "unreachable": once the rig has
        answered, only UNREACHABLE_AFTER failures in a row count. Before it has ever answered, report at once.
        """
        if st.kind == "unreachable":
            self._misses += 1
            needed = 1 if self._fast else self.UNREACHABLE_AFTER     # while stopping/powering off, "gone" is the expected answer
            return not self._seen_ok or self._misses >= needed
        self._misses = 0
        if st.kind == "ok":
            self._seen_ok = True
        return True

    def run(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                target = self._target
            if target is not None:
                try:
                    st = control.get_status(*target, timeout=3.0 if self._fast else self.POLL_TIMEOUT)
                    st.target = target       # lets the GUI discard a late reply for an old host
                    if self._debounce(st):
                        self.status.emit(st)
                except Exception:            # a bad poll must not end the loop
                    pass
            self._wake.wait(self.FAST if self._fast else self.SLOW)
            self._wake.clear()


class ActionWorker(QThread):
    """Sends one command (can block for seconds while systemd stops nodes)."""

    done = Signal(str, bool, str)   # action, ok, message

    def __init__(self, host: str, port: int, action: str, token: str):
        super().__init__()
        self._args = (host, port, action, token)

    def run(self) -> None:
        try:
            ok, message = control.send_action(*self._args)
        except Exception as exc:             # always report back, or the UI waits forever
            ok, message = False, f"Unexpected error: {exc!r}"[:90]
        self.done.emit(self._args[2], ok, message)
