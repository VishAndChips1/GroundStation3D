"""Base station GUI for the mine/landfill survey rig.

Polls the rover's HTTP point-cloud endpoint (xyz + rgb + thermal per point)
and renders the latest frame live. Built on Qt (PySide6) + PyVista/VTK.

See protocol.py for the wire format and README.md for usage.
"""

import faulthandler
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime

import matplotlib
import numpy as np
import pyvista as pv
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QScrollArea, QSlider,
    QSpinBox, QVBoxLayout, QWidget,
)
from pyvistaqt import QtInteractor

import theme
from protocol import Frame, MalformedFrameError, parse_frame

CAPTURES_DIR = "captures"
FETCH_TIMEOUT_SECONDS = 2.0
INVALID_THERMAL_COLOR = (128, 128, 128)  # gray for points with no thermal reading
THERMAL_COLORMAP = "inferno"
STATS_REFRESH_MS = 250


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------

class NetworkStats:
    """Rolling-window rate/fetch-time/frame counters, shared across threads."""

    def __init__(self, window_seconds: float = 1.0):
        self._lock = threading.Lock()
        self._window = window_seconds
        self._byte_log = deque()   # (t, nbytes) per HTTP response
        self._point_log = deque()  # (t, npoints) per decoded frame
        self.frames_received = 0
        self.frames_failed = 0
        self.last_fetch_seconds = None
        self.last_point_count = 0

    def record_frame(self, nbytes: int, npoints: int, fetch_seconds: float) -> None:
        now = time.time()
        with self._lock:
            self._byte_log.append((now, nbytes))
            self._point_log.append((now, npoints))
            self._trim(self._byte_log, now)
            self._trim(self._point_log, now)
            self.frames_received += 1
            self.last_fetch_seconds = fetch_seconds
            self.last_point_count = npoints

    def record_failure(self) -> None:
        with self._lock:
            self.frames_failed += 1

    def reset(self) -> None:
        with self._lock:
            self._byte_log.clear()
            self._point_log.clear()
            self.frames_received = 0
            self.frames_failed = 0
            self.last_fetch_seconds = None
            self.last_point_count = 0

    def _trim(self, log: deque, now: float) -> None:
        cutoff = now - self._window
        while log and log[0][0] < cutoff:
            log.popleft()

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._trim(self._byte_log, now)
            self._trim(self._point_log, now)
            return {
                "kbps": sum(n for _, n in self._byte_log) / 1024.0 / self._window,
                "pts_per_sec": sum(n for _, n in self._point_log) / self._window,
                "frames_received": self.frames_received,
                "frames_failed": self.frames_failed,
                "fetch_seconds": self.last_fetch_seconds,
                "point_count": self.last_point_count,
            }


class PollWorker(QThread):
    """Polls the rover endpoint on a background thread.

    Frames are handed to the GUI thread through a queued Qt signal, so no
    GUI object is ever touched from here.
    """

    frame_ready = Signal(object)
    state_changed = Signal(str, str)  # state ("live"/"waiting"/"error"), detail

    def __init__(self, url: str, poll_interval: float, stats: NetworkStats):
        super().__init__()
        self._url = url
        self._poll_interval = poll_interval
        self._stats = stats
        self._stop_event = threading.Event()
        self._last_state = None

    def stop(self) -> None:
        self._stop_event.set()

    def _emit_state(self, state: str, detail: str) -> None:
        # Only emit on change, so the GUI isn't spammed while idling.
        if (state, detail) != self._last_state:
            self._last_state = (state, detail)
            self.state_changed.emit(state, detail)

    def run(self) -> None:
        frame_id = 0
        self._emit_state("waiting", "connecting...")

        while not self._stop_event.is_set():
            start = time.time()
            try:
                with urllib.request.urlopen(self._url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                    data = resp.read()
            except urllib.error.HTTPError as exc:
                self._stats.record_failure()
                self._emit_state("error", f"HTTP {exc.code} from server")
                self._stop_event.wait(self._poll_interval)
                continue
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                self._stats.record_failure()
                self._emit_state("error", self._describe_error(exc))
                self._stop_event.wait(self._poll_interval)
                continue

            recv_time = time.time()
            fetch_seconds = recv_time - start

            try:
                frame = parse_frame(data, frame_id, recv_time, fetch_seconds)
            except MalformedFrameError as exc:
                self._stats.record_failure()
                self._emit_state("error", f"bad frame: {exc}")
                self._stop_event.wait(self._poll_interval)
                continue

            frame_id += 1
            self._stats.record_frame(len(data), frame.num_points, fetch_seconds)
            self._emit_state("live", f"{frame.num_points:,} points")
            self.frame_ready.emit(frame)

            elapsed = time.time() - start
            if elapsed < self._poll_interval:
                self._stop_event.wait(self._poll_interval - elapsed)

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        if "refused" in text.lower():
            return "connection refused - is the rover server running?"
        if "timed out" in text.lower() or isinstance(exc, TimeoutError):
            return "timed out - check the IP and that you're on the same network"
        if "unreachable" in text.lower():
            return "host unreachable - check the IP"
        return text[:80]


# --------------------------------------------------------------------------
# GUI helpers
# --------------------------------------------------------------------------

def make_divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    return line


def make_section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("SectionLabel")
    return label


class StatRow(QWidget):
    """A caption on the left, a value on the right."""

    def __init__(self, caption: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        cap = QLabel(caption)
        cap.setObjectName("StatCaption")
        self.value = QLabel("-")
        self.value.setObjectName("StatValue")
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(cap)
        layout.addStretch()
        layout.addWidget(self.value)

    def set(self, text: str) -> None:
        self.value.setText(text)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class BaseStation(QMainWindow):
    def __init__(self, host: str, port: int, poll_interval: float):
        super().__init__()
        self.setWindowTitle("Survey Rig Base Station")
        self.resize(1500, 900)

        self._stats = NetworkStats()
        self._worker: PollWorker | None = None
        self._latest_frame: Frame | None = None
        self._cloud: pv.PolyData | None = None
        self._actor = None
        self._bounds_actor = None
        self._origin_actor = None
        self._temp_range = None
        self._point_size = 3
        self._colormap = matplotlib.colormaps[THERMAL_COLORMAP]
        # Persistent buffers handed to VTK -- see _on_frame for why these
        # must be owned by the window rather than created per frame.
        self._xyz_buffer: np.ndarray | None = None
        self._color_buffer: np.ndarray | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- 3D canvas ---------------------------------------------------
        self.plotter = QtInteractor(central)
        self.plotter.set_background(theme.VIEWPORT_BG, top=theme.VIEWPORT_BG_TOP)
        # Corner orientation marker -- rotates with the camera so you always
        # know which way X/Y/Z point.
        self.plotter.add_axes(interactive=False)
        root.addWidget(self.plotter.interactor, stretch=1)

        # -- side panel --------------------------------------------------
        root.addWidget(self._build_panel(host, port, poll_interval))

        self._show_origin(True)
        self._show_scale_grid(True)

        # Stats refresh on the GUI thread.
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(STATS_REFRESH_MS)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.status_label.setText(""))

    # -- panel construction ---------------------------------------------

    def _build_panel(self, host: str, port: int, poll_interval: float) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SidePanel")
        panel.setFixedWidth(330)

        scroll = QScrollArea(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        title = QLabel("Base Station")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)
        subtitle = QLabel("Live rover point cloud")
        subtitle.setObjectName("SubtitleLabel")
        layout.addWidget(subtitle)
        layout.addSpacing(6)

        # -- connection ---------------------------------------------------
        layout.addWidget(make_section_label("Connection"))

        self.host_edit = QLineEdit(host)
        self.host_edit.setPlaceholderText("rover IP, e.g. 192.168.1.50")
        self.host_edit.setToolTip(
            "IP or hostname of the machine serving /latest.bin.\n"
            "Works with any sender on the network -- WSL, a Pi on the rig, etc.")
        layout.addWidget(self._labeled("Rover host", self.host_edit))

        port_row = QWidget()
        port_layout = QHBoxLayout(port_row)
        port_layout.setContentsMargins(0, 0, 0, 0)
        port_layout.setSpacing(8)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(port)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.02, 5.0)
        self.interval_spin.setSingleStep(0.05)
        self.interval_spin.setDecimals(2)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(poll_interval)
        self.interval_spin.setToolTip("Minimum time between requests")
        port_layout.addWidget(self._labeled("Port", self.port_spin))
        port_layout.addWidget(self._labeled("Interval", self.interval_spin))
        layout.addWidget(port_row)

        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("PrimaryButton")
        self.connect_button.clicked.connect(self._toggle_connection)
        layout.addWidget(self.connect_button)

        self.connection_label = QLabel("Not connected")
        self.connection_label.setObjectName("StatusLabel")
        self.connection_label.setWordWrap(True)
        layout.addWidget(self.connection_label)

        layout.addWidget(make_divider())

        # -- display ------------------------------------------------------
        layout.addWidget(make_section_label("Display"))

        size_row = QWidget()
        size_layout = QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_label = QLabel("Point size")
        size_label.setObjectName("StatCaption")
        self.size_value_label = QLabel("3")
        self.size_value_label.setObjectName("StatValue")
        size_layout.addWidget(size_label)
        size_layout.addStretch()
        size_layout.addWidget(self.size_value_label)
        layout.addWidget(size_row)

        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(1, 12)
        self.size_slider.setValue(self._point_size)
        self.size_slider.valueChanged.connect(self._on_point_size_changed)
        layout.addWidget(self.size_slider)

        self.color_combo = QComboBox()
        self.color_combo.addItems(["RGB (camera)", "Thermal"])
        self.color_combo.currentIndexChanged.connect(self._on_color_mode_changed)
        layout.addWidget(self._labeled("Color overlay", self.color_combo))

        self.temp_label = QLabel("Temp range: n/a")
        self.temp_label.setObjectName("StatusLabel")
        layout.addWidget(self.temp_label)

        layout.addWidget(make_divider())

        # -- view ---------------------------------------------------------
        layout.addWidget(make_section_label("View"))

        self.axes_check = QCheckBox("Orientation axes")
        self.axes_check.setChecked(True)
        self.axes_check.toggled.connect(self._on_axes_toggled)
        layout.addWidget(self.axes_check)

        self.origin_check = QCheckBox("Origin marker")
        self.origin_check.setChecked(True)
        self.origin_check.toggled.connect(self._show_origin)
        layout.addWidget(self.origin_check)

        self.scale_check = QCheckBox("Scale grid (metres)")
        self.scale_check.setChecked(True)
        self.scale_check.setToolTip(
            "Graduated bounding box with tick labels in metres.\n"
            "Ticks re-scale automatically as you zoom.")
        self.scale_check.toggled.connect(self._show_scale_grid)
        layout.addWidget(self.scale_check)

        view_buttons = QWidget()
        vb_layout = QHBoxLayout(view_buttons)
        vb_layout.setContentsMargins(0, 0, 0, 0)
        vb_layout.setSpacing(8)
        reset_button = QPushButton("Reset view")
        reset_button.clicked.connect(self._reset_view)
        top_button = QPushButton("Top-down")
        top_button.clicked.connect(self._top_view)
        vb_layout.addWidget(reset_button)
        vb_layout.addWidget(top_button)
        layout.addWidget(view_buttons)

        layout.addWidget(make_divider())

        # -- capture ------------------------------------------------------
        layout.addWidget(make_section_label("Capture"))

        self.save_button = QPushButton("Save point cloud (.pcd)")
        self.save_button.setObjectName("SuccessButton")
        self.save_button.clicked.connect(self._save_pcd)
        layout.addWidget(self.save_button)

        screenshot_button = QPushButton("Save screenshot (.png)")
        screenshot_button.clicked.connect(self._save_screenshot)
        layout.addWidget(screenshot_button)

        self.status_label = QLabel("")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addWidget(make_divider())

        # -- stats --------------------------------------------------------
        layout.addWidget(make_section_label("Live stats"))
        self.stat_rate = StatRow("Data rate")
        self.stat_points_sec = StatRow("Points / sec")
        self.stat_count = StatRow("Points in frame")
        self.stat_fetch = StatRow("Fetch time")
        self.stat_ok = StatRow("Frames OK")
        self.stat_failed = StatRow("Frames failed")
        for row in (self.stat_rate, self.stat_points_sec, self.stat_count,
                    self.stat_fetch, self.stat_ok, self.stat_failed):
            layout.addWidget(row)

        layout.addStretch()

        scroll.setWidget(inner)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.addWidget(scroll)
        return panel

    @staticmethod
    def _labeled(caption: str, widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QLabel(caption)
        label.setObjectName("StatCaption")
        layout.addWidget(label)
        layout.addWidget(widget)
        return container

    # -- connection -----------------------------------------------------

    def _toggle_connection(self) -> None:
        if self._worker is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        host = self.host_edit.text().strip()
        if not host:
            self.connection_label.setText("Enter the rover's IP address first.")
            return

        url = f"http://{host}:{self.port_spin.value()}/latest.bin"
        self._stats.reset()

        self._worker = PollWorker(url, self.interval_spin.value(), self._stats)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.state_changed.connect(self._on_state_changed)
        self._worker.start()

        self.connect_button.setText("Disconnect")
        self.connect_button.setObjectName("")
        self.connect_button.setStyleSheet("")
        self.connection_label.setText(f"Polling {url}")
        for widget in (self.host_edit, self.port_spin, self.interval_spin):
            widget.setEnabled(False)
        self._restyle(self.connect_button)

    def _disconnect(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None
        self.connect_button.setText("Connect")
        self.connect_button.setObjectName("PrimaryButton")
        self.connection_label.setText("Not connected")
        for widget in (self.host_edit, self.port_spin, self.interval_spin):
            widget.setEnabled(True)
        self._restyle(self.connect_button)

    def _restyle(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _on_state_changed(self, state: str, detail: str) -> None:
        if state == "live":
            self.connection_label.setText(f"● Live — {detail}")
        elif state == "waiting":
            self.connection_label.setText(f"○ {detail}")
        else:
            self.connection_label.setText(f"● {detail}")

    # -- rendering -------------------------------------------------------

    def _on_frame(self, frame: Frame) -> None:
        self._latest_frame = frame
        if frame.num_points == 0:
            return

        n = frame.num_points
        colors = self._compute_colors(frame)

        # VTK wraps numpy arrays by reference rather than copying them, so
        # anything handed to it must outlive the render. These buffers are
        # owned by the window and written in place; passing short-lived
        # locals here causes VTK to read freed memory and crash the process.
        rebuild = self._cloud is None or self._xyz_buffer is None \
            or len(self._xyz_buffer) != n

        if rebuild:
            self._xyz_buffer = np.empty((n, 3), dtype=np.float32)
            self._color_buffer = np.empty((n, 3), dtype=np.uint8)

        np.copyto(self._xyz_buffer, frame.xyz)
        np.copyto(self._color_buffer, colors)

        first_frame = self._cloud is None
        if rebuild:
            if self._actor is not None:
                self.plotter.remove_actor(self._actor, render=False)
            self._cloud = pv.PolyData(self._xyz_buffer)
            self._cloud["colors"] = self._color_buffer
            self._actor = self.plotter.add_mesh(
                self._cloud,
                scalars="colors",
                rgb=True,
                point_size=self._point_size,
                render_points_as_spheres=False,
                lighting=False,
                render=False,
            )
        else:
            # Same point count: the buffers VTK already points at have just
            # been overwritten, so only tell it they changed.
            self._cloud.GetPoints().Modified()
            scalars = self._cloud.GetPointData().GetScalars()
            if scalars is not None:
                scalars.Modified()
            self._cloud.Modified()

        if first_frame:
            self.plotter.reset_camera()
            if self.scale_check.isChecked():
                self._show_scale_grid(True)

        self.plotter.render()

    def _compute_colors(self, frame: Frame) -> np.ndarray:
        if self.color_combo.currentIndex() == 0:
            self._temp_range = None
            self.temp_label.setText("Temp range: n/a (RGB mode)")
            rgb = np.clip(frame.rgb, 0.0, 1.0)
            return (rgb * 255).astype(np.uint8)

        temp = frame.temp_c
        valid = ~np.isnan(temp)
        colors = np.tile(np.array(INVALID_THERMAL_COLOR, dtype=np.uint8),
                          (len(temp), 1))

        if not np.any(valid):
            self._temp_range = None
            self.temp_label.setText("Temp range: no thermal data in frame")
            return colors

        tmin = float(np.min(temp[valid]))
        tmax = float(np.max(temp[valid]))
        self._temp_range = (tmin, tmax)
        self.temp_label.setText(f"Temp range: {tmin:.1f} to {tmax:.1f} °C")

        span = max(tmax - tmin, 1e-6)
        norm = np.clip((temp[valid] - tmin) / span, 0.0, 1.0)
        colors[valid] = (self._colormap(norm)[:, :3] * 255).astype(np.uint8)
        return colors

    # -- view controls ---------------------------------------------------

    def _on_point_size_changed(self, value: int) -> None:
        self._point_size = value
        self.size_value_label.setText(str(value))
        if self._actor is not None:
            self._actor.prop.point_size = value
            self.plotter.render()

    def _on_color_mode_changed(self, _index: int) -> None:
        if self._latest_frame is not None:
            self._on_frame(self._latest_frame)

    def _on_axes_toggled(self, checked: bool) -> None:
        if checked:
            self.plotter.add_axes(interactive=False)
        else:
            self.plotter.hide_axes()
        self.plotter.render()

    def _show_origin(self, checked: bool) -> None:
        if self._origin_actor is not None:
            self.plotter.remove_actor(self._origin_actor, render=False)
            self._origin_actor = None
        if checked:
            # A small triad drawn at world (0, 0, 0) so the rover's origin
            # is always locatable in the scene.
            self._origin_actor = self.plotter.add_axes_at_origin(
                labels_off=True, line_width=3)
        self.plotter.render()

    def _show_scale_grid(self, checked: bool) -> None:
        if self._bounds_actor is not None:
            self.plotter.remove_bounds_axes()
            self._bounds_actor = None
        if checked:
            # Graduated bounding box: VTK re-computes the tick spacing as the
            # camera moves, so it doubles as a live scale reference.
            self._bounds_actor = self.plotter.show_bounds(
                grid="back",
                location="outer",
                ticks="both",
                minor_ticks=True,
                xtitle="X (m)",
                ytitle="Y (m)",
                ztitle="Z (m)",
                color=theme.TEXT_DIM,
                fmt="%.2f",
            )
        self.plotter.render()

    def _reset_view(self) -> None:
        self.plotter.reset_camera()
        self.plotter.render()

    def _top_view(self) -> None:
        self.plotter.view_xy()
        self.plotter.render()

    # -- capture ---------------------------------------------------------

    def _save_pcd(self) -> None:
        if self._latest_frame is None or self._cloud is None:
            self._set_status("No frame received yet - nothing to save.")
            return
        import open3d as o3d

        os.makedirs(CAPTURES_DIR, exist_ok=True)
        path = os.path.join(
            CAPTURES_DIR, f"frame_{datetime.now():%Y%m%d_%H%M%S}.pcd")

        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(
            self._latest_frame.xyz.astype(np.float64))
        pcd.colors = o3d.utility.Vector3dVector(
            np.asarray(self._cloud["colors"], dtype=np.float64) / 255.0)

        if o3d.io.write_point_cloud(path, pcd):
            self._set_status(f"Saved {path} ({self._latest_frame.num_points:,} pts)")
        else:
            self._set_status("Save failed - check the captures/ folder permissions.")

    def _save_screenshot(self) -> None:
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        path = os.path.join(
            CAPTURES_DIR, f"view_{datetime.now():%Y%m%d_%H%M%S}.png")
        self.plotter.screenshot(path)
        self._set_status(f"Saved {path}")

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self._status_timer.start(4000)

    # -- stats -----------------------------------------------------------

    def _refresh_stats(self) -> None:
        s = self._stats.snapshot()
        self.stat_rate.set(f"{s['kbps']:.1f} KB/s")
        self.stat_points_sec.set(f"{s['pts_per_sec']:,.0f}")
        self.stat_count.set(f"{s['point_count']:,}")
        self.stat_fetch.set(
            f"{s['fetch_seconds'] * 1000:.0f} ms" if s["fetch_seconds"] else "-")
        self.stat_ok.set(str(s["frames_received"]))
        self.stat_failed.set(str(s["frames_failed"]))

    # -- lifecycle -------------------------------------------------------

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(3000)
        self.plotter.close()
        super().closeEvent(event)


_fault_log = None


def _enable_fault_log() -> None:
    """Send native-crash tracebacks to crash.log (works without a console)."""
    global _fault_log
    try:
        _fault_log = open("crash.log", "a", buffering=1, encoding="utf-8")
        faulthandler.enable(file=_fault_log)
    except Exception:
        pass  # diagnostics are best-effort; never block startup


def main():
    import argparse

    # A crash inside VTK/Qt native code produces no Python traceback. Log
    # faults to a file rather than stderr: under pythonw.exe (the no-console
    # launcher) sys.stderr is None, and faulthandler.enable() would itself
    # raise and kill the app before the window ever appears.
    _enable_fault_log()

    parser = argparse.ArgumentParser(description="Survey rig base station")
    parser.add_argument("--rover-host", default="",
                         help="rover IP to pre-fill (editable in the GUI)")
    parser.add_argument("--rover-port", type=int, default=8080)
    parser.add_argument("--poll-interval", type=float, default=0.2)
    parser.add_argument("--connect", action="store_true",
                         help="connect immediately on startup")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyleSheet(theme.STYLESHEET)

    window = BaseStation(args.rover_host, args.rover_port, args.poll_interval)
    window.showMaximized()
    if args.connect and args.rover_host:
        window._connect()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
