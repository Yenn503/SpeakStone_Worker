"""First-run setup, as a list of small steps. Every outside effect (download, unzip, run a program, ask for the key,
read the GPU) is passed in through `Env`, so tests run the whole list with fakes and no GPU or network.

Recipe mirrors the Linux pod setup (uv venv, pip installs, cu128 torch, model snapshot):
  uv (portable) -> Python 3.11 -> index-tts 830f6f8 -> torch cu128 -> model IndexTeam/IndexTTS-2 -> ffmpeg -> init.
The worker key goes to `net_worker.py init` through the SSW_WORKER_KEY environment variable only. It is never part
of a command line, never logged and never stored by this app.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

INDEX_TTS_COMMIT = "830f6f8"
MIN_VRAM_MIB = 7900                      # "8 GB" cards report 8192 MiB; 7900 leaves room for reserved memory
UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
INDEX_TTS_URL = f"https://github.com/index-tts/index-tts/archive/{INDEX_TTS_COMMIT}.zip"
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
MODEL_REPO = "IndexTeam/IndexTTS-2"
WORKER_FILES = ("net_worker.py", "indextts2_worker.py", "runpod_worker.py")

STEPS = ("gpu", "folders", "worker_files", "uv", "python", "index_tts", "packages", "torch", "model", "ffmpeg",
         "key", "check")
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
    "ffmpeg": "Fetching audio tools",
    "key": "Saving your worker key",
    "check": "Final check",
}


class SetupError(Exception):
    pass


@dataclass
class Env:
    install: Path
    resource_dir: Path                                  # where the bundled net_worker.py etc. sit
    download: callable = None                           # (url, dest_path, progress(frac)) -> None
    run: callable = None                                # (cmd list, env dict, cwd, on_line) -> exit code
    ask_key: callable = None                            # () -> str   (hidden input; may return "")
    gpu_total_mib: callable = None                      # () -> int | None
    progress: callable = field(default=lambda step, frac, msg: None)
    base_env: dict = field(default_factory=lambda: dict(os.environ))

    # ---- derived paths
    @property
    def tools(self):
        return self.install / "tools"

    @property
    def uv(self):
        return self.tools / "uv.exe"

    @property
    def venv(self):
        return self.install / "venv"

    @property
    def py(self):
        return self.venv / "Scripts" / "python.exe"

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

    def tool_env(self, extra=None):
        e = dict(self.base_env)
        e["UV_PYTHON_INSTALL_DIR"] = str(self.install / "python")
        e["UV_CACHE_DIR"] = str(self.install / "uv_cache")
        e["HF_HOME"] = str(self.install / "hf")
        e["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        e["PYTHONUNBUFFERED"] = "1"
        e["PATH"] = str(self.ffmpeg_dir) + os.pathsep + e.get("PATH", "")
        e.update(extra or {})
        return e


# ---------------------------------------------------------------- default (real) effects
def real_download(url, dest, progress=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "SpeakStoneWorkerApp"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            got += len(b)
            if progress and total:
                progress(got / total)
    os.replace(part, dest)


def real_run(cmd, env, cwd, on_line):
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    p = subprocess.Popen([str(c) for c in cmd], env=env, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", **kw)
    for line in p.stdout:
        if on_line:
            on_line(line.rstrip())
    return p.wait()


# ---------------------------------------------------------------- state (so an interrupted setup resumes)
def load_state(env):
    try:
        d = json.loads(env.state_file.read_text(encoding="utf-8"))
        return set(d.get("done", []))
    except (OSError, ValueError):
        return set()


def save_state(env, done):
    env.install.mkdir(parents=True, exist_ok=True)
    env.state_file.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")


def _run(env, step, cmd, extra_env=None, cwd=None):
    """Run a command; the command line is logged, the environment never is."""
    env.progress(step, None, "> " + " ".join(str(c) for c in cmd[:6]))
    rc = env.run(cmd, env.tool_env(extra_env), cwd, lambda ln: env.progress(step, None, ln))
    if rc != 0:
        raise SetupError(f"{LABELS[step]} failed (exit {rc}). See the log above.")


def _unzip(zip_path, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for m in z.namelist():                           # refuse paths that climb out of the folder
            if not (root / m).resolve().is_relative_to(root):
                raise SetupError("downloaded archive has an unsafe path")
        z.extractall(dest)


# ---------------------------------------------------------------- the steps
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
    z = env.tools / "uv.zip"
    env.download(UV_URL, z, lambda f: env.progress("uv", f, ""))
    _unzip(z, env.tools / "uv_x")
    found = next((env.tools / "uv_x").rglob("uv.exe"), None)
    if not found:
        raise SetupError("uv.exe not found in the download")
    shutil.copy2(found, env.uv)
    shutil.rmtree(env.tools / "uv_x", ignore_errors=True)
    z.unlink(missing_ok=True)


def step_python(env):
    _run(env, "python", [env.uv, "python", "install", "3.11"])
    if not env.py.exists():
        _run(env, "python", [env.uv, "venv", env.venv, "--python", "3.11"])


def step_index_tts(env):
    if env.engine_dir.exists() and (env.engine_dir / "pyproject.toml").exists():
        return
    z = env.install / "index-tts.zip"
    env.download(INDEX_TTS_URL, z, lambda f: env.progress("index_tts", f, ""))
    _unzip(z, env.install / "_it")
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
    _run(env, "torch", [env.uv, "pip", "install", "--python", env.py, "--reinstall", "torch", "torchaudio",
                        "--index-url", TORCH_INDEX])
    _run(env, "torch", [env.uv, "pip", "install", "--python", env.py, "--upgrade", "omegaconf>=2.3", "protobuf<3.21"])


def step_model(env):
    code = (f"from huggingface_hub import snapshot_download; "
            f"snapshot_download('{MODEL_REPO}', local_dir=r'{env.model_dir}')")
    _run(env, "model", [env.py, "-c", code])


def step_ffmpeg(env):
    if (env.ffmpeg_dir / "ffmpeg.exe").exists():
        return
    z = env.install / "ffmpeg.zip"
    env.download(FFMPEG_URL, z, lambda f: env.progress("ffmpeg", f, ""))
    _unzip(z, env.install / "_ff")
    exe = next((env.install / "_ff").rglob("ffmpeg.exe"), None)
    if not exe:
        raise SetupError("ffmpeg.exe not found in the download")
    env.ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe, env.ffmpeg_dir / "ffmpeg.exe")
    shutil.rmtree(env.install / "_ff", ignore_errors=True)
    z.unlink(missing_ok=True)


def step_key(env):
    cfg = env.install / "worker_config.json"
    if cfg.exists():
        return                                          # already initialised; key kept as is
    key = env.ask_key()
    if not key:
        raise SetupError("A worker key is needed. Ask the owner for one.")
    # The key travels only in the child's environment. It is not in `cmd`, so no log line can contain it.
    _run(env, "key", [env.py, env.install / "net_worker.py", "init", "--config", cfg,
                      "--model-dir", env.model_dir, "--work-dir", env.work_dir],
         extra_env={"SSW_WORKER_KEY": key}, cwd=env.install)
    if not cfg.exists():
        raise SetupError("The worker key was not accepted (it looks like sswk_ followed by 43 characters).")


def step_check(env):
    _run(env, "check", [env.py, "-c",
                        "import torch; assert torch.cuda.is_available(), 'no CUDA'; "
                        "print('graphics card ready:', torch.cuda.get_device_name(0))"])


FUNCS = {"gpu": step_gpu, "folders": step_folders, "worker_files": step_worker_files, "uv": step_uv,
         "python": step_python, "index_tts": step_index_tts, "packages": step_packages, "torch": step_torch,
         "model": step_model, "ffmpeg": step_ffmpeg, "key": step_key, "check": step_check}


def run_setup(env, only=None):
    """Run every step not yet done, saving progress after each. Returns the list of steps run."""
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
