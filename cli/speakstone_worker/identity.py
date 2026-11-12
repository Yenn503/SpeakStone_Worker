"""This worker's label and tier, for the menu, the board and the tray.

Setup writes `enrolled.json` (label and tier, never the key), but a PC set up by an older version, or one whose key was
typed in, has no such file. Then the site is asked once (`GET /api/worker/me`, the key as a bearer token, exactly as the
worker does it) and the answer is saved there. A known label is asked about again at most once a day, in case it was
renamed on the site. Every failure is silent and keeps what was known. The key is read, sent and never stored, printed
or logged here.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

from . import __version__

REFRESH_SECONDS = 24 * 3600            # a known label is re-read at most this often
RETRY_SECONDS = 30 * 60                # an unknown label is retried at most this often (background loop)
TIMEOUT = 2.0                          # never hold the menu for longer than this


def read_key(install):
    try:
        key = json.loads((Path(install) / "worker_config.json").read_text(encoding="utf-8")).get("worker_key")
    except (OSError, ValueError, AttributeError):
        return ""
    return key if isinstance(key, str) else ""


def cached(install):
    """What is saved: {"label", "tier", "checked_at", ...} (empty when nothing is)."""
    if not install:
        return {}
    try:
        d = json.loads((Path(install) / "enrolled.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def label_of(install):
    return str(cached(install).get("label") or "")


def is_fresh(info, now):
    try:
        age = now - float(info.get("checked_at") or 0)
    except (TypeError, ValueError):
        age = REFRESH_SECONDS
    return bool(info.get("label")) and age < REFRESH_SECONDS


def fetch(base, key, opener=urllib.request.urlopen, timeout=TIMEOUT):
    """-> {"label", "tier"} from the site, or None on any failure."""
    req = urllib.request.Request(base.rstrip("/") + "/api/worker/me", method="GET",
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                          "User-Agent": f"speakstone-worker-cli/{__version__}",
                                          "X-Worker-Protocol": "1"})
    try:
        with opener(req, timeout=timeout) as r:
            if getattr(r, "status", 200) != 200:
                return None
            data = json.loads(r.read(100_000).decode("utf-8"))
    except Exception:                                   # unreachable, refused, slow, not JSON: all the same here
        return None
    if isinstance(data, dict) and isinstance(data.get("worker"), dict):
        data = data["worker"]
    if not isinstance(data, dict) or not isinstance(data.get("label"), str) or not data["label"].strip():
        return None
    tier = data.get("tier")
    return {"label": data["label"].strip()[:80], "tier": tier if isinstance(tier, str) else ""}


def save(install, info, now):
    """Merge label and tier into enrolled.json (keeping worker_id and the rest). Never touches the key."""
    d = dict(cached(install))
    d.update({"label": info["label"], "tier": info.get("tier") or d.get("tier"), "checked_at": now})
    d.pop("key", None)
    path = Path(install) / "enrolled.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return d


def refresh(install, base, opener=urllib.request.urlopen, now=None, timeout=TIMEOUT, force=False):
    """Ask the site when the label is unknown or a day old and a key exists. -> the saved info (possibly empty)."""
    now = time.time() if now is None else now
    info = cached(install)
    if not force and is_fresh(info, now):
        return info
    key = read_key(install)
    if not key:
        return info                                     # no key, no request
    got = fetch(base, key, opener, timeout)
    return save(install, got, now) if got else info


def describe(install):
    """The sentence for Settings line 4."""
    info = cached(install)
    label = str(info.get("label") or "")
    if label:
        tier = str(info.get("tier") or "")
        return f"{label} ({tier})" if tier else label
    return "(could not ask the site yet)" if read_key(install) else "(not set up yet - choose Start now)"
