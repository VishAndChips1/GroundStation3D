"""Where the app keeps its files.

Source lives in basestation/; anything the app *writes* (recent hosts, crash
log, captured clouds) goes in data/ beside it, so the project root stays clean
and one folder is all you ever need to back up or delete.
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
DATA_DIR = ROOT / "data"
ASSETS_DIR = PACKAGE_DIR / "assets"


def data_path(*parts: str) -> Path:
    """A path inside data/, creating data/ on first use."""
    DATA_DIR.mkdir(exist_ok=True)
    return DATA_DIR.joinpath(*parts)
