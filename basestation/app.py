"""Base station GUI for the mine/landfill survey rig.

Polls the rover's HTTP point-cloud endpoint (xyz + rgb + thermal per point)
and renders the latest frame live. Built on Qt (PySide6) + PyVista/VTK.

Layout: the 3D viewport is the hero. A floating dock over it switches the
active tool (Navigate / Crop / Measure); a three-tab inspector on the right
holds everything else (Rover, Controls, Scene).

See protocol.py for the wire format and README.md for usage.
"""

import ctypes
import ctypes.wintypes
import faulthandler
import json
import os
import sys
import time
from datetime import datetime

import matplotlib
import numpy as np
import pyvista as pv
from PySide6.QtCore import QEvent, QPoint, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QColorDialog, QLineEdit, QMenu, QMessageBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QScrollArea, QSlider,
    QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)
from pyvistaqt import QtInteractor
from vtkmodules.vtkRenderingCore import vtkPointPicker

from basestation import paths
from basestation.core import control
from basestation.ui import icons
from basestation.core import processing
from basestation.ui import theme
from basestation.ui import widgets
from basestation.ui.diagnostics_dialog import DiagnosticsDialog
from basestation.net.workers import (
    ActionWorker, ControlWorker, DEFAULT_FETCH_TIMEOUT, DiscoveryWorker, FrameRecorder,
    NetworkStats, PollWorker,
)
from basestation.core.protocol import Frame

RECENT_HOSTS_FILE = paths.data_path("recent_hosts.json")
MAX_RECENT_HOSTS = 8

CAPTURES_DIR = paths.data_path("captures")
PANEL_WIDTH = 380
INVALID_THERMAL_COLOR = (128, 128, 128)  # gray for points with no thermal reading
THERMAL_COLORMAP = "inferno"
STATS_REFRESH_MS = 250
CLICK_SLOP_PX = 5                         # press/release travel still counted as a click

# Win32 hit-test codes, used to keep native resize/snap on a frameless window.
_WM_NCHITTEST = 0x0084
_HT = {
    "client": 1, "caption": 2, "left": 10, "right": 11, "top": 12,
    "topleft": 13, "topright": 14, "bottom": 15, "bottomleft": 16,
    "bottomright": 17,
}

TOOL_TITLES = {"navigate": "Navigate", "crop": "Crop", "measure": "Measure"}


def _spin(lo, hi, value, step=0.1, decimals=2, suffix=" m", width=104) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setSingleStep(step)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    box.setValue(value)
    box.setFixedWidth(width)
    box.setKeyboardTracking(False)
    return box


class BaseStation(QMainWindow):
    RESIZE_MARGIN = 6

    def __init__(self, host: str, port: int, poll_interval: float,
                 timeout: float = DEFAULT_FETCH_TIMEOUT):
        super().__init__()
        self.setWindowTitle("Survey Rig Base Station")
        self.setWindowIcon(icons.app_icon(theme.ACCENT))
        self.resize(1500, 920)
        self.setMinimumSize(1000, 640)
        self._timeout = timeout

        # Frameless so the title bar can match the palette. On Windows the
        # native hit-test below keeps real resizing, snapping and
        # double-click-to-maximise working.
        self.setWindowFlag(Qt.FramelessWindowHint, True)

        self._stats = NetworkStats()
        self._recorder = FrameRecorder()
        self._worker = None
        self._scanner = None
        self._diag = None
        self._scan_hits = 0
        self._stream_frame: Frame | None = None   # latest frame off the wire, even while paused
        self._latest_frame: Frame | None = None   # latest frame the view is allowed to show
        self._view: Frame | None = None           # _latest_frame after the processing pipeline
        self._pipeline = processing.Pipeline()
        self._cloud: pv.PolyData | None = None
        self._actor = None
        self._bounds_actor = None
        self._origin_actor = None
        self._temp_range = None
        self._camera_fitted = False
        self._point_size = 3
        self._bg_base = theme.VIEWPORT_BG_CENTER
        self._bg_custom = False     # user picked their own colour
        self._bg_pixels: np.ndarray | None = None
        self._bg_texture = None
        self._colormap = matplotlib.colormaps[THERMAL_COLORMAP]
        # Persistent buffers handed to VTK -- see _render_view for why these
        # must be owned by the window rather than created per frame.
        self._xyz_buffer: np.ndarray | None = None
        self._color_buffer: np.ndarray | None = None

        # Tool state
        self._tool = "navigate"
        self._view_paused = False
        self._paused_by_measure = False
        self._crop_widget = None
        self._crop_seeded = False
        self._measure_pts: list = []              # [(xyz ndarray, temp_c float)]
        self._measure_refs: list = []             # keep arrays alive for VTK
        self._press_pos = None
        self._observer_tags: list = []

        # Sensor-PC power / control
        self._settings = QSettings("SurveyRig", "BaseStation")
        self._control = None
        self._action_worker = None
        self._control_status = None
        self._pending = None            # (action, deadline) while a command is in flight
        self._poweroff_notice = False   # we shut the Pi down; it needs a power-cycle

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
        self.power_button = widgets.PowerButton()
        self.power_button.clicked.connect(self._on_power_clicked)
        self.title_bar.add_widget(self.power_button)
        self.theme_toggle = widgets.IconToggleGroup([
            ("light", "sun", "Light appearance"),
            ("dark", "moon", "Dark appearance"),
            ("system", "monitor", "Match system appearance"),
        ])
        self.theme_toggle.set_key(theme.mode())
        self.theme_toggle.changed.connect(self._on_theme_chosen)
        self.title_bar.add_widget(self.theme_toggle)
        self.title_bar.add_action("Diagnostics", "pulse", self._open_diagnostics)
        shell.addWidget(self.title_bar)

        body = QWidget()
        shell.addWidget(body, 1)
        root = QHBoxLayout(body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- canvas column: toolbar strip above the 3D view ----------------
        # The controls live in a real strip, not floating over the render
        # surface: the VTK view is a native window, and any widget laid over
        # one becomes an opaque rectangle (black corners behind rounded chrome).
        canvas_col = QWidget()
        col = QVBoxLayout(canvas_col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        self.toolbar = QFrame()
        self.toolbar.setObjectName("CanvasBar")
        self.toolbar.setFixedHeight(56)
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(theme.SPACE_4, theme.SPACE_2, theme.SPACE_4, theme.SPACE_2)
        bar.setSpacing(theme.SPACE_3)

        self.dock = widgets.Dock()
        self.dock.add_tool("navigate", "orbit", "Navigate",
                            "Orbit, pan and zoom the cloud")
        self.dock.add_tool("crop", "crop", "Crop",
                            "Drag a box to keep or remove a region")
        self.dock.add_tool("measure", "ruler", "Measure",
                            "Click points to measure distance, path and area")
        self.dock.tool_changed.connect(self._on_tool_changed)
        bar.addWidget(self.dock)

        self.home_button = QPushButton()
        self.home_button.setObjectName("BarButton")
        self.home_button.setFixedWidth(44)
        self.home_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)   # as tall as the dock
        self.home_button.setCursor(Qt.PointingHandCursor)
        self.home_button.setToolTip("Home view  (Home key)\nBack to the default angle, fitted to the cloud")
        self.home_button.setAccessibleName("Home view")
        theme.bind_icon(self.home_button, "home", "text", 18, 1.9)
        self.home_button.clicked.connect(self._home_view)
        bar.addWidget(self.home_button)

        self.readout = widgets.Readout()
        bar.addWidget(self.readout, 1)

        self.canvas_overlay = widgets.CanvasOverlay()
        self.canvas_overlay.pause_toggled.connect(self._on_pause_toggled)
        bar.addWidget(self.canvas_overlay)
        col.addWidget(self.toolbar)

        self.plotter = QtInteractor(canvas_col)
        self._apply_viewport_background()
        self._add_orientation_axes()
        col.addWidget(self.plotter.interactor, 1)
        root.addWidget(canvas_col, 1)

        # -- inspector ---------------------------------------------------
        root.addWidget(self._build_panel(host, port, poll_interval))

        # Re-apply any CLI host after populating recent hosts: populating the
        # combo can move the current index and overwrite the editable text.
        self._load_recent_hosts()
        self.host_combo.setCurrentText(host or "")

        self._show_origin(True)
        self._show_scale_grid(True)
        self._install_pick_observers()
        self._start_control()

        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_scheme_changed)

        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self._undo_measure_point)
        QShortcut(QKeySequence("Home"), self, activated=self._home_view)
        QShortcut(QKeySequence("Esc"), self, activated=lambda: self.dock.select("navigate"))

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
        left, right = pos.x() < margin, pos.x() > w - margin
        top, bottom = pos.y() < margin, pos.y() > h - margin

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

    # ======================================================================
    # Appearance
    # ======================================================================

    def _on_theme_chosen(self, mode: str) -> None:
        theme.set_mode(mode)
        QSettings("SurveyRig", "BaseStation").setValue("theme", mode)
        self._restyle_everything()

    def _on_system_scheme_changed(self, *_args) -> None:
        if theme.mode() == "system":
            theme.set_mode("system")
            self._restyle_everything()

    def _restyle_everything(self) -> None:
        """Re-skin the live window: stylesheet, icons, and the 3D scene."""
        QApplication.instance().setStyleSheet(theme.get_stylesheet())
        theme.refresh_bindings()
        self.setWindowIcon(icons.app_icon(theme.ACCENT))
        if not self._bg_custom:
            self._bg_base = theme.VIEWPORT_BG_CENTER
        self._apply_viewport_background()
        self._update_bg_swatch()
        if self.grid_switch.isChecked():
            self._show_scale_grid(True)
        self._update_measure()                      # redraws in the new accent
        self._restyle(self.connect_button)
        self.plotter.render()

    # ======================================================================
    # Inspector
    # ======================================================================

    def _build_panel(self, host: str, port: int, poll_interval: float) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SidePanel")
        panel.setFixedWidth(PANEL_WIDTH)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, 0)
        outer.setSpacing(theme.SPACE_3)

        self.tabs = widgets.Segmented(["Rover", "Controls", "Scene"],
                                       ["server", "sliders", "layers"])
        self.tabs.changed.connect(self._show_tab)
        outer.addWidget(self.tabs)

        self.page_title = QLabel("Rover")
        self.page_title.setObjectName("LargeTitle")
        outer.addWidget(self.page_title)

        self.pages = QStackedWidget()
        self._tab_titles = ["Rover", "Controls", "Scene"]
        for builder in (lambda: self._rover_page(host, port, poll_interval),
                        self._controls_page, self._scene_page):
            self.pages.addWidget(self._scrolling(builder()))
        outer.addWidget(self.pages, 1)
        return panel

    def _scrolling(self, content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, theme.SPACE_1, theme.SPACE_4)
        layout.setSpacing(theme.SPACE_5 - 4)
        return page, layout

    def _show_tab(self, index: int) -> None:
        self.tabs.set_index(index)
        self.pages.setCurrentIndex(index)
        self.page_title.setText(self._tab_titles[index])

    # -- Rover ------------------------------------------------------------

    def _rover_page(self, host: str, port: int, poll_interval: float) -> QWidget:
        page, layout = self._page()
        layout.addWidget(self._connection_section(host, port, poll_interval))
        layout.addWidget(self._power_section())
        layout.addWidget(self._stats_section())
        layout.addWidget(self._recording_section())
        layout.addStretch()
        return page

    def _connection_section(self, host: str, port: int, poll_interval: float) -> QWidget:
        sec = widgets.Section("Connection", "wifi")

        self.host_combo = QComboBox()
        self.host_combo.setEditable(True)
        self.host_combo.lineEdit().setPlaceholderText("rover IP, e.g. 192.168.1.50")
        self.host_combo.setToolTip(
            "IP or hostname of the machine serving /latest.bin.\n"
            "Type one in, or press Scan to find senders on this network.")
        self.host_combo.activated.connect(self._on_host_picked)
        if host:
            self.host_combo.setCurrentText(host)
        sec.add(widgets.stacked("Rover host", self.host_combo))

        self.scan_button = QPushButton("  Scan for senders")
        theme.bind_icon(self.scan_button, "search", "text", 15)
        self.scan_button.setToolTip(
            "Probes this machine, any WSL distro, and the local subnets for\n"
            "servers actually answering /latest.bin.")
        self.scan_button.clicked.connect(self._toggle_scan)
        self.scan_label = widgets.hint("")
        self.scan_label.setVisible(False)
        scan_box = QWidget()
        sv = QVBoxLayout(scan_box)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(theme.SPACE_2)
        sv.addWidget(self.scan_button)
        sv.addWidget(self.scan_label)
        sec.add(scan_box)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(port)
        self.port_spin.setFixedWidth(104)
        sec.add(widgets.row("Port", self.port_spin)[0])

        self.interval_spin = _spin(0.02, 5.0, poll_interval, 0.05, 2, " s")
        self.interval_spin.setToolTip("Minimum time between requests")
        sec.add(widgets.row("Poll interval", self.interval_spin)[0])

        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("PrimaryButton")
        self.connect_button.clicked.connect(self._toggle_connection)
        sec.add(self.connect_button)

        self.status_pill = widgets.StatusPill()
        sec.add(self.status_pill)
        return sec

    def _stats_section(self) -> QWidget:
        sec = widgets.Section("Live stats", "activity")
        rows = {}
        for key, caption in (("rate", "Data rate"), ("pts_sec", "Points / sec"),
                              ("count", "Points in frame"), ("fetch", "Fetch time"),
                              ("ok", "Frames OK"), ("failed", "Frames failed")):
            w, value = widgets.row(caption)
            sec.add(w)
            rows[key] = value
        self.stat_rate, self.stat_points_sec = rows["rate"], rows["pts_sec"]
        self.stat_count, self.stat_fetch = rows["count"], rows["fetch"]
        self.stat_ok, self.stat_failed = rows["ok"], rows["failed"]
        return sec

    def _recording_section(self) -> QWidget:
        sec = widgets.Section(
            "Recording", "record",
            footer="Saves the processed view (what you see) on an interval, "
                   "for a set length of time.")
        self.rec_interval_spin = _spin(0.05, 60.0, 1.0, 0.25, 2, " s")
        self.rec_duration_spin = _spin(1.0, 86400.0, 30, 10.0, 0, " s")
        sec.add(widgets.row("Save every", self.rec_interval_spin)[0])
        sec.add(widgets.row("For", self.rec_duration_spin)[0])

        self.rec_folder_button = QPushButton("  Choose folder...")
        theme.bind_icon(self.rec_folder_button, "folder", "text", 15)
        self.rec_folder_button.clicked.connect(self._choose_record_folder)
        sec.add(self.rec_folder_button)
        self.rec_folder_label = widgets.hint("No folder chosen")
        sec.add(self.rec_folder_label)

        self.record_button = QPushButton("  Start recording")
        theme.bind_icon(self.record_button, "record", "danger", 15)
        self.record_button.clicked.connect(self._toggle_recording)
        sec.add(self.record_button)

        self.record_pill = widgets.StatusPill()
        self.record_pill.set_state("idle", "Not recording")
        sec.add(self.record_pill)
        return sec

    # -- Controls ---------------------------------------------------------

    def _controls_page(self) -> QWidget:
        page, layout = self._page()

        self.tool_seg = widgets.Segmented(
            ["Navigate", "Crop", "Measure"], ["orbit", "crop", "ruler"])
        self.tool_seg.changed.connect(
            lambda i: self.dock.select(("navigate", "crop", "measure")[i]))
        layout.addWidget(self.tool_seg)

        # Only the active tool's panel is visible. (A QStackedWidget would
        # reserve the height of the tallest page and leave a gap.)
        self.tool_panels = [self._navigate_panel(), self._crop_panel(), self._measure_panel()]
        holder = QWidget()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(0, 0, 0, 0)
        for i, panel in enumerate(self.tool_panels):
            panel.setVisible(i == 0)
            hv.addWidget(panel)
        layout.addWidget(holder)

        layout.addWidget(self._process_section())
        layout.addStretch()
        return page

    def _navigate_panel(self) -> QWidget:
        sec = widgets.Section(
            "Navigate", "orbit",
            footer="Left-drag orbits, middle-drag pans, scroll or right-drag zooms.")
        reset = QPushButton("  Reset view")
        theme.bind_icon(reset, "home", "text", 15)
        reset.clicked.connect(self._reset_view)
        top = QPushButton("  Top-down")
        theme.bind_icon(top, "grid", "text", 15)
        top.clicked.connect(self._top_view)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(theme.SPACE_2)
        h.addWidget(reset)
        h.addWidget(top)
        sec.add(row)
        return sec

    def _crop_panel(self) -> QWidget:
        sec = widgets.Section(
            "Crop", "crop",
            footer="Drag the box's faces in the view, or type exact limits. "
                   "Applies to every incoming frame.")
        self.crop_switch = widgets.Switch(False)
        self.crop_switch.toggled.connect(self._on_crop_toggled)
        sec.add(widgets.row("Crop to box", self.crop_switch)[0])

        self.crop_mode = widgets.Segmented(["Keep inside", "Remove inside"])
        self.crop_mode.changed.connect(self._on_crop_mode)
        sec.add(self.crop_mode)

        self._crop_spins = []
        for axis, lo_name in (("X", 0), ("Y", 2), ("Z", 4)):
            lo = _spin(-1000, 1000, -1.0, 0.1, 2, " m", 116)
            hi = _spin(-1000, 1000, 1.0, 0.1, 2, " m", 116)
            lo.valueChanged.connect(self._on_crop_spin)
            hi.valueChanged.connect(self._on_crop_spin)
            self._crop_spins += [lo, hi]
            pair = QWidget()
            h = QHBoxLayout(pair)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(theme.SPACE_2)
            h.addWidget(lo)
            h.addWidget(hi)
            sec.add(widgets.row(axis, pair)[0])

        fit = QPushButton("  Fit box to cloud")
        theme.bind_icon(fit, "target", "text", 15)
        fit.clicked.connect(self._fit_crop_to_cloud)
        sec.add(fit)
        return sec

    def _measure_panel(self) -> QWidget:
        sec = widgets.Section(
            "Measure", "ruler",
            footer="Click points on the cloud. The view pauses while you "
                   "measure so points stay put. Ctrl+Z removes the last point.")
        self.measure_mode = widgets.Segmented(["Distance", "Path && area"])
        self.measure_mode.changed.connect(lambda _i: self._on_measure_mode())
        sec.add(self.measure_mode)

        self.measure_text = QLabel("No points yet.")
        self.measure_text.setObjectName("StatValue")
        self.measure_text.setWordWrap(True)
        self.measure_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.measure_text.setStyleSheet(
            "font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px;")
        sec.add(self.measure_text)

        self.measure_undo = QPushButton("  Undo point")
        theme.bind_icon(self.measure_undo, "undo", "text", 15)
        self.measure_undo.clicked.connect(self._undo_measure_point)
        self.measure_clear = QPushButton("  Clear")
        self.measure_clear.setObjectName("DestructiveButton")
        theme.bind_icon(self.measure_clear, "trash", "danger", 15)
        self.measure_clear.clicked.connect(self._clear_measure)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(theme.SPACE_2)
        h.addWidget(self.measure_undo)
        h.addWidget(self.measure_clear)
        sec.add(row)
        return sec

    def _filter_block(self, title: str, switch: widgets.Switch, params: list) -> QWidget:
        """Switch row with parameter rows that appear only while it is on."""
        block = QWidget()
        v = QVBoxLayout(block)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(theme.SPACE_2)
        v.addWidget(widgets.row(title, switch)[0])
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(theme.SPACE_2)
        for p in params:
            bv.addWidget(p)
        body.setVisible(switch.isChecked())
        switch.toggled.connect(body.setVisible)
        v.addWidget(body)
        return block

    def _process_section(self) -> QWidget:
        sec = widgets.Section(
            "Process", "wand",
            footer="Filters run on every incoming frame, cheapest first. "
                   "Saving and recording use the processed result.")

        # Range
        self.range_switch = widgets.Switch(False)
        self.range_min = _spin(0, 500, 0.0, 0.5)
        self.range_max = _spin(0.1, 500, 50.0, 0.5)
        sec.add(self._filter_block("Distance range", self.range_switch, [
            widgets.row("Nearest", self.range_min)[0],
            widgets.row("Farthest", self.range_max)[0]]))

        # Thermal band
        self.thermal_switch = widgets.Switch(False)
        self.thermal_min = _spin(-100, 1500, 20.0, 1.0, 1, " °C")
        self.thermal_max = _spin(-100, 1500, 80.0, 1.0, 1, " °C")
        self.thermal_drop = widgets.Switch(False)
        sec.add(self._filter_block("Temperature band", self.thermal_switch, [
            widgets.row("From", self.thermal_min)[0],
            widgets.row("To", self.thermal_max)[0],
            widgets.row("Drop unknown", self.thermal_drop)[0]]))

        # Isolated points
        self.isolated_switch = widgets.Switch(False)
        self.isolated_radius = _spin(0.02, 5.0, 0.20, 0.05)
        self.isolated_neighbors = QSpinBox()
        self.isolated_neighbors.setRange(1, 200)
        self.isolated_neighbors.setValue(3)
        self.isolated_neighbors.setFixedWidth(104)
        sec.add(self._filter_block("Remove stray points", self.isolated_switch, [
            widgets.row("Radius", self.isolated_radius)[0],
            widgets.row("Min neighbours", self.isolated_neighbors)[0]]))

        # Voxel
        self.voxel_switch = widgets.Switch(False)
        self.voxel_size = _spin(0.005, 5.0, 0.05, 0.01, 3)
        sec.add(self._filter_block("Thin out (voxel grid)", self.voxel_switch, [
            widgets.row("Cell size", self.voxel_size)[0]]))

        for sw in (self.range_switch, self.thermal_switch, self.thermal_drop,
                   self.isolated_switch, self.voxel_switch):
            sw.toggled.connect(self._on_pipeline_changed)
        for sp in (self.range_min, self.range_max, self.thermal_min, self.thermal_max,
                   self.isolated_radius, self.isolated_neighbors, self.voxel_size):
            sp.valueChanged.connect(self._on_pipeline_changed)

        self.process_result = QLabel("No filters active.")
        self.process_result.setObjectName("StatValue")
        sec.add(self.process_result)

        reset = QPushButton("Reset all filters")
        reset.clicked.connect(self._reset_pipeline)
        sec.add(reset)
        return sec

    # -- Scene ------------------------------------------------------------

    def _scene_page(self) -> QWidget:
        page, layout = self._page()

        disp = widgets.Section("Display", "sliders")
        self.size_value_label = QLabel(str(self._point_size))
        self.size_value_label.setObjectName("StatValue")
        disp.add(self._point_size_row())

        self.color_seg = widgets.Segmented(["RGB camera", "Thermal"])
        self.color_seg.changed.connect(lambda _i: self._on_color_mode_changed())
        disp.add(widgets.stacked("Colour overlay", self.color_seg))

        w, self.temp_label = widgets.row("Temperature range", value="n/a")
        disp.add(w)

        self.bg_button = QPushButton("  Background")
        self.bg_button.setToolTip("Pick the viewport colour. The vignette is rebuilt around it.")
        self.bg_button.clicked.connect(self._choose_background)
        reset_bg = QPushButton("Reset")
        reset_bg.clicked.connect(self._reset_background)
        bg_row = QWidget()
        h = QHBoxLayout(bg_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(theme.SPACE_2)
        h.addWidget(self.bg_button, 1)
        h.addWidget(reset_bg)
        disp.add(bg_row)
        self._update_bg_swatch()
        layout.addWidget(disp)

        ov = widgets.Section("Overlays", "eye")
        self.axes_switch = widgets.Switch(True)
        self.axes_switch.toggled.connect(self._on_axes_toggled)
        ov.add(widgets.row("Orientation axes", self.axes_switch)[0])
        self.origin_switch = widgets.Switch(True)
        self.origin_switch.toggled.connect(self._show_origin)
        ov.add(widgets.row("Origin marker", self.origin_switch)[0])
        self.grid_switch = widgets.Switch(True)
        self.grid_switch.setToolTip(
            "Graduated bounding box with tick labels in metres.\n"
            "Ticks re-scale automatically as you zoom.")
        self.grid_switch.toggled.connect(self._show_scale_grid)
        ov.add(widgets.row("Scale grid (metres)", self.grid_switch)[0])
        layout.addWidget(ov)

        cap = widgets.Section("Capture", "camera")
        self.save_cloud_button = QPushButton("  Save point cloud...")
        theme.bind_icon(self.save_cloud_button, "download", "text", 15)
        self.save_cloud_button.clicked.connect(self._save_pcd)
        cap.add(self.save_cloud_button)
        self.save_png_button = QPushButton("  Save screenshot...")
        theme.bind_icon(self.save_png_button, "image", "text", 15)
        self.save_png_button.clicked.connect(self._save_screenshot)
        self.capture_status = widgets.hint("")
        png_box = QWidget()
        pv_ = QVBoxLayout(png_box)
        pv_.setContentsMargins(0, 0, 0, 0)
        pv_.setSpacing(theme.SPACE_2)
        pv_.addWidget(self.save_png_button)
        pv_.addWidget(self.capture_status)
        cap.add(png_box)
        layout.addWidget(cap)

        layout.addStretch()
        return page

    def _point_size_row(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(theme.SPACE_2)
        head = QHBoxLayout()
        cap = QLabel("Point size")
        cap.setObjectName("FieldLabel")
        head.addWidget(cap)
        head.addStretch()
        head.addWidget(self.size_value_label)
        v.addLayout(head)
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(1, 12)
        self.size_slider.setValue(self._point_size)
        self.size_slider.valueChanged.connect(self._on_point_size_changed)
        v.addWidget(self.size_slider)
        return box

    # ======================================================================
    # Discovery / connection
    # ======================================================================

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
        theme.bind_icon(self.scan_button, "stop", "text", 15)

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
        theme.bind_icon(self.scan_button, "search", "text", 15)

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
        self._on_state_changed("connecting", "Connecting...")
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
        self._on_state_changed("idle", "Not connected")
        for widget in (self.host_combo, self.port_spin, self.interval_spin,
                        self.scan_button):
            widget.setEnabled(True)
        self._restyle(self.connect_button)

    def _restyle(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _on_state_changed(self, state: str, detail: str) -> None:
        self.status_pill.set_state(state, detail)
        self.title_bar.set_status(state, detail if state != "live" else "Live")
        self.canvas_overlay.set_live(state == "live")

    # ======================================================================
    # Sensor-PC power
    # ======================================================================

    _POWER_TEXT = {
        control.ON: "Running", control.OFF: "Stopped", control.STARTING: "Starting...",
        control.STOPPING: "Stopping...", control.FAILED: "Failed - check the Pi's logs",
        control.OFFLINE: "Unreachable", control.NO_DAEMON: "No control service",
    }

    def _current_host(self) -> str:
        host = self.host_combo.currentText().strip()
        return host.split()[0] if " - " in host else host

    def _power_section(self) -> QWidget:
        sec = widgets.Section(
            "Sensor PC power", "power",
            footer="Use the power icon in the title bar. On starts the sensor "
                   "pipeline; when it is running, the icon offers stop, restart "
                   "or power off.")
        w, self.power_status_value = widgets.row("Pipeline", value="-")
        sec.add(w)

        self.control_port_spin = QSpinBox()
        self.control_port_spin.setRange(1, 65535)
        self.control_port_spin.setValue(
            int(self._settings.value("control_port", control.DEFAULT_CONTROL_PORT)))
        self.control_port_spin.setFixedWidth(104)
        self.control_port_spin.valueChanged.connect(self._on_control_port_changed)
        sec.add(widgets.row("Control port", self.control_port_spin)[0])

        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("printed by install_control.sh")
        self.token_edit.setText(str(self._settings.value("control_token", "")))
        self.token_edit.editingFinished.connect(
            lambda: self._settings.setValue("control_token", self.token_edit.text().strip()))
        sec.add(widgets.stacked("Control token", self.token_edit))

        self.power_hint = widgets.hint("")
        self.power_hint.setVisible(False)
        sec.add(self.power_hint)
        self.host_combo.editTextChanged.connect(lambda _t: self._retarget_control())
        return sec

    def _set_power_hint(self, text: str) -> None:
        self.power_hint.setText(text)
        self.power_hint.setVisible(bool(text))

    def _start_control(self) -> None:
        self._control = ControlWorker()
        self._control.status.connect(self._on_control_status)
        self._control.start()
        self._retarget_control()
        self._show_power_state(control.OFFLINE)

    def _retarget_control(self) -> None:
        if self._control is None:
            return
        self._control_status = None
        self._poweroff_notice = False
        host = self._current_host()
        self._control.set_target(host, self.control_port_spin.value())
        if not host:
            self._show_power_state(control.OFFLINE)

    def _on_control_port_changed(self, value: int) -> None:
        self._settings.setValue("control_port", value)
        self._retarget_control()

    def _on_control_status(self, st) -> None:
        self._control_status = st
        state = st.state
        now = time.time()

        if self._pending is not None:
            action, deadline = self._pending
            if action == "shutdown":
                if st.kind == "unreachable":          # it went down, as asked
                    self._pending = None
                    self._poweroff_notice = True
                elif now > deadline:
                    self._pending = None
                else:
                    state = control.STOPPING
            else:
                target = control.OFF if action == "stop" else control.ON
                if st.kind == "ok" and state == target or now > deadline:
                    self._pending = None
                else:
                    state = control.STOPPING if action == "stop" else control.STARTING

        if st.kind == "ok":
            self._poweroff_notice = False             # it is back (power-cycled)
        elif self._poweroff_notice:
            state = control.OFFLINE
        self._show_power_state(state)

    def _show_power_state(self, state: str) -> None:
        self.power_button.set_state(state)
        self.power_status_value.setText(self._POWER_TEXT.get(state, "-"))
        if self._control is not None:
            self._control.set_fast(state in control.BUSY)
        tips = {
            control.ON: "Sensors running - click for stop, restart or power off",
            control.OFF: "Sensors stopped - click to start them",
            control.STARTING: "Starting the sensor pipeline...",
            control.STOPPING: "Stopping...",
            control.FAILED: "Sensor pipeline failed - click to try starting it again",
            control.OFFLINE: "Sensor PC unreachable",
            control.NO_DAEMON: "Control service not installed on the sensor PC "
                               "(see sensor_side/README.md)",
        }
        tip = tips.get(state, "")
        if state == control.OFFLINE and self._poweroff_notice:
            tip = "Powered off - power-cycle the Raspberry Pi to turn it back on"
            self._set_power_hint(
                "The Raspberry Pi is powered off. Power-cycle it (disconnect and "
                "reconnect its power) to turn it back on.")
        elif state == control.OFFLINE and not self._current_host():
            tip = "Enter the rover's address on the Rover tab first"
        self.power_button.setToolTip(tip)

    def _on_power_clicked(self) -> None:
        state = self.power_button.state
        if state in control.BUSY:
            return
        if not self._current_host():
            self._show_tab(0)
            self.host_combo.setFocus()
            self._set_power_hint("Enter the rover's address first.")
            return
        if state == control.OFFLINE:
            self._show_tab(0)
            self._set_power_hint(
                "The Raspberry Pi is not answering. If it was powered off, it has "
                "to be power-cycled by hand to turn it back on."
                if self._poweroff_notice else
                "The sensor PC is not answering. Check its address and that it is on "
                "the network.")
            return
        if state == control.NO_DAEMON:
            self._show_tab(0)
            self._set_power_hint(
                "No control service on the sensor PC. Install it with "
                "sensor_side/install_control.sh (see sensor_side/README.md).")
            return
        if not self.token_edit.text().strip():
            self._show_tab(0)
            self.token_edit.setFocus()
            self._set_power_hint("Enter the control token to send commands. "
                                 "install_control.sh prints it on the Pi.")
            return

        self._set_power_hint("")
        if state in (control.OFF, control.FAILED):
            self._send("start")
            return

        menu = QMenu(self)
        menu.addAction("Restart sensors", lambda: self._send("restart"))
        menu.addAction("Stop sensors", lambda: self._send("stop"))
        menu.addSeparator()
        can = bool(self._control_status and self._control_status.can_shutdown)
        poweroff = menu.addAction("Power off Raspberry Pi..." if can
                                  else "Power off Raspberry Pi... (not permitted)",
                                  lambda: self._send("shutdown"))
        poweroff.setEnabled(can)
        menu.exec(self.power_button.mapToGlobal(QPoint(0, self.power_button.height() + 4)))

    def _confirm_power_off(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Power off the Raspberry Pi?")
        box.setText("<b>Power off the Raspberry Pi?</b>")
        box.setInformativeText(
            "This shuts the Pi down completely, and the live point-cloud stream will stop.\n\n"
            "It cannot be turned back on from this app. Someone has to power-cycle it "
            "- disconnect and reconnect its power - before it will start again.")
        off = box.addButton("Power off", QMessageBox.DestructiveRole)
        off.setObjectName("DestructiveButton")
        cancel = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is off

    def _send(self, action: str) -> None:
        if self._action_worker is not None and self._action_worker.isRunning():
            return
        if action == "shutdown" and not self._confirm_power_off():
            return
        host = self._current_host()
        self._pending = (action, time.time() + (30 if action == "shutdown" else 45))
        self._show_power_state(control.STOPPING if action in ("stop", "shutdown")
                               else control.STARTING)
        self._action_worker = ActionWorker(host, self.control_port_spin.value(), action,
                                           self.token_edit.text().strip())
        self._action_worker.done.connect(self._on_action_done)
        self._action_worker.start()

    def _on_action_done(self, action: str, ok: bool, message: str) -> None:
        if not ok:
            self._pending = None
            self._set_power_hint(f"Command failed: {message}")
            self._control.poll_now()
            return
        if action == "shutdown":
            self._set_power_hint(
                "Powering off. Once it has stopped, power-cycle the Raspberry Pi to "
                "turn it back on.")
            if self._worker is not None:
                self._disconnect()
        self._control.poll_now()

    # ======================================================================
    # Diagnostics
    # ======================================================================

    def _stream_info(self) -> dict:
        """The base station's own measurements, shown beside the sensor PC's."""
        s = self._stats.snapshot()
        f = self._stream_frame
        thermal = None
        if f is not None and f.num_points:
            thermal = 100.0 * float(np.count_nonzero(~np.isnan(f.temp_c))) / f.num_points
        last = s["last_frame_time"]
        return {
            "connected": self._worker is not None,
            "fps": s["fps"], "points": s["point_count"], "thermal_pct": thermal,
            "age_s": None if last is None else time.time() - last,
            "kbps": s["kbps"], "failed": s["frames_failed"],
        }

    def _open_diagnostics(self) -> None:
        host = self._current_host()
        if not host:
            self._show_tab(0)
            self.status_pill.set_state(
                "error", "Enter the rover's address first, then open Diagnostics.")
            return
        port = self.port_spin.value()
        if self._diag is not None:
            self._diag.set_target(host, port)
            self._diag.raise_()
            self._diag.activateWindow()
            return
        self._diag = DiagnosticsDialog(self, host, port, self._stream_info)
        self._diag.setAttribute(Qt.WA_DeleteOnClose)
        self._diag.destroyed.connect(lambda *_: setattr(self, "_diag", None))
        self._diag.show()

    # ======================================================================
    # Rendering
    # ======================================================================

    def _choose_background(self) -> None:
        current = QColor.fromRgbF(*self._bg_base)
        chosen = QColorDialog.getColor(current, self, "Viewport background colour")
        if not chosen.isValid():
            return
        self._bg_base = (chosen.redF(), chosen.greenF(), chosen.blueF())
        self._bg_custom = True
        self._apply_viewport_background()
        self._update_bg_swatch()
        self.plotter.render()

    def _reset_background(self) -> None:
        self._bg_base = theme.VIEWPORT_BG_CENTER
        self._bg_custom = False
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
        outer = inner * theme.VIEWPORT_VIGNETTE      # edges fall off toward the page tone
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
            self.plotter.set_background(tuple(np.array(self._bg_base) * theme.VIEWPORT_VIGNETTE),
                                         top=self._bg_base)

    def _add_orientation_axes(self) -> None:
        self.plotter.add_axes(
            interactive=False,
            x_color=theme.AXIS_X, y_color=theme.AXIS_Y, z_color=theme.AXIS_Z,
        )

    def _on_pause_toggled(self, paused: bool) -> None:
        self._view_paused = paused
        if not paused:
            self._paused_by_measure = False
            if self._latest_frame is not self._stream_frame and self._stream_frame:
                self._latest_frame = self._stream_frame
                self._refresh_view()

    def _on_frame(self, frame: Frame) -> None:
        self._stream_frame = frame
        if self._view_paused:
            # Data keeps arriving and stats keep counting (that happens on
            # the worker thread), but the view -- and what Save/Record act
            # on -- stays exactly as it was when paused.
            return
        self._latest_frame = frame
        self._refresh_view(record=True)

    def _refresh_view(self, record: bool = False) -> None:
        """Run the processing pipeline on the current frame and draw it."""
        frame = self._latest_frame
        if frame is None:
            return
        idx = self._pipeline.apply(frame.xyz, frame.temp_c)
        if idx is None:
            view = frame
        else:
            view = Frame(frame.frame_id, frame.recv_time, frame.fetch_seconds,
                         frame.xyz[idx], frame.rgb[idx], frame.temp_c[idx])
        self._view = view
        self._update_process_result(frame.num_points, view.num_points)
        self._render_view(view)
        if record:
            self._maybe_record(view)

    def _update_process_result(self, before: int, after: int) -> None:
        if not self._pipeline.active:
            self.process_result.setText("No filters active.")
        else:
            self.process_result.setText(f"{after:,} of {before:,} points kept")

    def _render_view(self, view: Frame) -> None:
        n = view.num_points
        if n == 0:
            # A crop or filter can legitimately leave nothing. Drop the actor
            # rather than leaving the previous frame on screen.
            if self._actor is not None:
                self.plotter.remove_actor(self._actor, render=False)
            self._actor = self._cloud = None
            self._xyz_buffer = self._color_buffer = None
            self.plotter.render()
            return

        colors = self._compute_colors(view)

        # VTK wraps numpy arrays by reference rather than copying them, so
        # anything handed to it must outlive the render. These buffers are
        # owned by the window and written in place; passing short-lived
        # locals here causes VTK to read freed memory and crash the process.
        rebuild = self._cloud is None or self._xyz_buffer is None \
            or len(self._xyz_buffer) != n

        if rebuild:
            self._xyz_buffer = np.empty((n, 3), dtype=np.float32)
            self._color_buffer = np.empty((n, 3), dtype=np.uint8)

        np.copyto(self._xyz_buffer, view.xyz)
        np.copyto(self._color_buffer, colors)

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
                # Explicit, not left to default: add_mesh's default reset
                # policy is "reset unless camera_set is True", and camera_set
                # stays False forever once a mouse-driven orbit happens. Real
                # rover data changes point count almost every frame, forcing
                # this rebuild path constantly -- left on default, every one
                # of those snaps the view back.
                reset_camera=False,
            )
        else:
            # Same point count: the buffers VTK already points at have just
            # been overwritten, so only tell it they changed.
            self._cloud.GetPoints().Modified()
            scalars = self._cloud.GetPointData().GetScalars()
            if scalars is not None:
                scalars.Modified()
            self._cloud.Modified()

        if not self._camera_fitted:
            self._camera_fitted = True
            self.plotter.reset_camera()
            if self.grid_switch.isChecked():
                self._show_scale_grid(True)

        self.plotter.render()

    def _compute_colors(self, frame: Frame) -> np.ndarray:
        if self.color_seg.index() == 0:
            self._temp_range = None
            self.temp_label.setText("n/a")
            rgb = np.clip(frame.rgb, 0.0, 1.0)
            return (rgb * 255).astype(np.uint8)

        temp = frame.temp_c
        valid = ~np.isnan(temp)
        colors = np.tile(np.array(INVALID_THERMAL_COLOR, dtype=np.uint8), (len(temp), 1))

        if not np.any(valid):
            self._temp_range = None
            self.temp_label.setText("no thermal data")
            return colors

        tmin = float(np.min(temp[valid]))
        tmax = float(np.max(temp[valid]))
        self._temp_range = (tmin, tmax)
        self.temp_label.setText(f"{tmin:.1f} to {tmax:.1f} °C")

        span = max(tmax - tmin, 1e-6)
        norm = np.clip((temp[valid] - tmin) / span, 0.0, 1.0)
        colors[valid] = (self._colormap(norm)[:, :3] * 255).astype(np.uint8)
        return colors

    # -- scene controls ------------------------------------------------------

    def _on_point_size_changed(self, value: int) -> None:
        self._point_size = value
        self.size_value_label.setText(str(value))
        if self._actor is not None:
            self._actor.prop.point_size = value
            self.plotter.render()

    def _on_color_mode_changed(self) -> None:
        if self._latest_frame is not None:
            self._refresh_view()

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
                x_color=theme.AXIS_X, y_color=theme.AXIS_Y, z_color=theme.AXIS_Z,
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
                grid="back", location="outer", ticks="both", minor_ticks=True,
                xtitle="X (m)", ytitle="Y (m)", ztitle="Z (m)",
                color=theme.GRID_COLOR, fmt="%.2f",
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

    def _home_view(self) -> None:
        """The default view: isometric angle, zoom and centre fitted to the cloud.

        (reset_camera() alone only refits zoom -- it keeps whatever angle the
        user has orbited to, so it is not "home".)
        """
        self.plotter.view_isometric()
        self.plotter.reset_camera()
        self.plotter.render()

    def _reset_view(self) -> None:
        self._home_view()

    def _top_view(self) -> None:
        self.plotter.view_xy()
        self.plotter.render()

    # ======================================================================
    # Tools: navigate / crop / measure
    # ======================================================================

    def _on_tool_changed(self, key: str) -> None:
        previous, self._tool = self._tool, key
        index = ("navigate", "crop", "measure").index(key)
        self.tool_seg.set_index(index)
        for i, panel in enumerate(self.tool_panels):
            panel.setVisible(i == index)
        if key != "navigate":
            self._show_tab(1)

        # Leave the previous tool.
        if previous == "crop" and key != "crop":
            self._set_crop_widget_visible(False)
        if previous == "measure" and key != "measure" and self._paused_by_measure:
            self.canvas_overlay.set_paused(False)

        # Enter the new one.
        if key == "crop":
            self._enter_crop()
        elif key == "measure":
            # Points must not move under the cursor, so freeze the view.
            if not self._view_paused:
                self._paused_by_measure = True
                self.canvas_overlay.set_paused(True)
            self._update_measure()
        self.readout.set_shown(key == "measure" and bool(self._measure_pts))

    # -- crop ------------------------------------------------------------

    def _enter_crop(self) -> None:
        if not self._crop_seeded:
            self._fit_crop_to_cloud(enable=False)
        if not self.crop_switch.isChecked():
            self.crop_switch.setChecked(True)     # applies immediately via toggle handler
        self._set_crop_widget_visible(True)

    def _set_crop_widget_visible(self, visible: bool) -> None:
        if visible and self._crop_widget is None:
            self._crop_widget = self.plotter.add_box_widget(
                callback=self._on_crop_widget,
                bounds=self._pipeline.crop.bounds,
                factor=1.0,
                rotation_enabled=False,
                outline_translation=True,
                interaction_event="always",
            )
        if self._crop_widget is not None:
            self._crop_widget.SetEnabled(1 if visible else 0)
            if visible:
                self._crop_widget.PlaceWidget(*self._pipeline.crop.bounds)
        self.plotter.render()

    def _on_crop_widget(self, box) -> None:
        """Dragging the box in the view drives the numbers and the filter."""
        self._set_crop_bounds(tuple(float(v) for v in box.bounds), from_widget=True)

    def _on_crop_spin(self, _value=None) -> None:
        vals = [s.value() for s in self._crop_spins]
        bounds = (min(vals[0], vals[1]), max(vals[0], vals[1]),
                  min(vals[2], vals[3]), max(vals[2], vals[3]),
                  min(vals[4], vals[5]), max(vals[4], vals[5]))
        self._set_crop_bounds(bounds, from_spin=True)

    def _set_crop_bounds(self, bounds, from_widget=False, from_spin=False) -> None:
        self._pipeline.crop.bounds = bounds
        if not from_spin:
            for spin, v in zip(self._crop_spins, bounds):
                spin.blockSignals(True)
                spin.setValue(v)
                spin.blockSignals(False)
        if not from_widget and self._crop_widget is not None and self._tool == "crop":
            self._crop_widget.PlaceWidget(*bounds)
        self._refresh_view()

    def _fit_crop_to_cloud(self, enable: bool = True) -> None:
        source = self._latest_frame
        bounds = processing.bounds_of(source.xyz) if source is not None else None
        if bounds is None:
            bounds = (-1.0, 1.0, -1.0, 1.0, -1.0, 1.0)
        else:
            bounds = tuple(round(v, 2) for v in bounds)
        self._crop_seeded = True
        self._set_crop_bounds(bounds)

    def _on_crop_toggled(self, checked: bool) -> None:
        self._pipeline.crop.enabled = checked
        self._refresh_view()

    def _on_crop_mode(self, index: int) -> None:
        self._pipeline.crop.keep_inside = (index == 0)
        self._refresh_view()

    # -- measure -----------------------------------------------------------

    def _install_pick_observers(self) -> None:
        """Click-to-pick on the cloud, without stealing left-drag orbiting.

        A press and release within a few pixels is a click; anything longer
        is an orbit and is left alone.
        """
        vtk_iren = self.plotter.iren.interactor
        self._vtk_iren = vtk_iren
        self._observer_tags = [
            vtk_iren.AddObserver("LeftButtonPressEvent", self._on_left_press),
            vtk_iren.AddObserver("LeftButtonReleaseEvent", self._on_left_release),
        ]

    def _on_left_press(self, obj, _event) -> None:
        self._press_pos = obj.GetEventPosition()

    def _on_left_release(self, obj, _event) -> None:
        start, self._press_pos = self._press_pos, None
        if self._tool != "measure" or start is None:
            return
        x, y = obj.GetEventPosition()
        if abs(x - start[0]) > CLICK_SLOP_PX or abs(y - start[1]) > CLICK_SLOP_PX:
            return
        self._pick_at(x, y)

    def _pick_at(self, x: int, y: int) -> None:
        if self._actor is None or self._view is None:
            return
        picker = vtkPointPicker()
        picker.SetTolerance(0.012)
        picker.PickFromListOn()
        picker.AddPickList(self._actor)
        picker.Pick(x, y, 0, self.plotter.renderer)
        pid = picker.GetPointId()
        if pid < 0 or pid >= self._view.num_points:
            return
        point = np.array(self._view.xyz[pid], dtype=np.float64)
        temp = float(self._view.temp_c[pid])
        if self.measure_mode.index() == 0 and len(self._measure_pts) >= 2:
            self._measure_pts.clear()             # a third click starts a new distance
        self._measure_pts.append((point, temp))
        self._update_measure()

    def _on_measure_mode(self) -> None:
        if self.measure_mode.index() == 0 and len(self._measure_pts) > 2:
            del self._measure_pts[2:]
        self._update_measure()

    def _undo_measure_point(self) -> None:
        if self._measure_pts:
            self._measure_pts.pop()
            self._update_measure()

    def _clear_measure(self) -> None:
        self._measure_pts.clear()
        self._update_measure()

    def _remove_named(self, name: str) -> None:
        try:
            self.plotter.remove_actor(name, render=False)
        except Exception:
            pass

    def _update_measure(self) -> None:
        pts = np.array([p for p, _ in self._measure_pts], dtype=np.float64).reshape(-1, 3)
        temps = [t for _, t in self._measure_pts]
        # Hold references: VTK wraps these arrays rather than copying them.
        self._measure_refs = [pts]

        for name in ("measure_pts", "measure_line", "measure_labels"):
            self._remove_named(name)

        if len(pts):
            self.plotter.add_points(
                pts, name="measure_pts", color=theme.MEASURE_COLOR, point_size=13,
                render_points_as_spheres=True, pickable=False,
                reset_camera=False, render=False)
        if len(pts) >= 2:
            line_pts = pts
            if self.measure_mode.index() == 1 and len(pts) >= 3:
                line_pts = np.vstack([pts, pts[:1]])          # close the polygon
            self._measure_refs.append(line_pts)
            self.plotter.add_mesh(
                pv.lines_from_points(line_pts), name="measure_line",
                color=theme.MEASURE_COLOR, line_width=3, pickable=False,
                lighting=False, reset_camera=False, render=False)
            mids = (line_pts[:-1] + line_pts[1:]) / 2.0
            lengths = np.linalg.norm(np.diff(line_pts, axis=0), axis=1)
            self._measure_refs.append(mids)
            self.plotter.add_point_labels(
                mids, [f"{d:.2f} m" for d in lengths], name="measure_labels",
                font_size=14, text_color=theme.MEASURE_COLOR, shape="rounded_rect",
                shape_color="#1c1c1e", shape_opacity=0.82, show_points=False,
                always_visible=True, pickable=False, reset_camera=False, render=False)

        self._update_measure_text(pts, temps)
        self.plotter.render()

    def _update_measure_text(self, pts: np.ndarray, temps: list) -> None:
        if not len(pts):
            self.measure_text.setText("No points yet.")
            self.readout.set_shown(False)
            return

        lines = []
        for i, (p, t) in enumerate(zip(pts, temps), start=1):
            temp = f"{t:5.1f} °C" if not np.isnan(t) else "   no thermal"
            lines.append(f"{i:>2}  {p[0]:+8.2f} {p[1]:+8.2f} {p[2]:+8.2f}  {temp}")

        if len(pts) == 1:
            p, t = pts[0], temps[0]
            big = f"{p[0]:+.2f}, {p[1]:+.2f}, {p[2]:+.2f} m"
            small = "no thermal reading" if np.isnan(t) else f"{t:.1f} °C at this point"
        elif self.measure_mode.index() == 0:
            info = processing.segment_info(pts[0], pts[1])
            big = f"{info['distance']:.2f} m"
            small = (f"Horizontal {info['horizontal']:.2f} m   Vertical {info['vertical']:+.2f} m"
                     f"   Slope {info['slope_deg']:.1f}°")
            lines.append(f"\ndX {info['dx']:+.2f}  dY {info['dy']:+.2f}  dZ {info['dz']:+.2f}")
        else:
            total = processing.path_length(pts)
            big = f"{total:.2f} m"
            small = f"{len(pts)} points"
            if len(pts) >= 3:
                closed = total + float(np.linalg.norm(pts[-1] - pts[0]))
                area = processing.polygon_area_xy(pts)
                small += f"   Area {area:.2f} m² (plan view)   Closed loop {closed:.2f} m"
                lines.append(f"\nPath {total:.2f} m   Area {area:.2f} m²")
            else:
                lines.append(f"\nPath {total:.2f} m")

        self.measure_text.setText("\n".join(lines))
        self.readout.show_text(big, small)
        self.readout.set_shown(self._tool == "measure")

    # ======================================================================
    # Processing pipeline
    # ======================================================================

    def _on_pipeline_changed(self, _value=None) -> None:
        p = self._pipeline
        p.range_enabled = self.range_switch.isChecked()
        p.range_min = self.range_min.value()
        p.range_max = self.range_max.value()
        p.thermal_enabled = self.thermal_switch.isChecked()
        p.thermal_min = self.thermal_min.value()
        p.thermal_max = self.thermal_max.value()
        p.thermal_drop_unknown = self.thermal_drop.isChecked()
        p.isolated_enabled = self.isolated_switch.isChecked()
        p.isolated_radius = self.isolated_radius.value()
        p.isolated_min_neighbors = self.isolated_neighbors.value()
        p.voxel_enabled = self.voxel_switch.isChecked()
        p.voxel_size = self.voxel_size.value()
        self._refresh_view()

    def _reset_pipeline(self) -> None:
        for sw in (self.range_switch, self.thermal_switch, self.thermal_drop,
                   self.isolated_switch, self.voxel_switch, self.crop_switch):
            sw.setChecked(False)

    # ======================================================================
    # Capture
    # ======================================================================

    def _default_capture_dir(self) -> str:
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        return os.path.abspath(CAPTURES_DIR)

    def _save_pcd(self) -> None:
        if self._view is None or self._cloud is None:
            self._set_capture_status("Nothing to save - no points in view.")
            return

        suggested = os.path.join(
            self._default_capture_dir(), f"frame_{datetime.now():%Y%m%d_%H%M%S}.pcd")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save point cloud", suggested,
            "Point cloud (*.pcd);;PLY (*.ply);;All files (*)")
        if not path:
            return

        if self._write_cloud(path, self._view, self._color_buffer):
            self._set_capture_status(
                f"Saved {os.path.basename(path)} ({self._view.num_points:,} pts)")
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
            self._default_capture_dir(), f"view_{datetime.now():%Y%m%d_%H%M%S}.png")
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
            self.record_pill.set_state("error", "Connect to a rover before recording.")
            return

        self._recorder.start(folder, self.rec_interval_spin.value(),
                              self.rec_duration_spin.value())
        self.record_button.setText("  Stop recording")
        theme.bind_icon(self.record_button, "stop", "danger", 15)
        for widget in (self.rec_interval_spin, self.rec_duration_spin,
                        self.rec_folder_button):
            widget.setEnabled(False)
        self.record_pill.set_state("connecting", "Recording... 0 frames")

    def _stop_recording(self, message: str) -> None:
        saved, failed = self._recorder.saved, self._recorder.failed
        self._recorder.stop()
        self.record_button.setText("  Start recording")
        theme.bind_icon(self.record_button, "record", "danger", 15)
        for widget in (self.rec_interval_spin, self.rec_duration_spin,
                        self.rec_folder_button):
            widget.setEnabled(True)
        detail = f"{message} {saved} frame{'s' if saved != 1 else ''} saved"
        if failed:
            detail += f", {failed} failed"
        # Green is reserved for a genuinely completed action.
        self.record_pill.set_state("live" if saved and not failed else "idle", detail)

    def _maybe_record(self, view: Frame) -> None:
        if not self._recorder.active:
            return
        if self._recorder.expired():
            self._stop_recording("Recording complete.")
            return
        if not self._recorder.should_save() or view.num_points == 0:
            return

        name = f"frame_{self._recorder.saved:05d}_{datetime.now():%Y%m%d_%H%M%S}.pcd"
        path = os.path.join(self._recorder.folder, name)
        ok = self._write_cloud(path, view, self._color_buffer)
        self._recorder.note_saved(ok)

        remaining = max(0.0, self._recorder.duration - self._recorder.elapsed())
        self.record_pill.set_state(
            "connecting",
            f"Recording... {self._recorder.saved} frames, {remaining:.0f}s left")

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
        if self._diag is not None:
            self._diag.close()
        if self._control is not None:
            self._control.stop()
            self._control.wait(3000)
        if self._action_worker is not None:
            self._action_worker.wait(3000)
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(3000)
        if self._scanner is not None:
            self._scanner.stop()
            self._scanner.wait(3000)
        for tag in self._observer_tags:
            self._vtk_iren.RemoveObserver(tag)
        self.plotter.close()
        super().closeEvent(event)


_fault_log = None


def _enable_fault_log() -> None:
    """Send native-crash tracebacks to data/crash.log (works without a console)."""
    global _fault_log
    try:
        _fault_log = open(paths.data_path("crash.log"), "a", buffering=1, encoding="utf-8")
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

    # Windows groups a window under its *process*' identity. Run via
    # pythonw.exe, that is "Python" -- so the taskbar shows Python's icon and
    # name. Claiming our own AppUserModelID (before any window exists) makes
    # Windows use the window icon set below instead.
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "SurveyRig.BaseStation")
        except (AttributeError, OSError):
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("Survey Rig Base Station")
    saved = QSettings("SurveyRig", "BaseStation").value("theme", "system")
    theme.set_mode(saved if isinstance(saved, str) else "system")
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
