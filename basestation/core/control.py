"""Client side of the sensor-PC power/control daemon (sensor_side/pitfusion_control.py).

    GET  http://<host>:<control-port>/status
    POST http://<host>:<control-port>/control   {"action": ...}  + X-Control-Token

No Qt here -- plain, unit-testable. The Qt threads that call these live in
network.py.
"""

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from basestation.core.netutil import http_url, read_body

DEFAULT_CONTROL_PORT = 8081

# What the power button can show.
ON, OFF, STARTING, STOPPING, FAILED, OFFLINE, NO_DAEMON, UNKNOWN = (
    "on", "off", "starting", "stopping", "failed", "offline", "no_daemon", "unknown")
MAX_REPLY_BYTES = 100_000
BUSY = (STARTING, STOPPING)

_SYSTEMD_TO_UI = {"active": ON, "inactive": OFF, "activating": STARTING,
                  "reloading": STARTING, "deactivating": STOPPING, "failed": FAILED}


def ui_state(pipeline: str) -> str:
    """Map a systemd ActiveState to a power-button state.

    Anything unrecognised (including systemd's own "unknown", e.g. when
    systemctl itself failed) is UNKNOWN -- never OFF, which would invite a
    click on "start" for a pipeline whose state nobody actually knows.
    """
    return _SYSTEMD_TO_UI.get(str(pipeline).lower(), UNKNOWN)


@dataclass
class ControlStatus:
    """kind: ok | unreachable | no_daemon"""
    kind: str
    state: str = OFFLINE
    control_enabled: bool = False
    can_shutdown: bool = False
    can_sleep: bool = False          # the sensor PC has a sleep hook installed
    full_poweroff: object = None     # True: a halted Pi 5 draws almost nothing; False: it stays in standby; None: unknown
    hostname: str = ""
    since: str = ""
    detail: str = ""
    target: tuple = ()       # (host, port) this status answers, set by the poller
    installed: bool = True   # False: the pipeline's systemd unit does not exist on the Pi


def get_status(host: str, port: int, timeout: float = 2.0) -> ControlStatus:
    url = http_url(host, port, "/status")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(read_body(resp, MAX_REPLY_BYTES))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ControlStatus("no_daemon", NO_DAEMON,
                                 detail="Nothing answers as a control daemon here")
        return ControlStatus("unreachable", OFFLINE, detail=f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError):
            # The machine answered and said "nothing here": it is up, but the
            # control service is not running on that port. Say so, rather than
            # blaming the address or the network.
            return ControlStatus(
                "no_daemon", NO_DAEMON,
                detail=f"The sensor PC is reachable, but nothing is listening for "
                       f"control commands on port {port}.")
        return ControlStatus(
            "unreachable", OFFLINE,
            detail=f"No reply from {host} on port {port}: wrong address, the Pi is off, "
                   f"or a firewall is blocking that port.")
    except (ValueError, RecursionError):
        return ControlStatus("no_daemon", NO_DAEMON, detail="Unreadable reply")
    if not isinstance(data, dict):
        return ControlStatus("no_daemon", NO_DAEMON, detail="Unreadable reply")
    return ControlStatus(
        "ok", ui_state(data.get("pipeline", "")),
        control_enabled=data.get("control") == "enabled",
        can_shutdown=bool(data.get("can_shutdown")),
        can_sleep=bool(data.get("can_sleep")),
        full_poweroff=data.get("full_poweroff") if isinstance(data.get("full_poweroff"), bool) else None,
        hostname=str(data.get("hostname", "")), since=str(data.get("since", "")),
        installed=bool(data.get("installed", True)))


def send_action(host: str, port: int, action: str, token: str,
                timeout: float = 30.0) -> tuple[bool, str]:
    """POST one command. Returns (ok, human-readable message)."""
    if action == "sleep":
        timeout = max(timeout, 90.0)     # stopping the pipeline and switching the LiDAR can take a while
    body = json.dumps({"action": action}).encode()
    req = urllib.request.Request(
        http_url(host, port, "/control"), data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Control-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(read_body(resp, MAX_REPLY_BYTES))
        if not isinstance(data, dict):
            return False, "Unreadable reply"
        return bool(data.get("ok")), str(data.get("message", ""))
    except urllib.error.HTTPError as exc:
        try:
            msg = str(json.loads(exc.read(MAX_REPLY_BYTES)).get("error", ""))
        except (ValueError, AttributeError, OSError, RecursionError, http.client.HTTPException):
            msg = ""
        if exc.code == 403:
            msg = "Wrong control token"
        elif exc.code == 503:
            msg = msg or "Control is disabled on the sensor PC (no token set)"
        return False, msg or f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException):
        return False, "Sensor PC not answering"
    except (ValueError, RecursionError):
        return False, "Unreadable reply"
