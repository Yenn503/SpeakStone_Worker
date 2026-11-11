import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ssapp import autostart, controller, idle, setup_steps, settings, stats, supervisor, updater  # noqa: E402

KEY = "sswk_" + "A" * 43


# ------------------------------------------------------------------ idle.decide
def d(**kw):
    a = dict(mode="idle", running=False, idle_seconds=0, idle_minutes=10, fullscreen=False, on_battery=False,
             paused=False)
    a.update(kw)
    return idle.decide(**a)


def test_idle_starts_only_after_n_minutes():
    assert d(idle_seconds=599) == "keep"
    assert d(idle_seconds=600) == "start"


def test_idle_never_starts_on_battery_or_fullscreen():
    assert d(idle_seconds=9999, on_battery=True) == "keep"
    assert d(idle_seconds=9999, fullscreen=True) == "keep"
    assert d(idle_seconds=9999, fullscreen=True, block_fullscreen=False) == "start"


def test_idle_stops_when_user_returns_or_blocked():
    assert d(running=True, idle_seconds=2) == "stop"
    assert d(running=True, idle_seconds=900) == "keep"
    assert d(running=True, idle_seconds=900, fullscreen=True) == "stop"
    assert d(running=True, idle_seconds=900, on_battery=True) == "stop"


def test_pause_and_off_win():
    assert d(running=True, idle_seconds=900, paused=True) == "stop"
    assert d(mode="now", paused=True) == "keep"
    assert d(mode="now") == "start"
    assert d(mode="now", running=True, idle_seconds=0) == "keep"
    assert d(mode="off", running=True) == "stop"


def test_state_dot():
    assert idle.state_dot(True, True, True) == "paused"
    assert idle.state_dot(False, True, True) == "making"
    assert idle.state_dot(False, True, False) == "idle"
    assert idle.state_dot(False, False, True) == "idle"
    assert idle.state_dot(False, False, False, error=True) == "error"


# ------------------------------------------------------------------ settings / pause
def test_settings_roundtrip_and_pause(tmp_path):
    s = settings.Settings(tmp_path / "app.json")
    assert s["mode"] == "off" and s["update_check"] is False
    s["mode"] = "idle"
    s.pause_for(30, now=1000)
    s2 = settings.Settings(tmp_path / "app.json")
    assert s2["mode"] == "idle" and s2.paused(now=1000 + 29 * 60) and not s2.paused(now=1000 + 31 * 60)
    s2.resume()
    assert not s2.paused(now=1001)


# ------------------------------------------------------------------ stats from the worker log
LOG = """2026-11-11T10:00:00Z start job=- version=1.0.1
2026-11-11T10:01:00Z claim job=aaa attempt=1 lines=12 lease_until=x
2026-11-11T10:03:00Z run_start job=aaa run=1 lines=12
2026-11-11T10:06:00Z complete job=aaa ok=11 held=1 seconds=300.5
2026-11-11T10:07:00Z claim job=bbb attempt=1 lines=8
"""


def test_parse_line_and_log_stats(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "worker_2026-11-11.log").write_text(LOG, encoding="utf-8")
    ts = stats.parse_line('2026-11-11T10:00:00Z idle job=- reason=vram note="two words"')
    assert ts[1] == "idle" and ts[2]["note"] == "two words"
    from datetime import datetime, timezone
    now = datetime(2026, 11, 11, 11, 0, 0, tzinfo=timezone.utc).timestamp()
    s = stats.read_log_stats(tmp_path / "logs", now=now)
    assert s["lines"] == 12 and s["jobs"] == 1 and s["busy_seconds"] == 300.5
    assert s["making"] is True and s["current_lines"] == 8
    assert s["lines_per_hour"] == 12.0                               # 12 lines over 1 h since the first event
    later = now + 4 * 3600
    assert stats.read_log_stats(tmp_path / "logs", now=later)["making"] is False   # stale claim


def test_log_stats_missing_dir(tmp_path):
    assert stats.read_log_stats(tmp_path / "nope")["lines"] == 0


class _R:
    def __init__(self, out):
        self.stdout = out


def test_gpu_reading_and_failure():
    assert stats.gpu_reading(lambda *a, **k: _R("61, 87, 4800, 8192\n")) == \
        {"temp_c": 61, "util_pct": 87, "mem_used_mib": 4800, "mem_total_mib": 8192}
    def boom(*a, **k):
        raise FileNotFoundError
    assert stats.gpu_reading(boom) is None


def test_snapshot_time_left(tmp_path):
    snap = stats.snapshot(tmp_path, "https://x", gpu=lambda: None,
                          queue=lambda api: {"queued_lines": 100, "eta_seconds": 7200})
    assert snap["time_left"] == 7200 and stats.format_duration(7200) == "2.0 h"
    snap = stats.snapshot(tmp_path, "https://x", gpu=lambda: None, queue=lambda api: None)
    assert snap["time_left"] is None


# ------------------------------------------------------------------ setup with fakes
class Fake:
    def __init__(self, tmp, mib=12000, key=KEY):
        self.cmds, self.downloads, self.envs, self.lines = [], [], [], []
        self.mib, self.key = mib, key
        self.tmp = tmp

    def download(self, url, dest, progress):
        self.downloads.append(url)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as z:
            if "uv-" in url:
                z.writestr("uv-x86/uv.exe", "x")
            elif "index-tts" in url:
                z.writestr("index-tts-830f6f8/pyproject.toml", "x")
            else:
                z.writestr("ffmpeg-x/bin/ffmpeg.exe", "x")

    def run(self, cmd, env, cwd, on_line):
        self.cmds.append([str(c) for c in cmd])
        self.envs.append(env)
        if "init" in [str(c) for c in cmd]:                       # fake worker init writes its config
            cfg = Path(str(cmd[cmd.index("--config") + 1]))
            cfg.write_text("{}", encoding="utf-8")
        if "venv" in cmd and "python" not in cmd[:3]:
            pass
        return 0

    def env(self, install):
        res = self.tmp / "res"
        res.mkdir(exist_ok=True)
        for n in setup_steps.WORKER_FILES:
            (res / n).write_text("#", encoding="utf-8")
        return setup_steps.Env(install=install, resource_dir=res, download=self.download, run=self.run,
                               ask_key=lambda: self.key, gpu_total_mib=lambda: self.mib,
                               progress=lambda s, f, m: self.lines.append(m), base_env={"PATH": "p"})


def test_setup_runs_all_steps_and_keeps_key_out_of_commands_and_log(tmp_path):
    f = Fake(tmp_path)
    ran = setup_steps.run_setup(f.env(tmp_path / "inst"))
    assert ran == list(setup_steps.STEPS)
    flat = "\n".join(" ".join(c) for c in f.cmds)
    assert "uv.exe" in flat and "830f6f8" in " ".join(f.downloads)
    assert "https://download.pytorch.org/whl/cu128" in flat and "IndexTeam/IndexTTS-2" in flat
    assert KEY not in flat and all(KEY not in str(m) for m in f.lines)
    init_env = [e for c, e in zip(f.cmds, f.envs) if "init" in c][0]
    assert init_env["SSW_WORKER_KEY"] == KEY
    assert all("SSW_WORKER_KEY" not in e for c, e in zip(f.cmds, f.envs) if "init" not in c)
    assert all(e["HF_HUB_ENABLE_HF_TRANSFER"] == "0" for e in f.envs)
    assert (tmp_path / "inst" / "net_worker.py").exists() and (tmp_path / "inst" / "ffmpeg" / "ffmpeg.exe").exists()


def test_setup_resumes_and_skips_done_steps(tmp_path):
    f = Fake(tmp_path)
    env = f.env(tmp_path / "inst")
    setup_steps.run_setup(env)
    n = len(f.cmds)
    ran = setup_steps.run_setup(env)
    assert ran == ["gpu", "folders"] and len(f.cmds) == n


def test_setup_stops_on_small_or_missing_gpu(tmp_path):
    with pytest.raises(setup_steps.SetupError, match="8 GB"):
        setup_steps.run_setup(Fake(tmp_path, mib=6144).env(tmp_path / "a"))
    f = Fake(tmp_path)
    env = f.env(tmp_path / "b")
    env.gpu_total_mib = lambda: None
    with pytest.raises(setup_steps.SetupError, match="NVIDIA"):
        setup_steps.run_setup(env)


def test_setup_needs_key_and_reports_failed_step(tmp_path):
    with pytest.raises(setup_steps.SetupError, match="key"):
        setup_steps.run_setup(Fake(tmp_path, key="").env(tmp_path / "c"))
    f = Fake(tmp_path)
    f.run = lambda cmd, env, cwd, on_line: 1
    with pytest.raises(setup_steps.SetupError, match="failed"):
        setup_steps.run_setup(f.env(tmp_path / "d"))


def test_unzip_refuses_path_traversal(tmp_path):
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(setup_steps.SetupError):
        setup_steps._unzip(z, tmp_path / "out")


# ------------------------------------------------------------------ controller + supervisor
class FakeProc:
    def __init__(self):
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False


def make_sup(tmp_path, mode="idle", **readers):
    s = settings.Settings(tmp_path / "app.json")
    s["install_dir"] = str(tmp_path / "inst")
    s["mode"] = mode
    procs = []
    clock = [1000.0]

    def popen(cmd, **kw):
        procs.append((cmd, kw))
        return FakeProc()

    w = controller.WorkerProcess(s, popen=popen, clock=lambda: clock[0])
    r = {"idle_seconds": lambda: 0, "fullscreen": lambda: False, "battery": lambda: False}
    r.update(readers)
    return supervisor.Supervisor(s, w, readers=r, clock=lambda: clock[0]), w, procs, clock, s


def test_worker_command_has_no_key_and_stop_file(tmp_path):
    sup, w, procs, clock, s = make_sup(tmp_path, mode="now")
    sup.tick()
    cmd, kw = procs[0]
    assert cmd[1:3] == [str(s.install / "net_worker.py"), "run"] and KEY not in " ".join(cmd)
    assert "SSW_WORKER_KEY" not in kw["env"] and kw["env"]["PATH"].startswith(str(s.install / "ffmpeg"))
    w.request_stop()
    assert (s.work_dir / "STOP").exists()
    clock[0] += 200
    assert w.poll_stop(grace=120) is False and w.proc.terminated      # ignored STOP: ended after the grace period
    assert w.poll_stop() is True


def test_idle_flow_start_then_user_returns(tmp_path):
    state = {"idle": 700}
    sup, w, procs, clock, s = make_sup(tmp_path, idle_seconds=lambda: state["idle"])
    sup.tick()
    assert w.running and len(procs) == 1
    state["idle"] = 1                                                   # user moved the mouse
    sup.tick()
    assert (s.work_dir / "STOP").exists() and sup.stopping
    w.proc.alive = False                                                # worker gave the job back and left
    sup.tick()
    assert not w.running and not sup.stopping


def test_pause_stops_and_blocks_restart(tmp_path):
    sup, w, procs, clock, s = make_sup(tmp_path, mode="now")
    sup.tick()
    sup.pause(15)
    w.proc.alive = False
    sup.tick()
    assert not w.running and sup.dot() == "paused"
    clock[0] += 16 * 60
    sup.tick()
    assert w.running


# ------------------------------------------------------------------ updater / autostart
def test_version_compare():
    assert updater.is_newer("v0.2.0", "0.1.9") and not updater.is_newer("0.1.0", "0.1.0")
    assert updater.parse_version("1.10") > updater.parse_version("1.9.9")


def _release(tag, sha_text, **kw):
    rel = {"tag_name": tag, "assets": [
        {"name": updater.ASSET, "browser_download_url": "https://x/exe"},
        {"name": updater.SHA_ASSET, "browser_download_url": "https://x/sha"}]}
    rel.update(kw)

    class Resp:
        def __init__(self, b): self.b = b
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n): return self.b

    def opener(req, timeout=0):
        url = req.full_url
        return Resp(json.dumps(rel).encode() if "api.github.com" in url else sha_text.encode())
    return opener


def test_update_check_and_hash_verify(tmp_path):
    exe = b"new exe bytes"
    sha = hashlib.sha256(exe).hexdigest()
    info = updater.check(_release("v9.0.0", sha + "  SpeakStoneWorker.exe\n"))
    assert info and info["sha256"] == sha and info["version"] == "9.0.0"
    assert updater.check(_release("v0.0.1", sha)) is None
    assert updater.check(_release("v9.0.0", "not a hash")) is None
    assert updater.check(_release("v9.0.0", sha, prerelease=True)) is None
    dl = lambda url, dest, prog: Path(dest).write_bytes(exe)
    assert updater.download_verified(info, tmp_path / "n.exe", dl) is True
    bad = dict(info, sha256="0" * 64)
    assert updater.download_verified(bad, tmp_path / "m.exe", dl) is False and not (tmp_path / "m.exe").exists()


def test_task_xml_is_per_user_logon_task():
    x = autostart.build_task_xml(r"C:\Users\a&b\SpeakStoneWorker.exe")
    assert "LogonTrigger" in x and "LeastPrivilege" in x and "InteractiveToken" in x and "a&amp;b" in x
    assert "Service" not in x
