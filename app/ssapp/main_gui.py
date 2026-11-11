"""Windowed (tkinter) entry point, kept for a later option 1. The CLI is main.py.  SpeakStoneWorker.exe            -> setup on first run, then the tray app
                 SpeakStoneWorker.exe --tray     -> tray app only (what the log-on task runs)
                 SpeakStoneWorker.exe --stats    -> tray app with the stats window open
                 SpeakStoneWorker.exe --setup    -> setup again (repair)
                 SpeakStoneWorker.exe --unregister-idle   -> remove the log-on task
"""
import argparse
import os
import queue
import sys
import time
from pathlib import Path

from . import APP_NAME, autostart, settings as settings_mod, stats, updater


def resource_dir():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "worker"
    return Path(__file__).resolve().parents[2]          # repo root: net_worker.py and friends


def default_install_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SpeakStoneWorker"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="SpeakStoneWorker")
    ap.add_argument("--tray", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--unregister-idle", action="store_true")
    a = ap.parse_args(argv)

    s = settings_mod.Settings()
    if a.unregister_idle:
        return 0 if autostart.unregister() else 1

    from . import gui
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()

    def launch():
        from .controller import WorkerProcess
        from .supervisor import Supervisor
        from .tray import Tray
        worker = WorkerProcess(s)
        started = time.time()
        sup = Supervisor(s, worker, stats_reader=lambda: stats.read_log_stats(s.work_dir / "logs", since=started))
        api = "https://speakstone.beanw.co.uk"
        def on_mode(mode):
            # 'Run when idle' needs the tray app to start at log-on; any other mode removes that task again.
            xml = settings_mod.settings_dir() / "task.xml"
            xml.parent.mkdir(parents=True, exist_ok=True)
            autostart.register(autostart.exe_path(), xml) if mode == "idle" else autostart.unregister()

        win = gui.StatsWindow(sup, s, api, session_start=started, root=root, on_mode=on_mode)
        calls = queue.Queue()

        def quit_app():
            worker.request_stop()
            tray.stop()
            root.after(1500, lambda: (worker.poll_stop(grace=0), root.destroy()))

        tray = Tray(sup, calls.put, win.show, quit_app)
        if not a.stats:
            win.win.withdraw()

        def loop():
            try:
                while True:
                    calls.get_nowait()()
            except queue.Empty:
                pass
            tray.set_state(sup.tick())
            root.after(3000, loop)

        tray.run()
        loop()
        if s["update_check"]:
            info = updater.check()
            if info:
                tray.icon.title = f"{APP_NAME}: update {info['version']} available"

    def after_setup(install):
        s["install_dir"] = str(install)
        s["setup_done"] = True
        s["mode"] = "now" if s["mode"] == "off" else s["mode"]
        launch()

    if not s["setup_done"] or a.setup:
        gui.run_setup_window(s.install or default_install_dir(), resource_dir(), after_setup, root=root)
    else:
        launch()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
