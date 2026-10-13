"""Where EdgeDesk keeps its code and its data.

Run from a folder (python3 server.py), everything lives in that folder. Run as the packaged desktop
app, the code is read-only inside the app, so data and the optional .env go in the user's own
application-data folder. EDGEDESK_HOME overrides either.
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))     # code and static files


def _default_home():
    if not FROZEN:
        return ROOT
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/EdgeDesk")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "EdgeDesk")
    return os.path.expanduser("~/.edgedesk")


HOME = os.environ.get("EDGEDESK_HOME") or _default_home()                               # .env and data
DATA = os.path.join(HOME, "data")
os.makedirs(DATA, exist_ok=True)
