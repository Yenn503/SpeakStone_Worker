"""Menu, live board, settings and the hidden --background mode."""
import argparse
import json
import os
import platform
import re
import socket
import sys
import threading
import time

from rich.console import Console
from rich.live import Live

from . import APP_NAME, __version__, board, enroll as enroll_mod, idle, paths, service, setup, stats
from .keys import KeyReader
from .runner import WorkerRunner
from .settings import Settings, in_pause_window, parse_hhmm


def make_console():
    for stream in (sys.stdout, sys.stderr):          # piped/cp1252 consoles would choke on the block letters
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    return Console(highlight=False)


def worker_version(install):
    try:
        m = re.search(r'WORKER_VERSION\s*=\s*"([^"]+)"', (install / "net_worker.py").read_text(encoding="utf-8"))
        return m.group(1) if m else "1.0.1"
    except OSError:
        return "1.0.1"


def worker_label(install):
    try:
        return json.loads((install / "enrolled.json").read_text(encoding="utf-8")).get("label") or ""
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------- setup with a progress board
def run_setup_ui(console, install, base):
    steps, done, cur = list(setup.STEPS), set(setup.load_state(setup.Env(install, paths.resource_dir()))), [None]
    view = {"frac": None, "line": "", "dl": "", "t0": time.time()}
    live = Live(console=console, refresh_per_second=8, transient=False)

    def redraw(error=None):
        live.update(board.render_setup(steps, setup.LABELS, done, cur[0], view["frac"], view["line"], view["dl"], error))

    def progress(step, frac, msg):
        if step == "done":
            return
        if step != cur[0]:
            if cur[0]:
                done.add(cur[0])
            cur[0], view["frac"], view["dl"], view["t0"] = step, None, "", time.time()
        if frac is not None and step not in ("gpu",):
            view["frac"] = frac if step in ("uv", "index_tts", "ffmpeg", "model") else view["frac"]
        if msg:
            view["line"] = msg
        redraw()

    def download(url, dest, cb):
        def inner(frac, got, total):
            if total:
                rate = got / max(0.5, time.time() - view["t0"])
                view["dl"] = f"{got / 1e6:.0f} of {total / 1e6:.0f} MB  {rate / 1e6:.1f} MB/s"
            cb(frac, got, total)
        setup.real_download(url, dest, inner)

    api = paths.api_base()
    host = socket.gethostname()

    def do_enroll():
        gpu = stats.gpu_reading() or {}
        return enroll_mod.enroll(api, host, gpu.get("name", "unknown"), gpu.get("mem_total_mib", 0),
                                 ("windows" if platform.system() == "Windows" else "linux"), worker_version(install))

    env = setup.Env(install, paths.resource_dir(), download=download, run=setup.real_run, enroll=do_enroll,
                    gpu_total_mib=stats.gpu_total_mib, progress=progress, api_base=(api if os.environ.get("SPEAKSTONE_API_BASE") else ""),
                    machine_name=host)
    with live:
        redraw()
        try:
            setup.run_setup(env)
        except (setup.SetupError, enroll_mod.EnrollError) as e:
            redraw(error=str(e))
            return str(e)
        done.update(steps)
        redraw()
    return None


def ensure_setup(console, install):
    if setup.is_set_up(install):
        return True
    err = run_setup_ui(console, install, os.environ)
    if err:
        console.print(f"\n[#e0962c]Setup stopped: {err}[/]\nRun this again to carry on from where it stopped.")
        return False
    return True


# ---------------------------------------------------------------- live board
class Collector:
    """Gathers board data; the slow queue call runs on its own thread."""

    def __init__(self, install, api, started):
        self.install, self.api, self.started = install, api, started
        self.queue = None
        self.gpu_cache = None
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop:
            self.queue = stats.fetch_queue(self.api)
            for _ in range(30):
                if self._stop:
                    return
                time.sleep(1)

    def close(self):
        self._stop = True


def board_state(install, queue, gpu, state, started, now=None, events=None):
    now = now or time.time()
    ev = events if events is not None else stats.read_events(install / "work" / "logs")
    s = stats.summarize(ev, now)
    s["queue"], s["gpu"], s["state"] = queue, gpu, state
    s["time_left"] = stats.time_left(queue, s["words_hour"], s["lines_hour"])
    s["since_label"] = time.strftime("%H:%M", time.localtime(s["first_today"] or started))
    if s["making"]:
        s["job_pct"] = min(0.95, (now - s["job_started"]) / max(1.0, s["avg_job_seconds"]))
    if state == "paused":
        s["making"] = False
    return s


def start_now(console, install, settings):
    if not ensure_setup(console, install):
        return "menu"
    runner = WorkerRunner(install)
    started, paused = time.time(), False
    col = Collector(install, paths.api_base(), started)
    runner.start()
    result = "menu"
    try:
        with KeyReader() as keys, Live(console=console, screen=True, refresh_per_second=4) as live:
            last_gpu, gpu = 0, None
            while True:
                if time.time() - last_gpu > 2:
                    gpu, last_gpu = stats.gpu_reading(), time.time()
                state = "paused" if paused else ("stopped" if not runner.running else "idle")
                live.update(board.render_board(board_state(install, col.queue, gpu, state, started)))
                k = keys.poll(0.5)
                if k == "p":
                    paused = not paused
                    runner.request_stop() if paused else runner.start()
                elif k in ("s", "m"):
                    result = "exit" if k == "s" else "menu"
                    break
    except KeyboardInterrupt:
        result = "exit"
    finally:
        col.close()
        if runner.running:
            console.print("Giving the current job back before stopping...")
            runner.stop_and_wait()
    return result


# ---------------------------------------------------------------- settings
def settings_menu(console, install, st):
    while True:
        console.print(f"\n[{board.GOLD}]SETTINGS[/]")
        console.print(f"  [{board.GOLD}][1][/] Idle minutes before starting: [bold]{st['idle_minutes']}[/]")
        sched = f"{st['pause_from']} - {st['pause_to']}" if st["pause_from"] and st["pause_to"] else "none"
        console.print(f"  [{board.GOLD}][2][/] Daily pause (no work in this window): [bold]{sched}[/]")
        console.print(f"  [{board.GOLD}][3][/] Uninstall the service")
        console.print(f"  [{board.GOLD}][4][/] Show my worker label: [bold]{worker_label(install) or '(not set up yet)'}[/]")
        console.print(f"  [{board.GOLD}][B][/] Back")
        with KeyReader() as keys:
            k = keys.wait()
        if k == "1":
            v = input("Minutes of no keyboard or mouse use before starting (1-240): ").strip()
            if v.isdigit() and 1 <= int(v) <= 240:
                st["idle_minutes"] = int(v)
        elif k == "2":
            v = input("Pause window as HH:MM-HH:MM (for example 18:00-23:00), blank for none: ").strip()
            m = re.match(r"^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$", v)
            if m and parse_hhmm(m.group(1)) is not None and parse_hhmm(m.group(2)) is not None:
                st["pause_from"], st["pause_to"] = m.group(1), m.group(2)
            elif not v:
                st["pause_from"] = st["pause_to"] = ""
        elif k == "3":
            console.print("Service removed." if service.uninstall() else "No service was installed.")
        elif k == "4":
            console.print(f"Your worker label: {worker_label(install) or '(not set up yet)'}")
        elif k in ("b", "q", "\x1b"):
            return


# ---------------------------------------------------------------- background mode
def hide_console():
    if sys.platform == "win32":
        try:
            import ctypes
            h = ctypes.windll.kernel32.GetConsoleWindow()
            if h:
                ctypes.windll.user32.ShowWindow(h, 0)
        except Exception:
            pass


def background_tick(runner, st, now=None, readers=None):
    """One decision. Returns the action taken. Pure apart from starting/stopping the worker."""
    r = readers or {}
    idle_s = (r.get("idle_seconds") or idle.idle_seconds)()
    t = time.localtime(now or time.time())
    paused = in_pause_window(t.tm_hour * 60 + t.tm_min, st["pause_from"], st["pause_to"])
    mode = "idle"
    if idle_s is None:                         # no idle time available: always on, at low priority
        mode, idle_s, runner.nice = "now", 0, True
    action = idle.decide(mode, runner.running, idle_s, st["idle_minutes"], (r.get("fullscreen") or idle.fullscreen_busy)(),
                         (r.get("battery") or idle.on_battery)(), paused, st["block_fullscreen"], st["block_on_battery"])
    if action == "start":
        runner.start()
    elif action == "stop":
        runner.request_stop()
    runner.poll_stop()
    if not runner.running:
        runner.stop_asked_at = 0.0
    return action


def background(install, st):
    hide_console()
    if not setup.is_set_up(install):
        return 2
    runner = WorkerRunner(install)
    try:
        while True:
            background_tick(runner, st)
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        if runner.running:
            runner.stop_and_wait()
    return 0


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="speakstone_worker", description=APP_NAME)
    ap.add_argument("--background", action="store_true", help="run when the PC is idle (used by the service)")
    ap.add_argument("--start", action="store_true", help="skip the menu and start now")
    ap.add_argument("--version", action="store_true")
    a = ap.parse_args(argv)
    if a.version:
        print(f"{APP_NAME} {__version__}")
        return 0
    install, st = paths.data_dir(), Settings()
    if a.background:
        return background(install, st)
    console = make_console()
    try:
        choice = "1" if a.start else None
        while True:
            if choice is None:
                console.clear()
                console.print(board.menu_screen(service.is_installed()), end="")
                with KeyReader() as keys:
                    choice = keys.wait()
                console.print(choice.upper())
            if choice == "1":
                if start_now(console, install, st) == "exit":
                    return 0
            elif choice == "2":
                if service.is_installed():
                    console.print("Service removed." if service.uninstall() else "Could not remove the service.")
                elif ensure_setup(console, install):
                    ok = service.install(install)
                    console.print("Installed. It starts when your PC has been idle for "
                                  f"{st['idle_minutes']} minutes." if ok else "Could not install the service.")
                time.sleep(2)
            elif choice == "3":
                settings_menu(console, install, st)
            elif choice in ("q", "\x1b"):
                return 0
            choice = None
    except KeyboardInterrupt:
        return 0
