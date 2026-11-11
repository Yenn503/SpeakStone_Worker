"""First-run setup as small, resumable steps. Ported from app/ssapp/setup_steps.py, made cross-platform.

Every outside effect (download, run a program, enrol, read the GPU) is passed in through `Env`, so tests run the
whole list with fakes. The worker key goes to `net_worker.py init` through SSW_WORKER_KEY in the child's
environment only: never a command line, never a log line, never a file of ours.

  uv (portable) -> Python 3.11 -> index-tts 830f6f8 -> packages -> torch (PINNED) -> model -> extras -> ffmpeg -> enrol -> check
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

INDEX_TTS_COMMIT = "830f6f8"
MIN_VRAM_MIB = 7900                      # 8 GB cards report 8192 MiB; 7900 leaves room for reserved memory
UV_URLS = {
    "win": "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip",
    "linux": "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz",
}
INDEX_TTS_URL = f"https://github.com/index-tts/index-tts/archive/{INDEX_TTS_COMMIT}.zip"
FFMPEG_URLS = {
    "win": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "linux": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
}
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
# The owner hit this: an unpinned `--reinstall torch` pulled 2.11, which the voice engine does not work with.
TORCH_PINS = ("torch==2.8.0+cu128", "torchaudio==2.8.0+cu128")
MODEL_REPO = "IndexTeam/IndexTTS-2"
# What the engine (commit 830f6f8, indextts/infer_v2.py + utils/maskgct_utils.py) fetches on its first job.
EXTRA_SNAPSHOTS = (
    ("facebook/w2v-bert-2.0", ["*.json", "*.safetensors", "*.txt"]),
    ("nvidia/bigvgan_v2_22khz_80band_256x", ["config.json", "bigvgan_generator.pt"]),
)
EXTRA_FILES = (
    ("amphion/MaskGCT", "semantic_codec/model.safetensors"),
    ("funasr/campplus", "campplus_cn_common.bin"),
)
WORKER_FILES = ("net_worker.py", "indextts2_worker.py", "runpod_worker.py")

STEPS = ("gpu", "folders", "worker_files", "uv", "python", "index_tts", "packages", "torch", "model", "extras", "ffmpeg",
         "enroll", "check")
LABELS = {
    "gpu": "Checking your graphics card",
    "folders": "Preparing the install folder",
    "worker_files": "Copying the worker",
    "uv": "Fetching the installer tool",
    "python": "Installing Python 3.11 (private copy)",
    "index_tts": "Fetching the voice engine",
    "packages": "Installing the voice engine",
    "torch": "Installing the graphics-card libraries",
    "model": "Downloading the voice model (about 5.5 GB)",
    "extras": "Fetching extra voice files (about 2 GB)",
    "ffmpeg": "Fetching audio tools",
    "enroll": "Joining the queue",
    "check": "Final check",
}


class SetupError(Exception):
    pass


@dataclass
class Env:
    install: Path
    resource_dir: Path
    download: callable = None                           # (url, dest_path, progress(frac, got, total)) -> None
    run: callable = None                                # (cmd list, env dict, cwd, on_line) -> exit code
    enroll: callable = None                             # () -> {"key","label","worker_id","tier"}
    gpu_total_mib: callable = None                      # () -> int | None
    progress: callable = field(default=lambda step, frac, msg: None)
    base_env: dict = field(default_factory=lambda: dict(os.environ))
    is_win: bool = field(default_factory=lambda: sys.platform == "win32")
    api_base: str = ""                                  # only passed to the worker when it is not the default
    machine_name: str = ""

    @property
    def _exe(self):
        return ".exe" if self.is_win else ""

    @property
    def tools(self):
        return self.install / "tools"

    @property
    def uv(self):
        return self.tools / ("uv" + self._exe)

    @property
    def venv(self):
        return self.install / "venv"

    @property
    def py(self):
        return self.venv / ("Scripts/python.exe" if self.is_win else "bin/python")

    @property
    def model_dir(self):
        return self.install / "IndexTTS-2"

    @property
    def engine_dir(self):
        return self.install / "index-tts"

    @property
    def ffmpeg_dir(self):
        return self.install / "ffmpeg"

    @property
    def work_dir(self):
        return self.install / "work"

    @property
    def state_file(self):
        return self.install / "setup_state.json"

    @property
    def config_file(self):
        return self.install / "worker_config.json"

    @property
    def work_dir_2(self):
        return self.install / "work_2"

    @property
    def config_file_2(self):
        return self.install / "worker_config_2.json"

    @property
    def enrolled_file_2(self):
        return self.install / "enrolled_2.json"

    @property
    def enrolled_file(self):
        return self.install / "enrolled.json"           # label / tier only, never the key

    def tool_env(self, extra=None):
        e = dict(self.base_env)
        e["UV_PYTHON_INSTALL_DIR"] = str(self.install / "python")
        e["UV_CACHE_DIR"] = str(self.install / "uv_cache")
        e["HF_HOME"] = str(self.install / "hf")
        e["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        e["PYTHONUNBUFFERED"] = "1"
        e["PATH"] = str(self.ffmpeg_dir) + os.pathsep + e.get("PATH", "")
        e.pop("SSW_WORKER_KEY", None)
        e.update(extra or {})
        return e


# ---------------------------------------------------------------- real effects
def real_download(url, dest, progress=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "SpeakStoneWorkerCLI"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            got += len(b)
            if progress:
                progress(got / total if total else None, got, total)
    os.replace(part, dest)


def real_run(cmd, env, cwd, on_line):
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    p = subprocess.Popen([str(c) for c in cmd], env=env, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", **kw)
    for line in p.stdout:
        if on_line:
            on_line(line.rstrip())
    return p.wait()


# ---------------------------------------------------------------- resume state
def load_state(env):
    try:
        return set(json.loads(env.state_file.read_text(encoding="utf-8")).get("done", []))
    except (OSError, ValueError):
        return set()


def save_state(env, done):
    env.install.mkdir(parents=True, exist_ok=True)
    env.state_file.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")


_PCT = re.compile(r"(\d{1,3})%")


def _run(env, step, cmd, extra_env=None, cwd=None):
    """Run a command. The command line is reported, the environment never is."""
    env.progress(step, None, "> " + " ".join(str(c) for c in cmd[:6]))

    def on_line(ln):
        frac = None
        if step in ("model", "extras"):                          # huggingface_hub's tqdm: "Fetching 20 files:  45%|..."
            m = _PCT.findall(ln)
            if m:
                frac = min(100, int(m[-1])) / 100
        env.progress(step, frac, ln[-200:])

    rc = env.run(cmd, env.tool_env(extra_env), cwd, on_line)
    if rc != 0:
        raise SetupError(f"{LABELS[step]} failed (exit {rc}). See the lines above.")


def _extract(archive, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    archive = str(archive)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for m in z.namelist():
                if not (root / m).resolve().is_relative_to(root):
                    raise SetupError("downloaded archive has an unsafe path")
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                if not (root / m.name).resolve().is_relative_to(root) or m.issym() or m.islnk():
                    raise SetupError("downloaded archive has an unsafe path")
            t.extractall(dest)


def _plat(env):
    return "win" if env.is_win else "linux"


# ---------------------------------------------------------------- steps
def step_gpu(env):
    mib = env.gpu_total_mib()
    if mib is None:
        raise SetupError("No NVIDIA graphics card was found (nvidia-smi did not answer). "
                         "Install the current NVIDIA driver and try again.")
    if mib < MIN_VRAM_MIB:
        raise SetupError(f"Your graphics card has {mib // 1024} GB of memory; at least 8 GB is needed.")
    env.progress("gpu", 1, f"graphics card memory: {mib} MiB")


def step_folders(env):
    for d in (env.install, env.tools, env.work_dir):
        d.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(env.install).free
    if free < 20 * 1024 ** 3:
        raise SetupError(f"Only {free // 1024 ** 3} GB free on that drive; about 20 GB is needed.")


def step_worker_files(env):
    for n in WORKER_FILES:
        src = env.resource_dir / n
        if not src.exists():
            raise SetupError(f"{n} is missing from this app.")
        shutil.copy2(src, env.install / n)


def step_uv(env):
    if env.uv.exists():
        return
    z = env.tools / ("uv.zip" if env.is_win else "uv.tar.gz")
    env.download(UV_URLS[_plat(env)], z, lambda f, g=0, t=0: env.progress("uv", f, ""))
    _extract(z, env.tools / "uv_x")
    found = next((p for p in (env.tools / "uv_x").rglob("uv" + env._exe) if p.is_file()), None)
    if not found:
        raise SetupError("the installer tool was not in the download")
    shutil.copy2(found, env.uv)
    if not env.is_win:
        env.uv.chmod(0o755)
    shutil.rmtree(env.tools / "uv_x", ignore_errors=True)
    z.unlink(missing_ok=True)


def step_python(env):
    _run(env, "python", [env.uv, "python", "install", "3.11"])
    if not env.py.exists():
        _run(env, "python", [env.uv, "venv", env.venv, "--python", "3.11"])


def step_index_tts(env):
    if (env.engine_dir / "pyproject.toml").exists():
        return
    z = env.install / "index-tts.zip"
    env.download(INDEX_TTS_URL, z, lambda f, g=0, t=0: env.progress("index_tts", f, ""))
    _extract(z, env.install / "_it")
    sub = next((p for p in (env.install / "_it").iterdir() if p.is_dir()), None)
    if not sub:
        raise SetupError("the voice engine download was empty")
    shutil.rmtree(env.engine_dir, ignore_errors=True)
    shutil.move(str(sub), str(env.engine_dir))
    shutil.rmtree(env.install / "_it", ignore_errors=True)
    z.unlink(missing_ok=True)


def step_packages(env):
    _run(env, "packages", [env.uv, "pip", "install", "--python", env.py, "-e", env.engine_dir,
                           "soundfile", "numpy", "huggingface_hub"])


def step_torch(env):
    # Pinned. Never `--reinstall torch` unpinned: it pulls the newest release (2.11 at the time), which breaks the engine.
    _run(env, "torch", [env.uv, "pip", "install", "--python", env.py, *TORCH_PINS, "--index-url", TORCH_INDEX])
    _run(env, "torch", [env.uv, "pip", "install", "--python", env.py, "--upgrade", "omegaconf>=2.3", "protobuf<3.21"])


def step_model(env):
    code = (f"from huggingface_hub import snapshot_download; "
            f"snapshot_download('{MODEL_REPO}', local_dir=r'{env.model_dir}')")
    _run(env, "model", [env.py, "-c", code])


def extras_code():
    """Short script for the install's own Python: only downloads, never loads the voice model or the graphics card.
    Already-fetched files are skipped and a part-finished download carries on, so a re-run is cheap."""
    lines = ["from huggingface_hub import snapshot_download, hf_hub_download"]
    lines += [f"snapshot_download({repo!r}, allow_patterns={pats!r})" for repo, pats in EXTRA_SNAPSHOTS]
    lines += [f"hf_hub_download({repo!r}, filename={fn!r})" for repo, fn in EXTRA_FILES]
    return "; ".join(lines)


def step_extras(env):
    # Same cache the worker uses (HF_HOME inside the install folder, set by tool_env and by the runner).
    _run(env, "extras", [env.py, "-c", extras_code()])


def step_ffmpeg(env):
    exe = "ffmpeg" + env._exe
    if (env.ffmpeg_dir / exe).exists():
        return
    if not env.is_win and shutil.which("ffmpeg"):        # a system ffmpeg is fine
        return
    z = env.install / ("ffmpeg.zip" if env.is_win else "ffmpeg.tar.xz")
    env.download(FFMPEG_URLS[_plat(env)], z, lambda f, g=0, t=0: env.progress("ffmpeg", f, ""))
    _extract(z, env.install / "_ff")
    found = next((p for p in (env.install / "_ff").rglob(exe) if p.is_file()), None)
    if not found:
        raise SetupError("the audio tools were not in the download")
    env.ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(found, env.ffmpeg_dir / exe)
    if not env.is_win:
        (env.ffmpeg_dir / exe).chmod(0o755)
    shutil.rmtree(env.install / "_ff", ignore_errors=True)
    z.unlink(missing_ok=True)


def step_enroll(env):
    """Ask the site for a key and hand it straight to `net_worker.py init` (child environment only)."""
    if env.config_file.exists():
        return
    info = env.enroll()
    key = info["key"]
    cmd = [env.py, env.install / "net_worker.py", "init", "--config", env.config_file,
           "--model-dir", env.model_dir, "--work-dir", env.work_dir]
    if env.api_base:
        cmd += ["--api-base", env.api_base]
    if env.machine_name:
        cmd += ["--machine-name", env.machine_name]
    _run(env, "enroll", cmd, extra_env={"SSW_WORKER_KEY": key}, cwd=env.install)
    if not env.config_file.exists():
        raise SetupError("The worker key was not accepted by the worker. Please try again.")
    env.enrolled_file.write_text(json.dumps({"worker_id": info.get("worker_id"), "label": info.get("label"),
                                             "tier": info.get("tier")}), encoding="utf-8")
    env.progress("enroll", 1, f"Joined the queue as {info.get('label') or 'a new worker'}")


# Two workers on one card: each is an ordinary one-lease worker with its own key (the site limits a key to one lease,
# so sharing a key would hand the second process the first one's job).
MIN_TOTAL_MIB_FOR_TWO = 12000            # a "12 GB" card reports 12282-12288 MiB
MIN_FREE_MIB_FOR_TWO = 10000             # about 5 GB for each process; "10 GB" cards report a little under 10240


def second_worker_allowed(gpu):
    """(ok, sentence). `gpu` is stats.gpu_reading()."""
    if not gpu:
        return False, "No NVIDIA graphics card was found."
    if gpu.get("mem_total_mib", 0) < MIN_TOTAL_MIB_FOR_TWO:
        return False, "Two workers need a graphics card with at least 12 GB of memory."
    if gpu["mem_total_mib"] - gpu.get("mem_used_mib", 0) < MIN_FREE_MIB_FOR_TWO:
        return False, "Two workers need about 10 GB of free graphics memory right now. Close other programs and try again."
    return True, ""


def enroll_second(env):
    """Join the queue a second time as '<host>-2' and write worker_config_2.json. Kept if it already exists."""
    if env.config_file_2.exists():
        return
    name = (env.machine_name or "worker") + "-2"
    info = env.enroll(machine_name=name)
    key = info["key"]
    env.work_dir_2.mkdir(parents=True, exist_ok=True)
    cmd = [env.py, env.install / "net_worker.py", "init", "--config", env.config_file_2,
           "--model-dir", env.model_dir, "--work-dir", env.work_dir_2, "--machine-name", name]
    if env.api_base:
        cmd += ["--api-base", env.api_base]
    _run(env, "enroll", cmd, extra_env={"SSW_WORKER_KEY": key}, cwd=env.install)
    if not env.config_file_2.exists():
        raise SetupError("The second worker key was not accepted by the worker. Please try again.")
    env.enrolled_file_2.write_text(json.dumps({"worker_id": info.get("worker_id"), "label": info.get("label"),
                                               "tier": info.get("tier")}), encoding="utf-8")


def step_check(env):
    _run(env, "check", [env.py, "-c",
                        "import torch; assert torch.cuda.is_available(), 'no CUDA'; "
                        "print('graphics card ready:', torch.cuda.get_device_name(0))"])


FUNCS = {"gpu": step_gpu, "folders": step_folders, "worker_files": step_worker_files, "uv": step_uv,
         "python": step_python, "index_tts": step_index_tts, "packages": step_packages, "torch": step_torch,
         "model": step_model, "extras": step_extras, "ffmpeg": step_ffmpeg, "enroll": step_enroll, "check": step_check}


def is_set_up(install):
    try:
        done = set(json.loads((Path(install) / "setup_state.json").read_text(encoding="utf-8")).get("done", []))
    except (OSError, ValueError):
        return False
    return all(s in done for s in STEPS if s != "gpu")


def run_setup(env, only=None):
    """Run every step not yet done, saving progress after each. Returns the steps run."""
    done = load_state(env)
    ran = []
    total = len(STEPS)
    for i, name in enumerate(STEPS):
        if only and name not in only:
            continue
        env.progress(name, i / total, LABELS[name])
        if name in done and name not in ("gpu", "folders"):
            continue
        FUNCS[name](env)
        if name != "gpu":
            done.add(name)
            save_state(env, done)
        ran.append(name)
    env.progress("done", 1.0, "Setup finished.")
    return ran
