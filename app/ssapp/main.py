"""Entry point. Double-click SpeakStoneWorker.exe -> a console with the SpeakStone banner and a menu.

  SpeakStoneWorker.exe                    -> the menu (first run sets everything up and joins the network)
  SpeakStoneWorker.exe --tray             -> the idle service's tray app (what the log-on task runs; no window)
  SpeakStoneWorker.exe --board            -> read-only live board (opened from the tray icon)
  SpeakStoneWorker.exe --gui              -> the older windowed app (tkinter), kept for a later option
  SpeakStoneWorker.exe --unregister-idle  -> remove the log-on task
"""
import argparse
import os
import sys
from pathlib import Path

from . import autostart, settings as settings_mod


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
    ap.add_argument("--board", action="store_true")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--unregister-idle", action="store_true")
    a = ap.parse_args(argv)

    s = settings_mod.Settings()
    if a.unregister_idle:
        return 0 if autostart.unregister() else 1
    if a.gui:
        from . import main_gui
        return main_gui.main([])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")      # box and block characters on any console
        except (AttributeError, ValueError):
            pass
    from . import cli
    if (a.tray or a.board) and not (s["setup_done"] and s.install):
        print("Setup has not been done yet. Run SpeakStoneWorker.exe once and let it finish.")
        return 1
    if a.tray:
        return cli.run_tray(s)
    app = cli.App(s, resource_dir())
    if a.board:
        return app.run_viewer()
    try:
        return app.run_menu()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
