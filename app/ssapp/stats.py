"""Live stats for the window and tray: the worker's own log, one nvidia-smi reading, and the public queue figures."""
import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MAKING_EVENTS = {"claim", "resume", "run_start"}
DONE_EVENTS = {"complete", "fail", "idle", "stop", "lease_lost"}
_KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def parse_line(line):
    """'2026-11-11T10:00:00Z claim job=ab attempt=1 lines=12' -> (epoch, event, {kv}) or None."""
    parts = line.strip().split(" ", 2)
    if len(parts) < 2:
        return None
    try:
        ts = datetime.strptime(parts[0].rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    kv = {}
    for k, v in _KV.findall(parts[2] if len(parts) > 2 else ""):
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        kv[k] = v
    return ts, parts[1], kv


def _int(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def read_log_stats(log_dir, since=None, now=None, max_files=3):
    """Totals from the newest worker_*.log files. `since` (epoch) limits to this session."""
    now = now or time.time()
    out = {"lines": 0, "jobs": 0, "busy_seconds": 0.0, "making": False, "last_event": "", "last_event_at": 0,
           "lines_per_hour": 0.0, "current_lines": 0, "errors": 0,
           "words": 0, "audio_seconds": 0.0, "completions": [], "recent": [], "current_job": "", "current_since": 0}
    files = sorted(Path(log_dir).glob("worker_*.log"))[-max_files:]
    events = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    p = parse_line(ln)
                    if p and (since is None or p[0] >= since):
                        events.append(p)
        except OSError:
            pass
    first = events[0][0] if events else None
    for ts, ev, kv in events:
        job = kv.get("job", "-")
        if ev == "complete":
            n = _int(kv.get("ok")) + _int(kv.get("held"))
            out["lines"] += n
            out["jobs"] += 1
            out["busy_seconds"] += float(kv.get("seconds") or 0)
            out["words"] += _int(kv.get("words"))                  # present once the worker logs it
            out["audio_seconds"] += float(kv.get("audio") or 0)
            out["completions"].append((ts, n))
            out["recent"].append((ts, "ok", f"job {job[:6]} done - {n} lines"))
        elif ev == "error":
            out["errors"] += 1
            out["recent"].append((ts, "warn", "a problem was handled; the worker carries on"))
        elif ev == "fail":
            out["recent"].append((ts, "warn", f"job {job[:6]} handed back"))
        elif ev == "lease_lost":
            out["recent"].append((ts, "warn", f"job {job[:6]} was taken back by the site"))
        if ev in MAKING_EVENTS:
            out["making"] = True
            out["current_lines"] = _int(kv.get("lines"))
            if ev == "claim" or not out["current_job"]:
                out["current_job"], out["current_since"] = job, ts
            if ev in ("claim", "resume"):
                out["recent"].append((ts, "info", f"job {job[:6]} started - {out['current_lines']} lines"))
        elif ev in DONE_EVENTS:
            out["making"] = False
            out["current_lines"] = 0
            out["current_job"] = ""
        out["last_event"], out["last_event_at"] = ev, ts
    if out["making"] and now - out["last_event_at"] > 3 * 3600:
        out["making"] = False           # a job cannot run longer than the 3 h lease cap
    hours = ((now - first) / 3600.0) if first else 0
    if hours > 0.01:
        out["lines_per_hour"] = round(out["lines"] / hours, 1)
    return out


def gpu_reading(runner=subprocess.run):
    """Temperature, load and memory of the first GPU, or None."""
    try:
        r = runner(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        vals = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        t, u, mu, mt = (int(float(v)) for v in vals[:4])
        return {"temp_c": t, "util_pct": u, "mem_used_mib": mu, "mem_total_mib": mt}
    except Exception:
        return None


def gpu_info(runner=subprocess.run):
    """(name, total MiB) of the first GPU, or (None, None)."""
    try:
        r = runner(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                   capture_output=True, text=True, timeout=5)
        name, mib = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")[:2]]
        return name[:64], int(float(mib))
    except Exception:
        return None, None


BARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def sparkline(values):
    """Unicode bars scaled to the largest value; all zeros gives the lowest bar."""
    values = list(values)
    top = max(values) if values else 0
    if top <= 0:
        return BARS[0] * len(values)
    return "".join(BARS[min(7, int(v / top * 7.999))] for v in values)


def speed_series(completions, now, buckets=15, bucket_seconds=240):
    """Lines finished in each of the last `buckets` slices (oldest first): an hour at the defaults."""
    out = [0] * buckets
    for ts, n in completions:
        age = int((now - ts) // bucket_seconds)
        if 0 <= age < buckets:
            out[buckets - 1 - age] += n
    return out


def gpu_total_mib(runner=subprocess.run):
    g = gpu_reading(runner)
    return g["mem_total_mib"] if g else None


def fetch_queue(api_base, opener=urllib.request.urlopen, timeout=8):
    """Public figures from /api/network/stats: queued lines and the site's time estimate. None when unreachable."""
    try:
        with opener(api_base.rstrip("/") + "/api/network/stats", timeout=timeout) as r:
            d = json.loads(r.read(2_000_000).decode("utf-8"))
        q = d.get("queue") or {}
        return {"queued_lines": _int(q.get("queued_lines")) + _int(q.get("in_progress_lines")),
                "eta_seconds": d.get("eta_seconds")}
    except Exception:
        return None


def format_duration(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60} min"
    h = sec / 3600
    return f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} days"


def snapshot(work_dir, api_base, since=None, now=None, gpu=gpu_reading, queue=fetch_queue):
    """Everything the stats window shows, in one dict."""
    now = now or time.time()
    s = read_log_stats(Path(work_dir) / "logs", since=since, now=now)
    s["gpu"] = gpu()
    s["speed"] = speed_series(s["completions"], now)
    q = queue(api_base)
    s["queue"] = q
    # time left: the site's own estimate (all workers); ours alone when the site gives none
    if q and q.get("eta_seconds") is not None:
        s["time_left"] = q["eta_seconds"]
    elif q and s["lines_per_hour"] > 0:
        s["time_left"] = q["queued_lines"] / s["lines_per_hour"] * 3600
    else:
        s["time_left"] = None
    return s
