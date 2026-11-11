"""Where things live. Everything is per user; nothing needs administrator rights."""
import os
import sys
from pathlib import Path


def is_windows():
    return sys.platform == "win32"


def data_dir(env=None):
    env = os.environ if env is None else env
    if is_windows():
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SpeakStoneWorker"
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "speakstone-worker"


def resource_dir():
    """Where net_worker.py, indextts2_worker.py and runpod_worker.py sit (bundled, or the repo root)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "worker"
    return Path(__file__).resolve().parents[2]


def api_base(env=None):
    from . import DEFAULT_API_BASE
    env = os.environ if env is None else env
    return (env.get("SPEAKSTONE_API_BASE") or DEFAULT_API_BASE).rstrip("/")
