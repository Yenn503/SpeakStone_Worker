"""App settings (not the worker key: that lives in the worker's own config file, written by `net_worker.py init`)."""
import json
import os
import time
from pathlib import Path

DEFAULTS = {
    "install_dir": "",
    "mode": "off",              # off | now | idle
    "idle_minutes": 10,
    "block_fullscreen": True,
    "block_on_battery": True,
    "paused_until": 0,          # epoch seconds
    "update_check": False,      # off until the owner publishes releases
    "setup_done": False,
}


def settings_dir():
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "SpeakStoneWorkerApp"


class Settings:
    def __init__(self, path=None):
        self.path = Path(path) if path else settings_dir() / "app.json"
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                self.data.update({k: d[k] for k in DEFAULTS if k in d})
        except (OSError, ValueError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v
        self.save()

    # ---- derived paths
    @property
    def install(self):
        return Path(self.data["install_dir"]) if self.data["install_dir"] else None

    @property
    def work_dir(self):
        return self.install / "work"

    @property
    def python(self):
        return self.install / "venv" / "Scripts" / "python.exe"

    @property
    def worker_config(self):
        return self.install / "worker_config.json"

    def paused(self, now=None):
        return (now or time.time()) < float(self.data["paused_until"] or 0)

    def pause_for(self, minutes, now=None):
        self["paused_until"] = (now or time.time()) + minutes * 60

    def resume(self):
        self["paused_until"] = 0
