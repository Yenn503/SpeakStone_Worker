"""Starts and stops the worker process. Stopping is polite: the worker's STOP file makes it hand the current job
back and exit; only if it ignores that for `grace` seconds is the process ended."""
import os
import subprocess
import sys
import time
from pathlib import Path


class WorkerProcess:
    def __init__(self, settings, popen=subprocess.Popen, clock=time.time):
        self.s = settings
        self.popen = popen
        self.clock = clock
        self.proc = None
        self.started_at = 0.0
        self.stop_asked_at = 0.0

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def command(self):
        s = self.s
        return [str(s.python), str(s.install / "net_worker.py"), "run", "--config", str(s.worker_config)]

    def environment(self, base=None):
        e = dict(base if base is not None else os.environ)
        e["PATH"] = str(self.s.install / "ffmpeg") + os.pathsep + e.get("PATH", "")
        e["INDEXTTS2_PYTHON"] = str(self.s.python)
        e["INDEXTTS2_DIR"] = str(self.s.install / "IndexTTS-2")
        e["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        e["PYTHONUNBUFFERED"] = "1"
        e.pop("SSW_WORKER_KEY", None)               # the key comes from the worker's own config file
        return e

    def start(self):
        if self.running:
            return False
        stop = self.s.work_dir / "STOP"
        try:
            stop.unlink()
        except OSError:
            pass
        kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        self.proc = self.popen(self.command(), env=self.environment(), cwd=str(self.s.install),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.started_at = self.clock()
        self.stop_asked_at = 0.0
        return True

    def request_stop(self):
        """Ask nicely (STOP file). Call `poll_stop` on a timer to finish the job if the worker does not leave."""
        if not self.running:
            return
        if not self.stop_asked_at:
            try:
                (self.s.work_dir).mkdir(parents=True, exist_ok=True)
                (self.s.work_dir / "STOP").write_text("stop", encoding="utf-8")
            except OSError:
                pass
            self.stop_asked_at = self.clock()

    def poll_stop(self, grace=120):
        """True once the worker is gone. After `grace` seconds without leaving, ends it."""
        if not self.running:
            self.proc = None
            return True
        if self.stop_asked_at and self.clock() - self.stop_asked_at > grace:
            try:
                self.proc.terminate()
            except OSError:
                pass
        return False
