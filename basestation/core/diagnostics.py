"""Client side of the sensor-PC diagnostics feed.

The sensor PC answers `GET http://<host>:<port>/diagnostics` with a JSON
document (schema documented in sensor_side/sensor_diagnostics.py). This
module fetches it and turns it into `Report` objects with a health status
already worked out, so the GUI only has to draw them.

Health is decided here rather than on the sensor PC on purpose: thresholds
can change without touching the rig, and a sender that only reports raw
numbers (fps, connected, state) is enough. The parser is tolerant -- unknown
fields are kept and shown generically, missing ones are skipped.

No Qt imports: this is plain Python and unit-testable.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

OK, WARN, ERROR, UNKNOWN = "ok", "warn", "error", "unknown"
_SEVERITY = {UNKNOWN: 0, OK: 1, WARN: 2, ERROR: 3}

_BAD_STATES = {"stopped", "failed", "crashed", "error", "dead", "disconnected",
               "not_running", "missing", "offline"}
_WARN_STATES = {"starting", "stalled", "degraded", "waiting", "reconnecting",
                "initializing", "initialising", "paused"}
_GOOD_STATES = {"running", "ok", "connected", "streaming", "active", "ready", "online"}

FPS_WARN_RATIO = 0.85     # below this fraction of target -> warn
FPS_ERROR_RATIO = 0.50    # below this -> error
AGE_WARN_S = 2.0
AGE_ERROR_S = 5.0

# Keys consumed by the entry renderer itself; everything else is shown as
# an extra metric so a sender can add fields without a base-station change.
_CORE_KEYS = {"state", "connected", "fps", "target_fps", "last_frame_age_s",
              "message", "error", "status", "name", "unit"}

_LABELS = {"mb_per_s": ("Bandwidth", "{:.2f} MB/s"),
           "frames": ("Frames", "{:,.0f}"),
           "latency_ms": ("Latency", "{:.0f} ms"),
           "cpu_pct": ("CPU", "{:.0f} %"),
           "restarts": ("Restarts", "{:.0f}"),
           "dropped": ("Dropped", "{:,.0f}")}


def worst(statuses) -> str:
    result = UNKNOWN
    for s in statuses:
        if _SEVERITY.get(s, 0) > _SEVERITY[result]:
            result = s
    return result


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass
class Item:
    """One sensor or pipeline stage."""
    name: str
    status: str = UNKNOWN
    state: str = ""
    fps: float | None = None
    target_fps: float | None = None
    unit: str = "fps"
    age_s: float | None = None
    message: str = ""
    metrics: list = field(default_factory=list)   # [(label, text)]


@dataclass
class Vital:
    label: str
    value: str
    status: str = OK


@dataclass
class Report:
    hostname: str = ""
    uptime_s: float | None = None
    sensor_time: float | None = None
    sensors: list = field(default_factory=list)
    pipelines: list = field(default_factory=list)
    system: list = field(default_factory=list)
    latency_s: float = 0.0

    @property
    def overall(self) -> str:
        return worst([i.status for i in self.sensors + self.pipelines]
                     + [v.status for v in self.system])


# -- parsing --------------------------------------------------------------

def _pretty(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _fmt_value(key: str, value):
    if key in _LABELS and _num(value) is not None:
        label, fmt = _LABELS[key]
        return label, fmt.format(value)
    if isinstance(value, bool):
        return _pretty(key), "yes" if value else "no"
    if isinstance(value, float):
        return _pretty(key), f"{value:.2f}"
    return _pretty(key), str(value)


def classify_item(d: dict) -> tuple[str, str]:
    """(status, short state text) from raw fields of one entry."""
    reasons = []
    status = UNKNOWN

    def raise_to(level):
        nonlocal status
        status = worst([status, level])

    explicit = str(d.get("status", "")).lower()
    if explicit in ("ok", "good", "healthy"):
        raise_to(OK)
    elif explicit in ("warn", "warning", "degraded"):
        raise_to(WARN)
    elif explicit in ("error", "fail", "failed", "critical"):
        raise_to(ERROR)

    state = str(d.get("state", "")).strip().lower()
    if state in _BAD_STATES:
        raise_to(ERROR)
    elif state in _WARN_STATES:
        raise_to(WARN)
    elif state in _GOOD_STATES:
        raise_to(OK)

    connected = d.get("connected")
    if connected is False:
        raise_to(ERROR)
        state = state or "disconnected"
    elif connected is True:
        raise_to(OK)
        state = state or "connected"

    fps, target = _num(d.get("fps")), _num(d.get("target_fps"))
    if fps is not None:
        raise_to(OK)
        if target and target > 0:
            ratio = fps / target
            if ratio < FPS_ERROR_RATIO:
                raise_to(ERROR)
            elif ratio < FPS_WARN_RATIO:
                raise_to(WARN)
        elif fps <= 0 and status == OK and state in _GOOD_STATES:
            raise_to(ERROR)   # claims to be running but produces nothing

    age = _num(d.get("last_frame_age_s"))
    if age is not None:
        if age > AGE_ERROR_S:
            raise_to(ERROR)
        elif age > AGE_WARN_S:
            raise_to(WARN)

    if d.get("error"):
        raise_to(WARN)
    return status, state or "-"


def _parse_item(name: str, d: dict) -> Item:
    status, state = classify_item(d)
    item = Item(name=name, status=status, state=state,
                fps=_num(d.get("fps")), target_fps=_num(d.get("target_fps")),
                unit=str(d.get("unit") or "fps"),
                age_s=_num(d.get("last_frame_age_s")),
                message=str(d.get("message") or d.get("error") or ""))
    for key, value in d.items():
        if key in _CORE_KEYS or value is None or isinstance(value, (dict, list)):
            continue
        if key in ("topic", "node"):
            item.metrics.append((_pretty(key), str(value)))
            continue
        item.metrics.append(_fmt_value(key, value))
    return item


def _parse_section(raw) -> list:
    items = []
    if isinstance(raw, dict):
        for name, d in raw.items():
            if isinstance(d, dict):
                items.append(_parse_item(str(name), d))
    elif isinstance(raw, list):
        for i, d in enumerate(raw):
            if isinstance(d, dict):
                items.append(_parse_item(str(d.get("name", f"item {i + 1}")), d))
    return items


def _parse_system(s) -> list:
    if not isinstance(s, dict):
        return []
    vitals = []

    def pct(key, label, warn, err):
        v = _num(s.get(key))
        if v is not None:
            vitals.append(Vital(label, f"{v:.0f} %",
                                ERROR if v >= err else WARN if v >= warn else OK))

    pct("cpu_pct", "CPU", 85, 97)
    pct("mem_pct", "Memory", 85, 95)
    t = _num(s.get("temp_c"))
    if t is not None:
        vitals.append(Vital("SoC temperature", f"{t:.1f} °C",
                            ERROR if t >= 82 else WARN if t >= 75 else OK))
    pct("disk_pct", "Disk", 85, 95)
    load = _num(s.get("load_1m"))
    if load is not None:
        vitals.append(Vital("Load (1 min)", f"{load:.2f}"))
    if "under_voltage_now" in s:
        now, past = s["under_voltage_now"], s.get("under_voltage_since_boot")
        vitals.append(Vital(
            "Power", "UNDER-VOLTAGE NOW" if now
            else "under-voltage occurred" if past else "stable",
            ERROR if now else WARN if past else OK))
    if s.get("throttled_now"):
        vitals.append(Vital("CPU throttling", "active", ERROR))
    elif s.get("throttled_since_boot"):
        vitals.append(Vital("CPU throttling", "occurred since boot", WARN))
    return vitals


def parse_report(data: dict, latency_s: float = 0.0) -> Report:
    if not isinstance(data, dict):
        raise ValueError("diagnostics body is not a JSON object")
    return Report(
        hostname=str(data.get("hostname") or ""),
        uptime_s=_num(data.get("uptime_s")),
        sensor_time=_num(data.get("timestamp")),
        sensors=_parse_section(data.get("sensors")),
        pipelines=_parse_section(data.get("pipelines")),
        system=_parse_system(data.get("system")),
        latency_s=latency_s,
    )


# -- fetching -------------------------------------------------------------

@dataclass
class FetchResult:
    """kind: ok | unsupported (reachable, no /diagnostics) | unreachable | bad"""
    kind: str
    report: Report | None = None
    detail: str = ""


def fetch(host: str, port: int, timeout: float = 3.0) -> FetchResult:
    url = f"http://{host}:{port}/diagnostics"
    start = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return FetchResult(
                "unsupported",
                detail="The sensor PC is reachable but does not serve /diagnostics yet.")
        return FetchResult("unreachable", detail=f"HTTP {exc.code} from sensor PC")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = str(getattr(exc, "reason", exc))
        if "refused" in reason.lower():
            reason = "Connection refused - is the rover server running?"
        elif "timed out" in reason.lower() or isinstance(exc, TimeoutError):
            reason = "Timed out - sensor PC not answering"
        return FetchResult("unreachable", detail=reason[:100])
    latency = time.time() - start
    try:
        return FetchResult("ok", parse_report(json.loads(body), latency))
    except ValueError as exc:   # includes json.JSONDecodeError
        return FetchResult("bad", detail=f"Unreadable diagnostics: {exc}"[:100])
