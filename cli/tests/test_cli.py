import io
import json
import logging
import os
import sys
import time
import urllib.error
from pathlib import Path

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speakstone_worker import board, enroll, idle, service, setup, stats  # noqa: E402
from speakstone_worker.settings import in_pause_window  # noqa: E402

KEY = "sswk_" + "A" * 43


class FakeResp:
    def __init__(self, status, body):
        self.status, self._b = status, json.dumps(body).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


# ------------------------------------------------------------------ enrol
def test_enroll_success_sends_payload_and_returns_key():
    seen = {}

    def opener(req, timeout=0):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return FakeResp(201, {"worker_id": "w1", "key": KEY, "label": "Brave Otter", "tier": "new"})

    r = enroll.enroll("https://x.test/", "PC", "RTX 3070 Ti", 8192, "Windows 11", "1.0.1", opener=opener)
    assert seen["url"] == "https://x.test/api/worker/enroll"
    assert set(seen["body"]) == {"machine_name", "gpu", "vram_mib", "os", "worker_version"}
    assert r["key"] == KEY and r["label"] == "Brave Otter"


@pytest.mark.parametrize("code,word", [(429, "Too many"), (503, "not taking"), (422, "did not accept")])
def test_enroll_error_mapping(code, word):
    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b'{"error":"' + KEY.encode() + b'"}'))
    with pytest.raises(enroll.EnrollError) as e:
        enroll.enroll("https://x.test", "PC", "g", 8192, "os", "1", opener=opener)
    assert word in str(e.value) and KEY not in str(e.value)


def test_enroll_unreachable():
    def opener(req, timeout=0):
        raise urllib.error.URLError("down")
    with pytest.raises(enroll.EnrollError, match="Could not reach"):
        enroll.enroll("https://x.test", "PC", "g", 8192, "os", "1", opener=opener)


def test_api_base_env_override(monkeypatch):
    from speakstone_worker import paths
    monkeypatch.setenv("SPEAKSTONE_API_BASE", "http://localhost:9/")
    assert paths.api_base() == "http://localhost:9"


# ------------------------------------------------------------------ setup
def make_env(tmp_path, calls, logs, fail_at=None, gpu=8192):
    res = tmp_path / "res"
    res.mkdir(parents=True, exist_ok=True)
    for n in setup.WORKER_FILES:
        (res / n).write_text("# worker")
    inst = tmp_path / "inst"

    def run(cmd, env, cwd, on_line):
        c = [str(x) for x in cmd]
        calls.append((c, dict(env)))
        if "python" in c and "install" in c and not (inst / "venv").exists():
            pass
        if len(c) > 2 and c[2] == "init":                    # net_worker.py init writes the config
            (inst / "worker_config.json").write_text("{}")
        if "venv" in c:
            (inst / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
            (inst / "venv" / "Scripts" / "python.exe").write_text("")
        if fail_at and fail_at in " ".join(c):
            return 1
        on_line("ok")
        return 0

    def download(url, dest, cb):
        import zipfile
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("uv.exe" if "uv" in url else ("it/pyproject.toml" if "index-tts" in url else "bin/ffmpeg.exe"), "x")

    def do_enroll():
        return {"key": KEY, "label": "Brave Otter", "worker_id": "w1", "tier": "new"}

    return setup.Env(inst, res, download=download, run=run, enroll=do_enroll, gpu_total_mib=lambda: gpu,
                     progress=lambda s, f, m: logs.append((s, m)), is_win=True)


@pytest.fixture(autouse=True)
def big_disk(monkeypatch):
    import shutil
    from collections import namedtuple
    U = namedtuple("U", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(1, 1, 500 * 1024 ** 3))


def test_torch_pin_present_and_never_unpinned_reinstall(tmp_path):
    calls, logs = [], []
    env = make_env(tmp_path, calls, logs)
    setup.run_setup(env)
    torch_cmds = [c for c, _ in calls if "torch==2.8.0+cu128" in c]
    assert torch_cmds and "torchaudio==2.8.0+cu128" in torch_cmds[0]
    assert "--index-url" in torch_cmds[0] and setup.TORCH_INDEX in torch_cmds[0]
    assert not any("--reinstall" in c and "torch" in c for c, _ in calls)
    assert setup.TORCH_PINS == ("torch==2.8.0+cu128", "torchaudio==2.8.0+cu128")


def test_key_only_in_child_env_not_argv_logs_or_files(tmp_path, caplog):
    calls, logs = [], []
    env = make_env(tmp_path, calls, logs)
    setup.run_setup(env)
    init = [(c, e) for c, e in calls if len(c) > 2 and c[2] == "init"]
    assert len(init) == 1 and init[0][1]["SSW_WORKER_KEY"] == KEY
    assert all(KEY not in " ".join(c) for c, _ in calls)
    assert all(KEY not in m for _, m in logs)
    assert all("SSW_WORKER_KEY" not in e for c, e in calls if len(c) < 3 or c[2] != "init")
    for f in env.install.rglob("*"):
        if f.is_file() and f.stat().st_size < 1_000_000:
            assert KEY not in f.read_text(errors="ignore"), f
    assert json.loads(env.enrolled_file.read_text())["label"] == "Brave Otter"
    assert env.progress and "Joined the queue as Brave Otter" in [m for _, m in logs]


def test_setup_resumes_after_interruption(tmp_path):
    calls, logs = [], []
    env = make_env(tmp_path, calls, logs, fail_at="snapshot_download")
    with pytest.raises(setup.SetupError):
        setup.run_setup(env)
    assert "model" not in setup.load_state(env) and "torch" in setup.load_state(env)
    calls.clear()
    env2 = make_env(tmp_path, calls, logs)
    ran = setup.run_setup(env2)
    assert "torch" not in ran and "uv" not in ran and "model" in ran and "enroll" in ran
    assert setup.is_set_up(env2.install)


def test_gpu_too_small_and_missing(tmp_path):
    with pytest.raises(setup.SetupError, match="8 GB"):
        setup.run_setup(make_env(tmp_path, [], [], gpu=4096))
    with pytest.raises(setup.SetupError, match="No NVIDIA"):
        setup.run_setup(make_env(tmp_path / "b", [], [], gpu=None))


def test_model_download_disables_hf_transfer(tmp_path):
    calls = []
    setup.run_setup(make_env(tmp_path, calls, []))
    model = [e for c, e in calls if "snapshot_download" in " ".join(c)][0]
    assert model["HF_HUB_ENABLE_HF_TRANSFER"] == "0"


# ------------------------------------------------------------------ board
def sample_state():
    now = time.time()
    return {"lines": 1284, "words": 38910, "audio_min": 271, "lines_hour": 212, "words_hour": 4210,
            "since_label": "08:14", "queue": {"queued_lines": 12406, "eta_seconds": 3 * 3600 + 12 * 60},
            "time_left": 3 * 3600 + 12 * 60, "spark": [1, 2, 3, 3, 4, 5, 6, 5, 4, 5, 6, 8],
            "gpu": {"name": "NVIDIA RTX 3070 Ti", "util_pct": 92, "temp_c": 68, "mem_used_mib": 4608,
                    "mem_total_mib": 8192},
            "making": True, "job": "abcdef123456", "job_lines": 12, "job_pct": 0.68, "state": "idle",
            "events": [(now, "done", "job abcdef12 - 12 lines", "42 s"), (now - 90, "retry", "job 1234", "picked up again")]}


def render(renderable, width=100, height=30):
    c = Console(file=io.StringIO(), width=width, height=height, force_terminal=True, color_system="truecolor")
    c.print(renderable)
    return c.file.getvalue()


def plain(renderable, width=100, height=30):
    c = Console(file=io.StringIO(), width=width, height=height, force_terminal=False, color_system=None)
    c.print(renderable)
    return c.file.getvalue()


def test_board_snapshot_contains_every_box():
    out = plain(board.render_board(sample_state()))
    for needle in ("LINES TODAY", "1,284", "WORDS", "38,910", "AUDIO MINUTES", "271", "QUEUE / TIME LEFT", "12,406",
                   "~ 3 h 12 m", "GPU HEALTH", "RTX 3070 Ti", "Load", "92%", "68 C", "4.5/8 GB", "NOW MAKING",
                   "68%", "RECENT EVENTS", "done", "retry", "picked up again", "[P] pause", "[S] stop and exit",
                   "[M] menu", "Unofficial; not affiliated with Blizzard Entertainment"):
        assert needle in out, needle
    assert "▁" in out or "█" in out
    assert "┌" in out                                     # the mock-up's square corners


def test_board_uses_true_colour_gold():
    assert "38;2;233;187;77" in render(board.render_board(sample_state()))


def test_board_when_paused_and_without_gpu_or_queue():
    s = sample_state() | {"state": "paused", "gpu": None, "queue": None, "time_left": None, "events": []}
    out = plain(board.render_board(s))
    assert "Paused" in out and "[P] resume" in out and "Nothing finished yet." in out


def test_menu_and_banner_wording():
    out = plain(board.menu_screen(), width=80)
    for needle in ("[1] Start now", "[2] Install as a service (runs when your PC is idle)", "[3] Settings", "[Q] Quit",
                   "Lend your graphics card to the voice-line queue.",
                   "Unofficial; not affiliated with Blizzard Entertainment", "█████"):
        assert needle in out, needle
    low = out.lower() + plain(board.render_board(sample_state())).lower()
    for bad in (" ai ", "clone", "generated", "synthesis", "reference"):
        assert bad not in low


def test_setup_board_renders_ticks_and_bar():
    out = plain(board.render_setup(list(setup.STEPS), setup.LABELS, {"gpu", "folders"}, "model", 0.57, "x", "3.4 of 6.0 GB"))
    assert "✓" in out and "●" in out and "57%" in out and "3.4 of 6.0 GB" in out


def test_summarize_log_events():
    now = time.time()
    t = lambda off: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - off))
    lines = [f"{t(300)} claim job=job12345678 attempt=1 lines=10",
             f"{t(120)} complete job=job12345678 ok=9 held=1 seconds=42.0 words=300 audio=120",
             f"{t(60)} claim job=job22222222 attempt=1 lines=5"]
    ev = [stats.parse_line(x) for x in lines]
    s = stats.summarize(ev, now)
    assert s["lines"] == 10 and s["words"] == 300 and s["audio_min"] == 2 and s["making"] and s["job"] == "job22222222"
    assert s["events"][0][1] == "done"


# ------------------------------------------------------------------ service / idle
def test_windows_task_xml_and_commands():
    prog, args = service.launch_command(frozen=True, executable=r"C:\Apps\SpeakStoneWorker.exe")
    xml = service.build_task_xml(prog, args)
    assert "LogonTrigger" in xml and "--background" in xml and r"C:\Apps\SpeakStoneWorker.exe" in xml
    assert "LeastPrivilege" in xml and "<Hidden>true</Hidden>" in xml


def test_install_windows_calls_schtasks_only(tmp_path):
    calls = []

    class R:
        returncode = 0

    ok = service.install(tmp_path, runner=lambda c, **k: calls.append(c) or R(), platform="win32", frozen=True,
                         executable="C:/x/SpeakStoneWorker.exe")
    assert ok and calls[0][:3] == ["schtasks", "/Create", "/TN"] and calls[0][3] == service.TASK_NAME and "/XML" in calls[0]
    assert (tmp_path / "task.xml").exists()
    calls.clear()
    assert service.uninstall(runner=lambda c, **k: calls.append(c) or R(), platform="win32")
    assert ["schtasks", "/Delete", "/TN", service.TASK_NAME, "/F"] in calls


def test_install_linux_writes_unit_and_enables(tmp_path):
    calls = []

    class R:
        returncode = 0

    ok = service.install(tmp_path / "d", runner=lambda c, **k: calls.append(c) or R(), platform="linux",
                         unit_dir=tmp_path / "units", frozen=False, executable="/usr/bin/python3")
    unit = (tmp_path / "units" / service.UNIT_NAME).read_text()
    assert ok and "ExecStart=/usr/bin/python3 -m speakstone_worker --background" in unit and "Nice=10" in unit
    assert ["systemctl", "--user", "enable", "--now", service.UNIT_NAME] in calls


def test_idle_decide():
    d = idle.decide
    assert d("idle", False, 700, 10, False, False, False) == "start"
    assert d("idle", False, 700, 10, True, False, False) == "keep"          # full-screen app
    assert d("idle", False, 700, 10, False, True, False) == "keep"          # battery
    assert d("idle", True, 3, 10, False, False, False) == "stop"            # user is back
    assert d("idle", True, 700, 10, False, False, True) == "stop"           # paused
    assert d("idle", False, 100, 10, False, False, False) == "keep"


def test_pause_window_overnight():
    assert in_pause_window(23 * 60, "22:00", "06:00") and in_pause_window(3 * 60, "22:00", "06:00")
    assert not in_pause_window(12 * 60, "22:00", "06:00") and not in_pause_window(5, "", "")


def test_runner_stop_uses_stop_file_and_hides_key(tmp_path):
    from speakstone_worker.runner import WorkerRunner

    class P:
        def poll(self):
            return None

    seen = {}

    def popen(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return P()

    r = WorkerRunner(tmp_path, popen=popen)
    os.environ["SSW_WORKER_KEY"] = KEY
    try:
        r.start()
    finally:
        del os.environ["SSW_WORKER_KEY"]
    assert "SSW_WORKER_KEY" not in seen["env"] and KEY not in " ".join(seen["cmd"])
    r.request_stop()
    assert (tmp_path / "work" / "STOP").exists()
