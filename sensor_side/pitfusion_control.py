#!/usr/bin/env python3
"""Always-on power/control daemon for the PitFusion3D sensor PC.

Lets the base station start, stop and restart the sensor pipeline (the
`pitfusion3d.service` systemd user unit), put the rig to sleep, and shut the Pi down.

Why a separate process: "stop" tears down the whole pipeline, including
fusion_web_streamer.py. Anything hosted inside the pipeline could never
receive the following "start". This daemon has no ROS dependency and runs
as its own unit (pitfusion3d-control.service) so it survives the pipeline
stopping, crashing, or restarting.

    GET  /status                      no auth, read-only
    POST /control  {"action": ...}    header  X-Control-Token: <secret>
                   action = start | stop | restart | sleep | shutdown

Safety:
  * State-changing requests need the shared token. With no token configured
    the daemon answers 503 "control disabled" -- off by default.
  * Only the four whitelisted actions exist; nothing is passed through a
    shell, and no request data reaches a command line.
  * Plain HTTP, no encryption: for a trusted LAN or the rig's own hotspot.
    Anyone who can sniff the network can read the token.
  * "sleep" runs the operator's own hook script (default
    ~/.config/pitfusion3d/sleep_hook, or $PITFUSION_SLEEP_HOOK) with no arguments, e.g.
    to stop the pipeline and put a LiDAR into its lowest-power mode. The Pi stays up and
    reachable; "start" wakes the rig. With no hook installed, "sleep" is refused.
  * "shutdown" needs a sudoers rule (see sensor_side/README.md) and is not
    reversible from here -- a powered-off Pi must be power-cycled by hand.

Stdlib only.
"""

import argparse
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UNIT = "pitfusion3d.service"
ACTIONS = ("start", "stop", "restart", "sleep", "shutdown")
SLEEP_HOOK = os.environ.get("PITFUSION_SLEEP_HOOK") or os.path.expanduser("~/.config/pitfusion3d/sleep_hook")
DEFAULT_PORT = 8081
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.config/pitfusion3d/control_token")
MAX_BODY = 1024


def run(cmd, timeout=20):
    """Run an argv list (never a shell string); returns (returncode, stdout+stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return res.returncode, (res.stdout + res.stderr).strip()


def load_token(path: str) -> str:
    """Env var wins; else the token file. Empty string means control disabled."""
    env = os.environ.get("PITFUSION_CONTROL_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def token_ok(supplied: str, expected: str) -> bool:
    # Constant-time compare, and an empty expected token never matches.
    return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())


def pipeline_status(runner=run) -> dict:
    code, out = runner(["systemctl", "--user", "show", UNIT,
                        "--property=ActiveState,SubState,ActiveEnterTimestamp,LoadState"], 5)
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"pipeline": props.get("ActiveState", "unknown"),
            "sub": props.get("SubState", ""),
            "since": props.get("ActiveEnterTimestamp", ""),
            # False only when systemd positively says the unit does not exist; if
            # systemctl itself failed we don't know, so don't claim it is missing.
            "installed": props.get("LoadState", "") != "not-found"}


def can_shutdown(runner=run) -> bool:
    sysctl = shutil.which("systemctl")
    if not sysctl:
        return False
    code, _ = runner(["sudo", "-n", "-l", sysctl, "poweroff"], 5)
    return code == 0


def can_sleep() -> bool:
    """True when an executable sleep hook is installed."""
    return os.path.isfile(SLEEP_HOOK) and os.access(SLEEP_HOOK, os.X_OK)


def full_poweroff(runner=run):
    """True if a Pi 5 is set to cut all power when halted (EEPROM POWER_OFF_ON_HALT=1).

    False if it is not, None if it cannot be read (not a Pi, or vcgencmd missing).
    """
    code, out = runner(["vcgencmd", "bootloader_config"], 5)
    if code != 0 or "=" not in out:
        return None
    return any(line.replace(" ", "").strip() == "POWER_OFF_ON_HALT=1" for line in out.splitlines())


def perform(action: str, runner=run, schedule=None) -> tuple[bool, str]:
    """Carry out one whitelisted action. Returns (ok, message)."""
    if action not in ACTIONS:
        return False, f"unknown action {action!r}"
    if action == "shutdown":
        sysctl = shutil.which("systemctl")
        if not can_shutdown(runner):
            return False, ("shutdown is not permitted for this user - add the "
                           "sudoers rule described in sensor_side/README.md")
        # Answer first, then power off: the reply must leave before the NIC goes.
        (schedule or _after)(1.0, lambda: runner(["sudo", "-n", sysctl, "poweroff"], 10))
        return True, "shutting down"
    if action == "sleep":
        if not can_sleep():
            return False, "sleep is not set up on this sensor PC (no sleep hook installed)"
        code, out = runner([SLEEP_HOOK], 90)             # the operator's own script, no request data on its command line
        last = out.splitlines()[-1] if out else ""
        return (code == 0), (last or ("asleep" if code == 0 else "the sleep hook failed"))
    code, out = runner(["systemctl", "--user", action, UNIT], 25)
    return (code == 0), (out or f"{action} ok")


def _after(delay, fn):
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


SHUTDOWN_CHECK_TTL = 60.0


def make_handler(token_loader, runner=run, schedule=None, clock=time.monotonic):
    # `sudo -n -l` is a process spawn. The base station polls /status every
    # second or two, so asking on every poll burned CPU (and battery) on the Pi
    # for an answer that almost never changes. Ask at most once a minute.
    shutdown_cache = {"value": False, "at": None}
    poweroff_cache = {"value": None, "at": None}      # the EEPROM setting only changes at a reboot

    def cached_can_shutdown() -> bool:
        now = clock()
        if shutdown_cache["at"] is None or now - shutdown_cache["at"] >= SHUTDOWN_CHECK_TTL:
            shutdown_cache["value"] = can_shutdown(runner)
            shutdown_cache["at"] = now
        return shutdown_cache["value"]

    def cached_full_poweroff():
        now = clock()
        if poweroff_cache["at"] is None or now - poweroff_cache["at"] >= SHUTDOWN_CHECK_TTL:
            poweroff_cache["value"] = full_poweroff(runner)
            poweroff_cache["at"] = now
        return poweroff_cache["value"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Drop a keep-alive connection that goes quiet instead of holding its
        # thread forever (a laptop that slept, a dropped WiFi client).
        timeout = 15

        def log_message(self, fmt, *args):
            pass

        def _json(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/status":
                return self._json(404, {"error": "not found"})
            st = pipeline_status(runner)
            st.update({"hostname": socket.gethostname(),
                       "control": "enabled" if token_loader() else "disabled",
                       "can_shutdown": cached_can_shutdown(),
                       "can_sleep": can_sleep(),
                       "full_poweroff": cached_full_poweroff()})
            self._json(200, st)

        def do_POST(self):
            # Always consume the request body before replying, even when the
            # reply is a rejection. Answering first leaves unread bytes on the
            # socket, and closing it then can reset the connection before the
            # client has read the response (it would see "not answering"
            # instead of "wrong token").
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.close_connection = True      # can't safely reuse the socket
                return self._json(413 if length > MAX_BODY else 400,
                                  {"error": "bad or oversized request body"})
            raw = self.rfile.read(length) if length else b""

            if self.path != "/control":
                return self._json(404, {"error": "not found"})
            expected = token_loader()
            if not expected:
                return self._json(503, {"error": "control disabled: no token configured on the sensor PC"})
            if not token_ok(self.headers.get("X-Control-Token", ""), expected):
                time.sleep(0.5)                      # blunt brute-force damper
                return self._json(403, {"error": "wrong control token"})
            try:
                action = json.loads(raw).get("action", "")
            except (ValueError, AttributeError):
                return self._json(400, {"error": "expected JSON body {\"action\": ...}"})
            if action not in ACTIONS:
                return self._json(400, {"error": f"action must be one of {', '.join(ACTIONS)}"})
            ok, msg = perform(action, runner, schedule)
            print(f"[control] {self.client_address[0]} {action}: {'ok' if ok else 'FAILED'} - {msg}",
                  flush=True)
            st = pipeline_status(runner)
            self._json(200 if ok else 500, {"ok": ok, "action": action, "message": msg,
                                            "pipeline": st["pipeline"]})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="PitFusion3D power/control daemon")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    args = ap.parse_args()

    loader = lambda: load_token(args.token_file)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(loader))
    server.daemon_threads = True
    print(f"[control] listening on {args.host}:{args.port}; "
          f"control {'ENABLED' if loader() else 'DISABLED (no token)'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
