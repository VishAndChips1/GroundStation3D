"""Client side of the sensor-PC power/control daemon (sensor_side/pitfusion_control.py).

    GET  http://<host>:<control-port>/status
    POST http://<host>:<control-port>/control   {"action": ...}  + X-Control-Token

No Qt here -- plain, unit-testable. The Qt threads that call these live in
network.py.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_CONTROL_PORT = 8081

# What the power button can show.
ON, OFF, STARTING, STOPPING, FAILED, OFFLINE, NO_DAEMON = (
    "on", "off", "starting", "stopping", "failed", "offline", "no_daemon")
BUSY = (STARTING, STOPPING)

_SYSTEMD_TO_UI = {"active": ON, "inactive": OFF, "activating": STARTING,
                  "reloading": STARTING, "deactivating": STOPPING, "failed": FAILED}


def ui_state(pipeline: str) -> str:
    """Map a systemd ActiveState to a power-button state."""
    return _SYSTEMD_TO_UI.get(str(pipeline).lower(), OFF)


@dataclass
class ControlStatus:
    """kind: ok | unreachable | no_daemon"""
    kind: str
    state: str = OFFLINE
    control_enabled: bool = False
    can_shutdown: bool = False
    hostname: str = ""
    since: str = ""
    detail: str = ""


def get_status(host: str, port: int, timeout: float = 2.0) -> ControlStatus:
    url = f"http://{host}:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ControlStatus("no_daemon", NO_DAEMON,
                                 detail="Nothing answers as a control daemon here")
        return ControlStatus("unreachable", OFFLINE, detail=f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError):
        return ControlStatus("unreachable", OFFLINE, detail="Sensor PC not answering")
    except ValueError:
        return ControlStatus("no_daemon", NO_DAEMON, detail="Unreadable reply")
    if not isinstance(data, dict):
        return ControlStatus("no_daemon", NO_DAEMON, detail="Unreadable reply")
    return ControlStatus(
        "ok", ui_state(data.get("pipeline", "")),
        control_enabled=data.get("control") == "enabled",
        can_shutdown=bool(data.get("can_shutdown")),
        hostname=str(data.get("hostname", "")), since=str(data.get("since", "")))


def send_action(host: str, port: int, action: str, token: str,
                timeout: float = 30.0) -> tuple[bool, str]:
    """POST one command. Returns (ok, human-readable message)."""
    body = json.dumps({"action": action}).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/control", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Control-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return bool(data.get("ok")), str(data.get("message", ""))
    except urllib.error.HTTPError as exc:
        try:
            msg = str(json.loads(exc.read()).get("error", ""))
        except (ValueError, AttributeError, OSError):
            msg = ""
        if exc.code == 403:
            msg = "Wrong control token"
        elif exc.code == 503:
            msg = msg or "Control is disabled on the sensor PC (no token set)"
        return False, msg or f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError):
        return False, "Sensor PC not answering"
    except ValueError:
        return False, "Unreadable reply"
