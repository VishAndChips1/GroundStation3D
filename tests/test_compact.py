"""Compact frame format (format 2): parser checked against an independent encoder, plus malformed-input handling."""
import gzip
import struct
import unittest

import numpy as np

from basestation.core import protocol


def encode_compact(grid, rgb, quantum=0.0025, origin=(1.0, -2.0, 0.5), thermal=None, seq=7):
    """Independent encoder: grid (N,3) integer cells in the order given, rgb (N,3) uint8, thermal Kelvin*100 or None."""
    n = len(grid)
    d = np.diff(grid.astype(np.int64), axis=0, prepend=np.zeros((1, 3), np.int64)).astype("<i2")
    dc = (np.diff(rgb.astype(np.int16), axis=0, prepend=np.zeros((1, 3), np.int16)) & 0xFF).astype(np.uint8)
    out = struct.pack("<IIffffI", n, seq, *origin, quantum, 1 if thermal is not None else 0)
    out += b"".join(d[:, i].tobytes() for i in range(3))
    if thermal is not None:
        out += np.asarray(thermal, "<i2").tobytes()
    out += b"".join(dc[:, i].tobytes() for i in range(3))
    return out


class CompactFormatTests(unittest.TestCase):
    def cloud(self, n=500, seed=0):
        rng = np.random.default_rng(seed)
        grid = np.cumsum(rng.integers(-40, 40, size=(n, 3)), axis=0) + 2000
        rgb = rng.integers(0, 256, size=(n, 3)).astype(np.uint8)
        return grid, rgb

    def test_positions_and_colours_round_trip_exactly(self):
        grid, rgb = self.cloud()
        f = protocol.parse_frame(encode_compact(grid, rgb), 1, 0.0, 0.0, fmt=2)
        expect = grid * 0.0025 + np.array([1.0, -2.0, 0.5])
        self.assertEqual(f.num_points, 500)
        self.assertLess(np.abs(f.xyz - expect).max(), 1e-3)
        self.assertTrue(np.array_equal(np.rint(f.rgb * 255).astype(int), rgb))
        self.assertTrue(np.isnan(f.temp_c).all())               # no thermal section: unknown, not zero

    def test_colours_survive_wraparound_between_neighbours(self):
        grid = np.array([[0, 0, 0], [1, 1, 1], [2, 2, 2]])
        rgb = np.array([[250, 3, 128], [5, 250, 127], [255, 0, 129]], np.uint8)     # large jumps either way
        f = protocol.parse_frame(encode_compact(grid, rgb), 1, 0.0, 0.0, fmt=2)
        self.assertTrue(np.array_equal(np.rint(f.rgb * 255).astype(int), rgb))

    def test_thermal_section_is_read_and_sentinel_becomes_nan(self):
        grid, rgb = self.cloud(10)
        t = np.array([30000, -32768, 29315, 30000, 30000, 30000, -32768, 30000, 30000, 30000], np.int16)
        f = protocol.parse_frame(encode_compact(grid, rgb, thermal=t), 1, 0.0, 0.0, fmt=2)
        self.assertAlmostEqual(float(f.temp_c[0]), 300.0 - 273.15, places=2)
        self.assertAlmostEqual(float(f.temp_c[2]), 293.15 - 273.15, places=2)
        self.assertTrue(np.isnan(f.temp_c[1]) and np.isnan(f.temp_c[6]))

    def test_gzip_wrapped_frames_are_accepted(self):
        grid, rgb = self.cloud(100)
        f = protocol.parse_frame(gzip.compress(encode_compact(grid, rgb)), 1, 0.0, 0.0, fmt=2)
        self.assertEqual(f.num_points, 100)

    def test_empty_frame(self):
        f = protocol.parse_frame(struct.pack("<IIffffI", 0, 1, 0.0, 0.0, 0.0, 1.0, 0), 1, 0.0, 0.0, fmt=2)
        self.assertEqual(f.num_points, 0)

    def test_wrong_length_is_rejected_not_misread(self):
        grid, rgb = self.cloud(50)
        good = encode_compact(grid, rgb)
        for bad in (good[:-1], good + b"x", good[:20], struct.pack("<I", 5)):
            with self.assertRaises(protocol.MalformedFrameError):
                protocol.parse_frame(bad, 1, 0.0, 0.0, fmt=2)

    def test_invalid_quantum_is_rejected(self):
        grid, rgb = self.cloud(5)
        with self.assertRaises(protocol.MalformedFrameError):
            protocol.parse_frame(encode_compact(grid, rgb, quantum=0.0), 1, 0.0, 0.0, fmt=2)
        with self.assertRaises(protocol.MalformedFrameError):
            protocol.parse_frame(encode_compact(grid, rgb, quantum=float("nan")), 1, 0.0, 0.0, fmt=2)

    def test_older_layouts_are_unaffected_when_no_format_is_given(self):
        n = 3
        body = struct.pack("<IIffff", n, 1, 0.0, 0.0, 0.0, 0.01) + b"".join(struct.pack("<hhhBBBh", i, i, i, 1, 2, 3, -32768) for i in range(n))
        self.assertEqual(protocol.parse_frame(body, 1, 0.0, 0.0).num_points, 3)


if __name__ == "__main__":
    unittest.main()
