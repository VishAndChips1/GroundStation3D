"""Dark theme stylesheet for the base station UI.

Kept separate from main.py so the look can be tweaked without touching
application logic.
"""

# Palette
BG_DARKEST = "#14161a"
BG_DARK = "#1a1d23"
BG_PANEL = "#21252c"
BG_RAISED = "#2a2f38"
BG_HOVER = "#333945"
BORDER = "#333945"
TEXT = "#e6e9ef"
TEXT_DIM = "#8b93a3"
ACCENT = "#3d8bfd"
ACCENT_HOVER = "#5a9dff"
ACCENT_PRESSED = "#2b6fd4"
SUCCESS = "#2ea36a"
SUCCESS_HOVER = "#37bd7c"
DANGER = "#d9534f"

# Viewport background (VTK wants 0-1 floats)
VIEWPORT_BG = (0.055, 0.063, 0.078)
VIEWPORT_BG_TOP = (0.086, 0.098, 0.122)

STYLESHEET = f"""
QWidget {{
    background-color: {BG_DARK};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}

QFrame#SidePanel {{
    background-color: {BG_PANEL};
    border-left: 1px solid {BORDER};
}}

QLabel#TitleLabel {{
    font-size: 17px;
    font-weight: 600;
    color: {TEXT};
    padding: 0px;
}}

QLabel#SubtitleLabel {{
    color: {TEXT_DIM};
    font-size: 12px;
}}

QLabel#SectionLabel {{
    color: {TEXT_DIM};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
    padding-top: 4px;
}}

QLabel#StatCaption {{
    color: {TEXT_DIM};
}}

QLabel#StatValue {{
    color: {TEXT};
    font-weight: 600;
}}

QLabel#StatusLabel {{
    color: {TEXT_DIM};
    font-size: 12px;
}}

QFrame#Card {{
    background-color: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

QFrame#Divider {{
    background-color: {BORDER};
    max-height: 1px;
    border: none;
}}

QPushButton {{
    background-color: {BG_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {BG_HOVER};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {BG_DARKEST};
}}
QPushButton:disabled {{
    color: {TEXT_DIM};
    border-color: {BORDER};
    background-color: {BG_PANEL};
}}

QPushButton#PrimaryButton {{
    background-color: {ACCENT};
    border: none;
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#PrimaryButton:pressed {{ background-color: {ACCENT_PRESSED}; }}

QPushButton#SuccessButton {{
    background-color: {SUCCESS};
    border: none;
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#SuccessButton:hover {{ background-color: {SUCCESS_HOVER}; }}

QLineEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_DARKEST};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT};
}}

QComboBox {{
    background-color: {BG_DARKEST};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 10px;
}}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 8px;
    selection-background-color: {ACCENT};
    padding: 4px;
    outline: none;
}}

QSlider::groove:horizontal {{
    height: 5px;
    background: {BG_DARKEST};
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: #ffffff;
    width: 15px;
    height: 15px;
    margin: -6px 0;
    border-radius: 8px;
    border: 2px solid {ACCENT};
}}
QSlider::handle:horizontal:hover {{
    background: {ACCENT_HOVER};
    border-color: #ffffff;
}}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1px solid {BORDER};
    background: {BG_DARKEST};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

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
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QToolTip {{
    background-color: {BG_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
}}
"""
