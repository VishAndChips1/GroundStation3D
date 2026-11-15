"""Run with:  python -I -m unittest discover -s tests   (from the project root)."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sensor_side"))

from basestation.core import diagnostics
from basestation.core import processing as pr
import sensor_diagnostics as sd
import pitfusion_control as pc
from basestation.core import control


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        self.xyz = rng.uniform(-5, 5, (5000, 3)).astype(np.float32)
        self.temp = rng.uniform(10, 90, 5000).astype(np.float32)
        self.temp[:100] = np.nan

    def test_inactive_pipeline_returns_none(self):
        self.assertIsNone(pr.Pipeline().apply(self.xyz, self.temp))

    def test_crop_keep_and_remove_partition_the_cloud(self):
        p = pr.Pipeline()
        p.crop = pr.CropBox(True, (-1, 1, -1, 1, -1, 1), True)
        inside = p.apply(self.xyz, self.temp)
        p.crop.keep_inside = False
        outside = p.apply(self.xyz, self.temp)
        self.assertEqual(len(inside) + len(outside), len(self.xyz))
        self.assertTrue(np.all(np.abs(self.xyz[inside]) <= 1))

    def test_thermal_band_and_unknown_handling(self):
        p = pr.Pipeline(thermal_enabled=True, thermal_min=40, thermal_max=60)
        kept = p.apply(self.xyz, self.temp)
        t = self.temp[kept]
        self.assertTrue(np.all(np.isnan(t) | ((t >= 40) & (t <= 60))))
        p.thermal_drop_unknown = True
        self.assertFalse(np.any(np.isnan(self.temp[p.apply(self.xyz, self.temp)])))

    def test_voxel_keeps_one_point_per_cell(self):
        p = pr.Pipeline(voxel_enabled=True, voxel_size=2.0)
        kept = p.apply(self.xyz, self.temp)
        self.assertLess(len(kept), 300)
        cells = np.floor(self.xyz[kept] / 2.0)
        self.assertEqual(len(np.unique(cells, axis=0)), len(kept))

    def test_isolated_points_are_removed_but_cluster_survives(self):
        rng = np.random.default_rng(2)
        cluster = rng.normal(0, 0.05, (500, 3)).astype(np.float32)
        strays = np.array([[9, 9, 9], [-9, 9, 0], [5, -9, 3]], dtype=np.float32)
        xyz = np.vstack([cluster, strays])
        p = pr.Pipeline(isolated_enabled=True, isolated_radius=0.2,
                        isolated_min_neighbors=3)
        kept = p.apply(xyz, np.zeros(len(xyz), np.float32))
        self.assertFalse(np.any(kept >= 500))
        self.assertGreater(len(kept), 480)

    def test_range_filter(self):
        p = pr.Pipeline(range_enabled=True, range_min=2.0, range_max=4.0)
        d = np.linalg.norm(self.xyz[p.apply(self.xyz, self.temp)], axis=1)
        self.assertTrue(np.all((d >= 2.0) & (d <= 4.0)))

    def test_empty_result_is_safe(self):
        p = pr.Pipeline()
        p.crop = pr.CropBox(True, (100, 101, 100, 101, 100, 101), True)
        p.voxel_enabled = p.isolated_enabled = True
        self.assertEqual(len(p.apply(self.xyz, self.temp)), 0)

    def test_measurements(self):
        info = pr.segment_info((0, 0, 0), (3, 4, 12))
        self.assertAlmostEqual(info["distance"], 13.0)
        self.assertAlmostEqual(info["horizontal"], 5.0)
        self.assertAlmostEqual(pr.path_length([(0, 0, 0), (1, 0, 0), (1, 2, 0)]), 3.0)
        self.assertAlmostEqual(pr.polygon_area_xy([(0, 0, 0), (4, 0, 0), (4, 3, 5), (0, 3, 1)]), 12.0)


class DiagnosticsTests(unittest.TestCase):
    def test_healthy_report(self):
        r = diagnostics.parse_report({
            "hostname": "pi", "uptime_s": 4000,
            "sensors": {"camera": {"state": "streaming", "connected": True,
                                    "fps": 15, "target_fps": 15,
                                    "last_frame_age_s": 0.05}},
            "pipelines": {"fusion": {"state": "running", "fps": 9.8, "target_fps": 10}},
            "system": {"cpu_pct": 40, "temp_c": 55, "under_voltage_now": False},
        })
        self.assertEqual(r.overall, diagnostics.OK)

    def test_problems_are_flagged(self):
        r = diagnostics.parse_report({
            "sensors": {"thermal": {"state": "disconnected", "connected": False},
                         "lidar": {"state": "streaming", "fps": 3, "target_fps": 10}},
            "pipelines": {"merge": {"state": "stalled", "fps": 0.0, "target_fps": 1}},
            "system": {"under_voltage_now": True},
        })
        by = {i.name: i.status for i in r.sensors + r.pipelines}
        self.assertEqual(by["thermal"], diagnostics.ERROR)
        self.assertEqual(by["lidar"], diagnostics.ERROR)   # 30% of target
        self.assertEqual(r.system[0].status, diagnostics.ERROR)
        self.assertEqual(r.overall, diagnostics.ERROR)

    def test_slow_but_alive_is_a_warning(self):
        item = diagnostics.parse_report(
            {"sensors": {"cam": {"connected": True, "fps": 11, "target_fps": 15}}}
        ).sensors[0]
        self.assertEqual(item.status, diagnostics.WARN)

    def test_list_form_and_extras(self):
        r = diagnostics.parse_report({"sensors": [{"name": "gps", "state": "ok", "satellites": 9}]})
        self.assertEqual(r.sensors[0].name, "gps")
        self.assertIn(("Satellites", "9"), r.sensors[0].metrics)

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            diagnostics.parse_report([1, 2])


class SensorSideTests(unittest.TestCase):
    """The Pi-side reporter, exercised without ROS using a fake clock."""

    def test_rate_tracker_measures_fps_and_age(self):
        now = [100.0]
        tr = sd.RateTracker(clock=lambda: now[0])
        for _ in range(31):                       # 30 Hz for one second
            tr.tick(1000)
            now[0] += 1 / 30
        snap = tr.snapshot()
        self.assertAlmostEqual(snap["fps"], 30.0, delta=1.0)
        self.assertEqual(snap["frames"], 31)
        now[0] += 10                              # silence -> stale
        snap = tr.snapshot()
        self.assertEqual(snap["fps"], 0.0)
        self.assertGreater(snap["age"], 9)

    def test_classify(self):
        fresh = {"age": 0.1, "fps": 10, "frames": 5, "mb_per_s": 0}
        self.assertEqual(sd.classify(1, True, fresh, is_sensor=True)[0], "streaming")
        self.assertEqual(sd.classify(1, True, fresh, is_sensor=False)[0], "running")
        self.assertEqual(sd.classify(0, True, fresh, is_sensor=True)[0], "disconnected")
        self.assertEqual(sd.classify(1, False, fresh, is_sensor=False)[0], "not_running")
        self.assertEqual(sd.classify(1, True, {**fresh, "age": None}, is_sensor=True)[0], "waiting")
        self.assertEqual(sd.classify(1, True, {**fresh, "age": 9}, is_sensor=True)[0], "stalled")

    def test_report_round_trips_through_the_base_station_parser(self):
        col = sd.DiagnosticsCollector()
        for topic, tr in col._trackers.items():
            for _ in range(5):
                tr.tick(100)
        col.note_web_request()
        raw = col.report()
        import json
        report = diagnostics.parse_report(json.loads(json.dumps(raw)))
        names = {i.name for i in report.sensors}
        self.assertEqual(names, {"camera", "thermal", "lidar"})
        self.assertIn("web_stream", {i.name for i in report.pipelines})


class ControlTests(unittest.TestCase):
    """The power daemon's logic (fake systemctl) and the app-side client."""

    def setUp(self):
        self.calls = []

        def runner(cmd, timeout=20):
            self.calls.append(cmd)
            if cmd[:3] == ["systemctl", "--user", "show"]:
                return 0, "ActiveState=active\nSubState=running\nActiveEnterTimestamp=now"
            return 0, ""
        self.runner = runner
        self._which = pc.shutil.which
        pc.shutil.which = lambda _n: "/usr/bin/systemctl"

        from http.server import ThreadingHTTPServer
        import threading
        self.later = []
        self.token = "s3cret"
        handler = pc.make_handler(lambda: self.token, runner=runner,
                                  schedule=lambda d, fn: self.later.append(fn))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        pc.shutil.which = self._which
        self.server.shutdown()
        self.server.server_close()

    def test_token_check(self):
        self.assertTrue(pc.token_ok("abc", "abc"))
        self.assertFalse(pc.token_ok("abd", "abc"))
        self.assertFalse(pc.token_ok("", ""))          # no token configured never matches

    def test_only_whitelisted_actions_run(self):
        ok, _ = pc.perform("reboot; rm -rf /", self.runner)
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])               # nothing was executed

    def test_status_over_http(self):
        st = control.get_status("127.0.0.1", self.port)
        self.assertEqual((st.kind, st.state, st.control_enabled), ("ok", control.ON, True))

    def test_wrong_and_missing_token_rejected_and_nothing_runs(self):
        ok, msg = control.send_action("127.0.0.1", self.port, "stop", "nope", timeout=5)
        self.assertFalse(ok)
        self.assertIn("token", msg.lower())
        self.token = ""                                 # control disabled
        ok, msg = control.send_action("127.0.0.1", self.port, "stop", "anything", timeout=5)
        self.assertFalse(ok)
        self.assertIn("disabled", msg.lower())
        self.assertFalse(any(c[:3] == ["systemctl", "--user", "stop"] for c in self.calls))

    def test_stop_runs_systemctl_user_stop(self):
        ok, _ = control.send_action("127.0.0.1", self.port, "stop", "s3cret", timeout=5)
        self.assertTrue(ok)
        self.assertIn(["systemctl", "--user", "stop", "pitfusion3d.service"], self.calls)

    def test_shutdown_replies_before_powering_off(self):
        ok, msg = control.send_action("127.0.0.1", self.port, "shutdown", "s3cret", timeout=5)
        self.assertTrue(ok)
        self.assertFalse(any("poweroff" in c for c in self.calls if c[0] == "sudo" and "-l" not in c))
        self.later[0]()                                 # the deferred power-off fires
        self.assertTrue(any(c[0] == "sudo" and c[-1] == "poweroff" and "-l" not in c
                            for c in self.calls))

    def test_unreachable_and_state_mapping(self):
        self.assertEqual(control.get_status("127.0.0.1", 1, timeout=0.5).kind, "unreachable")
        self.assertEqual(control.ui_state("activating"), control.STARTING)
        self.assertEqual(control.ui_state("deactivating"), control.STOPPING)
        self.assertEqual(control.ui_state("failed"), control.FAILED)


class SleepAndPowerStateTests(unittest.TestCase):
    """The sleep action runs only the operator's hook; the status tells the app what a power-off will really do."""

    def setUp(self):
        import os
        import stat
        import tempfile
        self.calls = []
        self.dir = tempfile.mkdtemp()
        self.hook = os.path.join(self.dir, "sleep_hook")
        with open(self.hook, "w") as fh:
            fh.write("#!/bin/sh\necho done\n")
        os.chmod(self.hook, os.stat(self.hook).st_mode | stat.S_IXUSR)
        self._old_hook = pc.SLEEP_HOOK
        pc.SLEEP_HOOK = self.hook

        def runner(cmd, timeout=20):
            self.calls.append(cmd)
            if cmd[:3] == ["systemctl", "--user", "show"]:
                return 0, "ActiveState=inactive\nSubState=dead\nActiveEnterTimestamp=now"
            if cmd[0] == "vcgencmd":
                return 0, "[all]\nBOOT_UART=1\nPOWER_OFF_ON_HALT=1\n"
            if cmd[0] == self.hook:
                return 0, "lidar: low power confirmed\nasleep"
            return 0, ""
        self.runner = runner

    def tearDown(self):
        import shutil
        pc.SLEEP_HOOK = self._old_hook
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_sleep_runs_exactly_the_hook_with_no_arguments(self):
        ok, msg = pc.perform("sleep", self.runner)
        self.assertTrue(ok)
        self.assertEqual(self.calls, [[self.hook]])     # nothing from the request reaches a command line
        self.assertEqual(msg, "asleep")

    def test_sleep_is_refused_without_an_executable_hook(self):
        import os
        os.chmod(self.hook, 0o644)                      # present but not executable
        ok, msg = pc.perform("sleep", self.runner)
        self.assertFalse(ok)
        self.assertIn("not set up", msg)
        self.assertEqual(self.calls, [])
        pc.SLEEP_HOOK = self.hook + ".missing"
        self.assertFalse(pc.can_sleep())

    def test_a_failing_hook_is_reported_as_failure(self):
        ok, msg = pc.perform("sleep", lambda cmd, timeout=20: (1, "could not reach the lidar"))
        self.assertFalse(ok)
        self.assertEqual(msg, "could not reach the lidar")

    def test_full_poweroff_reads_the_bootloader_setting(self):
        self.assertIs(pc.full_poweroff(lambda c, t=5: (0, "[all]\nPOWER_OFF_ON_HALT=1\n")), True)
        self.assertIs(pc.full_poweroff(lambda c, t=5: (0, "[all]\nPOWER_OFF_ON_HALT=0\n")), False)
        self.assertIs(pc.full_poweroff(lambda c, t=5: (0, "[all]\nBOOT_UART=1\n")), False)   # default: stays in standby
        self.assertIsNone(pc.full_poweroff(lambda c, t=5: (127, "vcgencmd: not found")))     # not a Pi

    def test_status_and_sleep_over_http(self):
        import threading
        from http.server import ThreadingHTTPServer
        handler = pc.make_handler(lambda: "tok", runner=self.runner)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            st = control.get_status("127.0.0.1", port)
            self.assertTrue(st.can_sleep)
            self.assertIs(st.full_poweroff, True)
            self.assertEqual(st.state, control.OFF)
            ok, _ = control.send_action("127.0.0.1", port, "sleep", "wrong", timeout=5)
            self.assertFalse(ok)
            self.assertEqual([c for c in self.calls if c[0] == self.hook], [])   # a wrong token never runs the hook
            ok, msg = control.send_action("127.0.0.1", port, "sleep", "tok", timeout=5)
            self.assertTrue(ok)
            self.assertEqual(msg, "asleep")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
