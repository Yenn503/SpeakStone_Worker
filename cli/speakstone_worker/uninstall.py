"""Remove everything the app put on this PC: the logon task or service, and the whole install folder
(Python, packages, model files, download caches, logs, settings and the worker key)."""
import os
import shutil
import stat
from pathlib import Path

MARKERS = ("setup_state.json", "cli.json", "worker_config.json", "venv", "IndexTTS-2")


def folder_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def looks_like_install(path):
    """Only a folder with the app's name that holds the app's own files is ever deleted."""
    p = Path(path)
    return p.is_dir() and "speakstone" in p.name.lower() and any((p / m).exists() for m in MARKERS)


def _force(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)          # Windows: read-only files
        func(path)
    except OSError:
        pass


def remove_everything(install, stop_workers=None, remove_service=None, rmtree=shutil.rmtree):
    """-> {"ok", "freed", "left"}. Stops the workers, removes the service, deletes the folder."""
    install = Path(install)
    if not looks_like_install(install):
        return {"ok": False, "freed": 0, "left": str(install), "why": "not an install folder"}
    before = folder_bytes(install)
    if stop_workers:
        stop_workers()
    if remove_service:
        remove_service()
    rmtree(install, onerror=_force)
    after = folder_bytes(install) if install.exists() else 0
    return {"ok": after == 0, "freed": before - after, "left": str(install) if after else ""}
