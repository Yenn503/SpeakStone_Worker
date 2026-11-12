"""Self-update, own install folder, tray menu and the pause control. Every outside call is a fake."""
import hashlib
import json
import os
import shlex
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speakstone_worker import (APP_NAME, __version__, board, cli, control as control_mod, service, tray,  # noqa: E402
                               update)
from speakstone_worker.settings import Settings  # noqa: E402

GOOD = "https://github.com/dandwhelan/SpeakStone_Worker/releases/download/v1.3.0/"


def release_json(tag="v1.3.0", names=("SpeakStoneWorker-windows.exe", "SpeakStoneWorker-windows.exe.sha256",
                                      "SpeakStoneWorker-linux", "SpeakStoneWorker-linux.sha256"), base=GOOD):
    return {"tag_name": tag, "assets": [{"name": n, "browser_download_url": base + n} for n in names]}


# ------------------------------------------------------------------ version and asset choice
def test_version_is_shown_and_bumped():
    assert __version__ == "1.2.0"
    assert f"{APP_NAME} {__version__}" in board.menu_screen().plain
    assert cli.window_title() == f"{APP_NAME} {__version__}"
    assert "Update now" in board.menu_screen(False, "1.3.0").plain
    assert "Update now" not in board.menu_screen(False, None).plain


def test_version_compare_is_numeric():
    assert update.is_newer("1.10.0", "1.9.0")
    assert update.is_newer("v1.2.1", "1.2.0")
    assert not update.is_newer("1.2.0", "1.2.0")
    assert not update.is_newer("1.1.9", "1.2.0")
    assert update.is_newer("2", "1.9.9") and not update.is_newer("1.2", "1.2.0")
    assert not update.is_newer("nightly", "1.2.0") and not update.is_newer("1.3.0", "x")


def test_asset_chosen_per_platform():
    w = update.pick_release(release_json(), "win32")
    li = update.pick_release(release_json(), "linux")
    assert w.asset_url.endswith("SpeakStoneWorker-windows.exe") and w.sha_url.endswith("-windows.exe.sha256")
    assert li.asset_url.endswith("SpeakStoneWorker-linux") and li.sha_url.endswith("-linux.sha256")
    assert w.version == "1.3.0"


def test_no_fingerprint_asset_means_no_update():
    data = release_json(names=("SpeakStoneWorker-windows.exe", "SpeakStoneWorker-linux"))
    with pytest.raises(update.UpdateError, match="fingerprint"):
        update.pick_release(data, "win32")


def test_no_asset_for_this_system():
    with pytest.raises(update.UpdateError, match="no file for this system"):
        update.pick_release(release_json(names=("SpeakStoneWorker-linux", "SpeakStoneWorker-linux.sha256")), "win32")


@pytest.mark.parametrize("url,ok", [
    ("https://github.com/x/y/releases/download/v1/a", True),
    ("https://objects.githubusercontent.com/abc", True),
    ("http://github.com/x", False),
    ("https://evil.example.com/a", False),
    ("https://github.com.evil.example/a", False),
    ("https://githubusercontent.com.evil.io/a", False),
    ("ftp://github.com/a", False)])
def test_only_https_github_hosts(url, ok):
    assert update.url_allowed(url) is ok


def test_foreign_host_or_plain_http_in_release_is_refused():
    with pytest.raises(update.UpdateError, match="somewhere other than GitHub"):
        update.pick_release(release_json(base="https://evil.example.com/"), "win32")
    with pytest.raises(update.UpdateError):
        update.pick_release(release_json(base="http://github.com/x/"), "win32")


def test_redirect_to_a_foreign_host_is_refused():
    h = update._CheckedRedirects()
    with pytest.raises(urllib.error.URLError):
        h.redirect_request(update._request(GOOD + "a"), None, 302, "Found", {}, "https://evil.example.com/a")


def test_http_get_refuses_plain_http():
    with pytest.raises(update.UpdateError):
        update.http_get("http://api.github.com/x")


def test_check_newer_uptodate_disabled_and_unreachable():
    def get(url):
        return json.dumps(release_json()).encode()
    assert update.check("1.2.0", env={}, platform="win32", get=get).version == "1.3.0"
    assert update.check("1.3.0", env={}, platform="win32", get=get) is None
    with pytest.raises(update.UpdateError, match="switched off"):
        update.check("1.2.0", env={"SSW_NO_UPDATE": "1"}, get=get)

    def down(url):
        raise urllib.error.URLError("no network")
    with pytest.raises(update.UpdateError, match="could not reach"):
        update.check("1.2.0", env={}, get=down)
    seen = []
    update.check("1.2.0", env={"SSW_UPDATE_URL": "https://example.test/r"}, platform="win32",
                 get=lambda u: seen.append(u) or json.dumps(release_json()).encode())
    assert seen == ["https://example.test/r"]


def test_user_agent_carries_the_version():
    assert update._request("https://github.com/x").get_header("User-agent") == f"SpeakStoneWorker/{__version__}"


# ------------------------------------------------------------------ download and verify
PAYLOAD = b"MZ" + b"x" * 100


def fetch_with(tmp_path, payload=PAYLOAD, fingerprint=None, version_out=None, download=None, min_bytes=50):
    exe = tmp_path / "app" / "SpeakStoneWorker.exe"
    exe.parent.mkdir(exist_ok=True)
    exe.write_bytes(b"old")
    rel = update.Release("1.3.0", GOOD + "a.exe", GOOD + "a.exe.sha256")
    fp = fingerprint if fingerprint is not None else hashlib.sha256(payload).hexdigest()

    def dl(url, dest):
        Path(dest).write_bytes(payload)
    ran = []

    class R:
        returncode = 0
        stdout = version_out if version_out is not None else f"{APP_NAME} 1.3.0\n"

    def run(cmd):
        ran.append(cmd)
        return R()
    try:
        return update.fetch_verified(rel, exe, get=lambda u: (fp + "  a.exe\n").encode(), download=download or dl,
                                     run=run, min_bytes=min_bytes), exe, ran
    except update.UpdateError as e:
        return e, exe, ran


def test_good_download_is_kept_after_all_three_checks(tmp_path):
    new, exe, ran = fetch_with(tmp_path)
    assert new == tmp_path / "app" / "SpeakStoneWorker.new.exe" and new.exists()
    assert ran == [[str(new), "--version"]]


def test_bad_hash_deletes_the_file(tmp_path):
    err, exe, ran = fetch_with(tmp_path, fingerprint="0" * 64)
    assert isinstance(err, update.UpdateError) and "fingerprint" in str(err)
    assert not update.staging_name(exe).exists() and ran == [] and exe.read_bytes() == b"old"


def test_wrong_version_output_deletes_the_file(tmp_path):
    err, exe, ran = fetch_with(tmp_path, version_out=f"{APP_NAME} 1.2.0\n")
    assert isinstance(err, update.UpdateError) and "expected version" in str(err)
    assert not update.staging_name(exe).exists()


def test_too_small_and_unreadable_fingerprint_and_failed_download(tmp_path):
    err, exe, _ = fetch_with(tmp_path, min_bytes=10_000)
    assert "too small" in str(err) and not update.staging_name(exe).exists()
    err, exe, _ = fetch_with(tmp_path, fingerprint="not a hash")
    assert "could not be read" in str(err)

    def boom(url, dest):
        Path(dest).write_bytes(b"half")
        raise OSError("connection reset")
    err, exe, _ = fetch_with(tmp_path, download=boom)
    assert isinstance(err, update.UpdateError) and not update.staging_name(exe).exists()


def test_cleanup_old_removes_leftovers(tmp_path):
    exe = tmp_path / "SpeakStoneWorker.exe"
    for p in (update.old_name(exe), update.staging_name(exe)):
        p.write_bytes(b"x")
    update.cleanup_old(exe)
    assert not update.old_name(exe).exists() and not update.staging_name(exe).exists()
    update.cleanup_old(None)


# ------------------------------------------------------------------ the swap
def test_windows_swap_sequence(tmp_path):
    exe, new = tmp_path / "SpeakStoneWorker.exe", tmp_path / "SpeakStoneWorker.new.exe"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")
    update.old_name(exe).write_bytes(b"older")           # an .old from the last swap is replaced
    spawned, exits = [], []
    update.swap_windows(exe, new, ["--background"], spawn=lambda c: spawned.append(c), exit_fn=lambda c: exits.append(c))
    assert exe.read_bytes() == b"new" and update.old_name(exe).read_bytes() == b"old" and not new.exists()
    assert exits == [0] and len(spawned) == 1
    assert spawned[0][1:] == ["--headless", str(exe), "--background"]       # still windowless


def test_windows_swap_puts_the_old_one_back_when_the_move_fails(tmp_path):
    exe, new = tmp_path / "SpeakStoneWorker.exe", tmp_path / "SpeakStoneWorker.new.exe"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")

    def bad_replace(a, b):
        raise PermissionError("locked")
    with pytest.raises(OSError):
        update.swap_windows(exe, new, [], replace=bad_replace, spawn=lambda c: None, exit_fn=lambda c: None)
    assert exe.read_bytes() == b"old"


def test_windows_swap_keeps_running_when_the_new_one_cannot_start(tmp_path):
    exe, new = tmp_path / "SpeakStoneWorker.exe", tmp_path / "SpeakStoneWorker.new.exe"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")
    exits = []

    def nospawn(c):
        raise OSError("no")
    with pytest.raises(update.UpdateError):
        update.swap_windows(exe, new, [], spawn=nospawn, exit_fn=exits.append)
    assert exits == [] and exe.read_bytes() == b"new"


def test_linux_swap_replaces_then_reexecs(tmp_path, monkeypatch):
    monkeypatch.delenv(update.RESET_ENV, raising=False)
    exe, new = tmp_path / "SpeakStoneWorker", tmp_path / "SpeakStoneWorker.new"
    exe.write_bytes(b"old")
    new.write_bytes(b"new")
    calls = []
    update.swap_linux(exe, new, ["--background"], execv=lambda p, a: calls.append((p, a)))
    assert exe.read_bytes() == b"new" and calls == [(str(exe), [str(exe), "--background"])]
    assert os.environ[update.RESET_ENV] == "1"
    monkeypatch.delenv(update.RESET_ENV, raising=False)


def test_apply_picks_the_swap_for_the_platform_and_cleans_up_on_failure(tmp_path):
    rel = update.Release("1.3.0", "u", "s")
    exe = tmp_path / "SpeakStoneWorker.exe"
    new = tmp_path / "SpeakStoneWorker.new.exe"
    new.write_bytes(b"n")
    seen = []
    update.apply(rel, exe, ["--background"], platform="win32", fetch=lambda r, e: new,
                 rename=lambda a, b: seen.append("rename"), replace=lambda a, b: seen.append("replace"),
                 spawn=lambda c: seen.append("spawn"), remove=lambda p: None, exit_fn=lambda c: seen.append("exit"))
    assert seen == ["rename", "replace", "spawn", "exit"]
    seen.clear()
    update.apply(rel, exe, [], platform="linux", fetch=lambda r, e: new, replace=lambda a, b: seen.append("replace"),
                 execv=lambda p, a: seen.append("exec"))
    assert seen == ["replace", "exec"]

    def bad(a, b):
        raise OSError("x")
    new.write_bytes(b"n")
    with pytest.raises(update.UpdateError):
        update.apply(rel, exe, [], platform="linux", fetch=lambda r, e: new, replace=bad, execv=lambda p, a: None)
    assert not new.exists()
    os.environ.pop(update.RESET_ENV, None)


def test_relaunch_keeps_background_windowless_and_menu_plain():
    cmd = update.relaunch_command(r"C:\A\SpeakStoneWorker.exe", ["--background"], "win32", {"SystemRoot": r"D:\Win"})
    assert cmd == [r"D:\Win\System32\conhost.exe", "--headless", r"C:\A\SpeakStoneWorker.exe", "--background"]
    assert update.relaunch_command("/a/x", ["--start"], "linux") == ["/a/x", "--start"]


# ------------------------------------------------------------------ when it may swap
def test_only_frozen_in_the_install_folder_swaps(tmp_path):
    app = tmp_path / "app" / "SpeakStoneWorker.exe"
    app.parent.mkdir()
    app.write_bytes(b"x")
    other = tmp_path / "share" / "SpeakStoneWorker.exe"
    other.parent.mkdir()
    other.write_bytes(b"x")
    assert update.can_self_update(True, app, app) == (True, "")
    assert not update.can_self_update(False, app, app)[0]
    assert not update.can_self_update(True, other, app)[0]


def make_flow(tmp_path, frozen=True, in_app=True, check_result=None, apply_fn=None, log=None):
    app = tmp_path / "app" / "SpeakStoneWorker.exe"
    app.parent.mkdir(exist_ok=True)
    app.write_bytes(b"x")
    exe = app if in_app else tmp_path / "SpeakStoneWorker.exe"
    exe.write_bytes(b"x")
    t = [1000.0]
    f = update.UpdateFlow(log=(log or (lambda s: None)), check_fn=lambda env=None: check_result,
                          apply_fn=apply_fn or (lambda *a: None), frozen=frozen, exe=str(exe), app_exe=app,
                          args=["--background"], clock=lambda: t[0])
    f.t = t
    return f


REL = update.Release("1.3.0", GOOD + "a", GOOD + "a.sha256")


def test_flow_from_source_or_other_folder_only_reports(tmp_path):
    called = []
    for kw in ({"frozen": False}, {"in_app": False}):
        f = make_flow(tmp_path, check_result=REL, apply_fn=lambda *a: called.append(a), **kw)
        assert f.check() == REL
        assert "1.3.0 is available" in f.apply() and called == []


def test_flow_failed_check_is_one_log_line_and_silent(tmp_path):
    logs = []

    def down(env=None):
        raise update.UpdateError("could not reach the release page")
    f = make_flow(tmp_path, log=logs.append)
    f.check_fn = down
    assert f.check() is None and logs == ["update check: could not reach the release page"]
    assert not f.due() and f.release is None
    f.t[0] += update.CHECK_EVERY + 1
    assert f.due()


def test_flow_failed_apply_forgets_and_retries_next_check(tmp_path):
    logs = []

    def fail(rel, exe, args):
        raise update.UpdateError("the downloaded file does not match its fingerprint")
    f = make_flow(tmp_path, check_result=REL, apply_fn=fail, log=logs.append)
    f.check()
    assert "was not installed" in f.apply() and f.release is None and any("not installed" in x for x in logs)


# ------------------------------------------------------------------ background update step
def step_setup(tmp_path, st=None, **kw):
    applied = []
    f = make_flow(tmp_path, check_result=REL, apply_fn=lambda r, e, a: applied.append((r.version, e, a)), **kw)
    return f, control_mod.Control(), (st if st is not None else {"auto_update": True}), applied


def test_update_step_never_swaps_while_a_job_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, applied = step_setup(tmp_path)
    assert cli.update_step(f, c, st, running=True) is False
    assert c.update_version == "1.3.0" and applied == []
    assert cli.update_step(f, c, st, running=False) is True and applied[0][0] == "1.3.0"
    assert applied[0][2] == ["--background"]


def test_update_step_auto_off_only_tells_until_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    f, c, st, applied = step_setup(tmp_path, st={"auto_update": False})
    assert cli.update_step(f, c, st, running=False) is False and c.update_version == "1.3.0" and applied == []
    c.request_check()                                       # "Check for updates now" in the tray
    assert cli.update_step(f, c, st, running=False) is True and len(applied) == 1


def test_update_step_respects_the_off_switch_and_ineligible_copy(tmp_path, monkeypatch):
    f, c, st, applied = step_setup(tmp_path)
    assert cli.update_step(f, c, st, running=False) is False and applied == []        # SSW_NO_UPDATE=1 from conftest
    monkeypatch.delenv("SSW_NO_UPDATE")
    logs = []
    f, c, st, applied = step_setup(tmp_path, in_app=False, log=logs.append)
    c.request_check()
    assert cli.update_step(f, c, st, running=False) is False and applied == []
    assert any("not installed here" in x for x in logs)


def test_checks_only_every_six_hours(tmp_path, monkeypatch):
    monkeypatch.delenv("SSW_NO_UPDATE")
    n = []
    f, c, st, _ = step_setup(tmp_path, st={"auto_update": False})
    f.check_fn = lambda env=None: n.append(1) or None
    cli.update_step(f, c, st, running=True)
    cli.update_step(f, c, st, running=True)
    assert len(n) == 1
    f.t[0] += 6 * 3600 + 1
    cli.update_step(f, c, st, running=True)
    assert len(n) == 2


# ------------------------------------------------------------------ service install copies the program
class R0:
    returncode = 0


def test_service_install_copies_the_exe_and_points_the_task_at_the_copy(tmp_path):
    share = tmp_path / "share" / "SpeakStoneWorker.exe"
    share.parent.mkdir()
    share.write_bytes(b"program")
    target = tmp_path / "data" / "app" / "SpeakStoneWorker.exe"
    calls = []
    ok = service.install(tmp_path / "data", runner=lambda c, **k: calls.append(c) or R0(), platform="win32", frozen=True,
                         executable=str(share), app_target=target)
    xml = (tmp_path / "data" / "task.xml").read_text(encoding="utf-16")
    assert ok and target.read_bytes() == b"program"
    assert str(target) in xml and "share" not in xml and "--headless" in xml and "--background" in xml


def test_service_install_from_the_installed_copy_does_not_copy_onto_itself(tmp_path):
    target = tmp_path / "app" / "SpeakStoneWorker.exe"
    target.parent.mkdir()
    target.write_bytes(b"p")
    seen = []
    ok = service.install(tmp_path, runner=lambda c, **k: R0(), platform="win32", frozen=True, executable=str(target),
                         app_target=target, copier=lambda a, b: seen.append(1))
    assert ok and seen == []


def test_service_install_fails_when_the_copy_fails_rather_than_pointing_at_the_share(tmp_path):
    share = tmp_path / "SpeakStoneWorker.exe"
    share.write_bytes(b"p")
    calls = []

    def bad(a, b):
        raise OSError("disk full")
    ok = service.install(tmp_path / "d", runner=lambda c, **k: calls.append(c) or R0(), platform="win32", frozen=True,
                         executable=str(share), app_target=tmp_path / "a" / "x.exe", copier=bad)
    assert ok is False and calls == []


def test_install_copy_moves_a_busy_copy_aside(tmp_path):
    src, dst = tmp_path / "src.exe", tmp_path / "app" / "SpeakStoneWorker.exe"
    src.write_bytes(b"new")
    dst.parent.mkdir()
    dst.write_bytes(b"old")
    first = [True]

    def replace(a, b):                                       # the first move onto a running file is refused
        if first[0] and Path(b) == dst:
            first[0] = False
            raise PermissionError("in use")
        os.replace(a, b)
    update.install_copy(src, dst, replace=replace)
    assert dst.read_bytes() == b"new" and update.old_name(dst).read_bytes() == b"old"


def test_linux_service_runs_the_copy(tmp_path):
    share = tmp_path / "SpeakStoneWorker"
    share.write_bytes(b"p")
    target = tmp_path / "app" / "SpeakStoneWorker"
    ok = service.install(tmp_path / "d", runner=lambda c, **k: R0(), platform="linux", unit_dir=tmp_path / "u",
                         frozen=True, executable=str(share), app_target=target)
    assert ok and f"ExecStart={shlex.quote(str(target))} --background" in (tmp_path / "u" / service.UNIT_NAME).read_text()


# ------------------------------------------------------------------ tray menu model
SETTINGS = {"block_fullscreen": True, "block_on_battery": False, "auto_update": True}


def by_key(items):
    return {i.key: i for i in items if i.key}


def test_menu_header_status_and_disabled_lines():
    m = by_key(tray.build_menu("1.2.0", "Working", False, SETTINGS))
    assert m["title"].label == "SpeakStone Worker 1.2.0" and not m["title"].enabled
    assert m["status"].label == "Working" and not m["status"].enabled
    assert m["board"].default and m["pause"].label == "Pause" and "resume" not in m


@pytest.mark.parametrize("status", [control_mod.WORKING, control_mod.WAITING_WORK, control_mod.PAUSED,
                                    control_mod.WAITING_IDLE, control_mod.UPDATING])
def test_menu_shows_each_status(status):
    m = by_key(tray.build_menu("1.2.0", status, status == control_mod.PAUSED, SETTINGS))
    assert m["status"].label == status
    assert ("resume" in m) == (status == control_mod.PAUSED)


def test_menu_toggles_reflect_settings_and_update_item_appears_when_known():
    m = by_key(tray.build_menu("1.2.0", "Working", False, SETTINGS))
    assert m["block_fullscreen"].checked is True and m["block_on_battery"].checked is False
    assert m["auto_update"].checked is True and "update_now" not in m
    m = by_key(tray.build_menu("1.2.0", "Working", False, SETTINGS, "1.3.0"))
    assert m["update_now"].label == "Update to 1.3.0 now"
    keys = [i.key for i in tray.build_menu("1.2.0", "x", False, SETTINGS)]
    assert keys[-1] == "quit" and {"check", "logs", "board"} <= set(keys)


def test_tooltip_has_name_version_status():
    assert tray.tooltip("1.2.0", "Paused") == "SpeakStone Worker 1.2.0 - Paused"


class FakePystray:
    class Menu:
        SEPARATOR = object()

        def __init__(self, items):
            self.items = items

    class MenuItem:
        def __init__(self, text, action, **kw):
            self.text, self.action, self.kw = text, action, kw

    class Icon:
        def __init__(self, name, image, title, menu):
            self.name, self.image, self.title, self.menu, self.updates, self.stopped = name, image, title, menu, 0, False

        def run_detached(self):
            self.running = True

        def update_menu(self):
            self.updates += 1

        def stop(self):
            self.stopped = True


def fake_tray(actions, state=None, log=None):
    state = state or {"status": "Working", "paused": False, "settings": SETTINGS, "update_version": None}
    t = tray.Tray("1.2.0", lambda: state, actions, object(), FakePystray, log or (lambda s: None), poll_seconds=3600)
    return t, state


def test_each_menu_item_calls_its_action():
    calls = []
    keys = ["pause", "resume", "block_fullscreen", "block_on_battery", "auto_update", "board", "check", "update_now",
            "logs", "quit"]
    t, _ = fake_tray({k: (lambda k=k: calls.append(k)) for k in keys})
    for k in keys:
        t._run(k)()
    assert calls == keys


def test_menu_items_map_to_pystray_with_checks_and_default():
    t, st = fake_tray({"board": lambda: None})
    items = t._menu_items()
    named = {i.text: i for i in items if hasattr(i, "text")}
    assert named["Open the board"].kw["default"] is True
    assert named["Update automatically"].kw["checked"](None) is True
    assert named["SpeakStone Worker 1.2.0"].kw["enabled"] is False
    assert FakePystray.Menu.SEPARATOR in items


def test_a_failing_action_is_swallowed_and_logged():
    logs = []

    def boom():
        raise RuntimeError("x")
    t, _ = fake_tray({"quit": boom}, log=logs.append)
    t._run("quit")()
    assert "tray action quit failed" in logs[0]


def test_tray_refresh_updates_tooltip_and_menu_only_on_change():
    t, st = fake_tray({})
    t.refresh()
    t.refresh()
    assert t.icon.updates == 1 and t.icon.title.endswith("Working")
    st["status"] = "Paused"
    t.refresh()
    assert t.icon.updates == 2 and t.icon.title.endswith("Paused")


# ------------------------------------------------------------------ tray missing -> background unaffected
def test_tray_start_never_raises_and_says_why(tmp_path):
    logs = []

    def no_module(name):
        raise ImportError("no pystray")
    assert tray.start("1.2.0", dict, {}, tmp_path / "i.png", logs.append, platform="win32", env={},
                      importer=no_module) is None
    assert tray.start("1.2.0", dict, {}, tmp_path / "i.png", logs.append, platform="linux", env={}) is None
    assert tray.start("1.2.0", dict, {}, tmp_path / "i.png", logs.append, platform="win32",
                      env={"SSW_NO_TRAY": "1"}) is None
    assert len(logs) == 3 and all(x.startswith("tray:") for x in logs)


def test_missing_icon_file_also_means_no_tray_not_a_crash(tmp_path):
    logs = []
    assert tray.start("1.2.0", dict, {}, tmp_path / "missing.png", logs.append, platform="win32", env={},
                      importer=lambda n: FakePystray) is None and logs


def test_display_needed_on_linux_only():
    assert tray.display_available("win32", {}) and not tray.display_available("linux", {})
    assert tray.display_available("linux", {"DISPLAY": ":0"})


# ------------------------------------------------------------------ pause, quit, actions
class FakeRunner:
    def __init__(self, running=False):
        self.running, self.exit_code, self.stop_asked_at, self.nice = running, None, 0.0, False
        self.starts, self.stops = 0, 0

    def start(self):
        self.starts += 1
        self.running = True

    def request_stop(self):
        self.stops += 1
        self.stop_asked_at = 1.0

    def poll_stop(self, grace=120):
        return not self.running

    def stop_and_wait(self):
        self.running = False

    def shutdown_fast(self):
        self.running = False


RD = {"idle_seconds": lambda: 9999, "fullscreen": lambda: False, "battery": lambda: False}
ST = {"idle_minutes": 1, "pause_from": "", "pause_to": "", "block_fullscreen": False, "block_on_battery": False}


def test_pause_stops_the_worker_and_blocks_new_starts_until_resume():
    c, r = control_mod.Control(), FakeRunner(running=True)
    c.set_paused(True)
    assert cli.background_tick(r, ST, readers=RD, control=c) == "stop" and r.stops == 1
    r.running = False
    assert cli.background_tick(r, ST, readers=RD, control=c) == "keep" and r.starts == 0
    c.set_paused(False)
    assert cli.background_tick(r, ST, readers=RD, control=c) == "start" and r.starts == 1


def test_status_lines():
    f = control_mod.status_for
    assert f(True, True, False) == "Working" and f(True, False, False) == "Waiting for work"
    assert f(True, True, True) == "Paused" and f(False, False, False) == "Waiting for the PC to be idle"
    assert f(False, False, False, updating=True) == "Updating"


def test_tray_actions_change_control_and_settings(tmp_path):
    c = control_mod.Control()
    st = Settings(tmp_path / "cli.json")
    opened, folders = [], []
    acts = cli.make_tray_actions(tmp_path, c, st, None, open_board=lambda cmd: opened.append(cmd),
                                 open_folder=lambda p: folders.append(p))
    acts["pause"]()
    assert c.paused
    acts["resume"]()
    assert not c.paused
    acts["block_fullscreen"]()
    assert st["block_fullscreen"] is False
    assert json.loads((tmp_path / "cli.json").read_text())["block_fullscreen"] is False      # saved: survives a restart
    acts["auto_update"]()
    assert st["auto_update"] is False
    acts["board"]()
    assert opened and opened[0][0] == sys.executable
    acts["logs"]()
    assert folders == [tmp_path / "work" / "logs"] and folders[0].exists()
    acts["check"]()
    assert c.take_check() and not c.take_check()
    acts["quit"]()
    assert c.quit


def test_tray_state_reader_reads_live_settings(tmp_path):
    c, st = control_mod.Control(), Settings(tmp_path / "cli.json")
    read = cli.tray_state_reader(c, st)
    assert read()["settings"] == {"block_fullscreen": True, "block_on_battery": True, "auto_update": True}
    st["block_on_battery"] = False
    c.update_version = "1.3.0"
    assert read()["settings"]["block_on_battery"] is False and read()["update_version"] == "1.3.0"


def test_background_quit_from_the_tray_ends_the_loop_cleanly(tmp_path, monkeypatch):
    from speakstone_worker import setup
    monkeypatch.setattr(cli, "hide_console", lambda: None)
    monkeypatch.setattr(cli, "set_title", lambda t: None)
    monkeypatch.setattr(setup, "is_set_up", lambda i: True)
    monkeypatch.setattr(setup, "extras_cached", lambda i: True)
    monkeypatch.setattr(setup, "sync_worker_files", lambda *a: None)
    monkeypatch.setattr(cli, "install_close_handlers", lambda f: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    runner = FakeRunner(running=True)
    monkeypatch.setattr(cli, "WorkerGroup", lambda install, n: runner)
    seen = {}

    def tray_start(version, get_state, actions, icon, log):
        seen["version"], seen["state"], seen["actions"] = version, get_state(), actions
        seen["stopped"] = stopped = []
        return type("T", (), {"stop": lambda self: stopped.append(1)})()

    def tick(runner, st, gate=None, control=None):
        seen["actions"]["quit"]()

    monkeypatch.setattr(cli, "background_tick", tick)
    flow = make_flow(tmp_path, check_result=None)
    st = {"workers": 1, "auto_update": True, "block_fullscreen": True, "block_on_battery": True}
    assert cli.background(tmp_path, st, flow=flow, tray_start=tray_start, making=lambda *a: False) == 0
    assert seen["version"] == __version__ and seen["state"]["status"] == control_mod.WAITING_IDLE
    assert seen["stopped"] == [1] and runner.running is False       # tray removed, the job handed back


def test_open_board_starts_a_new_console_on_windows():
    calls = []
    assert tray.open_board([r"C:\a\x.exe"], popen=lambda cmd, **kw: calls.append((cmd, kw)), platform="win32")
    assert calls[0][0] == [r"C:\a\x.exe"] and calls[0][1]["creationflags"] == 0x10
    assert calls[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_console_title_seam():
    got = []
    cli.set_title("T", platform="linux", write=got.append)
    assert got == ["\x1b]0;T\x07"]

    class K:
        def SetConsoleTitleW(self, t):
            got.append(t)
    cli.set_title("W", platform="win32", kernel32=K())
    assert got[-1] == "W"


def test_board_foot_shows_version_and_update():
    from rich.console import Console
    from speakstone_worker import stats
    s = {"lines": 0, "words": 0, "audio_min": 0, "lines_hour": 0, "words_hour": 0, "events": [],
         "spark": [0.0] * stats.BUCKETS, "queue": None, "gpu": None, "state": "idle", "making": False,
         "update": "1.3.0", "workers": 1}
    con = Console(width=110, height=40, record=True, file=open(os.devnull, "w", encoding="utf-8"))
    con.print(board.render_board(s))
    text = con.export_text()
    assert f"v{__version__}" in text and "update 1.3.0 available" in text


def test_settings_default_and_reload(tmp_path):
    a = Settings(tmp_path / "cli.json")
    assert a["auto_update"] is True
    b = Settings(tmp_path / "cli.json")
    b["idle_minutes"] = 42
    a.reload()
    assert a["idle_minutes"] == 42


def test_paths_app_exe_is_inside_the_data_folder():
    from speakstone_worker import paths
    assert paths.app_exe({"LOCALAPPDATA": "C:/L", "XDG_DATA_HOME": "/x"}).parent.name == "app"
    assert (paths.assets_dir() / "icon.png").exists() and (paths.assets_dir() / "icon.ico").exists()
