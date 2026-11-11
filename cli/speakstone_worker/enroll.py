"""Auto-enrol: one POST to the site, which answers with this PC's worker key."""
import json
import urllib.error
import urllib.request


class EnrollError(Exception):
    """Message is written for the person at the keyboard. Never contains a key."""


def friendly_error(status):
    if status == 429:
        return "Too many sign-ups from this network just now. Please try again in a little while."
    if status == 503:
        return "The queue is not taking new workers right now. Please try again later."
    if status == 422:
        return "The site did not accept this PC's details (graphics card or system name). Nothing was changed."
    return f"The site answered with an unexpected error (HTTP {status}). Please try again later."


def enroll(api_base, machine_name, gpu, vram_mib, os_name, worker_version, opener=urllib.request.urlopen, timeout=30):
    body = json.dumps({"machine_name": machine_name, "gpu": gpu, "vram_mib": int(vram_mib), "os": os_name,
                       "worker_version": worker_version}).encode("utf-8")
    req = urllib.request.Request(api_base.rstrip("/") + "/api/worker/enroll", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"speakstone-worker-cli/{worker_version}"})
    try:
        with opener(req, timeout=timeout) as r:
            status = getattr(r, "status", 201)
            data = json.loads(r.read(100_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise EnrollError(friendly_error(e.code)) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise EnrollError("Could not reach the SpeakStone site. Check your internet connection and try again.") from None
    except ValueError:
        raise EnrollError("The site's answer was not understood. Please try again later.") from None
    if status != 201 or not isinstance(data, dict) or not isinstance(data.get("key"), str) or not data["key"]:
        raise EnrollError(friendly_error(status) if status != 201 else "The site's answer was incomplete.")
    return {"worker_id": data.get("worker_id"), "key": data["key"], "label": str(data.get("label") or ""),
            "tier": data.get("tier")}
