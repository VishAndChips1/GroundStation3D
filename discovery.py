"""Find point-cloud senders on the local network.

A candidate only counts as a sender if it actually answers /latest.bin with
a body that matches the protocol header -- an open port alone isn't enough,
otherwise every unrelated web server on the subnet would show up.

Only the first 4 bytes are downloaded per probe; the declared point count is
validated against Content-Length, so scanning a subnet stays cheap even when
frames are megabytes.
"""

import ipaddress
import os
import socket
import struct
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from protocol import POINT_DTYPE

PROBE_TIMEOUT = 0.6
MAX_SUBNETS = 3
MAX_WORKERS = 64
PLAUSIBLE_MAX_POINTS = 50_000_000

# Windows: keep subprocess calls from flashing a console window under
# pythonw.exe (the no-console launcher).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def local_ipv4s() -> list[str]:
    """Best-effort list of this machine's IPv4 addresses."""
    found: set[str] = set()
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        found.update(a for a in addrs if not a.startswith("127."))
    except OSError:
        pass

    # Ask the routing table which local address reaches the outside world.
    # connect() on UDP assigns a route without sending any packets.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        found.add(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()

    return sorted(found)


def wsl_hosts() -> list[str]:
    """IPv4 addresses of running WSL distros, if any (Windows only)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["wsl", "hostname", "-I"],
            capture_output=True, text=True, timeout=8,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    hosts = []
    for token in out.stdout.split():
        try:
            ipaddress.IPv4Address(token)
        except ipaddress.AddressValueError:
            continue
        hosts.append(token)
    return hosts


def probe(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> int | None:
    """Return the sender's point count, or None if this isn't a sender."""
    url = f"http://{host}:{port}/latest.bin"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            head = resp.read(4)
            if len(head) < 4:
                return None
            (num_points,) = struct.unpack("<I", head)
            if num_points > PLAUSIBLE_MAX_POINTS:
                return None
            declared = resp.headers.get("Content-Length")
            if declared is not None:
                expected = 4 + num_points * POINT_DTYPE.itemsize
                if int(declared) != expected:
                    return None
            return num_points
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


def candidate_hosts() -> list[str]:
    """Addresses worth probing: loopback, WSL, then each local /24."""
    ordered: list[str] = ["127.0.0.1"]
    seen = {"127.0.0.1"}

    for host in wsl_hosts():
        if host not in seen:
            ordered.append(host)
            seen.add(host)

    subnets = []
    for ip in local_ipv4s():
        try:
            net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        except ValueError:
            continue
        if net not in subnets:
            subnets.append(net)

    for net in subnets[:MAX_SUBNETS]:
        for addr in net.hosts():
            host = str(addr)
            if host not in seen:
                ordered.append(host)
                seen.add(host)

    return ordered


def scan(port: int, on_found=None, should_stop=None) -> list[tuple[str, int]]:
    """Probe every candidate host; return [(host, point_count), ...].

    `on_found` is called as each sender is discovered so a UI can populate
    incrementally; `should_stop` is polled to allow cancellation.
    """
    hosts = candidate_hosts()
    results: list[tuple[str, int]] = []

    def work(host: str):
        if should_stop is not None and should_stop():
            return None
        count = probe(host, port)
        return (host, count) if count is not None else None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for outcome in pool.map(work, hosts):
            if should_stop is not None and should_stop():
                break
            if outcome is not None:
                results.append(outcome)
                if on_found is not None:
                    on_found(*outcome)

    return results
