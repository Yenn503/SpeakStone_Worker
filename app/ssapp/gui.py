"""tkinter windows in the parchment look: setup wizard and the stats window. No logic here beyond layout."""
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from . import APP_NAME, APP_VERSION, DISCLAIMER, setup_steps, stats, theme


def _btn(parent, text, cmd, width=16):
    b = tk.Button(parent, text=text, command=cmd, width=width, bg=theme.BTN_BG, fg=theme.BTN_TEXT,
                  activebackground=theme.BTN_HOVER, activeforeground=theme.BTN_TEXT, relief="flat", bd=0,
                  font=(theme.FONT_UI, 10), padx=8, pady=6, cursor="hand2")
    return b


def _label(parent, text, size=10, bold=False, color=None, serif=False, **kw):
    f = (theme.FONT if serif else theme.FONT_UI, size, "bold" if bold else "normal")
    return tk.Label(parent, text=text, bg=theme.PANEL, fg=color or theme.TEXT, font=f, **kw)


def _window(title, w, h, root=None):
    win = tk.Toplevel(root) if root else tk.Tk()
    win.title(title)
    win.configure(bg=theme.PANEL)
    win.geometry(f"{w}x{h}")
    win.resizable(False, False)
    return win


def _header(win, subtitle):
    _label(win, APP_NAME, 20, True, theme.TITLE, serif=True).pack(anchor="w", padx=20, pady=(16, 0))
    _label(win, subtitle, 10, color=theme.DIM).pack(anchor="w", padx=20, pady=(0, 8))
    tk.Frame(win, bg=theme.EDGE, height=1).pack(fill="x", padx=20)


def _footer(win):
    _label(win, DISCLAIMER + f"   v{APP_VERSION}", 8, color=theme.DIM).pack(side="bottom", pady=8)


# ------------------------------------------------------------------ setup wizard
def run_setup_window(default_dir, resource_dir, on_done, root=None):
    win = _window(APP_NAME + " setup", 640, 520, root)
    _header(win, "Make voice lines on your own graphics card, similar to the in-game characters.")
    body = tk.Frame(win, bg=theme.PANEL)
    body.pack(fill="both", expand=True, padx=20, pady=10)

    _label(body, "Install folder (about 20 GB)").pack(anchor="w")
    row = tk.Frame(body, bg=theme.PANEL)
    row.pack(fill="x", pady=(2, 10))
    folder = tk.StringVar(value=str(default_dir))
    tk.Entry(row, textvariable=folder, bg=theme.BOX_BG, fg=theme.TEXT, relief="flat",
             font=(theme.FONT_UI, 10)).pack(side="left", fill="x", expand=True, ipady=4)
    _btn(row, "Browse...", lambda: folder.set(filedialog.askdirectory(initialdir=folder.get()) or folder.get()),
         10).pack(side="left", padx=(8, 0))

    _label(body, "Worker key (from the owner; typing is hidden)").pack(anchor="w")
    key = tk.StringVar()
    tk.Entry(body, textvariable=key, show="*", bg=theme.BOX_BG, fg=theme.TEXT, relief="flat",
             font=(theme.FONT_UI, 10)).pack(fill="x", ipady=4, pady=(2, 10))

    bar = tk.Canvas(body, height=14, bg=theme.INSET, highlightthickness=1, highlightbackground=theme.EDGE)
    bar.pack(fill="x")
    status = _label(body, "Ready.", 10, True, theme.TITLE)
    status.pack(anchor="w", pady=(6, 2))
    log = tk.Text(body, height=10, bg=theme.BOX_BG, fg=theme.TEXT, relief="flat", font=("Consolas", 8), wrap="word")
    log.pack(fill="both", expand=True)
    q = queue.Queue()
    go = _btn(body, "Install", lambda: start(), 14)
    go.pack(anchor="e", pady=8)
    _footer(win)

    def pump():
        try:
            while True:
                kind, a, b = q.get_nowait()
                if kind == "p":
                    if a not in (None, "done") and setup_steps.LABELS.get(a):
                        status.config(text=setup_steps.LABELS[a])
                    if b:
                        log.insert("end", str(b)[:300] + "\n")
                        log.see("end")
                elif kind == "frac":
                    bar.delete("all")
                    bar.create_rectangle(0, 0, int(bar.winfo_width() * a), 14, fill=theme.ACCENT, width=0)
                elif kind == "err":
                    status.config(text="Stopped: " + a, fg="#9b2d1f")
                    go.config(state="normal")
                elif kind == "ok":
                    status.config(text="All set.")
                    on_done(Path(folder.get()))
                    win.destroy()
                    return
        except queue.Empty:
            pass
        win.after(150, pump)

    def start():
        if not key.get().strip() and not (Path(folder.get()) / "worker_config.json").exists():
            messagebox.showinfo(APP_NAME, "Please enter your worker key.")
            return
        go.config(state="disabled")
        k = key.get().strip()
        key.set("")                                  # do not keep it in the window
        install = Path(folder.get())

        def progress(step, frac, msg):
            q.put(("p", step, msg))
            if frac is not None:
                q.put(("frac", frac, None))

        env = setup_steps.Env(install=install, resource_dir=resource_dir, download=setup_steps.real_download,
                              run=setup_steps.real_run, ask_key=lambda: k, gpu_total_mib=stats.gpu_total_mib,
                              progress=progress)

        def work():
            try:
                setup_steps.run_setup(env)
                q.put(("ok", None, None))
            except Exception as e:                    # SetupError text is user-safe; others show their type only
                q.put(("err", str(e) if isinstance(e, setup_steps.SetupError) else type(e).__name__, None))

        threading.Thread(target=work, daemon=True).start()

    pump()
    return win


# ------------------------------------------------------------------ stats window
class StatsWindow:
    def __init__(self, supervisor, settings, api_base, session_start=None, root=None, on_mode=None):
        self.sup, self.s, self.api = supervisor, settings, api_base
        self.on_mode = on_mode
        self.since = session_start or time.time()
        self.win = _window(APP_NAME, 520, 560, root)
        _header(self.win, "Voice lines for the SpeakStone add-on")
        self.dot = tk.Canvas(self.win, width=16, height=16, bg=theme.PANEL, highlightthickness=0)
        self.dot.place(x=484, y=24)
        self.state_lbl = _label(self.win, "", 10, True, theme.TITLE)
        self.state_lbl.place(x=372, y=22, width=108)
        self.state_lbl.config(anchor="e")
        grid = tk.Frame(self.win, bg=theme.PANEL)
        grid.pack(fill="x", padx=20, pady=10)
        self.vals = {}
        rows = [("lines", "Voice lines made"), ("jobs", "Jobs finished"), ("speed", "Speed"), ("queue", "Waiting in the queue"),
                ("left", "Time left (whole queue)"), ("now", "Making right now"), ("gpu", "Graphics card")]
        for i, (k, label) in enumerate(rows):
            _label(grid, label, 10, color=theme.DIM).grid(row=i, column=0, sticky="w", pady=3)
            v = _label(grid, "-", 12, True, theme.TITLE, serif=True)
            v.grid(row=i, column=1, sticky="e", padx=(40, 0))
            grid.columnconfigure(1, weight=1)
            self.vals[k] = v
        tk.Frame(self.win, bg=theme.EDGE, height=1).pack(fill="x", padx=20)
        mode = tk.Frame(self.win, bg=theme.PANEL)
        mode.pack(fill="x", padx=20, pady=10)
        self.mode = tk.StringVar(value=settings["mode"])
        for text, val in (("Off", "off"), ("Run now", "now"), ("Run when idle", "idle")):
            tk.Radiobutton(mode, text=text, value=val, variable=self.mode, command=self._mode, bg=theme.PANEL,
                           fg=theme.TEXT, activebackground=theme.PANEL, selectcolor=theme.BOX_BG,
                           font=(theme.FONT_UI, 10)).pack(side="left", padx=(0, 14))
        idle_row = tk.Frame(self.win, bg=theme.PANEL)
        idle_row.pack(fill="x", padx=20)
        _label(idle_row, "Start after this many idle minutes:").pack(side="left")
        self.minutes = tk.Spinbox(idle_row, from_=1, to=240, width=4, command=self._minutes, bg=theme.BOX_BG,
                                  fg=theme.TEXT, relief="flat")
        self.minutes.delete(0, "end")
        self.minutes.insert(0, str(settings["idle_minutes"]))
        self.minutes.pack(side="left", padx=6)
        self.minutes.bind("<FocusOut>", lambda e: self._minutes())
        _label(self.win, "Idle mode stops when you move the mouse, start a full-screen app or unplug the power.",
               9, color=theme.DIM, wraplength=470, justify="left").pack(anchor="w", padx=20, pady=(6, 0))
        _footer(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        self.tick()

    def _mode(self):
        self.sup.set_mode(self.mode.get())
        if self.on_mode:
            self.on_mode(self.mode.get())

    def _minutes(self):
        try:
            self.s["idle_minutes"] = max(1, min(240, int(self.minutes.get())))
        except ValueError:
            pass

    def show(self):
        self.win.deiconify()
        self.win.lift()

    def tick(self):
        try:
            snap = stats.snapshot(self.s.work_dir, self.api, since=self.since)
            dot = self.sup.dot()
            self.dot.delete("all")
            self.dot.create_oval(2, 2, 14, 14, fill=theme.DOT[dot], outline=theme.EDGE)
            self.state_lbl.config(text={"idle": "Waiting", "making": "Making", "paused": "Paused",
                                        "error": "Problem"}[dot])
            self.vals["lines"].config(text=f"{snap['lines']:,}")
            self.vals["jobs"].config(text=f"{snap['jobs']:,}")
            self.vals["speed"].config(text=f"{snap['lines_per_hour']:.0f} lines/h" if snap["lines_per_hour"] else "-")
            q = snap["queue"]
            self.vals["queue"].config(text=f"{q['queued_lines']:,} lines" if q else "-")
            self.vals["left"].config(text=stats.format_duration(snap["time_left"]))
            self.vals["now"].config(text=f"{snap['current_lines']} lines" if snap["making"] else "-")
            g = snap["gpu"]
            self.vals["gpu"].config(text=f"{g['temp_c']} C, {g['util_pct']}% busy" if g else "-")
        except Exception:
            pass
        self.win.after(5000, self.tick)
