"""The command-line app: banner, menu, live board. Replaces the tkinter window as the entry point (the tkinter code
stays in gui.py for a later windowed option).

  [1] Start now                run the worker here with a live stats board
  [2] Install as a service     per-user log-on task + tray; runs when the PC is idle
  [3] Settings                 idle minutes, safety stops, repair, uninstall the service
  [Q] Quit

First run: setup installs everything with a progress display, then joins the network automatically (no key to type).
"""
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn
from rich.text import Text

from . import APP_NAME, DISCLAIMER, autostart, board, enroll, settings as settings_mod, setup_steps, stats

API_BASE = enroll.DEFAULT_API
TRAY_MUTEX = "Local\\SpeakStoneWorkerTray"


# ---------------------------------------------------------------- keyboard (Windows console; plain input() elsewhere)
class Keys:
    def poll(self):
        """A pressed key (lower-case) or None, without waiting."""
        try:
            import msvcrt
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    return None
                return ch.lower()
        except ImportError:
            pass
        return None

    def wait(self):
        try:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()
                return ""
            return ch.lower()
        except ImportError:
            return (input() or " ")[:1].lower()


# ---------------------------------------------------------------- one reading of everything the board shows
class BoardData:
    """Log totals and the GPU every couple of seconds, the public queue once a minute (it is a network call)."""

    def __init__(self, work_dir, api=API_BASE, since=None, clock=time.time, gpu=stats.gpu_reading,
                 queue_reader=stats.fetch_queue, log_reader=stats.read_log_stats, queue_every=60, every=2):
        self.work_dir, self.api, self.since, self.clock = Path(work_dir), api, since, clock
        self.gpu, self.queue_reader, self.log_reader = gpu, queue_reader, log_reader
        self.queue_every, self.every = queue_every, every
        self._at = self._q_at = -1e18
        self._snap = {}
        self._queue = None

    def read(self):
        now = self.clock()
        if now - self._at >= self.every:
            self._at = now
            s = self.log_reader(self.work_dir / "logs", since=self.since, now=now)
            s["gpu"] = self.gpu()
            s["speed"] = stats.speed_series(s["completions"], now)
            self._snap = s
        if now - self._q_at >= self.queue_every:
            self._q_at = now
            self._queue = self.queue_reader(self.api)
        s = dict(self._snap)
        q = self._queue
        s["queue"] = q
        if q and q.get("eta_seconds") is not None:
            s["time_left"] = q["eta_seconds"]
        elif q and s.get("lines_per_hour"):
            s["time_left"] = q["queued_lines"] / s["lines_per_hour"] * 3600
        else:
            s["time_left"] = None
        return s


# ---------------------------------------------------------------- setup progress
class SetupView:
    """A rich progress display for setup_steps.run_setup: overall bar, current step, the last few output lines."""

    def __init__(self, console):
        self.console = console
        self.bar = Progress(TextColumn("[bold #e8b04a]{task.description}"), BarColumn(bar_width=34, style="#8a7a5c",
                            complete_style="#e8b04a", finished_style="#7fc29a"), TextColumn("{task.percentage:>3.0f}%"),
                            console=console)
        self.overall = self.bar.add_task("Setting up", total=100)
        self.step = ""
        self.tail = []
        self.live = Live(self._render(), console=console, refresh_per_second=8, transient=False)

    def _render(self):
        lines = [Text(f"  {self.step}", style=board.PARCH)] + [Text(f"    {ln[:100]}", style=board.DIM) for ln in self.tail[-4:]]
        return Group(self.bar, *lines)

    def progress(self, step, frac, msg):
        if step == "done":
            self.bar.update(self.overall, completed=100)
            self.step = "Finished."
        elif frac is not None:
            self.bar.update(self.overall, completed=max(0.0, min(1.0, frac)) * 100)
            self.step = msg or setup_steps.LABELS.get(step, step)
        elif msg:
            self.tail.append(msg)
        self.live.update(self._render())

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, *a):
        self.live.stop()


def build_env(install, resource_dir, view=None, gpu_info=stats.gpu_info, api=API_BASE, enroller=None):
    return setup_steps.Env(
        install=install, resource_dir=resource_dir, download=setup_steps.real_download, run=setup_steps.real_run,
        gpu_total_mib=stats.gpu_total_mib,
        enroll=enroller or enroll.make_enroller(api, gpu_info),
        progress=view.progress if view else (lambda step, frac, msg: None))


# ---------------------------------------------------------------- Windows helpers (best effort, never raise)
def hide_console():
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def single_instance(name=TRAY_MUTEX):
    """True when this is the only process holding the named mutex (the handle is kept for the process lifetime)."""
    if sys.platform != "win32":
        return True
    try:
        h = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        single_instance.handle = h
        return ctypes.windll.kernel32.GetLastError() != 183       # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def tray_running(name=TRAY_MUTEX):
    if sys.platform != "win32":
        return False
    try:
        h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, name)    # SYNCHRONIZE
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def spawn_detached(args, new_console=False):
    exe = autostart.exe_path()
    cmd = ([exe] if getattr(sys, "frozen", False) else [sys.executable, exe]) + list(args)
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000010 if new_console else 0x08000000 | 0x00000008        # CREATE_NEW_CONSOLE | NO_WINDOW+DETACHED
    return subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL,
                            **({} if new_console else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}))


# ---------------------------------------------------------------- the app
class App:
    def __init__(self, s, resource_dir, console=None, keys=None, sleep=time.sleep, clock=time.time,
                 api=API_BASE, env_factory=build_env, worker_factory=None):
        self.s = s
        self.resource_dir = resource_dir
        self.console = console or Console(highlight=False)
        self.keys = keys or Keys()
        self.sleep, self.clock, self.api = sleep, clock, api
        self.env_factory = env_factory
        self.worker_factory = worker_factory

    # ---- screens
    def health(self):
        rows = []
        name, mib = stats.gpu_info()
        rows.append(("GPU", f"{name} · {mib // 1024} GB · ready" if name else "no NVIDIA card found",
                     board.GOOD if name else board.BAD))
        joined = self.s.install is not None and self.s.worker_config.exists()
        rows.append(("Joined", "yes, this PC has its own place in the network" if joined else "not yet (first run sets it up)",
                     board.GOOD if joined else board.WARN))
        q = stats.fetch_queue(self.api, timeout=4)
        rows.append(("Queue", f"{q['queued_lines']:,} lines waiting" if q else "queue unavailable", board.PARCH if q else board.DIM))
        rows.append(("Mode", {"now": "run now", "idle": "service (runs when idle)"}.get(self.s["mode"], "not running"), board.PARCH))
        return rows

    def show_start(self):
        self.console.clear()
        self.console.print(board.start_screen(self.health()))

    def pause_for_key(self, text="Press any key to go back"):
        self.console.print(Text(f"  {text}", style=board.DIM))
        self.keys.wait()

    # ---- first run
    def needs_setup(self):
        return not self.s["setup_done"]

    def run_setup(self):
        install = self.s.install or default_install_dir()
        self.console.print(board.message_panel("First run", [
            "Setting up your PC to make voice lines, similar to the in-game characters.",
            "This downloads about 6 GB and takes a while. If it stops, run the program again: it carries on.",
            "Joining the network is automatic; there is no key to type."]))
        with SetupView(self.console) as view:
            env = self.env_factory(install, self.resource_dir, view, api=self.api)
            try:
                setup_steps.run_setup(env)
            except setup_steps.SetupError as e:
                view.live.stop()
                self.console.print(Text(f"\n  Setup stopped: {e}", style=board.BAD))
                self.pause_for_key("Press any key to go back")
                return False
        self.s["install_dir"] = str(install)
        self.s["setup_done"] = True
        return True

    # ---- [1] start now
    def make_worker(self):
        if self.worker_factory:
            return self.worker_factory(self.s)
        from .controller import WorkerProcess
        return WorkerProcess(self.s)

    def start_now(self):
        if tray_running():
            self.console.print(board.message_panel("The service is running", [
                "The idle service already owns the worker on this PC.",
                "Use its tray icon (Start now), or remove the service in Settings first."], board.WARN))
            self.pause_for_key()
            return
        from .supervisor import Supervisor
        worker = self.make_worker()
        started = self.clock()
        sup = Supervisor(self.s, worker)
        self.s["mode"] = "now"
        sup.start_now()
        self.live_board(worker, sup, started, "run now", "S stop after job   P pause   Q quit")
        self.s["mode"] = "off"

    def live_board(self, worker, sup, started, label, keys_hint, frames=None):
        data = BoardData(self.s.work_dir, self.api, since=started, clock=self.clock)
        stopping_since = None
        paused = False
        with Live(board.board(data.read(), label, "making", keys=keys_hint), console=self.console,
                  refresh_per_second=2, screen=True) as live:
            n = 0
            while True:
                k = self.keys.poll()
                if k == "q" or k == "s":
                    if stopping_since is None:
                        worker.request_stop()
                        stopping_since = self.clock()
                elif k == "p" and stopping_since is None:
                    paused = not paused
                    if paused:
                        worker.request_stop()
                    else:
                        sup.start_now()
                if stopping_since is not None and worker.poll_stop(grace=120):
                    break
                if not worker.running and stopping_since is None and not paused:
                    sup.tick()                                  # worker ended on its own: start it again
                snap = data.read()
                state = "paused" if paused else "making"
                keys_text = "stopping: handing the current job back..." if stopping_since else keys_hint
                live.update(board.board(snap, label, state, now=self.clock(), keys=keys_text))
                n += 1
                if frames is not None and n >= frames:
                    worker.request_stop()
                    break
                self.sleep(0.5)

    # ---- [2] service
    def install_service(self):
        if not self.s.install:
            return
        xml = settings_mod.settings_dir() / "task.xml"
        xml.parent.mkdir(parents=True, exist_ok=True)
        ok = autostart.register(autostart.exe_path(), xml)
        self.s["mode"] = "idle"
        self.console.clear()
        if not ok:
            self.console.print(board.message_panel("Could not add the log-on task", [
                "Windows refused to add the per-user log-on task. The tray will run until you sign out."], board.WARN))
        if not tray_running():
            try:
                spawn_detached(["--tray"])
            except OSError as e:
                self.console.print(Text(f"  Could not start the tray: {e}", style=board.BAD))
        data = BoardData(self.s.work_dir, self.api, since=None, clock=self.clock)
        self.console.print(board.service_panel(data.read(), self.s["idle_minutes"]))
        self.pause_for_key("Press any key to close this menu (the service keeps running)")

    # ---- [3] settings
    def settings_screen(self):
        while True:
            self.console.clear()
            self.console.print(board.banner())
            s = self.s
            rows = [("1", f"Start after {s['idle_minutes']} min idle"),
                    ("2", f"Never during a full-screen game: {'yes' if s['block_fullscreen'] else 'no'}"),
                    ("3", f"Never on battery: {'yes' if s['block_on_battery'] else 'no'}"),
                    ("4", f"Check for app updates: {'yes' if s['update_check'] else 'no'}"),
                    ("5", "Remove the idle service (log-on task)"),
                    ("6", "Run setup again (repair)"),
                    ("B", "Back")]
            for k, text in rows:
                self.console.print(Text(f"  [{k}] ", style=f"bold {board.GOLD}") + Text(text, style=board.PARCH))
            k = self.keys.wait()
            if k in ("b", "q", "\x1b", ""):
                return
            if k == "1":
                self.console.print(Text("  Minutes idle before starting (1-240): ", style=board.PARCH), end="")
                try:
                    v = int(input().strip())
                    if 1 <= v <= 240:
                        s["idle_minutes"] = v
                except (ValueError, EOFError):
                    pass
            elif k == "2":
                s["block_fullscreen"] = not s["block_fullscreen"]
            elif k == "3":
                s["block_on_battery"] = not s["block_on_battery"]
            elif k == "4":
                s["update_check"] = not s["update_check"]
            elif k == "5":
                autostart.unregister()
                if s["mode"] == "idle":
                    s["mode"] = "off"
                self.console.print(Text("  Service removed. Quit its tray icon if it is showing.", style=board.GOOD))
                self.pause_for_key()
            elif k == "6":
                s["setup_done"] = False
                self.run_setup()
                self.pause_for_key()

    # ---- main menu
    def run_menu(self):
        if self.needs_setup():
            self.console.clear()
            self.console.print(board.banner())
            if not self.run_setup():
                return 1
        while True:
            self.show_start()
            k = self.keys.wait()
            if k == "1":
                self.start_now()
            elif k == "2":
                self.install_service()
            elif k == "3":
                self.settings_screen()
            elif k in ("q", "\x03"):
                self.console.print(Text(f"  {DISCLAIMER}", style=board.DIM))
                return 0

    # ---- board viewer (opened from the tray; read-only, closing it harms nothing)
    def run_viewer(self, frames=None):
        data = BoardData(self.s.work_dir, self.api, since=None, clock=self.clock)
        label = "service" if self.s["mode"] == "idle" else "run now"
        n = 0
        with Live(board.board(data.read(), label, "idle", keys="Q close"), console=self.console,
                  refresh_per_second=2, screen=True) as live:
            while self.keys.poll() not in ("q", "\x1b"):
                snap = data.read()
                if self.s["mode"] == "idle":
                    live.update(board.service_panel(snap, self.s["idle_minutes"],
                                                    state="making" if snap.get("making") else "idle"))
                else:
                    live.update(board.board(snap, label, "making" if snap.get("making") else "idle",
                                            now=self.clock(), keys="Q close"))
                n += 1
                if frames is not None and n >= frames:
                    break
                self.sleep(0.5)
        return 0


def default_install_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SpeakStoneWorker"


# ---------------------------------------------------------------- the tray process (what the log-on task runs)
def run_tray(s, hide=True):
    """No window of its own: the supervisor decides when to run (idle.decide); the tray icon is the way in."""
    if not single_instance():
        return 0
    if hide:
        hide_console()
    from .controller import WorkerProcess
    from .supervisor import Supervisor
    from .tray import Tray
    worker = WorkerProcess(s)
    started = time.time()
    sup = Supervisor(s, worker, stats_reader=lambda: stats.read_log_stats(s.work_dir / "logs", since=started))
    calls = queue.Queue()
    done = threading.Event()

    def quit_app():
        worker.request_stop()
        done.set()

    tray = Tray(sup, calls.put, lambda: spawn_detached(["--board"], new_console=True), quit_app)
    tray.run()
    try:
        while not done.is_set():
            try:
                while True:
                    calls.get_nowait()()
            except queue.Empty:
                pass
            tray.set_state(sup.tick())
            done.wait(3)
        deadline = time.time() + 120
        while not worker.poll_stop(grace=60) and time.time() < deadline:
            time.sleep(1)
    finally:
        tray.stop()
    return 0
