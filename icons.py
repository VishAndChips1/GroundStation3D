"""Monoline icons, drawn from inline SVG.

Lucide-style geometry (24x24 box, 2px stroke, round caps/joins) written out
directly so the app needs no icon package or asset files. Icons are
rendered at the requested colour and size and cached.
"""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# `{c}` is substituted with the requested colour, for filled shapes.
_ICONS: dict[str, str] = {
    "wifi": (
        '<path d="M12 20h.01"/>'
        '<path d="M2 8.82a15 15 0 0 1 20 0"/>'
        '<path d="M5 12.86a10 10 0 0 1 14 0"/>'
        '<path d="M8.5 16.43a5 5 0 0 1 7 0"/>'
    ),
    "server": (
        '<rect x="2" y="3" width="20" height="8" rx="2"/>'
        '<rect x="2" y="13" width="20" height="8" rx="2"/>'
        '<path d="M6 7h.01M6 17h.01"/>'
    ),
    "hash": (
        '<path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/>'
    ),
    "clock": (
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M12 7v5l3.5 2"/>'
    ),
    "search": (
        '<circle cx="11" cy="11" r="7"/>'
        '<path d="m20 20-3.7-3.7"/>'
    ),
    "sliders": (
        '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/>'
        '<path d="M1 14h6M9 8h6M17 16h6"/>'
    ),
    "layers": (
        '<path d="M12 2 2 7l10 5 10-5-10-5z"/>'
        '<path d="m2 17 10 5 10-5"/>'
        '<path d="m2 12 10 5 10-5"/>'
    ),
    "eye": (
        '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/>'
        '<circle cx="12" cy="12" r="3"/>'
    ),
    "crosshair": (
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M22 12h-4M6 12H2M12 6V2M12 22v-4"/>'
    ),
    "grid": (
        '<rect x="3" y="3" width="18" height="18" rx="2"/>'
        '<path d="M3 9h18M3 15h18M9 3v18M15 3v18"/>'
    ),
    "camera": (
        '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 '
        '2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>'
        '<circle cx="12" cy="13" r="3.2"/>'
    ),
    "download": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
        '<path d="m7 10 5 5 5-5"/>'
        '<path d="M12 15V3"/>'
    ),
    "image": (
        '<rect x="3" y="3" width="18" height="18" rx="2"/>'
        '<circle cx="9" cy="9" r="2"/>'
        '<path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>'
    ),
    "activity": (
        '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>'
    ),
    "dots": (
        '<circle cx="6.5" cy="7" r="1.3" fill="{c}" stroke="none"/>'
        '<circle cx="16" cy="8.5" r="2.1" fill="{c}" stroke="none"/>'
        '<circle cx="9.5" cy="16.5" r="2.9" fill="{c}" stroke="none"/>'
    ),
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-up": '<path d="m18 15-6-6-6 6"/>',
    "record": (
        '<circle cx="12" cy="12" r="9"/>'
        '<circle cx="12" cy="12" r="4.5" fill="{c}" stroke="none"/>'
    ),
    "stop": (
        '<circle cx="12" cy="12" r="9"/>'
        '<rect x="9" y="9" width="6" height="6" rx="1.2" fill="{c}" stroke="none"/>'
    ),
    "pause": (
        '<rect x="6" y="4" width="4" height="16" rx="1" fill="{c}" stroke="none"/>'
        '<rect x="14" y="4" width="4" height="16" rx="1" fill="{c}" stroke="none"/>'
    ),
    "play": '<path d="M7 4v16l13-8z" fill="{c}" stroke="none" stroke-linejoin="round"/>',
    "folder": (
        '<path d="M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v9a2 '
        '2 0 0 1-2 2z"/>'
    ),
    "cube": (
        '<path d="M12 2 3 7v10l9 5 9-5V7z"/>'
        '<path d="m3 7 9 5 9-5"/>'
        '<path d="M12 12v10"/>'
    ),
    # Window controls
    "minimize": '<path d="M5 12h14"/>',
    "maximize": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "restore": (
        '<rect x="8" y="3" width="13" height="13" rx="2"/>'
        '<path d="M16 19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2"/>'
    ),
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
}

_cache: dict[tuple[str, str, int, float], QPixmap] = {}


def pixmap(name: str, color: str, size: int = 16,
           stroke: float = 2.0) -> QPixmap:
    """Render an icon to a pixmap at the given colour and size."""
    key = (name, color, size, stroke)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    body = _ICONS[name].replace("{c}", color)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-linejoin="round">{body}</svg>'
    )

    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()

    _cache[key] = pm
    return pm


def icon(name: str, color: str, size: int = 16, stroke: float = 2.0) -> QIcon:
    return QIcon(pixmap(name, color, size, stroke))


def write_to_file(name: str, color: str, size: int, path,
                   stroke: float = 2.0) -> str:
    """Save an icon to disk and return its path with forward slashes.

    Qt stylesheets can only reference arrow/indicator glyphs via
    `image: url(...)`, which needs a real file -- the CSS border-triangle
    trick that works in web CSS does not reliably render as a triangle in
    Qt's QSS engine (verified: it paints a small solid rectangle instead).
    """
    pixmap(name, color, size, stroke).save(str(path), "PNG")
    return str(path).replace("\\", "/")


# App mark: a rounded badge with a wireframe cube over a scatter of points,
# which reads at small sizes far better than a detailed glyph would.
_APP_MARK = """
<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}"
     viewBox="0 0 64 64">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#2b303a"/>
      <stop offset="100%" stop-color="#171b21"/>
    </linearGradient>
  </defs>
  <rect x="2" y="2" width="60" height="60" rx="14" fill="url(#bg)"/>
  <rect x="2.75" y="2.75" width="58.5" height="58.5" rx="13.25"
        fill="none" stroke="rgba(255,255,255,0.10)" stroke-width="1.5"/>
  <g fill="none" stroke="{accent}" stroke-width="2.6"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M32 13 15 22.5v19L32 51l17-9.5v-19z"/>
    <path d="m15 22.5 17 9.5 17-9.5"/>
    <path d="M32 32v19"/>
  </g>
  <g fill="{dot}">
    <circle cx="22" cy="30" r="2.1"/>
    <circle cx="42" cy="30" r="2.1"/>
    <circle cx="32" cy="42" r="2.4"/>
  </g>
</svg>
"""


def app_icon(accent: str = "#3d8bfd", dot: str = "#e7eaf0") -> QIcon:
    """Multi-resolution window/taskbar icon."""
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        svg = _APP_MARK.format(s=size, accent=accent, dot=dot)
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing, True)
        renderer.render(painter)
        painter.end()
        result.addPixmap(pm)
    return result
