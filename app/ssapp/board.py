"""The look of the command-line app: parchment and gold, double-line frames. Everything here is a pure function from
plain data to `rich` renderables, so tests can render it to text. Public wording rule (CurseForge): it is "voice
lines made", "similar to the in-game characters"; test_cli.py greps this file for the banned terms.
"""
import time

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import APP_NAME, APP_VERSION, DISCLAIMER, stats

GOLD = "#e8b04a"
PARCH = "#e8d9b5"
DIM = "#8a7a5c"
EDGE = "#a8782a"
GOOD = "#7fc29a"
WARN = "#e0a458"
BAD = "#e07b5f"

BANNER = r"""   ____                    _     ____  _
  / ___| _ __   ___  __ _| | __/ ___|| |_ ___  _ __   ___
  \___ \| '_ \ / _ \/ _` | |/ /\___ \| __/ _ \| '_ \ / _ \
   ___) | |_) |  __/ (_| |   <  ___) | || (_) | | | |  __/
  |____/| .__/ \___|\__,_|_|\_\|____/ \__\___/|_| |_|\___|
        |_|"""

MENU = (("1", "Start now", "until you close this window"),
        ("2", "Install as a service", "runs when the PC is idle"),
        ("3", "Settings", "idle time, safety stops"),
        ("Q", "Quit", ""))


def bar(frac, width=22, color=GOLD):
    """Filled and empty blocks, e.g. 64% -> 14 of 22 filled."""
    frac = 0.0 if frac is None else max(0.0, min(1.0, float(frac)))
    n = round(frac * width)
    t = Text()
    t.append("█" * n, style=color)
    t.append("░" * (width - n), style=DIM)
    return t


def heat_color(temp_c):
    return GOOD if temp_c < 70 else WARN if temp_c < 82 else BAD


def hms_ago(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


def audio_text(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds < 3600 else f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def banner(subtitle=None):
    t = Text(BANNER, style=f"bold {GOLD}")
    t.append(f"  worker {APP_VERSION}\n", style=DIM)
    t.append(f"  {DISCLAIMER.rstrip('.')}\n", style=DIM)
    if subtitle:
        t.append(f"  {subtitle}\n", style=PARCH)
    return t


def start_screen(health, subtitle=None):
    """Banner, a few health lines ((label, text, style) rows) and the menu."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=f"bold {PARCH}", min_width=7)
    grid.add_column()
    for label, text, style in health:
        grid.add_row(label, Text(text, style=style))
    menu = Table.grid(padding=(0, 2))
    menu.add_column(style=f"bold {GOLD}")
    menu.add_column(style=PARCH, min_width=22)
    menu.add_column(style=DIM)
    for k, name, note in MENU:
        menu.add_row(f"[{k}]", name, note)
    return Group(banner(subtitle), grid, Text(""), menu, Text(""), Text("  › Press a key", style=DIM))


def _frame(body, title, footer):
    return Panel(body, box=box.DOUBLE, border_style=EDGE, padding=(1, 2),
                 title=Text(f"◆ {title} ◆", style=f"bold {GOLD}"),
                 subtitle=Text(footer, style=DIM), style=PARCH)


def board(snap, mode_label="run now", state="making", now=None, keys="P pause   S stop after job   Q quit",
          session_start=None):
    """The live stats board (concept B). `snap` is a BoardData.read() dict."""
    now = now or time.time()
    g = Table.grid(padding=(0, 2))
    g.add_column(style=f"bold {PARCH}", min_width=10)
    g.add_column()

    if state == "paused":
        g.add_row("Now", Text("❚❚ paused", style=WARN))
    elif snap.get("making"):
        since = snap.get("current_since") or now
        g.add_row("Now", Text(f"● job {str(snap.get('current_job', ''))[:6]} · {snap.get('current_lines', 0)} lines"
                              f" · running {hms_ago(now - since)}", style=GOOD))
    else:
        g.add_row("Now", Text("○ waiting for the next job", style=DIM))
    g.add_row("", Text(""))
    g.add_row("This PC", Text(f"{snap.get('lines', 0):,} lines made · {snap.get('jobs', 0)} jobs", style=PARCH))
    g.add_row("Words", Text(f"{snap.get('words', 0):,}", style=PARCH))
    g.add_row("Audio", Text(audio_text(snap.get("audio_seconds")), style=PARCH))
    lph = snap.get("lines_per_hour") or 0
    g.add_row("Speed", Text(f"{lph:g} lines/hour", style=PARCH))
    q = snap.get("queue")
    g.add_row("Queue", Text(f"{q['queued_lines']:,} lines waiting" if q else "queue unavailable (offline?)",
                            style=PARCH if q else DIM))
    g.add_row("Time left", Text(f"about {stats.format_duration(snap['time_left'])} at today's speed"
                                if snap.get("time_left") is not None else "-", style=PARCH))
    g.add_row("", Text(""))
    gpu = snap.get("gpu")
    if gpu:
        row = Text()
        row.append(f"{gpu['temp_c']}°C  ", style=heat_color(gpu["temp_c"]))
        row.append_text(bar(gpu["temp_c"] / 90, 16, heat_color(gpu["temp_c"])))
        g.add_row("GPU temp", row)
        row = Text()
        row.append(f"{gpu['util_pct']:>3}%   ", style=PARCH)
        row.append_text(bar(gpu["util_pct"] / 100, 16))
        g.add_row("GPU load", row)
        row = Text()
        row.append(f"{gpu['mem_used_mib'] / 1024:.1f}/{gpu['mem_total_mib'] / 1024:.0f} GB  ", style=PARCH)
        row.append_text(bar(gpu["mem_used_mib"] / max(1, gpu["mem_total_mib"]), 16))
        g.add_row("VRAM", row)
    else:
        g.add_row("GPU", Text("no reading", style=DIM))
    speed = snap.get("speed") or []
    g.add_row("Last hour", Text(stats.sparkline(speed), style=GOLD) if speed else Text("-", style=DIM))
    g.add_row("", Text(""))
    recent = list(snap.get("recent") or [])[-6:][::-1]
    if not recent:
        g.add_row("Events", Text("nothing yet", style=DIM))
    for i, (ts, kind, text) in enumerate(recent):
        mark, style = {"ok": ("✓", GOOD), "warn": ("!", WARN), "info": ("·", DIM)}.get(kind, ("·", DIM))
        clock = time.strftime("%H:%M", time.localtime(ts))
        g.add_row("Events" if i == 0 else "", Text(f"{clock} {mark} {text}", style=style))
    return _frame(g, f"{APP_NAME} · {mode_label}", f"{keys}")


def service_panel(snap, idle_minutes=10, today_lines=0, state="idle", now=None):
    """The service / idle view (concept C), shown after installing and in the board viewer."""
    g = Table.grid(padding=(0, 2))
    g.add_column(style=f"bold {PARCH}", min_width=10)
    g.add_column()
    dot, text, style = {"making": ("●", "making voice lines now", GOOD),
                        "paused": ("❚❚", "paused", WARN)}.get(state, ("●", "waiting for idle", GOLD))
    g.add_row("Status", Text(f"{dot} {text}", style=style))
    g.add_row("Idle rule", Text(f"start after {idle_minutes} min idle; stop the moment you are back", style=PARCH))
    g.add_row("Never when", Text("a full-screen game is running, or on battery", style=PARCH))
    g.add_row("", Text(""))
    g.add_row("Today", Text(f"{today_lines:,} lines made while you were away", style=PARCH))
    g.add_row("All time", Text(f"{snap.get('lines', 0):,} lines on this PC (this log)", style=PARCH))
    g.add_row("", Text(""))
    g.add_row("", Text("This window can close; the service keeps running.", style=DIM))
    return _frame(g, f"{APP_NAME} · service", "Q close   (U uninstall from Settings)")


def message_panel(title, lines, style=PARCH):
    body = Group(*[Text(ln, style=style) for ln in lines])
    return Panel(body, box=box.ROUNDED, border_style=EDGE, title=Text(title, style=f"bold {GOLD}"), padding=(1, 2))
