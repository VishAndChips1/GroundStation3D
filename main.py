"""Base station GUI for the mine/landfill survey rig.

Polls the rover's HTTP point-cloud endpoint (xyz + rgb + thermal per point)
and renders the latest frame live. Built on Qt (PySide6) + PyVista/VTK.

See protocol.py for the wire format and README.md for usage.
"""

import ctypes
import faulthandler
import json
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
from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget,
)
from pyvistaqt import QtInteractor

import discovery
import icons
import theme
import widgets
from protocol import Frame, MalformedFrameError, parse_frame

RECENT_HOSTS_FILE = "recent_hosts.json"
MAX_RECENT_HOSTS = 8

CAPTURES_DIR = "captures"
PANEL_MIN_WIDTH = 340
PANEL_MAX_WIDTH = 460
# Generous by default: over real WiFi a full-resolution frame can be several
# MB, and a tight timeout would fail every request on a marginal link.
DEFAULT_FETCH_TIMEOUT = 10.0
INVALID_THERMAL_COLOR = (128, 128, 128)  # gray for points with no thermal reading
THERMAL_COLORMAP = "inferno"
STATS_REFRESH_MS = 250

# Win32 hit-test codes, used to keep native resize/snap on a frameless window.
_WM_NCHITTEST = 0x0084
_HT = {
    "client": 1, "caption": 2, "left": 10, "right": 11, "top": 12,
    "topleft": 13, "topright": 14, "bottom": 15, "bottomleft": 16,
    "bottomright": 17,
}


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
    state_changed = Signal(str, str)  # state ("live"/"connecting"/"error"), detail

    def __init__(self, url: str, poll_interval: float, stats: NetworkStats,
                 timeout: float = DEFAULT_FETCH_TIMEOUT):
        super().__init__()
        self._url = url
        self._poll_interval = poll_interval
        self._timeout = timeout
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
        self._emit_state("connecting", "Connecting...")

        while not self._stop_event.is_set():
            start = time.time()
            try:
                with urllib.request.urlopen(self._url, timeout=self._timeout) as resp:
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
                self._emit_state("error", f"Bad frame: {exc}")
                self._stop_event.wait(self._poll_interval)
                continue

            frame_id += 1
            self._stats.record_frame(len(data), frame.num_points, fetch_seconds)
            self._emit_state("live", f"Live - {frame.num_points:,} points")
            self.frame_ready.emit(frame)

            elapsed = time.time() - start
            if elapsed < self._poll_interval:
                self._stop_event.wait(self._poll_interval - elapsed)

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        if "refused" in text.lower():
            return "Connection refused - is the rover server running?"
        if "timed out" in text.lower() or isinstance(exc, TimeoutError):
            return "Timed out - check the IP and that you're on the same network"
        if "unreachable" in text.lower():
            return "Host unreachable - check the IP"
        return text[:80]


class DiscoveryWorker(QThread):
    """Scans the local network for point-cloud senders, off the GUI thread."""

    found = Signal(str, int)      # host, point count
    progress = Signal(str)
    done = Signal(int)            # number of senders found

    def __init__(self, port: int):
        super().__init__()
        self._port = port
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.progress.emit("Scanning local network...")
        results = discovery.scan(
            self._port,
            on_found=lambda host, n: self.found.emit(host, n),
            should_stop=self._stop_event.is_set,
        )
        self.done.emit(len(results))


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

class FrameRecorder:
    """Saves incoming frames to disk on an interval, for a fixed duration."""

    def __init__(self):
        self.active = False
        self.folder = ""
        self.interval = 1.0
        self.duration = 30.0
        self.saved = 0
        self.failed = 0
        self._started_at = 0.0
        self._last_saved_at = 0.0

    def start(self, folder: str, interval: float, duration: float) -> None:
        self.folder = folder
        self.interval = interval
        self.duration = duration
        self.saved = 0
        self.failed = 0
        self._started_at = time.time()
        # Zero means "save the first frame that arrives".
        self._last_saved_at = 0.0
        self.active = True

    def stop(self) -> None:
        self.active = False

    def elapsed(self) -> float:
        return time.time() - self._started_at if self.active else 0.0

    def expired(self) -> bool:
        return self.active and self.elapsed() >= self.duration

    def should_save(self) -> bool:
        if not self.active:
            return False
        return (time.time() - self._last_saved_at) >= self.interval

    def note_saved(self, ok: bool) -> None:
        self._last_saved_at = time.time()
        if ok:
            self.saved += 1
        else:
            self.failed += 1


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class BaseStation(QMainWindow):
    RESIZE_MARGIN = 6

    def __init__(self, host: str, port: int, poll_interval: float,
                 timeout: float = DEFAULT_FETCH_TIMEOUT):
        super().__init__()
        self.setWindowTitle("Survey Rig Base Station")
        self.setWindowIcon(icons.app_icon(theme.ACCENT))
        self.resize(1500, 920)
        self.setMinimumSize(900, 600)
        self._timeout = timeout

        # Frameless so the title bar can match the palette. On Windows the
        # native hit-test below keeps real resizing, snapping and
        # double-click-to-maximise working.
        self.setWindowFlag(Qt.FramelessWindowHint, True)

        self._stats = NetworkStats()
        self._recorder = FrameRecorder()
        self._worker: PollWorker | None = None
        self._scanner: DiscoveryWorker | None = None
        self._scan_hits = 0
        self._latest_frame: Frame | None = None
        self._cloud: pv.PolyData | None = None
        self._actor = None
        self._bounds_actor = None
        self._origin_actor = None
        self._temp_range = None
        self._point_size = 3
        self._bg_base = theme.VIEWPORT_BG_CENTER
        self._bg_pixels: np.ndarray | None = None
        self._bg_texture = None
        self._colormap = matplotlib.colormaps[THERMAL_COLORMAP]
        # Persistent buffers handed to VTK -- see _on_frame for why these
        # must be owned by the window rather than created per frame.
        self._xyz_buffer: np.ndarray | None = None
        self._color_buffer: np.ndarray | None = None

        central = QWidget()
        self.setCentralWidget(central)
        shell = QVBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # -- title bar ---------------------------------------------------
        self.title_bar = widgets.TitleBar("Survey Rig Base Station")
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self._toggle_maximized)
        self.title_bar.close_requested.connect(self.close)
        shell.addWidget(self.title_bar)

        body = QWidget()
        shell.addWidget(body, 1)
        root = QHBoxLayout(body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- 3D canvas ---------------------------------------------------
        self.plotter = QtInteractor(body)
        self._apply_viewport_background()
        self._add_orientation_axes()
        root.addWidget(self.plotter.interactor, stretch=1)

        # -- side panel --------------------------------------------------
        root.addWidget(self._build_panel(host, port, poll_interval))

        # Previously-used hosts, so a returning user can just pick one.
        # Re-apply any CLI host afterwards: populating the combo can move
        # the current index and overwrite the editable text.
        self._load_recent_hosts()
        self.host_combo.setCurrentText(host or "")

        self._show_origin(True)
        self._show_scale_grid(True)

        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(STATS_REFRESH_MS)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.capture_status.setText(""))

    # -- frameless window plumbing ---------------------------------------

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event):
        if event.type() == event.Type.WindowStateChange:
            self.title_bar.set_maximized(self.isMaximized())
        super().changeEvent(event)

    def nativeEvent(self, event_type, message):
        """Let Windows resize/snap a frameless window.

        Reporting the edges as frame hits (and the title bar as caption)
        hands drag-move, edge-resize, Aero Snap and double-click-maximise
        back to the OS, which behaves far better than reimplementing them.
        """
        if os.name != "nt" or event_type != b"windows_generic_MSG":
            return super().nativeEvent(event_type, message)
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
        except (TypeError, ValueError):
            return super().nativeEvent(event_type, message)

        if msg.message != _WM_NCHITTEST or self.isMaximized():
            return super().nativeEvent(event_type, message)

        # lParam packs a signed screen-space x/y pair, in physical pixels;
        # Qt geometry is in logical pixels, so scale before mapping.
        x = ctypes.c_short(msg.lParam & 0xFFFF).value
        y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
        ratio = self.devicePixelRatioF() or 1.0
        pos = self.mapFromGlobal(QPoint(round(x / ratio), round(y / ratio)))

        margin = self.RESIZE_MARGIN
        w, h = self.width(), self.height()
        left = pos.x() < margin
        right = pos.x() > w - margin
        top = pos.y() < margin
        bottom = pos.y() > h - margin

        if top and left:
            return True, _HT["topleft"]
        if top and right:
            return True, _HT["topright"]
        if bottom and left:
            return True, _HT["bottomleft"]
        if bottom and right:
            return True, _HT["bottomright"]
        if left:
            return True, _HT["left"]
        if right:
            return True, _HT["right"]
        if top:
            return True, _HT["top"]
        if bottom:
            return True, _HT["bottom"]

        # Title bar drags the window, except over its buttons.
        if pos.y() < self.title_bar.height():
            child = self.childAt(pos)
            if not isinstance(child, QPushButton):
                return True, _HT["caption"]

        return super().nativeEvent(event_type, message)

    # -- panel construction ---------------------------------------------

    def _build_panel(self, host: str, port: int, poll_interval: float) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SidePanel")

        scroll = QScrollArea(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(
            theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4)
        layout.setSpacing(theme.SPACE_3)

        title = QLabel("Base Station")
        title.setObjectName("TitleLabel")
        layout.addWidget(title)
        subtitle = QLabel("Live rover point cloud")
        subtitle.setObjectName("SubtitleLabel")
        layout.addWidget(subtitle)
        layout.addSpacing(theme.SPACE_1)

        layout.addWidget(self._connection_card(host, port, poll_interval))
        layout.addWidget(self._display_card())
        layout.addWidget(self._view_card())
        layout.addWidget(self._capture_card())
        layout.addWidget(self._recording_card())
        layout.addWidget(self._stats_card())
        layout.addStretch()

        scroll.setWidget(inner)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.addWidget(scroll)

        # Size the panel from what its controls actually need. Hard-coding a
        # width silently clips the widest card (and with horizontal scrolling
        # off, that content becomes unreachable).
        needed = inner.minimumSizeHint().width()
        scrollbar = scroll.verticalScrollBar().sizeHint().width()
        panel.setFixedWidth(
            max(PANEL_MIN_WIDTH,
                min(PANEL_MAX_WIDTH, needed + scrollbar + theme.SPACE_2)))
        return panel

    def _connection_card(self, host: str, port: int,
                          poll_interval: float) -> widgets.Card:
        card = widgets.Card("wifi", "Connection")

        self.host_combo = QComboBox()
        self.host_combo.setEditable(True)
        self.host_combo.lineEdit().setPlaceholderText("rover IP, e.g. 192.168.1.50")
        self.host_combo.setToolTip(
            "IP or hostname of the machine serving /latest.bin.\n"
            "Type one in, or press Scan to find senders on this network.\n"
            "Works with any sender -- WSL, a Pi on the rig, a rover on WiFi.")
        # Picking a discovered entry drops the "- N pts" label and leaves the
        # bare host in the editable field.
        self.host_combo.activated.connect(self._on_host_picked)
        if host:
            self.host_combo.setCurrentText(host)
        card.add(widgets.labeled_field("server", "Rover host", self.host_combo))

        self.scan_button = QPushButton("  Scan for senders")
        self.scan_button.setIcon(icons.icon("search", theme.TEXT, 15))
        self.scan_button.setToolTip(
            "Probes this machine, any WSL distro, and the local subnets for\n"
            "servers actually answering /latest.bin.")
        self.scan_button.clicked.connect(self._toggle_scan)
        card.add(self.scan_button)

        self.scan_label = QLabel("")
        self.scan_label.setObjectName("HintLabel")
        self.scan_label.setWordWrap(True)
        self.scan_label.setVisible(False)
        card.add(self.scan_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE_3)
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
        row.addWidget(widgets.labeled_field("hash", "Port", self.port_spin))
        row.addWidget(widgets.labeled_field("clock", "Interval", self.interval_spin))
        card.add_layout(row)

        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("PrimaryButton")
        self.connect_button.clicked.connect(self._toggle_connection)
        card.add(self.connect_button)

        self.status_pill = widgets.StatusPill()
        card.add(self.status_pill)
        return card

    def _display_card(self) -> widgets.Card:
        card = widgets.Card("sliders", "Display")

        size_row = QWidget()
        size_layout = QHBoxLayout(size_row)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(theme.SPACE_1 + 2)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap("dots", theme.TEXT_MUTED, 14))
        glyph.setFixedSize(14, 14)
        size_layout.addWidget(glyph)
        caption = QLabel("Point size")
        caption.setObjectName("FieldLabel")
        size_layout.addWidget(caption)
        size_layout.addStretch()
        self.size_value_label = QLabel("3")
        self.size_value_label.setObjectName("StatValue")
        size_layout.addWidget(self.size_value_label)
        card.add(size_row)

        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(1, 12)
        self.size_slider.setValue(self._point_size)
        self.size_slider.valueChanged.connect(self._on_point_size_changed)
        card.add(self.size_slider)

        self.color_combo = QComboBox()
        self.color_combo.addItems(["RGB (camera)", "Thermal"])
        self.color_combo.currentIndexChanged.connect(self._on_color_mode_changed)
        card.add(widgets.labeled_field("layers", "Color overlay", self.color_combo))

        self.temp_label = QLabel("Temp range: n/a")
        self.temp_label.setObjectName("HintLabel")
        self.temp_label.setWordWrap(True)
        card.add(self.temp_label)

        bg_row = QHBoxLayout()
        bg_row.setContentsMargins(0, 0, 0, 0)
        bg_row.setSpacing(theme.SPACE_2)
        self.bg_button = QPushButton("  Background colour")
        self.bg_button.setToolTip(
            "Pick the viewport colour. The vignette is rebuilt around it.")
        self.bg_button.clicked.connect(self._choose_background)
        bg_row.addWidget(self.bg_button, 1)
        reset_bg = QPushButton("Reset")
        reset_bg.setToolTip("Back to the default dark viewport")
        reset_bg.clicked.connect(self._reset_background)
        bg_row.addWidget(reset_bg)
        card.add_layout(bg_row)
        self._update_bg_swatch()
        return card

    def _view_card(self) -> widgets.Card:
        card = widgets.Card("eye", "View")

        self.axes_check = QCheckBox("Orientation axes")
        self.axes_check.setChecked(True)
        self.axes_check.toggled.connect(self._on_axes_toggled)
        card.add(self.axes_check)

        self.origin_check = QCheckBox("Origin marker")
        self.origin_check.setChecked(True)
        self.origin_check.toggled.connect(self._show_origin)
        card.add(self.origin_check)

        self.scale_check = QCheckBox("Scale grid (metres)")
        self.scale_check.setChecked(True)
        self.scale_check.setToolTip(
            "Graduated bounding box with tick labels in metres.\n"
            "Ticks re-scale automatically as you zoom.")
        self.scale_check.toggled.connect(self._show_scale_grid)
        card.add(self.scale_check)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE_2)
        reset_button = QPushButton("Reset view")
        reset_button.clicked.connect(self._reset_view)
        top_button = QPushButton("Top-down")
        top_button.clicked.connect(self._top_view)
        row.addWidget(reset_button)
        row.addWidget(top_button)
        card.add_layout(row)
        return card

    def _capture_card(self) -> widgets.Card:
        card = widgets.Card("camera", "Capture")

        self.save_cloud_button = QPushButton("  Save point cloud...")
        self.save_cloud_button.setIcon(icons.icon("download", theme.TEXT, 15))
        self.save_cloud_button.clicked.connect(self._save_pcd)
        card.add(self.save_cloud_button)

        self.save_png_button = QPushButton("  Save screenshot...")
        self.save_png_button.setIcon(icons.icon("image", theme.TEXT, 15))
        self.save_png_button.clicked.connect(self._save_screenshot)
        card.add(self.save_png_button)

        self.capture_status = QLabel("")
        self.capture_status.setObjectName("HintLabel")
        self.capture_status.setWordWrap(True)
        card.add(self.capture_status)
        return card

    def _recording_card(self) -> widgets.Card:
        card = widgets.Card("record", "Recording")

        hint = QLabel("Saves every incoming frame on an interval, for a set "
                       "length of time.")
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        card.add(hint)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE_3)
        self.rec_interval_spin = QDoubleSpinBox()
        self.rec_interval_spin.setRange(0.05, 60.0)
        self.rec_interval_spin.setSingleStep(0.25)
        self.rec_interval_spin.setDecimals(2)
        self.rec_interval_spin.setSuffix(" s")
        self.rec_interval_spin.setValue(1.0)
        self.rec_interval_spin.setToolTip("Time between saved frames")
        self.rec_duration_spin = QDoubleSpinBox()
        self.rec_duration_spin.setRange(1.0, 86400.0)
        self.rec_duration_spin.setSingleStep(10.0)
        self.rec_duration_spin.setDecimals(0)
        self.rec_duration_spin.setSuffix(" s")
        self.rec_duration_spin.setValue(30)
        self.rec_duration_spin.setToolTip("How long to keep recording")
        row.addWidget(widgets.labeled_field("clock", "Every",
                                             self.rec_interval_spin))
        row.addWidget(widgets.labeled_field("clock", "For",
                                             self.rec_duration_spin))
        card.add_layout(row)

        self.rec_folder_button = QPushButton("  Choose folder...")
        self.rec_folder_button.setIcon(icons.icon("folder", theme.TEXT, 15))
        self.rec_folder_button.clicked.connect(self._choose_record_folder)
        card.add(self.rec_folder_button)

        self.rec_folder_label = QLabel("No folder chosen")
        self.rec_folder_label.setObjectName("HintLabel")
        self.rec_folder_label.setWordWrap(True)
        card.add(self.rec_folder_label)

        self.record_button = QPushButton("  Start recording")
        self.record_button.setIcon(icons.icon("record", theme.TEXT, 15))
        self.record_button.clicked.connect(self._toggle_recording)
        card.add(self.record_button)

        self.record_pill = widgets.StatusPill()
        self.record_pill.set_state("idle", "Not recording")
        card.add(self.record_pill)
        return card

    def _stats_card(self) -> widgets.Card:
        card = widgets.Card("activity", "Live stats")
        self.stat_rate = self._stat_row(card, "Data rate")
        self.stat_points_sec = self._stat_row(card, "Points / sec")
        self.stat_count = self._stat_row(card, "Points in frame")
        self.stat_fetch = self._stat_row(card, "Fetch time")
        self.stat_ok = self._stat_row(card, "Frames OK")
        self.stat_failed = self._stat_row(card, "Frames failed")
        return card

    def _stat_row(self, card: widgets.Card, caption: str) -> QLabel:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_2)
        cap = QLabel(caption)
        cap.setObjectName("StatCaption")
        value = QLabel("-")
        value.setObjectName("StatValue")
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(cap)
        layout.addStretch()
        layout.addWidget(value)
        card.add(row)
        return value

    # -- discovery ------------------------------------------------------

    def _on_host_picked(self, index: int) -> None:
        host = self.host_combo.itemData(index)
        if host:
            self.host_combo.setCurrentText(host)

    def _toggle_scan(self) -> None:
        if self._scanner is not None:
            self._scanner.stop()
            self._set_scan_hint("Scan cancelled.")
            self._finish_scan()
            return

        self._scan_hits = 0
        self._scanner = DiscoveryWorker(self.port_spin.value())
        self._scanner.found.connect(self._on_sender_found)
        self._scanner.progress.connect(self._set_scan_hint)
        self._scanner.done.connect(self._on_scan_done)
        self._scanner.start()
        self.scan_button.setText("  Stop scan")
        self.scan_button.setIcon(icons.icon("stop", theme.TEXT, 15))

    def _set_scan_hint(self, text: str) -> None:
        self.scan_label.setText(text)
        self.scan_label.setVisible(bool(text))

    def _on_sender_found(self, host: str, num_points: int) -> None:
        self._scan_hits += 1
        label = f"{host}  -  {num_points:,} pts"
        for i in range(self.host_combo.count()):
            if self.host_combo.itemData(i) == host:
                self.host_combo.setItemText(i, label)
                break
        else:
            self.host_combo.insertItem(0, label, host)
        # First hit becomes the selection, so Connect works straight away.
        if self._scan_hits == 1:
            self.host_combo.setCurrentText(host)

    def _on_scan_done(self, count: int) -> None:
        if count:
            self._set_scan_hint(
                f"Found {count} sender{'s' if count != 1 else ''} - "
                "pick one above, then Connect.")
        else:
            self._set_scan_hint(
                f"No senders answering on port {self.port_spin.value()}. "
                "Check the rover server is running and on this network.")
        self._finish_scan()

    def _finish_scan(self) -> None:
        if self._scanner is not None:
            self._scanner.wait(2000)
            self._scanner = None
        self.scan_button.setText("  Scan for senders")
        self.scan_button.setIcon(icons.icon("search", theme.TEXT, 15))

    # -- recent hosts ----------------------------------------------------

    def _load_recent_hosts(self) -> None:
        try:
            with open(RECENT_HOSTS_FILE, encoding="utf-8") as handle:
                hosts = json.load(handle)
        except (OSError, ValueError):
            return
        for host in hosts:
            if isinstance(host, str) and host:
                self.host_combo.addItem(host, host)

    def _remember_host(self, host: str) -> None:
        hosts = [host]
        for i in range(self.host_combo.count()):
            existing = self.host_combo.itemData(i)
            if existing and existing != host:
                hosts.append(existing)
        hosts = hosts[:MAX_RECENT_HOSTS]
        try:
            with open(RECENT_HOSTS_FILE, "w", encoding="utf-8") as handle:
                json.dump(hosts, handle)
        except OSError:
            pass  # a non-writable folder shouldn't break connecting

    # -- connection -----------------------------------------------------

    def _toggle_connection(self) -> None:
        if self._worker is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        host = self.host_combo.currentText().strip()
        if not host:
            self.status_pill.set_state(
                "error", "Enter the rover's IP, or press Scan to find one.")
            return

        url = f"http://{host}:{self.port_spin.value()}/latest.bin"
        self._stats.reset()
        self._remember_host(host)

        self._worker = PollWorker(url, self.interval_spin.value(), self._stats,
                                   timeout=self._timeout)
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.state_changed.connect(self._on_state_changed)
        self._worker.start()

        self.connect_button.setText("Disconnect")
        self.connect_button.setObjectName("")
        self.status_pill.set_state("connecting", "Connecting...")
        for widget in (self.host_combo, self.port_spin, self.interval_spin,
                        self.scan_button):
            widget.setEnabled(False)
        self._restyle(self.connect_button)

    def _disconnect(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None
        if self._recorder.active:
            self._stop_recording("Recording stopped - disconnected.")
        self.connect_button.setText("Connect")
        self.connect_button.setObjectName("PrimaryButton")
        self.status_pill.set_state("idle", "Not connected")
        for widget in (self.host_combo, self.port_spin, self.interval_spin,
                        self.scan_button):
            widget.setEnabled(True)
        self._restyle(self.connect_button)

    def _restyle(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _on_state_changed(self, state: str, detail: str) -> None:
        self.status_pill.set_state(state, detail)

    # -- rendering -------------------------------------------------------

    def _choose_background(self) -> None:
        current = QColor.fromRgbF(*self._bg_base)
        chosen = QColorDialog.getColor(
            current, self, "Viewport background colour")
        if not chosen.isValid():
            return
        self._bg_base = (chosen.redF(), chosen.greenF(), chosen.blueF())
        self._apply_viewport_background()
        self._update_bg_swatch()
        self.plotter.render()

    def _reset_background(self) -> None:
        self._bg_base = theme.VIEWPORT_BG_CENTER
        self._apply_viewport_background()
        self._update_bg_swatch()
        self.plotter.render()

    def _update_bg_swatch(self) -> None:
        """Show the current colour as a rounded chip on the button."""
        colour = QColor.fromRgbF(*self._bg_base)
        pm = QPixmap(16, 16)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(colour)
        painter.setPen(QColor(255, 255, 255, 60))
        painter.drawRoundedRect(1, 1, 14, 14, 4, 4)
        painter.end()
        self.bg_button.setIcon(pm)

    def _apply_viewport_background(self) -> None:
        """Soft radial vignette: lighter at the centre, darker at the edges."""
        size = 256
        yy, xx = np.mgrid[0:size, 0:size]
        centre = (size - 1) / 2.0
        radius = np.sqrt((xx - centre) ** 2 + (yy - centre) ** 2)
        t = np.clip(radius / (centre * 1.35), 0.0, 1.0)
        t = (t * t * (3 - 2 * t))[..., None]        # smoothstep falloff
        inner = np.array(self._bg_base, dtype=float)
        outer = inner * 0.46                        # darker toward the edges
        # Held on the instance: pv.Texture wraps the array by reference, so a
        # local would be freed and VTK would render garbage from stale memory.
        self._bg_pixels = np.clip(
            (inner * (1 - t) + outer * t) * 255, 0, 255).astype(np.uint8)

        try:
            self._bg_texture = pv.Texture(self._bg_pixels)
            renderer = self.plotter.renderer
            renderer.SetBackgroundTexture(self._bg_texture)
            renderer.SetTexturedBackground(True)
        except Exception:
            # Fall back to a flat gradient if textured backgrounds aren't
            # available on this driver.
            self.plotter.set_background(tuple(np.array(self._bg_base) * 0.46),
                                         top=self._bg_base)

    def _add_orientation_axes(self) -> None:
        self.plotter.add_axes(
            interactive=False,
            x_color=theme.AXIS_X, y_color=theme.AXIS_Y, z_color=theme.AXIS_Z,
        )

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
        self._maybe_record(frame)

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
            self._add_orientation_axes()
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
                labels_off=True, line_width=3,
                x_color=theme.AXIS_X, y_color=theme.AXIS_Y,
                z_color=theme.AXIS_Z,
            )
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
                color=theme.GRID_COLOR,
                fmt="%.2f",
            )
            self._fade_grid(self._bounds_actor)
        self.plotter.render()

    @staticmethod
    def _fade_grid(actor) -> None:
        """Knock the gridlines back so they read as a reference, not content."""
        if actor is None:
            return
        for axis in ("X", "Y", "Z"):
            for kind in ("AxesLinesProperty", "AxesGridlinesProperty",
                          "AxesInnerGridlinesProperty"):
                getter = getattr(actor, f"Get{axis}{kind}", None)
                if getter is None:
                    continue
                try:
                    getter().SetOpacity(theme.GRID_OPACITY)
                except AttributeError:
                    pass

    def _reset_view(self) -> None:
        self.plotter.reset_camera()
        self.plotter.render()

    def _top_view(self) -> None:
        self.plotter.view_xy()
        self.plotter.render()

    # -- capture ---------------------------------------------------------

    def _default_capture_dir(self) -> str:
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        return os.path.abspath(CAPTURES_DIR)

    def _save_pcd(self) -> None:
        if self._latest_frame is None or self._cloud is None:
            self._set_capture_status("No frame received yet - nothing to save.")
            return

        suggested = os.path.join(
            self._default_capture_dir(),
            f"frame_{datetime.now():%Y%m%d_%H%M%S}.pcd")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save point cloud", suggested,
            "Point cloud (*.pcd);;PLY (*.ply);;All files (*)")
        if not path:
            return

        if self._write_cloud(path, self._latest_frame, self._color_buffer):
            self._set_capture_status(
                f"Saved {os.path.basename(path)} "
                f"({self._latest_frame.num_points:,} pts)")
        else:
            self._set_capture_status(f"Could not write {os.path.basename(path)}")

    @staticmethod
    def _write_cloud(path: str, frame: Frame, colors: np.ndarray) -> bool:
        import open3d as o3d

        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(frame.xyz.astype(np.float64))
        if colors is not None and len(colors) == frame.num_points:
            pcd.colors = o3d.utility.Vector3dVector(
                np.asarray(colors, dtype=np.float64) / 255.0)
        try:
            return bool(o3d.io.write_point_cloud(path, pcd))
        except (OSError, RuntimeError):
            return False

    def _save_screenshot(self) -> None:
        suggested = os.path.join(
            self._default_capture_dir(),
            f"view_{datetime.now():%Y%m%d_%H%M%S}.png")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save screenshot", suggested,
            "PNG image (*.png);;JPEG image (*.jpg);;All files (*)")
        if not path:
            return
        try:
            self.plotter.screenshot(path)
        except (OSError, RuntimeError):
            self._set_capture_status(f"Could not write {os.path.basename(path)}")
            return
        self._set_capture_status(f"Saved {os.path.basename(path)}")

    def _set_capture_status(self, text: str) -> None:
        self.capture_status.setText(text)
        self._status_timer.start(5000)

    # -- recording -------------------------------------------------------

    def _choose_record_folder(self) -> None:
        start = self._recorder.folder or self._default_capture_dir()
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder for recorded frames", start)
        if folder:
            self._recorder.folder = folder
            self.rec_folder_label.setText(folder)

    def _toggle_recording(self) -> None:
        if self._recorder.active:
            self._stop_recording("Recording stopped.")
            return

        folder = self._recorder.folder
        if not folder:
            self._choose_record_folder()
            folder = self._recorder.folder
            if not folder:
                self.record_pill.set_state("error", "Choose a folder first.")
                return

        if self._worker is None:
            self.record_pill.set_state(
                "error", "Connect to a rover before recording.")
            return

        self._recorder.start(folder, self.rec_interval_spin.value(),
                              self.rec_duration_spin.value())
        self.record_button.setText("  Stop recording")
        self.record_button.setIcon(icons.icon("stop", theme.TEXT, 15))
        for widget in (self.rec_interval_spin, self.rec_duration_spin,
                        self.rec_folder_button):
            widget.setEnabled(False)
        self.record_pill.set_state("connecting", "Recording... 0 frames")

    def _stop_recording(self, message: str) -> None:
        saved, failed = self._recorder.saved, self._recorder.failed
        self._recorder.stop()
        self.record_button.setText("  Start recording")
        self.record_button.setIcon(icons.icon("record", theme.TEXT, 15))
        for widget in (self.rec_interval_spin, self.rec_duration_spin,
                        self.rec_folder_button):
            widget.setEnabled(True)
        detail = f"{message} {saved} frame{'s' if saved != 1 else ''} saved"
        if failed:
            detail += f", {failed} failed"
        # Green is reserved for a genuinely completed action.
        self.record_pill.set_state("live" if saved and not failed else "idle",
                                    detail)

    def _maybe_record(self, frame: Frame) -> None:
        if not self._recorder.active:
            return
        if self._recorder.expired():
            self._stop_recording("Recording complete.")
            return
        if not self._recorder.should_save():
            return

        name = (f"frame_{self._recorder.saved:05d}_"
                f"{datetime.now():%Y%m%d_%H%M%S}.pcd")
        path = os.path.join(self._recorder.folder, name)
        ok = self._write_cloud(path, frame, self._color_buffer)
        self._recorder.note_saved(ok)

        remaining = max(0.0, self._recorder.duration - self._recorder.elapsed())
        self.record_pill.set_state(
            "connecting",
            f"Recording... {self._recorder.saved} frames, "
            f"{remaining:.0f}s left")

    # -- stats -----------------------------------------------------------

    def _refresh_stats(self) -> None:
        s = self._stats.snapshot()
        self.stat_rate.setText(f"{s['kbps']:.1f} KB/s")
        self.stat_points_sec.setText(f"{s['pts_per_sec']:,.0f}")
        self.stat_count.setText(f"{s['point_count']:,}")
        self.stat_fetch.setText(
            f"{s['fetch_seconds'] * 1000:.0f} ms" if s["fetch_seconds"] else "-")
        self.stat_ok.setText(str(s["frames_received"]))
        self.stat_failed.setText(str(s["frames_failed"]))

        # A recording can outlive its duration if frames stop arriving.
        if self._recorder.expired():
            self._stop_recording("Recording complete.")

    # -- lifecycle -------------------------------------------------------

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(3000)
        if self._scanner is not None:
            self._scanner.stop()
            self._scanner.wait(3000)
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
    parser.add_argument("--timeout", type=float, default=DEFAULT_FETCH_TIMEOUT,
                         help="HTTP request timeout in seconds; raise it for "
                              "large frames over a slow WiFi link")
    parser.add_argument("--connect", action="store_true",
                         help="connect immediately on startup")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Survey Rig Base Station")
    app.setWindowIcon(icons.app_icon(theme.ACCENT))
    app.setStyleSheet(theme.get_stylesheet())

    window = BaseStation(args.rover_host, args.rover_port, args.poll_interval,
                          timeout=args.timeout)
    window.showMaximized()
    if args.connect and args.rover_host:
        window._connect()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
