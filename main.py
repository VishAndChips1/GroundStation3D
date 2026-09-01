"""Base station GUI for the mine/landfill survey rig.

Polls the rover's HTTP point-cloud endpoint (xyz + rgb + thermal per point)
and renders/saves the latest frame live. See protocol.py for the wire
format and README.md for usage.
"""

import argparse
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime

import numpy as np
import open3d as o3d
import open3d.visualization.gui as gui
import open3d.visualization.rendering as rendering
import matplotlib

from protocol import Frame, MalformedFrameError, parse_frame

GEOMETRY_NAME = "live_cloud"
CAPTURES_DIR = "captures"
STATUS_MESSAGE_SECONDS = 3.0
THERMAL_COLORMAP_NAME = "inferno"
INVALID_THERMAL_COLOR = (0.5, 0.5, 0.5)  # points with NaN thermal in Thermal mode
FETCH_TIMEOUT_SECONDS = 2.0


class NetworkStats:
    """Rolling-window rate/fetch-time/frame counters.

    Written from the poller thread, read from the GUI thread. Plain int
    counters are safe to read across threads under the GIL; the byte/point
    logs are guarded by a lock since they're mutated with read-modify-write
    logic.
    """

    def __init__(self, window_seconds: float = 1.0):
        self._lock = threading.Lock()
        self._window = window_seconds
        self._byte_log = deque()   # (t, nbytes) per HTTP response received
        self._point_log = deque()  # (t, npoints) per completed frame
        self.frames_received = 0
        self.frames_dropped = 0
        self.last_fetch_seconds = None

    def record_response(self, nbytes: int) -> None:
        now = time.time()
        with self._lock:
            self._byte_log.append((now, nbytes))
            self._trim(self._byte_log, now)

    def record_frame(self, npoints: int, fetch_seconds: float) -> None:
        now = time.time()
        with self._lock:
            self._point_log.append((now, npoints))
            self._trim(self._point_log, now)
            self.frames_received += 1
            self.last_fetch_seconds = fetch_seconds

    def add_dropped(self, n: int) -> None:
        with self._lock:
            self.frames_dropped += n

    def _trim(self, log: deque, now: float) -> None:
        cutoff = now - self._window
        while log and log[0][0] < cutoff:
            log.popleft()

    def snapshot(self):
        with self._lock:
            byte_total = sum(n for _, n in self._byte_log)
            point_total = sum(n for _, n in self._point_log)
            return {
                "kbps": (byte_total / 1024.0) / self._window,
                "pts_per_sec": point_total / self._window,
                "frames_received": self.frames_received,
                "frames_dropped": self.frames_dropped,
                "fetch_seconds": self.last_fetch_seconds,
            }


def poller_thread_main(url, frame_queue, stats, stop_event, poll_interval):
    frame_id = 0
    while not stop_event.is_set():
        start = time.time()
        try:
            with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                data = resp.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            stats.add_dropped(1)
            stop_event.wait(poll_interval)
            continue

        recv_time = time.time()
        fetch_seconds = recv_time - start
        stats.record_response(len(data))

        try:
            frame = parse_frame(data, frame_id, recv_time, fetch_seconds)
        except MalformedFrameError:
            stats.add_dropped(1)
            stop_event.wait(poll_interval)
            continue

        frame_id += 1
        stats.record_frame(frame.num_points, fetch_seconds)
        try:
            frame_queue.put_nowait(frame)
        except queue.Full:
            try:
                frame_queue.get_nowait()
            except queue.Empty:
                pass
            frame_queue.put_nowait(frame)

        elapsed = time.time() - start
        if elapsed < poll_interval:
            stop_event.wait(poll_interval - elapsed)


ACCENT_COLOR = gui.Color(0.16, 0.53, 0.87)
SAVE_COLOR = gui.Color(0.18, 0.62, 0.36)
DIM_TEXT_COLOR = gui.Color(0.65, 0.65, 0.65)
PANEL_ACCENT_BG = gui.Color(0.20, 0.22, 0.26)
STATS_REFRESH_SECONDS = 0.25


class BaseStationApp:
    def __init__(self, frame_queue, stats, source_url, on_close):
        self._frame_queue = frame_queue
        self._stats = stats
        self._on_close_cb = on_close

        self._latest_frame: Frame | None = None
        self._color_mode = "RGB"
        self._temp_range = None
        self._status_expiry = None
        self._last_stats_update = 0.0

        self._colormap = matplotlib.colormaps[THERMAL_COLORMAP_NAME]

        self._pcd = o3d.geometry.PointCloud()
        self._geometry_added = False
        self._material = rendering.MaterialRecord()
        self._material.shader = "defaultUnlit"
        self._material.point_size = 3.0

        self.window = gui.Application.instance.create_window(
            "Survey Rig Base Station", 1400, 860)
        self.window.set_on_layout(self._on_layout)
        self.window.set_on_close(self._on_window_close)
        self.window.set_on_tick_event(self._on_tick)

        em = self.window.theme.font_size

        self._scene = gui.SceneWidget()
        self._scene.scene = rendering.Open3DScene(self.window.renderer)
        self._scene.scene.set_background([0.08, 0.09, 0.10, 1.0])
        self._scene.scene.scene.set_sun_light(
            [-1, -1, -1], [1, 1, 1], 75000)
        self._scene.scene.scene.enable_sun_light(True)
        self._scene.scene.show_axes(True)
        # ROTATE_CAMERA already gives left-drag orbit, right/middle-drag pan,
        # and scroll-wheel zoom for free -- this is Open3D's default mouse
        # scheme, nothing custom needed here.
        self._scene.set_view_controls(gui.SceneWidget.Controls.ROTATE_CAMERA)
        self.window.add_child(self._scene)

        self._panel = gui.Vert(0, gui.Margins(0))
        self.window.add_child(self._panel)

        # -- header ---------------------------------------------------
        header = gui.Vert(0.25 * em, gui.Margins(em, em, em, 0.5 * em))
        title = gui.Label("Base Station")
        title.text_color = ACCENT_COLOR
        header.add_child(title)
        self._link_label = gui.Label(f"polling {source_url}")
        self._link_label.text_color = DIM_TEXT_COLOR
        header.add_child(self._link_label)
        self._panel.add_child(header)
        self._panel.add_fixed(0.25 * em)
        self._panel.add_child(self._make_divider())

        # -- rendering section ------------------------------------------
        rendering_section = gui.CollapsableVert(
            "Rendering", 0.4 * em, gui.Margins(em, 0.5 * em, em, 0.5 * em))

        size_row = gui.Horiz(0.5 * em)
        size_row.add_child(gui.Label("Point size"))
        size_row.add_stretch()
        self._point_size_value_label = gui.Label("3")
        self._point_size_value_label.text_color = ACCENT_COLOR
        size_row.add_child(self._point_size_value_label)
        rendering_section.add_child(size_row)

        self._point_size_slider = gui.Slider(gui.Slider.INT)
        self._point_size_slider.set_limits(1, 10)
        self._point_size_slider.int_value = int(self._material.point_size)
        self._point_size_slider.background_color = PANEL_ACCENT_BG
        self._point_size_slider.set_on_value_changed(self._on_point_size_changed)
        rendering_section.add_child(self._point_size_slider)

        rendering_section.add_fixed(0.5 * em)
        rendering_section.add_child(gui.Label("Color overlay"))
        self._color_combo = gui.Combobox()
        self._color_combo.add_item("RGB")
        self._color_combo.add_item("Thermal")
        self._color_combo.selected_index = 0
        self._color_combo.background_color = PANEL_ACCENT_BG
        self._color_combo.set_on_selection_changed(self._on_color_mode_changed)
        rendering_section.add_child(self._color_combo)

        self._temp_range_label = gui.Label("Temp range: n/a (RGB mode)")
        self._temp_range_label.text_color = DIM_TEXT_COLOR
        rendering_section.add_child(self._temp_range_label)

        rendering_section.add_fixed(0.5 * em)
        rendering_section.add_child(gui.Label("View"))
        view_row = gui.Horiz(em)
        self._axes_checkbox = gui.Checkbox("Axes")
        self._axes_checkbox.checked = True
        self._axes_checkbox.set_on_checked(self._on_axes_toggled)
        view_row.add_child(self._axes_checkbox)
        self._grid_checkbox = gui.Checkbox("Grid")
        self._grid_checkbox.checked = False
        self._grid_checkbox.set_on_checked(self._on_grid_toggled)
        view_row.add_child(self._grid_checkbox)
        rendering_section.add_child(view_row)

        self._reset_view_button = gui.Button("Reset View")
        self._reset_view_button.background_color = PANEL_ACCENT_BG
        self._reset_view_button.vertical_padding_em = 0.3
        self._reset_view_button.set_on_clicked(self._on_reset_view_clicked)
        rendering_section.add_child(self._reset_view_button)

        self._panel.add_child(rendering_section)
        self._panel.add_child(self._make_divider())

        # -- capture section ------------------------------------------
        capture_section = gui.CollapsableVert(
            "Capture", 0.4 * em, gui.Margins(em, 0.5 * em, em, 0.5 * em))
        self._save_button = gui.Button("Save PCD")
        self._save_button.background_color = SAVE_COLOR
        self._save_button.vertical_padding_em = 0.4
        self._save_button.set_on_clicked(self._on_save_clicked)
        capture_section.add_child(self._save_button)

        self._status_label = gui.Label("")
        self._status_label.text_color = DIM_TEXT_COLOR
        capture_section.add_child(self._status_label)

        self._panel.add_child(capture_section)
        self._panel.add_child(self._make_divider())

        # -- live stats section ------------------------------------------
        stats_section = gui.CollapsableVert(
            "Live Stats", 0.4 * em, gui.Margins(em, 0.5 * em, em, 0.5 * em))
        self._rate_label = self._add_stat_row(stats_section, "Data rate")
        self._points_label = self._add_stat_row(stats_section, "Points/sec")
        self._fetch_label = self._add_stat_row(stats_section, "Fetch time")
        self._frames_label = self._add_stat_row(stats_section, "Frames OK")
        self._dropped_label = self._add_stat_row(stats_section, "Frames dropped")
        self._panel.add_child(stats_section)

        self._panel.add_stretch()

    def _make_divider(self) -> gui.Widget:
        line = gui.Horiz()
        line.preferred_height = 1
        line.background_color = gui.Color(0.3, 0.3, 0.3)
        return line

    def _add_stat_row(self, parent, caption: str) -> gui.Label:
        row = gui.Horiz(0.5 * self.window.theme.font_size)
        cap = gui.Label(caption)
        cap.text_color = DIM_TEXT_COLOR
        row.add_child(cap)
        row.add_stretch()
        value = gui.Label("-")
        row.add_child(value)
        parent.add_child(row)
        return value

    # -- layout / lifecycle -------------------------------------------------

    def _on_layout(self, layout_context):
        r = self.window.content_rect
        panel_width = min(20 * layout_context.theme.font_size, 0.4 * r.width)
        self._scene.frame = gui.Rect(r.x, r.y, r.width - panel_width, r.height)
        self._panel.frame = gui.Rect(r.get_right() - panel_width, r.y,
                                      panel_width, r.height)

    def _on_window_close(self):
        self._on_close_cb()
        return True

    # -- per-frame tick -------------------------------------------------

    def _on_tick(self):
        # Only tell Open3D the 3D scene needs a redraw when a new point
        # cloud actually arrived. This return value isn't just a framerate
        # knob: Open3D appears to permanently switch the renderer into a
        # continuous (vsync-locked, high-CPU) mode after enough cumulative
        # True returns, even if they're spread out over time and the app is
        # otherwise idle. So label/stat text is updated on every tick (cheap,
        # and repaints fine on its own) without ever counting toward the
        # scene's "needs redraw" signal.
        got_new_frame = False
        try:
            while True:
                self._latest_frame = self._frame_queue.get_nowait()
                got_new_frame = True
        except queue.Empty:
            pass

        if got_new_frame and self._latest_frame is not None:
            self._show_frame(self._latest_frame, reset_camera=not self._geometry_added)

        now = time.time()
        if now - self._last_stats_update >= STATS_REFRESH_SECONDS:
            self._last_stats_update = now
            self._update_stats_labels()

        if self._status_expiry is not None and now > self._status_expiry:
            self._status_label.text = ""
            self._status_expiry = None

        return got_new_frame

    # -- geometry / color -------------------------------------------------

    def _compute_colors(self, frame: Frame) -> np.ndarray:
        if self._color_mode == "RGB":
            self._temp_range = None
            # Rover already sends 0-1 floats; clip defensively since this
            # crosses a network boundary and isn't guaranteed in-range.
            return np.clip(frame.rgb.astype(np.float64), 0.0, 1.0)

        temp = frame.temp_c
        valid = ~np.isnan(temp)
        if not np.any(valid):
            self._temp_range = None
            return np.tile(INVALID_THERMAL_COLOR, (len(temp), 1))

        tmin = float(np.min(temp[valid]))
        tmax = float(np.max(temp[valid]))
        self._temp_range = (tmin, tmax)
        span = max(tmax - tmin, 1e-6)

        colors = np.tile(INVALID_THERMAL_COLOR, (len(temp), 1))
        norm = np.clip((temp[valid] - tmin) / span, 0.0, 1.0)
        colors[valid] = self._colormap(norm)[:, :3]
        return colors

    def _show_frame(self, frame: Frame, reset_camera: bool) -> None:
        self._pcd.points = o3d.utility.Vector3dVector(frame.xyz.astype(np.float64))
        self._pcd.colors = o3d.utility.Vector3dVector(self._compute_colors(frame))

        if self._geometry_added:
            self._scene.scene.remove_geometry(GEOMETRY_NAME)
        self._scene.scene.add_geometry(GEOMETRY_NAME, self._pcd, self._material)
        self._geometry_added = True

        if reset_camera and frame.num_points > 0:
            self._fit_camera_to_data()

        if self._temp_range is not None:
            tmin, tmax = self._temp_range
            self._temp_range_label.text = f"Temp range: {tmin:.1f} - {tmax:.1f} C"
        else:
            self._temp_range_label.text = "Temp range: n/a (RGB mode)"

    def _fit_camera_to_data(self) -> None:
        if len(self._pcd.points) == 0:
            return
        bounds = self._pcd.get_axis_aligned_bounding_box()
        self._scene.setup_camera(60, bounds, bounds.get_center())

    # -- widget callbacks -------------------------------------------------

    def _on_axes_toggled(self, checked: bool):
        self._scene.scene.show_axes(checked)
        self.window.post_redraw()

    def _on_grid_toggled(self, checked: bool):
        self._scene.scene.show_ground_plane(checked, rendering.Scene.GroundPlane.XY)
        self.window.post_redraw()

    def _on_reset_view_clicked(self):
        self._fit_camera_to_data()
        self.window.post_redraw()

    def _on_point_size_changed(self, value):
        self._material.point_size = float(value)
        self._point_size_value_label.text = str(int(value))
        if self._geometry_added:
            self._scene.scene.modify_geometry_material(GEOMETRY_NAME, self._material)
        self.window.post_redraw()

    def _on_color_mode_changed(self, text, index):
        self._color_mode = "RGB" if index == 0 else "Thermal"
        if self._latest_frame is not None:
            self._show_frame(self._latest_frame, reset_camera=False)
        self.window.post_redraw()

    def _on_save_clicked(self):
        if self._latest_frame is None:
            self._set_status("No frame received yet -- nothing to save.")
            return
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(CAPTURES_DIR, f"frame_{ts}.pcd")
        ok = o3d.io.write_point_cloud(path, self._pcd)
        if ok:
            self._set_status(f"Saved {path} ({self._latest_frame.num_points} pts)")
        else:
            self._set_status("Save failed -- see console.")

    def _set_status(self, text: str) -> None:
        self._status_label.text = text
        self._status_expiry = time.time() + STATUS_MESSAGE_SECONDS

    def _update_stats_labels(self) -> None:
        s = self._stats.snapshot()
        self._rate_label.text = f"{s['kbps']:.1f} KB/s"
        self._points_label.text = f"{s['pts_per_sec']:,.0f}"
        if s["fetch_seconds"] is not None:
            self._fetch_label.text = f"{s['fetch_seconds'] * 1000:.0f} ms"
        else:
            self._fetch_label.text = "-"
        self._frames_label.text = str(s["frames_received"])
        self._dropped_label.text = str(s["frames_dropped"])


def main():
    parser = argparse.ArgumentParser(description="Survey rig base station")
    parser.add_argument("--rover-host", required=True,
                         help="IP or hostname of the rover's HTTP point-cloud server")
    parser.add_argument("--rover-port", type=int, default=8080,
                         help="port of the rover's HTTP point-cloud server")
    parser.add_argument("--poll-interval", type=float, default=0.1,
                         help="minimum seconds between GET /latest.bin requests")
    args = parser.parse_args()

    source_url = f"http://{args.rover_host}:{args.rover_port}/latest.bin"

    frame_queue: queue.Queue = queue.Queue(maxsize=2)
    stats = NetworkStats()
    stop_event = threading.Event()

    poller = threading.Thread(
        target=poller_thread_main,
        args=(source_url, frame_queue, stats, stop_event, args.poll_interval),
        daemon=True,
    )
    poller.start()

    def shutdown():
        stop_event.set()

    gui.Application.instance.initialize()
    app = BaseStationApp(frame_queue, stats, source_url, shutdown)
    print(f"Polling point cloud frames from {source_url}")
    gui.Application.instance.run()

    stop_event.set()
    poller.join(timeout=1.0)


if __name__ == "__main__":
    main()
