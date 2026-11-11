"""Design tokens and stylesheet for the base station UI.

Direction: Apple's dark appearance, adapted for a desktop instrument.

  * Restraint -- one accent colour, used for the thing you can act on and
    for selection. Everything else is greys.
  * Depth by surface, not by outline -- window < sidebar < grouped section <
    control fill. Borders are hairlines or absent.
  * System typography -- the platform UI font, a short named type scale,
    weight (not size) for emphasis, and tracking that tightens as text
    grows and opens up as it shrinks.
  * Floating chrome (the tool dock, the pause pill) sits over the viewport
    on a translucent surface with a bright top edge, so the 3D scene stays
    the hero.

Application code references these names rather than hard-coding values.
"""

import tempfile
from pathlib import Path

from PySide6.QtGui import QFontDatabase

from basestation.ui import icons

# -- spacing (px): 4-pt grid ------------------------------------------------
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_5 = 24
SPACE_6 = 32

# -- radius -----------------------------------------------------------------
RADIUS_SM = 6      # chips, small buttons
RADIUS = 8         # controls
RADIUS_GROUP = 10  # grouped sections
RADIUS_FLOAT = 14  # floating chrome

# -- palettes ---------------------------------------------------------------
# Every colour the UI uses lives in one of these two dicts. set_mode() copies
# the active one into this module's globals, so application code just reads
# theme.TEXT, theme.BG_CARD ... and gets whichever appearance is current.

PALETTES = {
    "dark": dict(           # Apple dark appearance
        BG_APP="#1c1c1e", BG_PANEL="#18181a", BG_CARD="#262628", BG_INPUT="#1c1c1e",
        BG_HOVER="#323235", MENU_BG="#2c2c2e",
        FILL="rgba(118, 118, 128, 24%)", FILL_HOVER="rgba(118, 118, 128, 36%)",
        FILL_PRESSED="rgba(118, 118, 128, 50%)", FILL_DISABLED="rgba(118, 118, 128, 12%)",
        SEGMENT_ON="#636366", SEPARATOR="rgba(84, 84, 88, 55%)",
        BORDER="rgba(255, 255, 255, 8%)", BORDER_STRONG="rgba(255, 255, 255, 14%)",
        TEXT="#f5f5f7", TEXT_MUTED="#a1a1a6", TEXT_FAINT="#6e6e73",
        ACCENT="#0a84ff", ACCENT_HOVER="#409cff", ACCENT_PRESSED="#0071e3",
        BTN_BORDER="rgba(255, 255, 255, 11%)", ACCENT_BORDER="rgba(10, 132, 255, 60%)",
        ACCENT_TINT="rgba(10, 132, 255, 22%)", ACCENT_TINT_HOVER="rgba(10, 132, 255, 32%)",
        ACCENT_TINT_PRESSED="rgba(10, 132, 255, 44%)", ACCENT_TEXT="#409cff",
        SUCCESS="#30d158", WARNING="#ff9f0a", DANGER="#ff453a", NEUTRAL_DOT="#6e6e73",
        SUCCESS_TINT="rgba(48, 209, 88, 20%)", WARNING_TINT="rgba(255, 159, 10, 20%)",
        DANGER_TINT="rgba(255, 69, 58, 20%)",
        FLOAT_BG="rgba(38, 38, 40, 86%)", FLOAT_EDGE="rgba(255, 255, 255, 12%)",
        FLOAT_TOP="rgba(255, 255, 255, 22%)", FLOAT_HOVER="rgba(255, 255, 255, 9%)",
        FLOAT_PRESSED="rgba(255, 255, 255, 17%)", SCROLL="rgba(255, 255, 255, 18%)",
        SCROLL_HOVER="rgba(255, 255, 255, 32%)",
        VIEWPORT_BG=(0.043, 0.051, 0.063), VIEWPORT_BG_CENTER=(0.094, 0.106, 0.125),
        VIEWPORT_VIGNETTE=0.46, GRID_COLOR="#5c6675", GRID_OPACITY=0.35,
        MEASURE_COLOR="#ffd60a",
    ),
    "light": dict(          # Apple light appearance
        BG_APP="#f5f5f7", BG_PANEL="#ebebf0", BG_CARD="#ffffff", BG_INPUT="#e5e5ea",
        BG_HOVER="#e8e8ed", MENU_BG="#ffffff",
        FILL="rgba(120, 120, 128, 13%)", FILL_HOVER="rgba(120, 120, 128, 21%)",
        FILL_PRESSED="rgba(120, 120, 128, 31%)", FILL_DISABLED="rgba(120, 120, 128, 7%)",
        SEGMENT_ON="#ffffff", SEPARATOR="rgba(60, 60, 67, 18%)",
        BORDER="rgba(0, 0, 0, 8%)", BORDER_STRONG="rgba(0, 0, 0, 15%)",
        TEXT="#1d1d1f", TEXT_MUTED="#6e6e73", TEXT_FAINT="#86868b",
        ACCENT="#007aff", ACCENT_HOVER="#2b8cff", ACCENT_PRESSED="#0062cc",
        BTN_BORDER="rgba(0, 0, 0, 16%)", ACCENT_BORDER="rgba(0, 122, 255, 50%)",
        ACCENT_TINT="rgba(0, 122, 255, 13%)", ACCENT_TINT_HOVER="rgba(0, 122, 255, 21%)",
        ACCENT_TINT_PRESSED="rgba(0, 122, 255, 31%)", ACCENT_TEXT="#0066d6",
        SUCCESS="#34c759", WARNING="#ff9500", DANGER="#ff3b30", NEUTRAL_DOT="#8e8e93",
        SUCCESS_TINT="rgba(52, 199, 89, 20%)", WARNING_TINT="rgba(255, 149, 0, 20%)",
        DANGER_TINT="rgba(255, 59, 48, 18%)",
        FLOAT_BG="rgba(255, 255, 255, 84%)", FLOAT_EDGE="rgba(0, 0, 0, 10%)",
        FLOAT_TOP="rgba(255, 255, 255, 95%)", FLOAT_HOVER="rgba(0, 0, 0, 6%)",
        FLOAT_PRESSED="rgba(0, 0, 0, 12%)", SCROLL="rgba(0, 0, 0, 18%)",
        SCROLL_HOVER="rgba(0, 0, 0, 32%)",
        VIEWPORT_BG=(0.82, 0.83, 0.86), VIEWPORT_BG_CENTER=(0.93, 0.94, 0.96),
        VIEWPORT_VIGNETTE=0.88, GRID_COLOR="#7d8696", GRID_OPACITY=0.45,
        MEASURE_COLOR="#e07b00",
    ),
}

# Identical in both appearances.
AXIS_X = (1.00, 0.27, 0.23)
AXIS_Y = (0.19, 0.82, 0.35)
AXIS_Z = (0.04, 0.52, 1.00)

MODES = ("light", "dark", "system")
_mode = "dark"          # what the user chose
_resolved = "dark"      # what is actually applied


def system_prefers_dark() -> bool:
    """Ask the OS (via Qt) whether it is in dark mode; dark if unknown."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
    except (AttributeError, RuntimeError):
        return True
    return scheme != Qt.ColorScheme.Light


def set_mode(mode: str) -> str:
    """Choose "light", "dark" or "system"; returns the appearance applied."""
    global _mode, _resolved
    if mode not in MODES:
        mode = "system"
    _mode = mode
    _resolved = ("dark" if system_prefers_dark() else "light") if mode == "system" else mode
    globals().update(PALETTES[_resolved])
    return _resolved


def mode() -> str:
    return _mode


def resolved() -> str:
    return _resolved


globals().update(PALETTES["dark"])

# -- theme-aware icons -------------------------------------------------------
# Icons are pre-rendered in one colour, so a theme switch must re-render them.
# bind_icon / bind_pixmap render now and remember how, so refresh_bindings()
# can redo them. Registered by weak reference: a destroyed widget just drops out.

import weakref

_bindings: dict = {}


def bind(widget, fn, key: str = "icon") -> None:
    """Call fn(widget) now, and again after every theme change."""
    k = (id(widget), key)
    _bindings[k] = (weakref.ref(widget, lambda _r, k=k: _bindings.pop(k, None)), fn)
    fn(widget)


def refresh_bindings() -> None:
    for ref, fn in list(_bindings.values()):
        w = ref()
        if w is not None:
            try:
                fn(w)
            except RuntimeError:
                pass    # underlying C++ object already deleted


def _role(role: str) -> str:
    return globals()[role.upper()]


def bind_icon(button, name: str, role: str = "text", size: int = 15,
              stroke: float = 2.0) -> None:
    bind(button, lambda b: b.setIcon(icons.icon(name, _role(role), size, stroke)))


def bind_pixmap(label, name: str, role: str = "text", size: int = 16,
                stroke: float = 2.0) -> None:
    bind(label, lambda lb: lb.setPixmap(icons.pixmap(name, _role(role), size, stroke)))


def status_color(state: str) -> str:
    return {"idle": NEUTRAL_DOT, "connecting": WARNING,
            "live": SUCCESS, "error": DANGER}.get(state, NEUTRAL_DOT)


# -- typography -------------------------------------------------------------
PREFERRED_FONTS = ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Inter")
FONT_STACK = ('"Segoe UI Variable Text", "Segoe UI", "SF Pro Text", '
              '".AppleSystemUIFont", "Inter", sans-serif')

# Named type scale (px). Nothing in the app is set below FOOTNOTE except the
# uppercase CAPTION, which compensates with weight and open tracking.
FONT_LARGE_TITLE = 26    # 700, -0.4px  -- panel title
FONT_TITLE = 20          # 600, -0.2px  -- dialog title, big readouts
FONT_HEADLINE = 15       # 600          -- item names
FONT_BODY = 14           # 400/500      -- controls, values
FONT_CALLOUT = 13        # 400/500      -- captions, secondary text
FONT_FOOTNOTE = 12       # 400          -- hints and helper text
FONT_CAPTION = 11        # 600, +0.7px  -- UPPERCASE section labels

# Legacy aliases used elsewhere.
FONT_SIZE_HINT = FONT_FOOTNOTE
FONT_SIZE_SECTION = FONT_CAPTION
FONT_SIZE_LABEL = FONT_CALLOUT
FONT_SIZE_BASE = FONT_BODY
FONT_SIZE_SUBTITLE = FONT_CALLOUT
FONT_SIZE_TITLE = FONT_LARGE_TITLE


def resolve_font() -> str:
    """First preferred font actually installed, for reporting/diagnostics."""
    available = set(QFontDatabase.families())
    for name in PREFERRED_FONTS:
        if name in available:
            return name
    return "system default"


def _template() -> str:
    return f"""
/* Nothing here sets a background on bare QWidget: plain layout wrappers
   must stay transparent so they show whatever surface they sit on. Only
   named, visible surfaces get a fill below. */
QWidget {{
    color: {TEXT};
    font-family: {FONT_STACK};
    font-size: {FONT_BODY}px;
}}

QMainWindow, QMainWindow > QWidget {{ background-color: {BG_APP}; }}
QDialog {{ background-color: {BG_APP}; }}

/* -- shell ------------------------------------------------------------- */

QFrame#TitleBar {{
    background-color: {BG_PANEL};
    border-bottom: 1px solid {SEPARATOR};
}}
QLabel#AppName {{
    font-size: {FONT_CALLOUT}px;
    font-weight: 600;
    color: {TEXT_MUTED};
    letter-spacing: 0.2px;
}}
QPushButton#WinButton {{
    background: transparent; border: none; border-radius: {RADIUS_SM}px; padding: 0px;
}}
QPushButton#WinButton:hover {{ background-color: {FILL_HOVER}; }}
QPushButton#WinButton:pressed {{ background-color: {FILL_PRESSED}; }}
QPushButton#WinClose {{
    background: transparent; border: none; border-radius: {RADIUS_SM}px; padding: 0px;
}}
QPushButton#WinClose:hover {{ background-color: {DANGER}; }}

QPushButton#TitleAction {{
    background: transparent; border: none; border-radius: {RADIUS_SM}px;
    padding: {SPACE_1}px {SPACE_3}px; color: {TEXT_MUTED};
    font-size: {FONT_CALLOUT}px; font-weight: 500;
}}
QPushButton#TitleAction:hover {{ background-color: {FILL_HOVER}; color: {TEXT}; }}
QPushButton#TitleAction:pressed {{ background-color: {FILL_PRESSED}; }}

QFrame#SidePanel {{
    background-color: {BG_PANEL};
    border-left: 1px solid {SEPARATOR};
}}

/* -- typography -------------------------------------------------------- */

QLabel#LargeTitle {{
    font-size: {FONT_LARGE_TITLE}px; font-weight: 700; letter-spacing: -0.4px;
}}
QLabel#TitleLabel {{
    font-size: {FONT_TITLE}px; font-weight: 600; letter-spacing: -0.2px;
}}
QLabel#SubtitleLabel {{ color: {TEXT_MUTED}; font-size: {FONT_CALLOUT}px; }}
QLabel#Headline {{ font-size: {FONT_HEADLINE}px; font-weight: 600; }}

QLabel#SectionLabel {{
    color: {TEXT_FAINT}; font-size: {FONT_CAPTION}px; font-weight: 600;
    letter-spacing: 0.7px;
}}
QLabel#FieldLabel {{ color: {TEXT}; font-size: {FONT_BODY}px; }}
QLabel#StatCaption {{ color: {TEXT}; font-size: {FONT_BODY}px; }}
QLabel#StatValue {{
    color: {TEXT_MUTED}; font-size: {FONT_BODY}px; font-weight: 500;
}}
QLabel#StatusLabel {{ color: {TEXT_MUTED}; font-size: {FONT_CALLOUT}px; }}
QLabel#HintLabel {{ color: {TEXT_FAINT}; font-size: {FONT_FOOTNOTE}px; }}
QLabel#BigReadout {{
    font-size: {FONT_TITLE}px; font-weight: 600; letter-spacing: -0.2px;
}}

/* -- grouped sections (Apple "inset grouped" lists) -------------------- */

QFrame#Group {{
    background-color: {BG_CARD};
    border-radius: {RADIUS_GROUP}px;
}}
QFrame#Hairline {{ background-color: {SEPARATOR}; border: none; }}
QFrame#Card {{
    background-color: {BG_CARD};
    border-radius: {RADIUS_GROUP}px;
}}
QFrame#StatusPill {{
    background-color: {FILL};
    border-radius: {RADIUS}px;
}}

/* -- floating chrome --------------------------------------------------- */

QFrame#CanvasBar {{
    background-color: {BG_APP};
    border-bottom: 1px solid {SEPARATOR};
}}
QFrame#Dock, QFrame#CanvasOverlay, QFrame#Readout {{
    background-color: {FILL};
    border: 1px solid {BTN_BORDER};
    border-radius: {RADIUS_GROUP}px;
}}
QPushButton#DockTool {{
    background: transparent; border: 1px solid transparent; border-radius: 7px;
    padding: {SPACE_1 + 2}px {SPACE_3}px; color: {TEXT_MUTED};
    font-size: {FONT_CALLOUT}px; font-weight: 500;
}}
QPushButton#DockTool:hover {{ background-color: {FILL_HOVER}; color: {TEXT}; }}
QPushButton#DockTool:pressed {{ background-color: {FILL_PRESSED}; }}
QPushButton#DockTool:checked {{
    background-color: {ACCENT_TINT}; color: {ACCENT_TEXT}; font-weight: 600;
    border: 1px solid {ACCENT_BORDER};
}}
QPushButton#OverlayButton {{
    background: transparent; border: 1px solid transparent; border-radius: 7px;
    padding: {SPACE_1}px {SPACE_2}px; font-size: {FONT_CALLOUT}px; font-weight: 600;
}}
QPushButton#OverlayButton:hover {{ background-color: {FILL_HOVER}; }}
QPushButton#OverlayButton:pressed {{ background-color: {FILL_PRESSED}; }}

/* -- buttons ----------------------------------------------------------- */

/* Default: the "gray" fill button. No outline; the fill is the affordance. */
QPushButton {{
    background-color: {FILL};
    color: {TEXT};
    border: 1px solid {BTN_BORDER};
    border-radius: {RADIUS}px;
    padding: {SPACE_2 - 1}px {SPACE_4 - 1}px;
    font-size: {FONT_BODY}px;
    font-weight: 500;
}}
QPushButton:hover {{ background-color: {FILL_HOVER}; }}
QPushButton:pressed {{ background-color: {FILL_PRESSED}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background-color: {FILL_DISABLED}; }}
QPushButton:focus {{ outline: none; }}

QPushButton#PrimaryButton {{
    background-color: {ACCENT}; color: #ffffff; font-weight: 600;
    border: 1px solid {ACCENT};
}}
QPushButton#PrimaryButton:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#PrimaryButton:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#PrimaryButton:disabled {{
    background-color: {FILL_DISABLED}; color: {TEXT_FAINT};
}}

QPushButton#TintedButton {{
    background-color: {ACCENT_TINT}; color: {ACCENT_TEXT}; font-weight: 600;
    border: 1px solid {ACCENT_BORDER};
}}
QPushButton#TintedButton:hover {{ background-color: {ACCENT_TINT_HOVER}; }}
QPushButton#TintedButton:pressed {{ background-color: {ACCENT_TINT_PRESSED}; }}

QPushButton#DestructiveButton {{
    background-color: {DANGER_TINT}; color: {DANGER}; font-weight: 600;
    border: 1px solid {DANGER};
}}
QPushButton#DestructiveButton:hover {{ background-color: rgba(255,69,58,26%); }}
QPushButton#DestructiveButton:pressed {{ background-color: rgba(255,69,58,38%); }}

QPushButton#PlainButton {{
    background: transparent; color: {ACCENT_TEXT}; border: 1px solid transparent;
    padding: {SPACE_1}px {SPACE_2}px;
    font-weight: 500;
}}
QPushButton#PlainButton:hover {{ background-color: {ACCENT_TINT}; }}
QPushButton#PlainButton:pressed {{ background-color: {ACCENT_TINT_HOVER}; }}
QPushButton#PlainButton:disabled {{ color: {TEXT_FAINT}; background: transparent; }}

/* -- icon toggles (appearance) ------------------------------------------ */

QFrame#IconToggles {{ background-color: {FILL}; border: 1px solid {BTN_BORDER}; border-radius: 8px; }}
QPushButton#IconToggle {{
    background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 0px;
}}
QPushButton#IconToggle:hover {{ background-color: {FILL_HOVER}; }}
QPushButton#IconToggle:checked {{ background-color: {SEGMENT_ON}; border: 1px solid {BTN_BORDER}; }}

/* -- toolbar icon button (Home) ---------------------------------------------- */

QPushButton#BarButton {{
    background-color: {FILL}; border: 1px solid {BTN_BORDER};
    border-radius: {RADIUS_GROUP}px; padding: 0px;
}}
QPushButton#BarButton:hover {{ background-color: {FILL_HOVER}; }}
QPushButton#BarButton:pressed {{ background-color: {FILL_PRESSED}; }}

/* -- power button (title bar) --------------------------------------------- */

QPushButton#PowerButton {{
    background-color: {FILL}; border: none; border-radius: 14px; padding: 0px;
}}
QPushButton#PowerButton:hover {{ background-color: {FILL_HOVER}; }}
QPushButton#PowerButton:pressed {{ background-color: {FILL_PRESSED}; }}
QPushButton#PowerButton[state="on"] {{ background-color: {SUCCESS_TINT}; }}
QPushButton#PowerButton[state="starting"], QPushButton#PowerButton[state="stopping"] {{
    background-color: {WARNING_TINT};
}}
QPushButton#PowerButton[state="failed"] {{ background-color: {DANGER_TINT}; }}

/* -- segmented control -------------------------------------------------- */

QFrame#Segmented {{ background-color: {FILL}; border: 1px solid {BTN_BORDER}; border-radius: 9px; }}
QPushButton#SegButton {{
    background: transparent; color: {TEXT_MUTED}; border: 1px solid transparent;
    border-radius: 7px; padding: {SPACE_1 + 2}px {SPACE_3}px;
    font-size: {FONT_CALLOUT}px; font-weight: 500;
}}
QPushButton#SegButton:hover {{ color: {TEXT}; }}
QPushButton#SegButton:checked {{
    background-color: {SEGMENT_ON}; color: {TEXT}; font-weight: 600;
    border: 1px solid {BTN_BORDER};
}}

/* -- inputs ------------------------------------------------------------ */

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {FILL};
    border: 1px solid {BTN_BORDER};
    border-radius: {RADIUS}px;
    padding: {SPACE_2 - 1}px {SPACE_3}px;
    font-size: {FONT_BODY}px;
    selection-background-color: {ACCENT};
    color: {TEXT};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    background-color: {FILL_HOVER};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {TEXT_FAINT}; background-color: {FILL_DISABLED};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    border: none; width: 22px;
}}
/* A real icon file: QSS paints the CSS border-triangle trick as a rectangle. */
QComboBox::down-arrow {{
    image: url({{chevron_down}}); width: 10px; height: 10px; margin-right: {SPACE_2}px;
}}
QComboBox QAbstractItemView {{
    background-color: {MENU_BG};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS}px;
    selection-background-color: {ACCENT};
    padding: {SPACE_1}px;
    outline: none;
}}

QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    background-color: transparent; border: none; width: 18px; margin-right: 2px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    background-color: transparent; border: none; width: 18px; margin-right: 2px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({{chevron_up}}); width: 9px; height: 9px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({{chevron_down}}); width: 9px; height: 9px;
}}

/* -- slider ------------------------------------------------------------ */

QSlider::groove:horizontal {{ height: 4px; background: {FILL_PRESSED}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: #ffffff; width: 20px; height: 20px; margin: -8px 0;
    border-radius: 10px; border: 1px solid rgba(0,0,0,30%);
}}
QSlider::handle:horizontal:hover {{ background: #f2f2f7; }}

/* -- scroll ------------------------------------------------------------ */

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {SCROLL}; border-radius: 3px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {SCROLL_HOVER}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QToolTip {{
    background-color: {MENU_BG}; color: {TEXT};
    border: 1px solid {BORDER_STRONG}; border-radius: {RADIUS_SM}px;
    padding: {SPACE_1}px {SPACE_2}px; font-size: {FONT_FOOTNOTE}px;
}}

QMenu {{
    background-color: {MENU_BG}; border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS}px; padding: {SPACE_1}px;
}}
QMenu::item {{ padding: {SPACE_2 - 1}px {SPACE_4}px; border-radius: {RADIUS_SM}px; }}
QMenu::item:selected {{ background-color: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {SEPARATOR}; margin: {SPACE_1}px {SPACE_2}px; }}
"""

_stylesheet_cache: dict = {}


def get_stylesheet() -> str:
    """Stylesheet for the currently applied appearance (cached per appearance).

    Call only after a QApplication exists -- QPixmap/QPainter (used to
    render the chevron icons) hard-crash the process otherwise.
    """
    key = _resolved
    if key not in _stylesheet_cache:
        cache_dir = Path(tempfile.gettempdir()) / "survey-rig-base-station-icons"
        cache_dir.mkdir(exist_ok=True)
        down = icons.write_to_file(
            "chevron-down", TEXT_MUTED, 20, cache_dir / f"chevron_down_{key}.png", stroke=2.4)
        up = icons.write_to_file(
            "chevron-up", TEXT_MUTED, 20, cache_dir / f"chevron_up_{key}.png", stroke=2.4)
        # Plain substring replacement, not .format(): the f-string already
        # collapsed every rule's {{ }} to single braces.
        _stylesheet_cache[key] = (_template()
                                   .replace("{chevron_down}", down)
                                   .replace("{chevron_up}", up))
    return _stylesheet_cache[key]
