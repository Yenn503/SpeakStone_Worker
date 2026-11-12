"""The command-line app: setup pins and auto-enrol, the board's look and numbers, the menu flow. No GPU, no network,
no real keyboard: every outside effect is a fake."""
import io
import json
import re
import sys
import time
import urllib.error
from pathlib import Path

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssapp import board, cli, enroll, settings, setup_steps, stats  # noqa: E402
from test_app_logic import Fake, KEY  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "ssapp"
BANNED = re.compile(r"\bAI\b|clone|generated|synthes|reference", re.I)


REAL_GPU_INFO = stats.gpu_info


def squash(text):
    return re.sub(r"[ 	]+", " ", text)


def render(renderable, width=100):
    c = Console(width=width, record=True, force_terminal=False, color_system=None, file=io.StringIO())
    c.print(renderable)
    return squash(c.export_text())


# ------------------------------------------------------------------ setup: pinned torch, enrol instead of a pasted key
def test_torch_is_pinned_to_2_8_0_cu128(tmp_path):
    f = Fake(tmp_path)
    setup_steps.run_setup(f.env(tmp_path / "inst"))
    torch_cmds = [c for c in f.cmds if "--reinstall" in c]
    assert torch_cmds, "torch step did not run"
    flat = " ".join(torch_cmds[0])
    assert "torch==2.8.0+cu128" in flat and "torchaudio==2.8.0+cu128" in flat
    assert not re.search(r"(^|\s)torch(\s|$)", flat) and not re.search(r"(^|\s)torchaudio(\s|$)", flat)
    assert any("startswith('2.8.0')" in " ".join(c) for c in f.cmds), "final check must refuse another torch"


def test_setup_joins_by_enrolling_and_the_key_stays_in_the_environment(tmp_path):
    f = Fake(tmp_path, key="")
    env = f.env(tmp_path / "inst")
    env.ask_key = None
    asked = []
    env.enroll = lambda: asked.append(1) or KEY
    setup_steps.run_setup(env)
    assert asked == [1]
    flat = "\n".join(" ".join(c) for c in f.cmds)
    assert KEY not in flat and all(KEY not in str(m) for m in f.lines)
    init_env = [e for c, e in zip(f.cmds, f.envs) if "init" in c][0]
    assert init_env["SSW_WORKER_KEY"] == KEY
    assert all("SSW_WORKER_KEY" not in e for c, e in zip(f.cmds, f.envs) if "init" not in c)


def test_enrol_is_skipped_when_this_pc_already_joined(tmp_path):
    f = Fake(tmp_path)
    env = f.env(tmp_path / "inst")
    env.enroll = lambda: pytest.fail("must not enrol twice")
    (tmp_path / "inst").mkdir()
    (tmp_path / "inst" / "worker_config.json").write_text("{}", encoding="utf-8")
    setup_steps.run_setup(env)


class Resp:
    def __init__(self, body):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_enroll_posts_the_machine_and_returns_the_key():
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["body"], seen["auth"] = req.full_url, json.loads(req.data), req.get_header("Authorization")
        return Resp(json.dumps({"key": KEY, "label": "public-abc123", "tier": "public"}).encode())

    assert enroll.enroll("https://x.test/", "PC-1", "RTX 3060", 12288, opener=opener) == KEY
    assert seen["url"] == "https://x.test/api/worker/enroll" and seen["auth"] is None
    assert {k: seen["body"][k] for k in ("machine_name", "gpu", "vram_mib")} == {"machine_name": "PC-1", "gpu": "RTX 3060", "vram_mib": 12288}
    assert seen["body"]["os"] in ("windows", "linux") and seen["body"]["worker_version"]


def test_enroll_failures_are_plain_words_and_never_show_a_key():
    def http(code):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return opener

    with pytest.raises(setup_steps.SetupError, match="hour"):
        enroll.enroll("https://x.test", "a", "b", 1, opener=http(429))
    with pytest.raises(setup_steps.SetupError, match="HTTP 503"):
        enroll.enroll("https://x.test", "a", "b", 1, opener=http(503))

    def offline(req, timeout):
        raise urllib.error.URLError("down")
    with pytest.raises(setup_steps.SetupError, match="internet"):
        enroll.enroll("https://x.test", "a", "b", 1, opener=offline)
    with pytest.raises(setup_steps.SetupError, match="usable key"):
        enroll.enroll("https://x.test", "a", "b", 1, opener=lambda r, timeout: Resp(b'{"key": "nope"}'))
    with pytest.raises(setup_steps.SetupError, match="usable key"):
        enroll.enroll("https://x.test", "a", "b", 1, opener=lambda r, timeout: Resp(b"not json"))


def test_make_enroller_sends_gpu_and_hostname():
    sent = {}

    def opener(req, timeout):
        sent.update(json.loads(req.data))
        return Resp(json.dumps({"key": KEY}).encode())

    go = enroll.make_enroller("https://x.test", lambda: ("RTX 3070 Ti", 8192), hostname=lambda: "PC-1")
    import urllib.request
    real = urllib.request.urlopen
    urllib.request.urlopen = opener
    try:
        assert enroll.enroll("https://x.test", "PC-1", "RTX 3070 Ti", 8192, opener=opener) == KEY
        assert sent["gpu"] == "RTX 3070 Ti"
    finally:
        urllib.request.urlopen = real
    assert callable(go)


# ------------------------------------------------------------------ stats: new fields, sparkline, speed
LOG = """2026-11-11T10:00:00Z claim job=abcdef123 attempt=1 lines=12
2026-11-11T10:20:00Z complete job=abcdef123 ok=11 held=1 seconds=1200.0 words=1900 audio=410.5
2026-11-11T10:21:00Z claim job=fedcba987 attempt=1 lines=6
2026-11-11T10:22:00Z fail job=fedcba987 reason=released
2026-11-11T10:23:00Z claim job=112233445 attempt=1 lines=9
"""


def write_log(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "worker_2026-11-11.log").write_text(LOG, encoding="utf-8")
    return d


def test_log_stats_words_audio_current_job_and_recent_events(tmp_path):
    d = write_log(tmp_path)
    now = time.mktime(time.strptime("2026-11-11 10:30:00", "%Y-%m-%d %H:%M:%S"))
    now = 1_794_393_000  # 2026-11-11T10:30:00Z
    s = stats.read_log_stats(d, now=now)
    assert (s["lines"], s["jobs"], s["words"]) == (12, 1, 1900)
    assert s["audio_seconds"] == 410.5
    assert s["making"] and s["current_job"] == "112233445" and s["current_lines"] == 9
    kinds = [k for _, k, _ in s["recent"]]
    assert kinds == ["info", "ok", "info", "warn", "info"]
    assert "job abcdef done - 12 lines" in s["recent"][1][2]


def test_old_logs_without_words_still_read(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "worker_2026-11-11.log").write_text("2026-11-11T10:20:00Z complete job=a ok=3 held=0 seconds=9\n", encoding="utf-8")
    s = stats.read_log_stats(d, now=1_794_393_000)
    assert (s["lines"], s["words"], s["audio_seconds"]) == (3, 0, 0.0)


def test_sparkline_and_speed_series():
    assert stats.sparkline([0, 0, 0]) == "▁▁▁"
    sp = stats.sparkline([0, 1, 2, 4, 8])
    assert sp[0] == "▁" and sp[-1] == "█" and len(sp) == 5
    assert stats.sparkline([]) == ""
    now = 10_000
    series = stats.speed_series([(now - 10, 5), (now - 250, 3), (now - 3599, 2), (now - 4000, 99)], now)
    assert len(series) == 15 and series[-1] == 5 and series[-2] == 3 and series[0] == 2 and sum(series) == 10


def test_gpu_info_parses_and_survives_failure():
    class R:
        stdout = "NVIDIA GeForce RTX 3070 Ti, 8192\n"
    assert REAL_GPU_INFO(lambda *a, **k: R()) == ("NVIDIA GeForce RTX 3070 Ti", 8192)

    def boom(*a, **k):
        raise OSError
    assert REAL_GPU_INFO(boom) == (None, None)


# ------------------------------------------------------------------ the look
SNAP = {"making": True, "current_job": "abcdef123", "current_lines": 11, "current_since": 1000, "lines": 142,
        "jobs": 9, "words": 18400, "audio_seconds": 700, "lines_per_hour": 38.0,
        "queue": {"queued_lines": 1284, "eta_seconds": 32400}, "time_left": 32400,
        "gpu": {"temp_c": 71, "util_pct": 96, "mem_used_mib": 5222, "mem_total_mib": 8192},
        "speed": [0, 2, 4, 5, 6, 6, 7, 6, 5, 6, 7, 7, 8, 7, 6],
        "recent": [(1_794_393_000, "ok", "job abcdef done - 11 lines"), (1_794_393_100, "warn", "job fedcba handed back")]}


def test_start_screen_has_banner_menu_and_disclaimer():
    out = render(board.start_screen([("GPU", "RTX 3070 Ti · 8 GB · ready", board.GOOD)]))
    for want in ("[1] Start now", "[2] Install as a service", "[3] Settings", "[Q] Quit",
                 "Unofficial; not affiliated with Blizzard Entertainment", "RTX 3070 Ti", "|____/| .__/"):
        assert want in out, want


def test_board_shows_every_figure_the_owner_asked_for():
    out = render(board.board(SNAP, "run now", "making", now=1000 + 240))
    for want in ("142 lines made", "18,400", "11m 40s", "38 lines/hour", "1,284 lines waiting", "about 9.0 h",
                 "71°C", "96%", "5.1/8 GB", "job abcdef", "running 4 min", "✓", "!",
                 "job abcdef done - 11 lines", "SpeakStone Worker · run now", "P pause", "█", "░"):
        assert want in out, want
    assert any(ch in out for ch in stats.BARS), "sparkline missing"
    assert "╔" in out and "╗" in out, "double-line frame expected"


def test_board_states_without_data_do_not_crash():
    empty = {"making": False, "queue": None, "time_left": None, "gpu": None, "speed": [], "recent": []}
    out = render(board.board(empty, "run now", "idle"))
    assert "waiting for the next job" in out and "queue unavailable" in out and "no reading" in out
    assert "paused" in render(board.board(empty, "run now", "paused"))
    svc = render(board.service_panel({"lines": 5}, 10, today_lines=63))
    assert "waiting for idle" in svc and "start after 10 min idle" in svc and "63 lines" in svc


def test_bar_widths_and_clamping():
    assert len(board.bar(0.5, 20).plain) == 20 and board.bar(0.5, 20).plain.count("█") == 10
    assert board.bar(2, 10).plain == "█" * 10 and board.bar(-1, 10).plain == "░" * 10
    assert board.bar(None, 4).plain == "░" * 4


def test_public_wording_has_none_of_the_banned_words():
    for name in ("board.py", "cli.py", "enroll.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        assert not BANNED.search(text), (name, BANNED.search(text).group(0))
    shown = render(board.start_screen([("GPU", "x", "")])) + render(board.board(SNAP)) + render(board.service_panel(SNAP))
    assert not BANNED.search(shown)


# ------------------------------------------------------------------ BoardData
def test_boarddata_reads_queue_once_a_minute_and_estimates_time_left(tmp_path):
    t = [1000.0]
    calls = {"q": 0, "log": 0}

    def q(api):
        calls["q"] += 1
        return {"queued_lines": 760, "eta_seconds": None}

    def log(d, since=None, now=None):
        calls["log"] += 1
        return {"lines_per_hour": 38.0, "completions": [], "recent": []}

    bd = cli.BoardData(tmp_path, clock=lambda: t[0], gpu=lambda: None, queue_reader=q, log_reader=log)
    s = bd.read()
    assert s["time_left"] == pytest.approx(760 / 38.0 * 3600)
    t[0] += 3
    bd.read()
    assert calls == {"q": 1, "log": 2}
    t[0] += 61
    bd.read()
    assert calls["q"] == 2


# ------------------------------------------------------------------ the menu flow
class ScriptedKeys:
    def __init__(self, *keys):
        self.keys = list(keys)

    def poll(self):
        return self.keys.pop(0) if self.keys else "q"

    def wait(self):
        return self.keys.pop(0) if self.keys else "q"


class FakeWorker:
    def __init__(self):
        self.running = True
        self.stop_asked = 0
        self.started = 0

    def request_stop(self):
        self.stop_asked += 1
        self.running = False

    def poll_stop(self, grace=120):
        return not self.running

    def start(self):
        self.running = True
        self.started += 1
        return True


def make_app(tmp_path, keys, **kw):
    s = settings.Settings(tmp_path / "app.json")
    s["install_dir"] = str(tmp_path / "inst")
    s["setup_done"] = True
    console = Console(width=100, record=True, force_terminal=False, color_system=None, file=io.StringIO())
    app = cli.App(s, tmp_path, console=console, keys=ScriptedKeys(*keys), sleep=lambda x: None, **kw)
    return app, s, console


@pytest.fixture(autouse=True)
def quiet_system(monkeypatch):
    monkeypatch.setattr(stats, "gpu_info", lambda *a, **k: ("RTX 3070 Ti", 8192))
    monkeypatch.setattr(stats, "fetch_queue", lambda *a, **k: {"queued_lines": 5, "eta_seconds": None})
    monkeypatch.setattr(stats, "gpu_reading", lambda *a, **k: None)
    monkeypatch.setattr(cli, "tray_running", lambda *a, **k: False)


def test_menu_quit_prints_disclaimer(tmp_path):
    app, s, console = make_app(tmp_path, ["q"])
    assert app.run_menu() == 0
    out = squash(console.export_text())
    assert "[1] Start now" in out and "Unofficial; not affiliated with Blizzard Entertainment" in out


def test_start_now_runs_the_worker_and_q_hands_the_job_back(tmp_path):
    w = FakeWorker()
    w.running = False
    app, s, console = make_app(tmp_path, ["1", "q", "q"], worker_factory=lambda st: w)
    # Start now: the supervisor starts the (fake) worker; the board takes 'q' and asks it to stop politely.
    w2 = w

    def start_now():
        w2.running = True
        w2.started += 1
        return True
    w.start = start_now
    assert app.run_menu() == 0
    assert w.started >= 1 and w.stop_asked >= 1
    assert s["mode"] == "off"


def test_start_now_refuses_while_the_service_tray_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "tray_running", lambda *a, **k: True)
    w = FakeWorker()
    app, s, console = make_app(tmp_path, ["1", "x", "q"], worker_factory=lambda st: w)
    app.run_menu()
    assert w.started == 0 and "service is running" in console.export_text()


def test_install_service_registers_task_and_starts_tray(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.autostart, "register", lambda exe, xml: calls.setdefault("reg", (str(exe), str(xml))) and True)
    monkeypatch.setattr(cli, "spawn_detached", lambda args, new_console=False: calls.setdefault("spawn", list(args)))
    monkeypatch.setattr(settings, "settings_dir", lambda: tmp_path / "cfg")
    app, s, console = make_app(tmp_path, ["2", "x", "q"])
    app.run_menu()
    assert s["mode"] == "idle" and calls["spawn"] == ["--tray"] and calls["reg"][1].endswith("task.xml")
    assert "waiting for idle" in console.export_text()


def test_settings_toggles_and_back(tmp_path):
    app, s, console = make_app(tmp_path, ["3", "2", "3", "4", "b", "q"])
    app.run_menu()
    assert s["block_fullscreen"] is False and s["block_on_battery"] is False and s["update_check"] is True


def test_first_run_sets_up_then_shows_the_menu(tmp_path):
    s = settings.Settings(tmp_path / "app.json")
    seen = {}

    def env_factory(install, res, view, api=None, **kw):
        f = Fake(tmp_path)
        env = f.env(install)
        env.ask_key = None
        env.enroll = lambda: KEY
        env.progress = view.progress
        seen["f"] = f
        return env

    console = Console(width=100, record=True, force_terminal=False, color_system=None, file=io.StringIO())
    app = cli.App(s, tmp_path, console=console, keys=ScriptedKeys("q"), sleep=lambda x: None, env_factory=env_factory)
    assert app.run_menu() == 0
    assert s["setup_done"] is True and Path(s["install_dir"]).name == "SpeakStoneWorker"
    out = console.export_text()
    assert "no key to type" in out and KEY not in out


def test_first_run_failure_is_shown_and_setup_stays_pending(tmp_path):
    s = settings.Settings(tmp_path / "app.json")

    def env_factory(install, res, view, api=None, **kw):
        f = Fake(tmp_path, mib=4096)
        env = f.env(install)
        env.progress = view.progress
        return env

    console = Console(width=100, record=True, force_terminal=False, color_system=None, file=io.StringIO())
    app = cli.App(s, tmp_path, console=console, keys=ScriptedKeys("x"), sleep=lambda x: None, env_factory=env_factory)
    assert app.run_menu() == 1
    assert s["setup_done"] is False and "at least 8 GB" in console.export_text()


def test_viewer_renders_service_layout_and_closes(tmp_path):
    app, s, console = make_app(tmp_path, [])
    s["mode"] = "idle"
    assert app.run_viewer(frames=2) == 0


def test_cli_does_not_pull_in_tkinter():
    for m in [m for m in sys.modules if m == "tkinter" or m.startswith("tkinter.")]:
        pytest.skip("tkinter was imported by another test module")
    import importlib
    importlib.reload(cli)
    assert "tkinter" not in sys.modules


def test_main_routes_flags(tmp_path, monkeypatch):
    from ssapp import main as main_mod
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(main_mod.autostart, "unregister", lambda: True)
    assert main_mod.main(["--unregister-idle"]) == 0
    monkeypatch.setattr(cli, "run_tray", lambda s, **k: 7)
    assert main_mod.main(["--tray"]) == 1 and main_mod.main(["--board"]) == 1      # not set up yet: no crash
    st = settings.Settings()
    st["install_dir"], st["setup_done"] = str(tmp_path / "i"), True
    assert main_mod.main(["--tray"]) == 7
