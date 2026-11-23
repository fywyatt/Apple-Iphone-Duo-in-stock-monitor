"""Local runtime locations for the browser dashboard and worker."""

import os
import sys
from pathlib import Path


def default_home():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AppleStockMonitor"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AppleStockMonitor"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "AppleStockMonitor"


APP_HOME = Path(os.environ.get("APPLE_STOCK_APP_HOME") or default_home()).resolve()
WORK_FOLDER = Path(os.environ.get("APPLE_STOCK_WORK_FOLDER") or APP_HOME).resolve()
