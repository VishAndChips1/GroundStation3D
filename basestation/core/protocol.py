"""Wire format for the survey-rig's point-cloud feed.

The rover exposes an HTTP endpoint rather than pushing UDP datagrams:

    GET http://<rover-host>:<port>/latest.bin

    Binary body (little-endian):
        uint32      N -- point count
        float32[N][7] -- x, y, z, r, g, b, thermal
            xyz    in meters
            rgb    already normalized to 0-1 (not 0-255)
            thermal in Kelvin, or NaN where no thermal reading is available

Each request returns one complete, self-contained frame -- there's no
reassembly to do (TCP already guarantees the body arrives whole), so this
is just a straight parse. If the real endpoint's layout changes again, this
is the only file that needs editing -- rendering/GUI code only ever sees
`Frame` objects out of `parse_frame()`.
"""

import struct
from dataclasses import dataclass

import numpy as np

COUNT_STRUCT = struct.Struct("<I")

POINT_DTYPE = np.dtype([
    ("xyz", "<f4", (3,)),
    ("rgb", "<f4", (3,)),
    ("thermal_k", "<f4"),
])
assert POINT_DTYPE.itemsize == 28  # 7 float32 fields


class MalformedFrameError(ValueError):
    """Raised when a response body doesn't match the expected layout."""


@dataclass
class Frame:
    frame_id: int
    recv_time: float          # local time the response finished arriving
    fetch_seconds: float      # how long the HTTP GET took round-trip
    xyz: np.ndarray           # (N, 3) float32, meters
    rgb: np.ndarray           # (N, 3) float32, already 0-1
    temp_c: np.ndarray        # (N,) float32, Celsius; NaN where unknown

    @property
    def num_points(self) -> int:
        return self.xyz.shape[0]


def parse_frame(data: bytes, frame_id: int, recv_time: float,
                 fetch_seconds: float) -> Frame:
    """Parses one /latest.bin response body into a Frame.

    Raises MalformedFrameError if the body is too short or its length
    doesn't match the declared point count.
    """
    if len(data) < COUNT_STRUCT.size:
        raise MalformedFrameError(f"body too short for header: {len(data)} bytes")

    (num_points,) = COUNT_STRUCT.unpack_from(data, 0)
    payload = data[COUNT_STRUCT.size:]
    expected_len = num_points * POINT_DTYPE.itemsize
    if len(payload) != expected_len:
        raise MalformedFrameError(
            f"expected {expected_len} bytes for {num_points} points, got {len(payload)}")

    arr = np.frombuffer(payload, dtype=POINT_DTYPE, count=num_points)
    temp_c = arr["thermal_k"].copy()
    temp_c -= 273.15  # Kelvin -> Celsius; NaN stays NaN

    return Frame(
        frame_id=frame_id,
        recv_time=recv_time,
        fetch_seconds=fetch_seconds,
        xyz=arr["xyz"].copy(),
        rgb=arr["rgb"].copy(),
        temp_c=temp_c,
    )
