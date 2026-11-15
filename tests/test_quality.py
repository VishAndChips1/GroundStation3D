"""The adaptive point-count controller (no Qt needed)."""
import unittest

from basestation.core.quality import AdaptiveQuality


class AdaptiveQualityTests(unittest.TestCase):
    def make(self):
        q = AdaptiveQuality()
        q.set_levels([8000, 20000, 40000, 60000, 80000])
        return q

    def test_nothing_is_requested_until_the_rig_advertises_sizes(self):
        q = AdaptiveQuality()
        self.assertIsNone(q.requested())
        q.report(5.0, 0.0)                              # reports before that are harmless
        self.assertIsNone(q.requested())

    def test_starts_at_the_largest_size(self):
        self.assertEqual(self.make().requested(), 80000)

    def test_a_single_slow_frame_is_a_hiccup_two_in_a_row_step_down(self):
        q = self.make()
        q.report(3.0, 0.0)
        self.assertEqual(q.requested(), 80000)
        q.report(3.0, 1.0)
        self.assertEqual(q.requested(), 60000)

    def test_a_good_frame_between_slow_ones_resets_the_count(self):
        q = self.make()
        q.report(3.0, 0.0)
        q.report(1.0, 1.0)                              # neither slow nor fast
        q.report(3.0, 2.0)
        self.assertEqual(q.requested(), 80000)

    def test_a_failed_download_steps_down_at_once_and_floors_at_the_smallest(self):
        q = self.make()
        q.report(0.0, 0.0, failed=True)
        self.assertEqual(q.requested(), 60000)
        for i in range(10):
            q.report(0.0, 1.0 + i, failed=True)
        self.assertEqual(q.requested(), 8000)

    def test_climbs_back_only_after_the_hold_and_eight_fast_frames(self):
        q = self.make()
        q.report(0.0, 0.0, failed=True)                 # down to 60000, held until t = 60
        for i in range(20):
            q.report(0.1, 1.0 + i, failed=False)
        self.assertEqual(q.requested(), 60000)          # still held
        for i in range(8):
            q.report(0.1, 61.0 + i, failed=False)
        self.assertEqual(q.requested(), 80000)

    def test_never_exceeds_the_largest_size_and_new_levels_restart_at_the_top(self):
        q = self.make()
        for i in range(30):
            q.report(0.1, 100.0 + i)
        self.assertEqual(q.requested(), 80000)
        q.report(0.0, 200.0, failed=True)
        q.set_levels([8000, 20000, 40000, 60000, 80000])    # unchanged list: keep the current step
        self.assertEqual(q.requested(), 60000)
        q.set_levels([5000, 10000])                          # a different rig: start at its top
        self.assertEqual(q.requested(), 10000)


if __name__ == "__main__":
    unittest.main()
