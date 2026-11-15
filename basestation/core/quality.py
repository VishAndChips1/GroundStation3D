"""Adaptive point count for the live stream (no Qt, so it can be unit-tested).

The rig can serve a cloud at several sizes (it advertises them in the X-Quality-Levels header and accepts ?n=<points>).
This keeps the largest size while frames arrive comfortably, steps down when they struggle, and creeps back up once
the link has been healthy for a while. A single slow frame is treated as a hiccup, not a reason to lose detail.
"""


class AdaptiveQuality:
    SLOW_S = 1.5      # a frame taking longer than this to arrive means the link is struggling
    FAST_S = 0.6      # several frames faster than this in a row mean there is headroom
    SLOW_RUN = 2      # consecutive slow frames before stepping down
    FAST_RUN = 8      # consecutive fast frames before stepping up
    HOLD_S = 60.0     # after stepping down, do not step up again for this long

    def __init__(self):
        self.levels = []
        self.index = 0
        self._slow = 0
        self._fast = 0
        self._hold_until = 0.0

    def set_levels(self, levels) -> None:
        """Adopt the rig's list of sizes (ascending). Starts at the largest; ignored if unchanged."""
        levels = sorted({int(n) for n in levels if int(n) > 0})
        if levels != self.levels:
            self.levels = levels
            self.index = max(len(levels) - 1, 0)
            self._slow = self._fast = 0

    def requested(self):
        """The point count to ask for, or None while the rig has not advertised any sizes."""
        return self.levels[self.index] if self.levels else None

    def report(self, seconds: float, now: float, failed: bool = False) -> None:
        """Record how long one frame took (failed = the download timed out or broke off)."""
        if not self.levels:
            return
        if failed or seconds > self.SLOW_S:
            self._slow += 1
            self._fast = 0
            if (failed or self._slow >= self.SLOW_RUN) and self.index > 0:
                self.index -= 1
                self._slow = 0
                self._hold_until = now + self.HOLD_S
        elif seconds < self.FAST_S:
            self._fast += 1
            self._slow = 0
            if self._fast >= self.FAST_RUN and self.index < len(self.levels) - 1 and now >= self._hold_until:
                self.index += 1
                self._fast = 0
        else:
            self._slow = self._fast = 0
