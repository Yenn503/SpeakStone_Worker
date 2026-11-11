"""Update check against the repo's GitHub releases. Off by default (settings `update_check`) until releases exist.

A release must carry the app as `SpeakStoneWorker.exe` and its hash as `SpeakStoneWorker.exe.sha256` (one line, hex,
optionally followed by the file name). The download is only kept when its SHA-256 matches. This app never runs it by
itself: `apply_script` writes a small batch file that swaps the file after the app has closed.
"""
import hashlib
import json
import re
import urllib.request
from pathlib import Path

from . import APP_VERSION, REPO

ASSET = "SpeakStoneWorker.exe"
SHA_ASSET = ASSET + ".sha256"
_HEX = re.compile(r"\b[0-9a-fA-F]{64}\b")


def parse_version(v):
    nums = re.findall(r"\d+", str(v).lstrip("vV"))[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


def is_newer(remote, local=APP_VERSION):
    return parse_version(remote) > parse_version(local)


def _get(url, opener, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "SpeakStoneWorkerApp",
                                               "Accept": "application/vnd.github+json"})
    with opener(req, timeout=20) as r:
        return r.read(limit)


def check(opener=urllib.request.urlopen, repo=REPO, local=APP_VERSION):
    """Return {'version', 'exe_url', 'sha256'} when a newer, complete release exists, else None."""
    try:
        rel = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest", opener, 1_000_000))
        ver = rel.get("tag_name", "")
        if not is_newer(ver, local) or rel.get("draft") or rel.get("prerelease"):
            return None
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        if ASSET not in assets or SHA_ASSET not in assets:
            return None
        m = _HEX.search(_get(assets[SHA_ASSET], opener, 4096).decode("ascii", "replace"))
        if not m:
            return None
        return {"version": ver.lstrip("vV"), "exe_url": assets[ASSET], "sha256": m.group(0).lower()}
    except Exception:
        return None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def download_verified(info, dest, downloader):
    """Download to `dest`; delete it and return False when the hash does not match."""
    dest = Path(dest)
    downloader(info["exe_url"], dest, None)
    if sha256_of(dest) != info["sha256"]:
        dest.unlink(missing_ok=True)
        return False
    return True


def apply_script(new_exe, current_exe, script_path):
    """Batch file that waits for the app to close, swaps the file and starts it again."""
    Path(script_path).write_text(
        "@echo off\r\n"
        "ping -n 4 127.0.0.1 >nul\r\n"
        f'move /y "{new_exe}" "{current_exe}" >nul\r\n'
        f'start "" "{current_exe}"\r\n'
        'del "%~f0"\r\n', encoding="ascii")
    return script_path
