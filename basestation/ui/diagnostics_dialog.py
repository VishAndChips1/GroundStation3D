"""Sensor diagnostics window: what the sensor PC says about itself.

Polls /diagnostics on its own thread (independent of the point-cloud
stream, so it works while disconnected and shows *why* a stream is dead),
and draws sensors, pipelines and Pi vitals with traffic-light status.
A "Base station view" card shows what this end measures for itself, so a
sensor PC that claims 10 fps but delivers 2 is visible at a glance.
"""

import threading
import time

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from basestation.core import diagnostics
from basestation.ui import icons
from basestation.ui import theme
from basestation.ui import widgets
from basestation.core.diagnostics import ERROR, OK, UNKNOWN, WARN

POLL_SECONDS = 1.0


def _color(status: str) -> str:
    """Looked up per use so the colours follow the active theme."""
    return {OK: theme.SUCCESS, WARN: theme.WARNING, ERROR: theme.DANGER,
            UNKNOWN: theme.NEUTRAL_DOT}[status]


STATUS_WORD = {OK: "Healthy", WARN: "Degraded", ERROR: "Problem", UNKNOWN: "No data"}


class DiagnosticsWorker(QThread):
    result = Signal(object)   # diagnostics.FetchResult

    def __init__(self, host: str, port: int):
        super().__init__()
        self._host, self._port = host, port
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            started = time.time()
            self.result.emit(diagnostics.fetch(self._host, self._port))
            self._stop_event.wait(max(0.0, POLL_SECONDS - (time.time() - started)))


def _dot(status: str, size: int = 10) -> QLabel:
    dot = QLabel()
    dot.setFixedSize(size, size)
    dot.setStyleSheet(f"background-color: {_color(status)};"
                      f" border-radius: {size // 2}px;")
    return dot


def _row(caption: str, value: str, status: str | None = None) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(theme.SPACE_2)
    if status is not None:
        layout.addWidget(_dot(status, 8), 0, Qt.AlignVCenter)
    cap = QLabel(caption)
    cap.setObjectName("StatCaption")
    val = QLabel(value)
    val.setObjectName("StatValue")
    val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    val.setWordWrap(True)
    layout.addWidget(cap)
    layout.addStretch()
    layout.addWidget(val)
    return row


def _fmt_age(age) -> str:
    return "-" if age is None else f"{age:.1f} s ago"


class _ItemCard(QFrame):
    """One sensor or pipeline stage."""

    def __init__(self, item: diagnostics.Item):
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_4, theme.SPACE_3, theme.SPACE_4, theme.SPACE_3)
        layout.setSpacing(theme.SPACE_1 + 2)

        head = QHBoxLayout()
        head.setSpacing(theme.SPACE_2)
        head.addWidget(_dot(item.status, 11), 0, Qt.AlignVCenter)
        name = QLabel(item.name.replace("_", " ").title())
        name.setObjectName("Headline")
        head.addWidget(name)
        head.addStretch()
        state = QLabel(item.state.replace("_", " "))
        state.setStyleSheet(f"color: {_color(item.status)}; font-weight: 600;")
        head.addWidget(state)
        layout.addLayout(head)

        if item.fps is not None:
            text = f"{item.fps:.1f}"
            if item.target_fps:
                text += f" / {item.target_fps:g}"
            layout.addWidget(_row("Rate" if item.unit != "fps" else "Frame rate",
                                   f"{text} {item.unit}"))
        if item.age_s is not None:
            layout.addWidget(_row("Last frame", _fmt_age(item.age_s)))
        if item.metrics:
            # Secondary facts (topic, node, frame count, bandwidth ...) as one
            # quiet line instead of a row each: the headline numbers stay readable.
            detail = QLabel("  \u00b7  ".join(f"{k} {v}" for k, v in item.metrics))
            detail.setObjectName("HintLabel")
            detail.setWordWrap(True)
            layout.addWidget(detail)
        if item.message:
            msg = QLabel(item.message)
            msg.setObjectName("HintLabel")
            msg.setWordWrap(True)
            layout.addWidget(msg)


class DiagnosticsDialog(QDialog):
    """Non-modal window. `stream_info` is a callable returning a dict of
    the base station's own measurements (see BaseStation._stream_info)."""

    def __init__(self, parent, host: str, port: int, stream_info):
        super().__init__(parent)
        self.setWindowTitle("Sensor diagnostics")
        self.setWindowIcon(icons.app_icon(theme.ACCENT))
        self.resize(560, 780)
        self._stream_info = stream_info
        self._host, self._port = host, port
        self._last = None          # last FetchResult
        self._last_ok_at = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4)
        outer.setSpacing(theme.SPACE_3)

        top = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("Sensor diagnostics")
        title.setObjectName("TitleLabel")
        self.subtitle = QLabel(f"{host}:{port}")
        self.subtitle.setObjectName("SubtitleLabel")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        top.addLayout(titles, 1)
        self.overall = widgets.StatusPill()
        top.addWidget(self.overall, 0, Qt.AlignTop)
        outer.addLayout(top)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._body = QVBoxLayout(self._content)
        self._body.setContentsMargins(0, 0, theme.SPACE_2, 0)
        self._body.setSpacing(theme.SPACE_3)
        scroll.setWidget(self._content)
        outer.addWidget(scroll, 1)

        close = QPushButton("Close")
        close.clicked.connect(self.close)
        outer.addWidget(close, 0, Qt.AlignRight)

        self._render()
        self._worker = DiagnosticsWorker(host, port)
        self._worker.result.connect(self._on_result)
        self._worker.start()

        # Redraw the base-station card (and staleness) between sensor polls.
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._render)
        self._tick.start(500)

    def set_target(self, host: str, port: int) -> None:
        if (host, port) == (self._host, self._port):
            return
        self._host, self._port = host, port
        self.subtitle.setText(f"{host}:{port}")
        self._last = None
        self._shutdown_worker()
        self._worker = DiagnosticsWorker(host, port)
        self._worker.result.connect(self._on_result)
        self._worker.start()

    def _shutdown_worker(self) -> None:
        self._worker.stop()
        self._worker.wait(4000)

    def closeEvent(self, event):
        self._tick.stop()
        self._shutdown_worker()
        super().closeEvent(event)

    # -- data ----------------------------------------------------------

    def _on_result(self, result) -> None:
        self._last = result
        if result.kind == "ok":
            self._last_ok_at = time.time()
        self._render()

    # -- drawing -------------------------------------------------------

    def _clear(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _section(self, icon_name: str, title: str) -> None:
        self._body.addWidget(widgets.SectionHeader(icon_name, title))

    def _render(self) -> None:
        self._clear()
        result = self._last
        report = result.report if result and result.kind == "ok" else None

        if result is None:
            self.overall.set_state("connecting", "Contacting sensor PC...")
        elif result.kind == "ok":
            status = report.overall
            word = STATUS_WORD[status]
            self.overall.set_state(
                {"ok": "live", "warn": "connecting", "error": "error",
                 "unknown": "idle"}[status], word)
        elif result.kind == "unsupported":
            self.overall.set_state("connecting", "No diagnostics feed")
        else:
            self.overall.set_state("error", "Sensor PC unreachable")

        if result and result.kind in ("unreachable", "bad", "unsupported"):
            note = QLabel(result.detail + (
                "\nShowing only what the base station can measure itself. "
                "See sensor_side/README.md to enable /diagnostics."
                if result.kind == "unsupported" else ""))
            note.setObjectName("HintLabel")
            note.setWordWrap(True)
            self._body.addWidget(note)

        if report:
            meta = []
            if report.hostname:
                meta.append(report.hostname)
            if report.uptime_s is not None:
                meta.append(f"up {self._fmt_uptime(report.uptime_s)}")
            meta.append(f"link {report.latency_s * 1000:.0f} ms")
            self.subtitle.setText(f"{self._host}:{self._port}  -  " + "  -  ".join(meta))

            self._section("camera", "Sensors")
            for item in report.sensors:
                self._body.addWidget(_ItemCard(item))
            self._section("activity", "Pipelines")
            for item in report.pipelines:
                self._body.addWidget(_ItemCard(item))
            if report.system:
                self._section("server", "Sensor PC health")
                card = widgets.Card("server", "Vitals")
                for v in report.system:
                    card.add(_row(v.label, v.value, v.status))
                self._body.addWidget(card)

        self._section("wifi", "Base station view")
        card = widgets.Card("wifi", "Received stream")
        info = self._stream_info()
        if not info["connected"]:
            card.add(_row("Stream", "not connected", UNKNOWN))
        else:
            fps = info["fps"]
            card.add(_row("Frames received", f"{fps:.1f} fps",
                           OK if fps >= 1 else ERROR if fps <= 0 else WARN))
            card.add(_row("Points per frame", f"{info['points']:,}"))
            cov = info["thermal_pct"]
            card.add(_row("Thermal coverage",
                           "-" if cov is None else f"{cov:.0f} % of points",
                           UNKNOWN if cov is None else
                           ERROR if cov < 1 else WARN if cov < 20 else OK))
            age = info["age_s"]
            card.add(_row("Last frame", _fmt_age(age),
                           UNKNOWN if age is None else
                           ERROR if age > 5 else WARN if age > 2 else OK))
            card.add(_row("Data rate", f"{info['kbps']:.0f} KB/s"))
            card.add(_row("Failed requests", str(info["failed"]),
                           WARN if info["failed"] else OK))
        self._body.addWidget(card)
        self._body.addStretch()

    @staticmethod
    def _fmt_uptime(seconds: float) -> str:
        s = int(seconds)
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m = s // 60
        return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"
