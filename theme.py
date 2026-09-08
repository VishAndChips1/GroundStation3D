"""Design tokens and stylesheet for the base station UI.

Everything visual is defined here: one spacing scale, one corner radius,
one palette, one font stack. Application code references these names rather
than hard-coding numbers, so the look stays consistent.
"""

import tempfile
from pathlib import Path

from PySide6.QtGui import QFontDatabase

import icons

# -- spacing scale (px) ----------------------------------------------------
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_5 = 24

# -- radius ----------------------------------------------------------------
RADIUS = 8
RADIUS_SM = 6

# -- palette ---------------------------------------------------------------
BG_APP = "#15181d"          # page background
BG_PANEL = "#191d23"        # side panel
BG_CARD = "#1e232a"         # cards sit a shade above the panel
BG_INPUT = "#12151a"        # inputs sit a shade below the card
BG_HOVER = "#272d36"
BORDER = "rgba(255, 255, 255, 0.08)"
BORDER_STRONG = "rgba(255, 255, 255, 0.14)"

TEXT = "#e7eaf0"            # primary text
TEXT_MUTED = "#9aa3b2"      # field labels, secondary text
TEXT_FAINT = "#6b7482"      # section headers, hints

ACCENT = "#3d8bfd"
ACCENT_HOVER = "#5a9dff"
ACCENT_PRESSED = "#2f74d8"

SUCCESS = "#2ea36a"         # reserved for completed states only
WARNING = "#d99b32"
DANGER = "#d9534f"
NEUTRAL_DOT = "#6b7482"

# -- viewport --------------------------------------------------------------
VIEWPORT_BG = (0.043, 0.051, 0.063)       # edges (vignette outer)
VIEWPORT_BG_CENTER = (0.094, 0.106, 0.125)  # centre (vignette inner)
GRID_COLOR = "#5c6675"
GRID_OPACITY = 0.35
AXIS_X = (0.85, 0.42, 0.42)   # muted red
AXIS_Y = (0.48, 0.78, 0.52)   # muted green
AXIS_Z = (0.45, 0.62, 0.92)   # muted blue

# -- typography ------------------------------------------------------------
PREFERRED_FONTS = ("Inter", "IBM Plex Sans", "Roboto",
                   "Segoe UI Variable Text", "Segoe UI")
FONT_STACK = '"Inter", "IBM Plex Sans", "Roboto", "Segoe UI", sans-serif'

# One type scale, smallest to largest.
FONT_SIZE_HINT = 12      # helper text under a control
FONT_SIZE_SECTION = 11   # CONNECTION / DISPLAY / VIEW ...
FONT_SIZE_LABEL = 13     # field captions
FONT_SIZE_BASE = 14      # inputs, buttons, checkboxes, values
FONT_SIZE_SUBTITLE = 13
FONT_SIZE_TITLE = 21


def resolve_font() -> str:
    """First preferred font actually installed, for reporting/diagnostics."""
    available = set(QFontDatabase.families())
    for name in PREFERRED_FONTS:
        if name in available:
            return name
    return "system default"


_STYLESHEET_TEMPLATE = f"""
/* No background-color here on purpose. A blanket QWidget background
   forces every plain layout-wrapper widget (an icon+label row, a
   Port/Interval pair, anything grouped with a bare QWidget()) to paint
   its own opaque rectangle -- a different, wrong shade against whatever
   card it sits inside. Only named, actually-visible surfaces (window,
   title bar, panel, card, pill) get an explicit background below;
   everything else stays transparent and shows its parent through. */
QWidget {{
    color: {TEXT};
    font-family: {FONT_STACK};
    font-size: {FONT_SIZE_BASE}px;
}}

QMainWindow, QMainWindow > QWidget {{
    background-color: {BG_APP};
}}

/* -- shell ------------------------------------------------------------- */

QFrame#TitleBar {{
    background-color: {BG_PANEL};
    border-bottom: 1px solid {BORDER};
}}

QLabel#AppName {{
    font-size: {FONT_SIZE_BASE}px;
    font-weight: 600;
    color: {TEXT};
}}

QPushButton#WinButton {{
    background: transparent;
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 0px;
}}
QPushButton#WinButton:hover {{ background-color: {BG_HOVER}; }}
QPushButton#WinClose:hover {{ background-color: {DANGER}; }}

QFrame#SidePanel {{
    background-color: {BG_PANEL};
    border-left: 1px solid {BORDER};
}}

/* -- typography -------------------------------------------------------- */

QLabel#TitleLabel {{
    font-size: {FONT_SIZE_TITLE}px;
    font-weight: 700;
    color: {TEXT};
}}

QLabel#SubtitleLabel {{
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_SUBTITLE}px;
}}

QLabel#SectionLabel {{
    color: {TEXT_FAINT};
    font-size: {FONT_SIZE_SECTION}px;
    font-weight: 700;
    letter-spacing: 1.2px;
}}

QLabel#FieldLabel {{
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_LABEL}px;
    font-weight: 500;
}}

QLabel#StatCaption {{
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_LABEL}px;
    font-weight: 500;
}}

QLabel#StatValue {{
    color: {TEXT};
    font-size: {FONT_SIZE_BASE}px;
    font-weight: 600;
}}

QLabel#StatusLabel {{
    color: {TEXT_MUTED};
    font-size: {FONT_SIZE_LABEL}px;
}}

QLabel#HintLabel {{
    color: {TEXT_FAINT};
    font-size: {FONT_SIZE_HINT}px;
}}

/* -- cards ------------------------------------------------------------- */

QFrame#Card {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}

QFrame#StatusPill {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
}}

/* Floating pill over the 3D viewport -- a HUD control, not a panel row,
   so it gets its own translucent surface rather than the card colour. */
QFrame#CanvasOverlay {{
    background-color: rgba(20, 23, 28, 0.88);
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS}px;
}}
QPushButton#OverlayButton {{
    background: transparent;
    border: none;
    padding: {SPACE_1}px {SPACE_2}px;
    font-weight: 600;
}}
QPushButton#OverlayButton:hover {{
    background-color: rgba(255, 255, 255, 0.10);
    border-radius: {RADIUS_SM}px;
}}
QPushButton#OverlayButton:pressed {{
    background-color: rgba(255, 255, 255, 0.16);
}}

/* -- buttons ----------------------------------------------------------- */

/* Secondary / neutral: the default for every non-primary action. */
QPushButton {{
    background-color: transparent;
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS}px;
    padding: {SPACE_2}px {SPACE_3}px;
    font-size: {FONT_SIZE_BASE}px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {BG_HOVER};
    border-color: {TEXT_FAINT};
}}
QPushButton:pressed {{ background-color: {BG_INPUT}; }}
QPushButton:disabled {{
    color: {TEXT_FAINT};
    border-color: {BORDER};
    background-color: transparent;
}}

/* Primary: the one accent colour, for the main action only. */
QPushButton#PrimaryButton {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background-color: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton#PrimaryButton:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#PrimaryButton:disabled {{
    background-color: {BG_HOVER};
    border-color: {BORDER};
    color: {TEXT_FAINT};
}}

/* -- inputs ------------------------------------------------------------ */

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: {SPACE_2}px {SPACE_3}px;
    font-size: {FONT_SIZE_BASE}px;
    font-weight: 500;
    selection-background-color: {ACCENT};
    color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border-color: {BORDER_STRONG};
}}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled {{
    color: {TEXT_FAINT};
    background-color: {BG_PANEL};
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    border: none;
    width: 22px;
}}
/* A real icon file, not a CSS border-triangle: Qt's QSS engine paints that
   trick as a small solid rectangle instead of a triangle on this platform. */
QComboBox::down-arrow {{
    image: url({{chevron_down}});
    width: 10px;
    height: 10px;
    margin-right: {SPACE_2}px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS}px;
    selection-background-color: {ACCENT};
    padding: {SPACE_1}px;
    outline: none;
}}

/* Stack the two steppers explicitly, or Qt overlaps them into one blob. */
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    background-color: transparent;
    border: none;
    width: 18px;
    margin-right: 2px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    background-color: transparent;
    border: none;
    width: 18px;
    margin-right: 2px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({{chevron_up}});
    width: 9px;
    height: 9px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({{chevron_down}});
    width: 9px;
    height: 9px;
}}

/* -- slider ------------------------------------------------------------ */

QSlider::groove:horizontal {{
    height: 4px;
    background: {BG_INPUT};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: #ffffff;
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 7px;
    border: 2px solid {ACCENT};
}}
QSlider::handle:horizontal:hover {{ border-color: {ACCENT_HOVER}; }}

/* -- checkbox ---------------------------------------------------------- */

QCheckBox {{
    spacing: {SPACE_2}px;
    color: {TEXT};
    font-size: {FONT_SIZE_BASE}px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: {RADIUS_SM}px;
    border: 1px solid {BORDER_STRONG};
    background: {BG_INPUT};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

/* -- scroll area ------------------------------------------------------- */

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: {BG_HOVER};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_FAINT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QToolTip {{
    background-color: {BG_CARD};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_SM}px;
    padding: {SPACE_1}px {SPACE_2}px;
}}
"""

_stylesheet_cache: str | None = None


def get_stylesheet() -> str:
    """Build the full stylesheet, including rendered chevron icons.

    Call only after a QApplication exists -- QPixmap/QPainter (used to
    render the icons) hard-crash the process otherwise. Cached after the
    first call.
    """
    global _stylesheet_cache
    if _stylesheet_cache is None:
        cache_dir = Path(tempfile.gettempdir()) / "survey-rig-base-station-icons"
        cache_dir.mkdir(exist_ok=True)
        chevron_down = icons.write_to_file(
            "chevron-down", TEXT_MUTED, 20, cache_dir / "chevron_down.png",
            stroke=2.4)
        chevron_up = icons.write_to_file(
            "chevron-up", TEXT_MUTED, 20, cache_dir / "chevron_up.png",
            stroke=2.4)
        # Plain substring replacement, not .format(): the f-string above
        # already collapsed every QSS rule's {{ }} down to single braces,
        # so a second .format() pass would try to parse each rule body as
        # a format field and fail.
        _stylesheet_cache = (_STYLESHEET_TEMPLATE
                              .replace("{chevron_down}", chevron_down)
                              .replace("{chevron_up}", chevron_up))
    return _stylesheet_cache
