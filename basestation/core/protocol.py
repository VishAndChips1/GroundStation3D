"""Wire formats for the survey-rig's point-cloud feed.

The rover exposes an HTTP endpoint rather than pushing UDP datagrams:

    GET http://<rover-host>:<port>/latest.bin

Each request returns one complete, self-contained frame (TCP already
guarantees the body arrives whole), so this is a straight parse. Two body
layouts are understood, told apart by length alone -- they can never both fit
the same bytes, because 4 + 28*N == 24 + 11*N has no integer solution:

  "float32" (28 bytes/point), little-endian:
      uint32 N, then float32[N][7] = x y z r g b thermal
        xyz metres, rgb already 0-1, thermal Kelvin or NaN when unknown.

  "rig" (11 bytes/point), what PitFusion3D's fusion_web_streamer.py sends,
  usually gzip-compressed (Content-Encoding: gzip):
      uint32 N, uint32 seq, float32 ox, oy, oz, scale      (24 bytes)
      then N x { int16 x, y, z; uint8 r, g, b; int16 thermal }
        xyz = q * scale + offset (metres); rgb 0-255;
        thermal = Kelvin * 100, with -32768 meaning "no reading".

  "compact" (format 2, about 3.5-5.5 bytes/point), requested with ?fmt=2 once the rig advertises it
  in its X-Formats header (so it cannot be told apart by length: the caller says which one it asked for):
      uint32 N, uint32 seq, float32 ox, oy, oz, quantum, uint32 flags     (28 bytes; flags bit 0 = thermal present)
      then N x int16 each of dx, dy, dz  (differences between neighbouring points, running sum x quantum + origin),
      [N x int16 thermal * 100, -32768 = none, only if flagged], then N x uint8 each of dr, dg, db
      (differences modulo 256; a running sum rebuilds the colour exactly).

A gzip wrapper is detected by its magic bytes and removed first. Points whose
coordinates are NaN or infinite (a lidar's "no return") are dropped here, once,
so nothing downstream -- bounds, camera, voxel keys -- ever sees them.

If a layout changes, this is the only file that needs editing: rendering and
GUI code only ever see `Frame` objects out of `parse_frame()`.
"""

import struct
import zlib
from dataclasses import dataclass

import numpy as np

COUNT_STRUCT = struct.Struct("<I")
RIG_HEADER = struct.Struct("<IIffff")          # n, seq, ox, oy, oz, scale
COMPACT_HEADER = struct.Struct("<IIffffI")     # n, seq, ox, oy, oz, quantum, flags
THERMAL_SENTINEL = -32768
MAX_DECOMPRESSED_BYTES = 768 * 1024 * 1024     # refuse gzip bombs / absurd frames

POINT_DTYPE = np.dtype([
    ("xyz", "<f4", (3,)),
    ("rgb", "<f4", (3,)),
    ("thermal_k", "<f4"),
])
assert POINT_DTYPE.itemsize == 28  # 7 float32 fields

RIG_POINT_DTYPE = np.dtype([
    ("xyz", "<i2", (3,)),
    ("rgb", "u1", (3,)),
    ("t", "<i2"),
])
assert RIG_POINT_DTYPE.itemsize == 11


class MalformedFrameError(ValueError):
    """Raised when a response body doesn't match any expected layout."""


@dataclass
class Frame:
    frame_id: int
    recv_time: float          # local time the response finished arriving
    fetch_seconds: float      # how long the HTTP GET took round-trip
    xyz: np.ndarray           # (N, 3) float32, meters, always finite
    rgb: np.ndarray           # (N, 3) float32, 0-1
    temp_c: np.ndarray        # (N,) float32, Celsius; NaN where unknown

    @property
    def num_points(self) -> int:
        return self.xyz.shape[0]


def is_gzip(data: bytes) -> bool:
    return data[:2] == b"\x1f\x8b"


def gunzip(data: bytes, limit: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Decompress a gzip body, refusing anything that inflates past `limit`."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, limit + 1)
    except zlib.error as exc:
        raise MalformedFrameError(f"corrupt gzip body: {exc}") from exc
    if len(out) > limit:
        raise MalformedFrameError("frame is implausibly large after decompression")
    if not d.eof:
        raise MalformedFrameError("truncated gzip body")
    return out


def parse_frame(data: bytes, frame_id: int, recv_time: float,
                fetch_seconds: float, fmt: int = 0) -> Frame:
    """Parse one /latest.bin response body into a Frame.

    fmt=2 says the body was requested as the compact format (it is not guessed from the length).
    Raises MalformedFrameError if the body fits no expected layout.
    """
    if is_gzip(data):
        data = gunzip(data)
    if len(data) < COUNT_STRUCT.size:
        raise MalformedFrameError(f"body too short for header: {len(data)} bytes")

    (n,) = COUNT_STRUCT.unpack_from(data, 0)
    if fmt == 2:
        xyz, rgb, temp_c = _decode_compact(data, n)
    elif len(data) == COUNT_STRUCT.size + n * POINT_DTYPE.itemsize:
        xyz, rgb, temp_c = _decode_float32(data, n)
    elif len(data) == RIG_HEADER.size + n * RIG_POINT_DTYPE.itemsize:
        xyz, rgb, temp_c = _decode_rig(data, n)
    else:
        raise MalformedFrameError(
            f"{len(data):,} bytes does not match {n:,} points in either layout "
            f"(expected {COUNT_STRUCT.size + n * POINT_DTYPE.itemsize:,} or "
            f"{RIG_HEADER.size + n * RIG_POINT_DTYPE.itemsize:,})")

    xyz, rgb, temp_c = _drop_invalid(xyz, rgb, temp_c)
    return Frame(frame_id=frame_id, recv_time=recv_time, fetch_seconds=fetch_seconds,
                 xyz=xyz, rgb=rgb, temp_c=temp_c)


def _decode_float32(data: bytes, n: int):
    arr = np.frombuffer(data, dtype=POINT_DTYPE, count=n, offset=COUNT_STRUCT.size)
    temp_c = arr["thermal_k"].copy()
    temp_c -= 273.15                      # Kelvin -> Celsius; NaN stays NaN
    return arr["xyz"].copy(), arr["rgb"].copy(), temp_c


def _decode_rig(data: bytes, n: int):
    _n, _seq, ox, oy, oz, scale = RIG_HEADER.unpack_from(data, 0)
    if n and not (np.isfinite([ox, oy, oz, scale]).all() and scale > 0):
        raise MalformedFrameError("rig frame has an invalid scale/offset header")
    arr = np.frombuffer(data, dtype=RIG_POINT_DTYPE, count=n, offset=RIG_HEADER.size)
    offset = np.array([ox, oy, oz], dtype=np.float64)
    xyz = (arr["xyz"].astype(np.float64) * scale + offset).astype(np.float32)
    rgb = arr["rgb"].astype(np.float32) / 255.0
    t = arr["t"]
    temp_c = (t.astype(np.float32) / 100.0 - 273.15).astype(np.float32)
    temp_c[t == THERMAL_SENTINEL] = np.nan
    return xyz, rgb, temp_c


def _drop_invalid(xyz, rgb, temp_c):
    """Remove points with a non-finite coordinate; sanitise colour and thermal."""
    good = np.isfinite(xyz).all(axis=1)
    if not good.all():
        xyz, rgb, temp_c = xyz[good], rgb[good], temp_c[good]
    if not np.isfinite(rgb).all():
        rgb = np.nan_to_num(rgb, nan=0.5, posinf=1.0, neginf=0.0)
    bad_t = ~np.isfinite(temp_c)
    if bad_t.any():
        temp_c = np.where(bad_t, np.float32("nan"), temp_c).astype(np.float32)
    return xyz, rgb, temp_c


def encode_rig_frame(xyz: np.ndarray, rgb01: np.ndarray, thermal_k: np.ndarray,
                     seq: int = 0, gzip_it: bool = False) -> bytes:
    """Encode a frame in the rig layout (used by the mock server and tests)."""
    n = len(xyz)
    if n == 0:
        body = RIG_HEADER.pack(0, seq, 0.0, 0.0, 0.0, 1.0)
    else:
        mins = xyz.min(axis=0).astype(np.float64)
        extent = float((xyz.max(axis=0) - mins).max())
        scale = max(extent, 1e-6) / 32000.0
        arr = np.empty(n, dtype=RIG_POINT_DTYPE)
        arr["xyz"] = np.round((xyz - mins) / scale).astype(np.int16)
        arr["rgb"] = np.clip(np.round(rgb01 * 255.0), 0, 255).astype(np.uint8)
        t = np.full(n, THERMAL_SENTINEL, dtype=np.int16)
        ok = np.isfinite(thermal_k)
        t[ok] = np.clip(np.round(thermal_k[ok] * 100.0), -32767, 32767).astype(np.int16)
        arr["t"] = t
        body = RIG_HEADER.pack(n, seq, *mins, scale) + arr.tobytes()
    if gzip_it:
        import gzip
        return gzip.compress(body, compresslevel=3)
    return body


def _decode_compact(data: bytes, n: int):
    if len(data) < COMPACT_HEADER.size:
        raise MalformedFrameError(f"compact frame too short for its header: {len(data)} bytes")
    _n, _seq, ox, oy, oz, quantum, flags = COMPACT_HEADER.unpack_from(data, 0)
    has_thermal = bool(flags & 1)
    expected = COMPACT_HEADER.size + n * (11 if has_thermal else 9)
    if len(data) != expected:
        raise MalformedFrameError(f"compact frame is {len(data):,} bytes, expected {expected:,} for {n:,} points")
    if n and not (np.isfinite([ox, oy, oz, quantum]).all() and quantum > 0):
        raise MalformedFrameError("compact frame has an invalid quantum/offset header")
    off = COMPACT_HEADER.size
    steps = []
    for _ in range(3):
        steps.append(np.frombuffer(data, dtype="<i2", count=n, offset=off).astype(np.int64))
        off += 2 * n
    thermal = None
    if has_thermal:
        thermal = np.frombuffer(data, dtype="<i2", count=n, offset=off)
        off += 2 * n
    colour = []
    for _ in range(3):
        colour.append(np.cumsum(np.frombuffer(data, dtype=np.uint8, count=n, offset=off).astype(np.int64)) & 0xFF)
        off += n
    grid = np.stack([np.cumsum(c) for c in steps], axis=1) if n else np.zeros((0, 3), np.int64)
    xyz = (grid.astype(np.float64) * quantum + np.array([ox, oy, oz])).astype(np.float32)
    rgb = (np.stack(colour, axis=1).astype(np.float32) / 255.0) if n else np.zeros((0, 3), np.float32)
    temp_c = np.full(n, np.nan, dtype=np.float32)
    if thermal is not None:
        temp_c = (thermal.astype(np.float32) / 100.0 - 273.15).astype(np.float32)
        temp_c[thermal == THERMAL_SENTINEL] = np.nan
    return xyz, rgb, temp_c
