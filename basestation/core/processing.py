"""Point-cloud processing and measurement. Pure numpy, no Qt/VTK.

`Pipeline` is a set of independent, switchable filters applied to every
incoming frame (the stream is live, so processing is a *pipeline*, not a
one-off edit). Filters run cheapest-first and each only ever narrows the
set of surviving points, so the work stays proportional to what is left:

    range  ->  crop box  ->  thermal band  ->  isolated points  ->  voxel grid

`apply()` returns the surviving indices; the caller gathers xyz / rgb /
temperature with them, so the three arrays can never fall out of step.
"""

from dataclasses import dataclass, field

import numpy as np


@dataclass
class CropBox:
    enabled: bool = False
    bounds: tuple = (-1.0, 1.0, -1.0, 1.0, -1.0, 1.0)   # xmin xmax ymin ymax zmin zmax
    keep_inside: bool = True


@dataclass
class Pipeline:
    # Distance from the sensor origin.
    range_enabled: bool = False
    range_min: float = 0.0
    range_max: float = 50.0
    # Axis-aligned crop box.
    crop: CropBox = field(default_factory=CropBox)
    # Keep only points inside a temperature band (deg C).
    thermal_enabled: bool = False
    thermal_min: float = 20.0
    thermal_max: float = 80.0
    thermal_drop_unknown: bool = False
    # Remove points with too few neighbours nearby.
    isolated_enabled: bool = False
    isolated_radius: float = 0.20
    isolated_min_neighbors: int = 3
    # Uniform thinning on a voxel grid.
    voxel_enabled: bool = False
    voxel_size: float = 0.05

    @property
    def active(self) -> bool:
        return any((self.range_enabled, self.crop.enabled, self.thermal_enabled,
                    self.isolated_enabled, self.voxel_enabled))

    def apply(self, xyz: np.ndarray, temp_c: np.ndarray) -> np.ndarray | None:
        """Indices of surviving points, or None when nothing is filtered."""
        if not self.active or len(xyz) == 0:
            return None
        idx = np.arange(len(xyz))

        if self.range_enabled:
            dist = np.linalg.norm(xyz[idx], axis=1)
            idx = idx[(dist >= self.range_min) & (dist <= self.range_max)]

        if self.crop.enabled and len(idx):
            idx = idx[inside_box(xyz[idx], self.crop.bounds) == self.crop.keep_inside]

        if self.thermal_enabled and len(idx):
            t = temp_c[idx]
            known = ~np.isnan(t)
            in_band = known & (t >= self.thermal_min) & (t <= self.thermal_max)
            idx = idx[in_band if self.thermal_drop_unknown else (in_band | ~known)]

        if self.isolated_enabled and len(idx):
            idx = idx[neighbor_counts(xyz[idx], self.isolated_radius)
                      >= self.isolated_min_neighbors]

        if self.voxel_enabled and len(idx):
            idx = idx[voxel_first_indices(xyz[idx], self.voxel_size)]

        return idx


# -- filters --------------------------------------------------------------

def inside_box(xyz: np.ndarray, bounds) -> np.ndarray:
    x0, x1, y0, y1, z0, z1 = bounds
    return ((xyz[:, 0] >= x0) & (xyz[:, 0] <= x1) &
            (xyz[:, 1] >= y0) & (xyz[:, 1] <= y1) &
            (xyz[:, 2] >= z0) & (xyz[:, 2] <= z1))


def _voxel_keys(xyz: np.ndarray, size: float) -> np.ndarray:
    """One int64 per point identifying its voxel (21 bits per axis)."""
    cells = np.floor(xyz / max(size, 1e-6)).astype(np.int64)
    cells -= cells.min(axis=0)
    cells = np.minimum(cells, (1 << 21) - 2)
    return (cells[:, 0] << 42) | (cells[:, 1] << 21) | cells[:, 2]


def voxel_first_indices(xyz: np.ndarray, size: float) -> np.ndarray:
    """Index of one representative point per occupied voxel (sorted)."""
    _, first = np.unique(_voxel_keys(xyz, size), return_index=True)
    return np.sort(first)


def neighbor_counts(xyz: np.ndarray, radius: float) -> np.ndarray:
    """Approximate neighbour count per point from voxel occupancy.

    Counts every point in the point's voxel and the 26 surrounding ones
    (cell edge = radius), minus the point itself. That is a slightly
    generous neighbourhood (up to ~1.7 x radius) but needs no KD-tree and
    stays linear-ish in point count -- fine for removing stray returns.
    """
    cell = max(radius, 1e-6)
    cells = np.floor(xyz / cell).astype(np.int64)
    cells -= cells.min(axis=0)
    cells += 1                                    # room for the -1 neighbour
    cells = np.minimum(cells, (1 << 21) - 3)
    keys = (cells[:, 0] << 42) | (cells[:, 1] << 21) | cells[:, 2]
    uniq, counts = np.unique(keys, return_counts=True)

    total = np.zeros(len(xyz), dtype=np.int64)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                shifted = keys + ((dx << 42) + (dy << 21) + dz)
                pos = np.searchsorted(uniq, shifted)
                pos[pos >= len(uniq)] = len(uniq) - 1
                hit = uniq[pos] == shifted
                total += np.where(hit, counts[pos], 0)
    return total - 1


# -- measurement ----------------------------------------------------------

def segment_info(a, b) -> dict:
    """Distances between two 3D points: straight, horizontal, vertical."""
    d = np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
    horizontal = float(np.hypot(d[0], d[1]))
    return {
        "distance": float(np.linalg.norm(d)),
        "horizontal": horizontal,
        "vertical": float(d[2]),
        "dx": float(d[0]), "dy": float(d[1]), "dz": float(d[2]),
        "slope_deg": float(np.degrees(np.arctan2(abs(d[2]), horizontal)))
        if horizontal > 1e-9 else (90.0 if abs(d[2]) > 0 else 0.0),
    }


def path_length(points) -> float:
    pts = np.asarray(points, dtype=float)
    if len(pts) < 2:
        return 0.0
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())


def polygon_area_xy(points) -> float:
    """Plan-view (XY) area of the polygon through the points, shoelace."""
    pts = np.asarray(points, dtype=float)
    if len(pts) < 3:
        return 0.0
    x, y = pts[:, 0], pts[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def bounds_of(xyz: np.ndarray):
    """(xmin, xmax, ymin, ymax, zmin, zmax) of a cloud, or None if empty."""
    if len(xyz) == 0:
        return None
    lo, hi = xyz.min(axis=0), xyz.max(axis=0)
    return (float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]),
            float(lo[2]), float(hi[2]))
