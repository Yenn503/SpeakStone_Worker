"""Starts and stops `net_worker.py run` as a child process. Stopping is polite: the STOP file makes the worker hand the
current job back and exit; only if it ignores that for `grace` seconds is the process ended."""
import os
import subprocess
import sys
import time
from pathlib import Path


class WorkerRunner:
    def __init__(self, install, popen=subprocess.Popen, clock=time.time, nice=False):
        self.install = Path(install)
        self.popen = popen
        self.clock = clock
        self.nice = nice                    # Linux: run at low priority (used when idle time cannot be read)
        self.proc = None
        self.stop_asked_at = 0.0

    @property
    def python(self):
        return self.install / ("venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python")

    @property
    def work_dir(self):
        return self.install / "work"

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def exit_code(self):
        return None if self.proc is None else self.proc.poll()

    def command(self):
        return [str(self.python), str(self.install / "net_worker.py"), "run",
                "--config", str(self.install / "worker_config.json")]

    def environment(self, base=None):
        e = dict(base if base is not None else os.environ)
        e["PATH"] = str(self.install / "ffmpeg") + os.pathsep + e.get("PATH", "")
        e["INDEXTTS2_PYTHON"] = str(self.python)
        e["INDEXTTS2_DIR"] = str(self.install / "IndexTTS-2")
        e["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        e["PYTHONUNBUFFERED"] = "1"
        e.pop("SSW_WORKER_KEY", None)       # the key comes from the worker's own config file
        return e

    def start(self):
        if self.running:
            return False
        self.clear_stop()
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
        elif self.nice:
            kw["preexec_fn"] = lambda: os.nice(10)
        self.proc = self.popen(self.command(), env=self.environment(), cwd=str(self.install),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.stop_asked_at = 0.0
        return True

    def clear_stop(self):
        try:
            (self.work_dir / "STOP").unlink()
        except OSError:
            pass

    def request_stop(self):
        if not self.running or self.stop_asked_at:
            return
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / "STOP").write_text("stop", encoding="utf-8")
        except OSError:
            pass
        self.stop_asked_at = self.clock()

    def poll_stop(self, grace=120):
        """True once the worker is gone. After `grace` seconds without leaving, ends it."""
        if not self.running:
            return True
        if self.stop_asked_at and self.clock() - self.stop_asked_at > grace:
            try:
                self.proc.terminate()
            except OSError:
                pass
        return False

    def stop_and_wait(self, grace=120, sleep=time.sleep, on_wait=None):
        self.request_stop()
        while not self.poll_stop(grace):
            if on_wait:
                on_wait()
            sleep(0.5)
        self.clear_stop()
