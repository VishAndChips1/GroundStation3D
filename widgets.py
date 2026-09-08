"""Reusable UI pieces: cards, section headers, status pill, title bar."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

import icons
import theme


class SectionHeader(QWidget):
    """Icon + letter-spaced, muted section title."""

    def __init__(self, icon_name: str, text: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_2)

        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(icon_name, theme.TEXT_FAINT, 14, stroke=2.0))
        glyph.setFixedSize(14, 14)
        layout.addWidget(glyph)

        label = QLabel(text.upper())
        label.setObjectName("SectionLabel")
        layout.addWidget(label)
        layout.addStretch()


class Card(QFrame):
    """A grouped section: subtle raised background, border, uniform padding."""

    def __init__(self, icon_name: str, title: str):
        super().__init__()
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(
            theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4)
        self._layout.setSpacing(theme.SPACE_3)
        self._layout.addWidget(SectionHeader(icon_name, title))

    def add(self, widget: QWidget) -> QWidget:
        self._layout.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)

    def add_spacing(self, amount: int) -> None:
        self._layout.addSpacing(amount)


class FieldLabel(QWidget):
    """Small icon + caption, sitting above an input."""

    def __init__(self, icon_name: str, text: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_1 + 2)

        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(icon_name, theme.TEXT_MUTED, 14, stroke=1.9))
        glyph.setFixedSize(14, 14)
        layout.addWidget(glyph)

        label = QLabel(text)
        label.setObjectName("FieldLabel")
        layout.addWidget(label)
        layout.addStretch()


def labeled_field(icon_name: str, caption: str, widget: QWidget) -> QWidget:
    """An icon+caption stacked above its input."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(theme.SPACE_1 + 2)
    layout.addWidget(FieldLabel(icon_name, caption))
    layout.addWidget(widget)
    return container


class StatusPill(QFrame):
    """Coloured dot + label describing connection state.

    States: idle (grey), connecting (amber, pulsing), live (green),
    error (red).
    """

    _COLORS = {
        "idle": theme.NEUTRAL_DOT,
        "connecting": theme.WARNING,
        "live": theme.SUCCESS,
        "error": theme.DANGER,
    }

    def __init__(self):
        super().__init__()
        self.setObjectName("StatusPill")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            theme.SPACE_3, theme.SPACE_2, theme.SPACE_3, theme.SPACE_2)
        layout.setSpacing(theme.SPACE_2)

        self._dot = QLabel()
        self._dot.setFixedSize(9, 9)
        layout.addWidget(self._dot, 0, Qt.AlignVCenter)

        self._label = QLabel("Not connected")
        self._label.setObjectName("StatusLabel")
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._label, 1)

        self._state = "idle"
        self._pulse_on = True
        self._pulse = QTimer(self)
        self._pulse.setInterval(600)
        self._pulse.timeout.connect(self._toggle_pulse)

        self._paint_dot(1.0)

    def set_state(self, state: str, text: str) -> None:
        self._state = state if state in self._COLORS else "idle"
        self._label.setText(text)
        if self._state == "connecting":
            if not self._pulse.isActive():
                self._pulse_on = True
                self._pulse.start()
        else:
            self._pulse.stop()
        self._paint_dot(1.0)

    def _toggle_pulse(self) -> None:
        self._pulse_on = not self._pulse_on
        self._paint_dot(1.0 if self._pulse_on else 0.35)

    def _paint_dot(self, opacity: float) -> None:
        color = self._COLORS[self._state]
        self._dot.setStyleSheet(
            f"background-color: {color}; border-radius: 4px;"
        )
        self._dot.setWindowOpacity(opacity)
        # QLabel has no per-widget opacity in a stylesheet, so fade by
        # blending the dot toward the pill background instead.
        if opacity < 1.0:
            self._dot.setStyleSheet(
                f"background-color: {theme.BG_INPUT};"
                f" border: 2px solid {color}; border-radius: 4px;"
            )


class TitleBar(QFrame):
    """Slim custom title bar: app mark + name, window controls on the right."""

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    HEIGHT = 38

    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("TitleBar")
        self.setFixedHeight(self.HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_3, 0, theme.SPACE_2, 0)
        layout.setSpacing(theme.SPACE_2)

        mark = QLabel()
        mark.setPixmap(icons.pixmap("cube", theme.ACCENT, 16, stroke=1.9))
        mark.setFixedSize(16, 16)
        layout.addWidget(mark)

        name = QLabel(title)
        name.setObjectName("AppName")
        layout.addWidget(name)
        layout.addStretch()

        self._max_button = self._win_button("maximize", self.maximize_requested)
        layout.addWidget(self._win_button("minimize", self.minimize_requested))
        layout.addWidget(self._max_button)
        layout.addWidget(self._win_button("close", self.close_requested,
                                           object_name="WinClose"))

    def _win_button(self, icon_name: str, signal,
                     object_name: str = "WinButton") -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        # Both names share the WinButton styling; WinClose only overrides hover.
        if object_name == "WinClose":
            button.setProperty("class", "WinButton")
        button.setIcon(icons.icon(icon_name, theme.TEXT_MUTED, 16, stroke=1.8))
        button.setFixedSize(32, 26)
        button.setCursor(Qt.ArrowCursor)
        button.clicked.connect(signal.emit)
        return button

    def set_maximized(self, maximized: bool) -> None:
        self._max_button.setIcon(icons.icon(
            "restore" if maximized else "maximize",
            theme.TEXT_MUTED, 16, stroke=1.8))
