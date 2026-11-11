"""Reusable UI pieces, styled by the Apple-style design system in theme.py."""

from PySide6.QtCore import (
    QEasingCurve, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from basestation.ui import icons
from basestation.ui import theme


# -- sections and rows ---------------------------------------------------------

class SectionHeader(QWidget):
    """Small uppercase caption, optionally led by a glyph."""

    def __init__(self, icon_name: str | None, text: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_1, 0, 0, 0)
        layout.setSpacing(theme.SPACE_2 - 2)
        if icon_name:
            glyph = QLabel()
            theme.bind_pixmap(glyph, icon_name, "text_faint", 13, 2.1)
            glyph.setFixedSize(13, 13)
            layout.addWidget(glyph)
        label = QLabel(text.upper())
        label.setObjectName("SectionLabel")
        layout.addWidget(label)
        layout.addStretch()


class Section(QWidget):
    """An inset grouped list: caption above, rounded surface below, rows
    divided by hairlines. Rows are added with add(); the first has no rule."""

    def __init__(self, title: str | None = None, icon_name: str | None = None,
                 footer: str | None = None):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(theme.SPACE_2 - 2)
        if title:
            outer.addWidget(SectionHeader(icon_name, title))
        self._surface = QFrame()
        self._surface.setObjectName("Group")
        self._rows = QVBoxLayout(self._surface)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(0)
        outer.addWidget(self._surface)
        if footer:
            note = QLabel(footer)
            note.setObjectName("HintLabel")
            note.setWordWrap(True)
            note.setContentsMargins(theme.SPACE_1, 0, theme.SPACE_1, 0)
            outer.addWidget(note)
        self._count = 0

    def add(self, widget: QWidget, padded: bool = True) -> QWidget:
        if self._count:
            rule = QFrame()
            rule.setObjectName("Hairline")
            rule.setFixedHeight(1)
            holder = QWidget()
            h = QHBoxLayout(holder)
            h.setContentsMargins(theme.SPACE_3, 0, 0, 0)
            h.addWidget(rule)
            holder.setFixedHeight(1)
            self._rows.addWidget(holder)
        if padded:
            wrap = QWidget()
            w = QVBoxLayout(wrap)
            w.setContentsMargins(theme.SPACE_3, theme.SPACE_2 + 1,
                                  theme.SPACE_3, theme.SPACE_2 + 1)
            w.addWidget(widget)
            self._rows.addWidget(wrap)
        else:
            self._rows.addWidget(widget)
        self._count += 1
        return widget

    def add_layout(self, layout) -> None:
        holder = QWidget()
        holder.setLayout(layout)
        self.add(holder)


def Card(icon_name: str, title: str) -> Section:
    """Compatibility constructor: Card(icon, title) -> Section."""
    return Section(title, icon_name)


def row(caption: str, control: QWidget | None = None, value: str | None = None):
    """label ...... control   (or label ...... value text).

    Returns (widget, value_label); value_label is None when a control is used.
    """
    w = QWidget()
    layout = QHBoxLayout(w)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(theme.SPACE_3)
    cap = QLabel(caption)
    cap.setObjectName("FieldLabel")
    layout.addWidget(cap)
    layout.addStretch()
    if control is not None:
        layout.addWidget(control)
        return w, None
    val = QLabel(value if value is not None else "-")
    val.setObjectName("StatValue")
    val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    layout.addWidget(val)
    return w, val


def stacked(caption: str, control: QWidget) -> QWidget:
    """Caption above a full-width control."""
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(theme.SPACE_1 + 2)
    cap = QLabel(caption)
    cap.setObjectName("StatusLabel")
    layout.addWidget(cap)
    layout.addWidget(control)
    return w


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("HintLabel")
    label.setWordWrap(True)
    return label


# -- controls ---------------------------------------------------------------------

class Switch(QAbstractButton):
    """iOS-style toggle. The knob animates from wherever it currently is, so
    flipping it again mid-slide reverses smoothly instead of jumping."""

    def __init__(self, checked: bool = False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 26)
        self._t = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:
        return QSize(42, 26)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._t)           # presentation value, not target
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _on_anim(self, value) -> None:
        self._t = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        off, on = QColor(120, 120, 128, 90 if theme.resolved() == "dark" else 70), QColor(theme.SUCCESS)
        mix = lambda a, b: int(a + (b - a) * self._t)
        track = QColor(mix(off.red(), on.red()), mix(off.green(), on.green()),
                       mix(off.blue(), on.blue()), mix(off.alpha(), on.alpha()))
        if not self.isEnabled():
            track.setAlpha(track.alpha() // 2)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, 42, 26), 13, 13)
        x = 3 + (42 - 26) * self._t
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(QPointF(x + 10, 13 + 1), 10, 10)       # soft shadow
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(x + 10, 13), 10, 10)


class Segmented(QFrame):
    """Segmented control. Emits the chosen index on change."""

    changed = Signal(int)

    def __init__(self, labels, icons_=None):
        super().__init__()
        self.setObjectName("Segmented")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = []
        for i, text in enumerate(labels):
            b = QPushButton(("  " + text) if icons_ and icons_[i] else text)
            b.setObjectName("SegButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if icons_ and icons_[i]:
                theme.bind_icon(b, icons_[i], "text", 14)
            self._group.addButton(b, i)
            layout.addWidget(b, 1)
            self._buttons.append(b)
        self._buttons[0].setChecked(True)
        self._group.idClicked.connect(self.changed.emit)

    def index(self) -> int:
        return self._group.checkedId()

    def set_index(self, i: int) -> None:
        self._buttons[i].setChecked(True)


class StatusPill(QFrame):
    """Coloured dot + label describing a state.

    States: idle (grey), connecting (amber, pulsing), live (green), error (red).
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("StatusPill")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_3, theme.SPACE_2, theme.SPACE_3, theme.SPACE_2)
        layout.setSpacing(theme.SPACE_2)

        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)
        layout.addWidget(self._dot, 0, Qt.AlignVCenter)

        self._label = QLabel("Not connected")
        self._label.setObjectName("StatusLabel")
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._label, 1)

        self._state = "idle"
        self._pulse_on = True
        self._pulse = QTimer(self)
        self._pulse.setInterval(650)
        self._pulse.timeout.connect(self._toggle_pulse)
        theme.bind(self, lambda w: w._paint_dot(1.0), "dot")

    def set_state(self, state: str, text: str) -> None:
        self._state = state if state in ("idle", "connecting", "live", "error") else "idle"
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
        self._paint_dot(1.0 if self._pulse_on else 0.3)

    def _paint_dot(self, opacity: float) -> None:
        c = QColor(theme.status_color(self._state))
        c.setAlphaF(opacity)
        self._dot.setStyleSheet(
            f"background-color: rgba({c.red()},{c.green()},{c.blue()},{c.alpha()});"
            " border-radius: 4px;")


# -- window chrome ---------------------------------------------------------------------

class TitleBar(QFrame):
    """Slim title bar: mark + name, a connection chip, actions, window controls."""

    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    HEIGHT = 40

    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("TitleBar")
        self.setFixedHeight(self.HEIGHT)

        self._layout = QHBoxLayout(self)
        layout = self._layout
        layout.setContentsMargins(theme.SPACE_3, 0, theme.SPACE_2, 0)
        layout.setSpacing(theme.SPACE_2)

        mark = QLabel()
        mark.setPixmap(icons.app_pixmap(18))
        mark.setFixedSize(18, 18)
        layout.addWidget(mark)

        name = QLabel(title)
        name.setObjectName("AppName")
        layout.addWidget(name)

        layout.addSpacing(theme.SPACE_3)
        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)
        layout.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._status = QLabel("")
        self._status.setObjectName("StatusLabel")
        layout.addWidget(self._status)
        self._state = "idle"
        self.set_status("idle", "Not connected")
        theme.bind(self, lambda w: w.set_status(w._state, w._status.text()), "dot")
        layout.addStretch()

        self._actions_at = layout.count()
        self._max_button = self._win_button("maximize", self.maximize_requested)
        layout.addSpacing(theme.SPACE_2)
        layout.addWidget(self._win_button("minimize", self.minimize_requested))
        layout.addWidget(self._max_button)
        layout.addWidget(self._win_button("close", self.close_requested, "WinClose"))

    def set_status(self, state: str, text: str) -> None:
        self._state = state
        color = theme.status_color(state)
        self._dot.setStyleSheet(f"background-color: {color}; border-radius: 4px;")
        self._status.setText(text)

    def add_widget(self, widget: QWidget) -> None:
        """Place a widget with the title-bar actions (left of window controls)."""
        self._layout.insertWidget(self._actions_at, widget)
        self._actions_at += 1

    def add_action(self, text: str, icon_name: str, callback) -> QPushButton:
        button = QPushButton("  " + text)
        button.setObjectName("TitleAction")
        theme.bind_icon(button, icon_name, "text_muted", 15, 1.9)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(callback)
        self._layout.insertWidget(self._actions_at, button)
        self._actions_at += 1
        return button

    def _win_button(self, icon_name: str, signal, object_name: str = "WinButton") -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        theme.bind_icon(button, icon_name, "text_muted", 16, 1.8)
        button.setFixedSize(34, 28)
        button.setCursor(Qt.ArrowCursor)
        button.clicked.connect(signal.emit)
        return button

    def set_maximized(self, maximized: bool) -> None:
        theme.bind_icon(self._max_button, "restore" if maximized else "maximize",
                        "text_muted", 16, 1.8)


# -- floating chrome over the viewport -------------------------------------------------------

class BlinkingDot(QWidget):
    """A small round LED, blinking red while active."""

    def __init__(self, diameter: int = 10):
        super().__init__()
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(diameter, diameter)
        self._radius = diameter // 2
        self._active = False
        self._lit = True
        self._timer = QTimer(self)
        self._timer.setInterval(550)
        self._timer.timeout.connect(self._tick)
        theme.bind(self, lambda w: w._paint(), "dot")

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if active:
            self._lit = True
            self._timer.start()
        else:
            self._timer.stop()
        self._paint()

    def _tick(self) -> None:
        self._lit = not self._lit
        self._paint()

    def _paint(self) -> None:
        if not self._active:
            color = theme.FILL_PRESSED
        elif self._lit:
            color = theme.DANGER
        else:
            color = "rgba(255, 69, 58, 0.30)"
        self.setStyleSheet(f"background-color: {color}; border-radius: {self._radius}px;")


class CanvasOverlay(QFrame):
    """Floating pill over the viewport: live LED + one Pause/Resume toggle."""

    pause_toggled = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setObjectName("CanvasOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._paused = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACE_3, 3, theme.SPACE_2, 3)
        layout.setSpacing(theme.SPACE_2)

        self.dot = BlinkingDot(9)
        layout.addWidget(self.dot, 0, Qt.AlignVCenter)

        self.button = QPushButton()
        self.button.setObjectName("OverlayButton")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(self.toggle)
        layout.addWidget(self.button)
        self._apply_button_state()

    @property
    def paused(self) -> bool:
        return self._paused

    def toggle(self) -> None:
        self.set_paused(not self._paused)

    def set_paused(self, paused: bool) -> None:
        if paused == self._paused:
            return
        self._paused = paused
        self._apply_button_state()
        self.pause_toggled.emit(self._paused)

    def _apply_button_state(self) -> None:
        if self._paused:
            self.button.setText("  Resume")
            theme.bind_icon(self.button, "play", "text", 13)
        else:
            self.button.setText("  Pause")
            theme.bind_icon(self.button, "pause", "text", 13)

    def set_live(self, live: bool) -> None:
        self.dot.set_active(live)


class Dock(QFrame):
    """Floating tool dock: exclusive, checkable tools. Emits the tool key."""

    tool_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("Dock")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._keys: dict[int, str] = {}
        self._buttons: dict[str, QPushButton] = {}
        self._group.idClicked.connect(lambda i: self.tool_changed.emit(self._keys[i]))

    def add_tool(self, key: str, icon_name: str, text: str, tip: str = "") -> None:
        b = QPushButton("  " + text)
        b.setObjectName("DockTool")
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        theme.bind_icon(b, icon_name, "text_muted", 16, 1.9)
        b.setIconSize(QSize(16, 16))
        b.setToolTip(tip)
        i = len(self._keys)
        self._keys[i] = key
        self._buttons[key] = b
        self._group.addButton(b, i)
        self._layout.addWidget(b)
        if i == 0:
            b.setChecked(True)

    def select(self, key: str) -> None:
        self._buttons[key].setChecked(True)
        self.tool_changed.emit(key)

    def current(self) -> str:
        return self._keys[self._group.checkedId()]


class Readout(QWidget):
    """Measurement result in the toolbar strip: a big number, then detail.

    The outer widget always takes the strip's spare width (so the Pause pill
    stays at the right edge); the pill inside fills it while there is
    something to say and is hidden otherwise.
    """

    def __init__(self):
        super().__init__()
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._pill = QFrame()
        self._pill.setObjectName("Readout")
        inner = QHBoxLayout(self._pill)
        inner.setContentsMargins(theme.SPACE_4, theme.SPACE_1 + 2, theme.SPACE_4, theme.SPACE_1 + 2)
        inner.setSpacing(theme.SPACE_3)
        self.big = QLabel("")
        self.big.setObjectName("BigReadout")
        self.small = QLabel("")
        self.small.setObjectName("StatusLabel")
        # Ignored: a long detail line is clipped, never forces the window wider.
        self.small.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        inner.addWidget(self.big)
        inner.addWidget(self.small, 1)
        outer.addWidget(self._pill, 1)       # takes the strip's spare width
        self._pill.hide()

    def show_text(self, big: str, small: str = "") -> None:
        self.big.setText(big)
        self.small.setText(small)
        self.small.setVisible(bool(small))

    def set_shown(self, shown: bool) -> None:
        self._pill.setVisible(shown)


class PowerButton(QPushButton):
    """Round power icon whose colour and tint show the sensor pipeline state.

    States (see control.py): on, off, starting, stopping, failed, offline,
    no_daemon. Starting/stopping blink the icon so a pending command reads
    as "working", not "frozen".
    """

    _ICON_ROLE = {"on": "success", "off": "text_muted", "starting": "warning",
                  "stopping": "warning", "failed": "danger",
                  "offline": "text_faint", "no_daemon": "text_faint"}

    def __init__(self):
        super().__init__()
        self.setObjectName("PowerButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(28, 28)
        self.setIconSize(QSize(16, 16))
        self._state = "offline"
        self._lit = True
        self._blink = QTimer(self)
        self._blink.setInterval(450)
        self._blink.timeout.connect(self._tick)
        theme.bind(self, lambda w: w._paint(), "icon")
        self._apply_property()

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._lit = True
        if state in ("starting", "stopping"):
            self._blink.start()
        else:
            self._blink.stop()
        self._apply_property()
        self._paint()

    def _apply_property(self) -> None:
        self.setProperty("state", self._state)
        self.style().unpolish(self)
        self.style().polish(self)

    def _tick(self) -> None:
        self._lit = not self._lit
        self._paint()

    def _paint(self) -> None:
        role = self._ICON_ROLE.get(self._state, "text_muted")
        color = getattr(theme, role.upper()) if self._lit else theme.TEXT_FAINT
        self.setIcon(icons.icon("power", color, 16, 2.1))


class IconToggleGroup(QFrame):
    """A few icon-only, mutually exclusive toggles (used for the appearance
    choice). Emits the key of the one chosen."""

    changed = Signal(str)

    def __init__(self, items):
        """items: [(key, icon_name, tooltip), ...]"""
        super().__init__()
        self.setObjectName("IconToggles")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = {}
        self._keys = {}
        for i, (key, icon_name, tip) in enumerate(items):
            b = QPushButton()
            b.setObjectName("IconToggle")
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setAccessibleName(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(30, 24)
            b.setIconSize(QSize(15, 15))
            theme.bind_icon(b, icon_name, "text_muted", 15, 1.9)
            self._group.addButton(b, i)
            self._keys[i] = key
            self._buttons[key] = b
            layout.addWidget(b)
        self._group.idClicked.connect(lambda i: self.changed.emit(self._keys[i]))

    def set_key(self, key: str) -> None:
        if key in self._buttons:
            self._buttons[key].setChecked(True)
