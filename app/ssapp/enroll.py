"""Joining the network: ask the site for this PC's own worker key (POST /api/worker/enroll, spec 12a "Auto-enrol").

No key is typed or pasted. The key comes back once, goes straight into `net_worker.py init` through an environment
variable, and is never printed, logged or kept by this app.
"""
import json
import socket
import sys
import urllib.error
import urllib.request

from . import APP_VERSION
from .setup_steps import SetupError

DEFAULT_API = "https://speakstone.beanw.co.uk"


def enroll(api_base, machine_name, gpu, vram_mib, opener=urllib.request.urlopen, timeout=20):
    """Return the new worker key (sswk_...). Raises SetupError with a plain-words message."""
    body = json.dumps({"machine_name": (machine_name or "")[:64], "gpu": (gpu or "")[:64],
                       "vram_mib": int(vram_mib or 0),
                       "os": "windows" if sys.platform.startswith("win") else "linux",
                       "worker_version": str(APP_VERSION)[:32]}).encode()
    req = urllib.request.Request(api_base.rstrip("/") + "/api/worker/enroll", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"SpeakStoneWorkerApp/{APP_VERSION}"})
    try:
        with opener(req, timeout=timeout) as r:
            raw = r.read(100_000)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise SetupError("The site is not taking new helpers right now. Try again in an hour.") from None
        raise SetupError(f"The site would not let this PC join (HTTP {e.code}). Try again later.") from None
    except (urllib.error.URLError, OSError):
        raise SetupError("Could not reach the SpeakStone site to join. Check the internet connection and run again.") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        data = None
    key = data.get("key") if isinstance(data, dict) else None
    if not (isinstance(key, str) and key.startswith("sswk_") and len(key) == 48):
        raise SetupError("The site answered, but not with a usable key. Try again later.")
    return key


def make_enroller(api_base, gpu_info, hostname=socket.gethostname):
    """() -> key, for Env.enroll."""
    def go():
        name, mib = gpu_info()
        return enroll(api_base, hostname(), name or "", mib or 0)
    return go
