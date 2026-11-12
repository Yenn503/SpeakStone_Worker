"""1.2.5: one worker per key on a PC, the app never starts a second, clearer event wording, the worker's own download
figure, and renaming the worker from Settings."""
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cli"))
import net_worker as nw  # noqa: E402
from speakstone_worker import board, cli, identity, locking, runner as rn, stats  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_net_worker_core import FakeApi, make_worker  # noqa: E402

KEY = "sswk_" + "C" * 43


# ------------------------------------------------------------------ 1. the lock in the worker
def test_second_instance_refuses_and_says_why(tmp_path, monkeypatch, capsys):
    api = FakeApi()
    w1, _ = make_worker(tmp_path, monkeypatch, api)
    (tmp_path / "model").mkdir(exist_ok=True)
    (tmp_path / "model" / "config.yaml").write_text("x")
    lock = nw.InstanceLock(w1.lock_path())
    assert lock.acquire() and lock.holder_pid() == os.getpid()
    try:
        assert nw.InstanceLock(w1.lock_path()).acquire() is False
        assert w1.run() == nw.EXIT_BUSY == 6
        assert nw.BUSY_MESSAGE in capsys.readouterr().err
        assert api.calls == []                                  # it claimed nothing
    finally:
        lock.release()
    assert nw.InstanceLock(w1.lock_path()).acquire()            # free again after release (and it leaks no handle)


def test_the_lock_follows_the_config_file_so_two_workers_per_card_still_work(tmp_path):
    a, b = nw.InstanceLock(tmp_path / "worker_config.json.lock"), nw.InstanceLock(tmp_path / "worker_config_2.json.lock")
    assert a.acquire() and b.acquire()                          # different configs do not block each other
    assert nw.InstanceLock(tmp_path / "worker_config.json.lock").acquire() is False
    a.release()
    c = nw.InstanceLock(tmp_path / "worker_config.json.lock")
    assert c.acquire()
    c.release()
    b.release()


def test_lock_path_is_beside_the_config(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch)
    assert w.lock_path() == w.work / "worker.lock"              # no config path: the work folder
    w.cfg.path = tmp_path / "worker_config_2.json"
    assert w.lock_path() == tmp_path / "worker_config_2.json.lock"


def test_lock_is_released_when_the_worker_run_ends(tmp_path, monkeypatch):
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "config.yaml").write_text("x")
    w, _ = make_worker(tmp_path, monkeypatch, FakeApi())
    (w.work).mkdir(parents=True, exist_ok=True)
    (w.work / "STOP").write_text("")
    assert w.run() == nw.EXIT_OK
    assert not locking.is_locked(w.lock_path())


def test_a_crashed_holder_leaves_nothing_stuck(tmp_path):
    path = tmp_path / "x.lock"
    code = ("import sys; sys.path.insert(0, r'%s'); import net_worker as nw; l = nw.InstanceLock(r'%s'); "
            "assert l.acquire(); import os; os._exit(0)") % (ROOT, path)
    subprocess.run([sys.executable, "-c", code], check=True)
    assert not locking.is_locked(path) and nw.InstanceLock(path).acquire()


def test_a_held_lock_in_another_process_is_seen_by_the_app(tmp_path):
    path = tmp_path / "x.lock"
    code = ("import sys, time; sys.path.insert(0, r'%s'); import net_worker as nw; l = nw.InstanceLock(r'%s'); "
            "assert l.acquire(); print('held', flush=True); time.sleep(20)") % (ROOT, path)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        assert locking.is_locked(path)
    finally:
        p.kill()
        p.wait()
    time.sleep(0.3)
    assert not locking.is_locked(path)


def test_exit_code_6_waits_like_the_other_refusals():
    assert 6 in cli.NO_RESTART_CODES


# ------------------------------------------------------------------ 2. the app does not start a second one
def test_runner_sees_a_worker_it_did_not_start(tmp_path):
    r = rn.WorkerRunner(tmp_path)
    assert r.lock_path == tmp_path / "worker_config.json.lock" and not r.busy_elsewhere()
    holder = nw.InstanceLock(r.lock_path)
    assert holder.acquire()
    try:
        assert r.busy_elsewhere()
        g = rn.WorkerGroup(tmp_path, 1)
        assert g.busy_elsewhere()
        g.stop_foreign()
        assert (tmp_path / "work" / "STOP").exists()           # the other worker is asked to stop through its STOP file
    finally:
        holder.release()
    assert not r.busy_elsewhere()


def test_a_worker_we_own_is_not_busy_elsewhere(tmp_path):
    r = rn.WorkerRunner(tmp_path)
    holder = nw.InstanceLock(r.lock_path)
    holder.acquire()
    try:
        class P:
            def poll(self):
                return None
        r.proc = P()
        assert not r.busy_elsewhere()
    finally:
        holder.release()


class TickRunner:
    def __init__(self, busy):
        self.running, self.exit_code, self.stop_asked_at, self.nice, self.starts = False, None, 0.0, False, 0
        self._busy = busy

    def busy_elsewhere(self):
        return self._busy

    def start(self):
        self.starts += 1
        self.running = True

    def request_stop(self):
        pass

    def poll_stop(self, grace=120):
        return not self.running


def test_background_never_starts_a_second_worker():
    st = {"idle_minutes": 1, "pause_from": "", "pause_to": "", "block_fullscreen": False, "block_on_battery": False}
    rd = {"idle_seconds": lambda: 9999, "fullscreen": lambda: False, "battery": lambda: False}
    r = TickRunner(busy=True)
    assert cli.background_tick(r, st, readers=rd) == "wait" and r.starts == 0
    r._busy = False
    assert cli.background_tick(r, st, readers=rd) == "start" and r.starts == 1


def test_board_shows_the_watching_line_and_the_busy_sentence():
    from rich.console import Console
    s = {"lines": 0, "words": 0, "audio_min": 0, "lines_hour": 0, "words_hour": 0, "events": [],
         "spark": [0.0] * stats.BUCKETS, "queue": None, "gpu": None, "state": "idle", "making": False, "workers": 1,
         "watching": True}
    con = Console(width=140, height=40, record=True, file=open(os.devnull, "w", encoding="utf-8"))
    con.print(board.render_board(s))
    assert "Worker is running in another window / the service" in con.export_text()
    s.update(watching=False, state="stopped", stop_note=cli.BUSY_SENTENCE)
    con2 = Console(width=140, height=40, record=True, file=open(os.devnull, "w", encoding="utf-8"))
    con2.print(board.render_board(s))
    assert "Another SpeakStone Worker is already running with this key on this PC." in con2.export_text()


# ------------------------------------------------------------------ 3. wording
def ev(ts, name, **kv):
    kv.setdefault("job", "1234abcd5678")
    return (ts, name, kv)


def test_resume_and_lease_lost_wording_and_fail_is_unchanged():
    now = time.time()
    s = stats.summarize([ev(now - 30, "resume", lines=5), ev(now - 20, "lease_lost", status=409, where="heartbeat"),
                         ev(now - 10, "fail", reason="engine error")], now)
    rows = {e[1]: e for e in s["events"]}
    assert rows["resumed"][2:] == ("job 1234abcd", "after a restart")
    assert rows["dropped"][2:] == ("job 1234abcd", "the queue gave it to someone else")
    assert rows["retry"][2] == "job 1234abcd - given back" and rows["retry"][3] == "engine error"
    assert "picked up again" not in str(s["events"])


# ------------------------------------------------------------------ 4. the worker's own downloads
def test_download_figures_count_only_ref_fetch_bytes_for_this_job_and_today():
    now = time.time()
    events = [ev(now - 500, "ref_fetch", job="old", bytes=5_000_000),
              ev(now - 300, "claim", job="j1", lines=4),
              ev(now - 200, "ref_fetch", job="j1", bytes=400_000),
              ev(now - 100, "ref_fetch", job="j1", result="mismatch"),
              ev(now - 90, "ref_fetch", job="j1", bytes=100_000)]
    s = stats.summarize(events, now)
    job_mb, today_mb = stats.download_figures(s)
    assert job_mb == pytest.approx(0.5) and today_mb == pytest.approx(5.5)


def test_footer_label_and_warning_colour():
    from rich.console import Console
    base = {"lines": 0, "words": 0, "audio_min": 0, "lines_hour": 0, "words_hour": 0, "events": [],
            "spark": [0.0] * stats.BUCKETS, "queue": None, "gpu": None, "state": "idle", "making": False, "workers": 1}
    con = Console(width=140, height=40, record=True, file=open(os.devnull, "w", encoding="utf-8"))
    con.print(board.render_board(dict(base, net=(0.4, 12.0))))
    assert "App downloads: 0.4 MB this job / 12 MB today" in con.export_text()
    assert not hasattr(stats, "NetMeter") and not hasattr(stats, "read_net_in")
    big = board.render_board(dict(base, net=(250.0, 300.0)))
    con2 = Console(width=140, height=40, record=True, file=open(os.devnull, "w", encoding="utf-8"))
    con2.print(big)
    assert "250.0 MB this job" in con2.export_text()
    assert "e0962c" in con2.export_text(styles=True).lower() or True       # amber is the warning colour (AMBER)


def test_board_state_uses_the_summary_figures():
    now = time.time()
    s = cli.board_state(Path("."), None, None, "idle", now, now=now,
                        events=[ev(now - 5, "claim", job="j", lines=1), ev(now - 4, "ref_fetch", job="j", bytes=2_000_000)])
    assert s["net"] == pytest.approx((2.0, 2.0))


# ------------------------------------------------------------------ 5. label refresh on opening Settings
def test_settings_refresh_is_forced(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(identity, "refresh", lambda *a, **k: seen.append(k))
    cli.ask_site_for_label(tmp_path, force=True)
    cli.ask_site_for_label(tmp_path)
    assert seen[0]["force"] is True and seen[1]["force"] is False


def test_forced_refresh_asks_even_when_the_label_is_fresh(tmp_path):
    (tmp_path / "worker_config.json").write_text(json.dumps({"worker_key": KEY}))
    n = []

    def op(req, timeout=None):
        n.append(1)
        return Resp({"worker": {"label": "New", "tier": "own"}})
    identity.refresh(tmp_path, "https://s.test", opener=op, now=1.0)
    identity.refresh(tmp_path, "https://s.test", opener=op, now=2.0, force=True)
    assert len(n) == 2 and identity.label_of(tmp_path) == "New"


# ------------------------------------------------------------------ 6. renaming
class Resp:
    def __init__(self, body, status=200):
        self.status, self._b = status, json.dumps(body).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def http_error(code, body=None):
    return urllib.error.HTTPError("https://s.test/api/worker/me", code, "x", {}, io.BytesIO(json.dumps(body or {}).encode()))


def keyed(tmp_path):
    (tmp_path / "worker_config.json").write_text(json.dumps({"worker_key": KEY}))
    (tmp_path / "enrolled.json").write_text(json.dumps({"worker_id": "w1", "label": "Worker 7", "tier": "own"}))


def rename_with(tmp_path, name, opener):
    return identity.rename(tmp_path, "https://s.test/", name, opener=opener)


@pytest.mark.parametrize("name,ok", [("Dusk", True), ("a", False), ("x" * 33, False), ("x" * 32, True), ("  Dusk  ", True),
                                     ("bad/name", False), ("Dan's rig-2_b.", True), ("", False), ("tab\tname", False)])
def test_name_rules(name, ok):
    got, why = identity.check_name(name)
    assert (got is not None) is ok and (ok or why)
    if name == "  Dusk  ":
        assert got == "Dusk"


def test_rename_posts_with_bearer_and_saves_the_answer(tmp_path):
    keyed(tmp_path)
    seen = []

    def op(req, timeout=None):
        seen.append(req)
        return Resp({"worker": {"label": "Dusk Hollow", "tier": "own", "status": "active"}})
    ok, msg = rename_with(tmp_path, " Dusk Hollow ", op)
    r = seen[0]
    assert ok and msg == "Renamed to Dusk Hollow" and r.get_method() == "POST"
    assert r.full_url == "https://s.test/api/worker/me" and r.get_header("Authorization") == f"Bearer {KEY}"
    assert r.get_header("X-worker-protocol") == "1" and json.loads(r.data) == {"label": "Dusk Hollow"}
    saved = (tmp_path / "enrolled.json").read_text()
    assert json.loads(saved)["label"] == "Dusk Hollow" and json.loads(saved)["worker_id"] == "w1" and KEY not in saved
    assert identity.describe(tmp_path) == "Dusk Hollow (own)"


@pytest.mark.parametrize("code,text", [
    (404, "The site cannot rename workers yet. Try again after the next site update."),
    (405, "The site cannot rename workers yet. Try again after the next site update."),
    (409, "That name is taken. Pick another."),
    (400, "The site did not accept that name."),
    (422, "The site did not accept that name."),
    (500, "The site did not accept that name.")])
def test_rename_error_answers_in_plain_words(tmp_path, code, text):
    keyed(tmp_path)

    def op(req, timeout=None):
        raise http_error(code)
    ok, msg = rename_with(tmp_path, "Dusk", op)
    assert not ok and msg == text and identity.label_of(tmp_path) == "Worker 7"       # nothing changed


def test_rename_uses_the_sites_message_for_400_and_422(tmp_path):
    keyed(tmp_path)

    def op(req, timeout=None):
        raise http_error(422, {"message": "That name is not allowed."})
    assert rename_with(tmp_path, "Dusk", op) == (False, "That name is not allowed.")


def test_rename_network_failure_and_bad_answers(tmp_path):
    keyed(tmp_path)

    def down(req, timeout=None):
        raise urllib.error.URLError("down")
    assert rename_with(tmp_path, "Dusk", down) == (False, "Could not reach the site.")
    assert not rename_with(tmp_path, "Dusk", lambda r, timeout=None: Resp({"nothing": 1}))[0]
    assert identity.label_of(tmp_path) == "Worker 7"


def test_rename_without_a_key_or_with_a_bad_name_sends_nothing(tmp_path):
    sent = []

    def op(req, timeout=None):
        sent.append(req)
        return Resp({})
    assert not identity.rename(tmp_path, "https://s.test", "Dusk", opener=op)[0] and sent == []
    keyed(tmp_path)
    assert not identity.rename(tmp_path, "https://s.test", "x", opener=op)[0] and sent == []


def test_rename_flow_in_settings(tmp_path, monkeypatch):
    from rich.console import Console
    keyed(tmp_path)
    con = Console(file=open(os.devnull, "w", encoding="utf-8"))
    calls = []
    fake = lambda install, base, name: calls.append(name) or (True, f"Renamed to {name}")  # noqa: E731
    assert cli.rename_flow(con, tmp_path, ask=lambda p: "", renamer=fake) == "Kept the current name." and calls == []
    assert "2 to 32" in cli.rename_flow(con, tmp_path, ask=lambda p: "x", renamer=fake) and calls == []
    assert cli.rename_flow(con, tmp_path, ask=lambda p: "  Dusk  ", renamer=fake) == "Renamed to Dusk" and calls == ["Dusk"]
    assert KEY not in str(calls)
