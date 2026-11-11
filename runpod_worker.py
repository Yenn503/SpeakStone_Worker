#!/usr/bin/env python3
"""
runpod_worker.py - Hardened voice-line worker helpers (level + encode, quality gate, seeds, validity check).
Consumes pre-flighted deterministic rows, guarantees single-model VRAM residence,
applies exact audition recipes with FFmpeg post-processing (fixed 24 kHz mono Vorbis @ 32 kbps),
writes temporary files atomically, and records comprehensive JSONL provenance with cryptographic
recipe fingerprints for verified checkpoint/resume.
"""
import argparse
import gc
import hashlib
import json
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Enforce persistent cache directories if running in RunPod environment (/workspace exists)
WORKSPACE_PATH = Path("/workspace")
if WORKSPACE_PATH.exists():
    os.environ.setdefault("HF_HOME", "/workspace/hf_cache")
    os.environ.setdefault("TORCH_HOME", "/workspace/torch_cache")
    Path("/workspace/hf_cache").mkdir(parents=True, exist_ok=True)
    Path("/workspace/torch_cache").mkdir(parents=True, exist_ok=True)

import numpy as np
import soundfile as sf
import torch

try:
    import faster_qwen3_tts
    from faster_qwen3_tts import FasterQwen3TTS
    FASTER_QWEN_VERSION = getattr(faster_qwen3_tts, "__version__", "0.5.3")
except ImportError:
    FasterQwen3TTS = None
    FASTER_QWEN_VERSION = "0.5.3"

PINNED_MODELS = {
    "qwen_06b": {
        "repo_id": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        "revision": "5d83992436eae1d760afd27aff78a71d676296fc"
    },
    "faster_qwen_17b": {
        "repo_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "revision": "fd4b254389122332181a7c3db7f27e918eec64e3"
    }
}

ALT_ENGINES = {"indextts2"}     # made by indextts2_worker.py

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REFS = SCRIPT_DIR / "references"
OUTPUT_SAMPLE_RATE = 24000
OUTPUT_CHANNELS = 1
MAX_NEW_TOKENS = 1024


QC_HELD_DIR = "_held_qc"
QC_LOG = "_qc_held.jsonl"
QC_TRIES = 3


def qc_clip(duration, text):
    """Why a made take fails the quality gate, or "": under 0.3 s, or longer than 0.75 s per word + 6 s (a loop or a
    runaway). The rule previously applied by hand."""
    words = len(re.sub(r"<[^>]*>", " ", text or "").split())
    if duration < 0.3:
        return f"too short: {duration:.2f} s"
    if words and duration > words * 0.75 + 6:
        return f"too long for {words} words: {duration:.1f} s (limit {words * 0.75 + 6:.1f} s)"
    return ""
LANGUAGE = "English"
APPEND_SILENCE = True


def is_gpu_fatal(error):
    """Return True when retrying later rows in this process is unsafe."""
    if isinstance(error, torch.OutOfMemoryError):
        return True
    message = str(error).lower()
    fatal_markers = (
        "cuda out of memory",
        "device-side assert",
        "illegal memory access",
        "cublas_status",
        "cudnn_status",
        "cuda error",
    )
    return any(marker in message for marker in fatal_markers)


def get_audio_info(filepath):
    """Verify audio file readability and extract duration using libsndfile / soundfile."""
    try:
        info = sf.info(str(filepath))
        return info.duration, info.samplerate, info.channels
    except Exception:
        return 0.0, 0, 0

def is_valid_output(filepath):
    """Require a readable, non-empty 24 kHz mono output file."""
    duration, sample_rate, channels = get_audio_info(filepath)
    return (
        duration > 0.05
        and sample_rate == OUTPUT_SAMPLE_RATE
        and channels == OUTPUT_CHANNELS
    )

def compute_generation_seed(row):
    """Return the stable per-clip seed used for stochastic model sampling."""
    if "generation_seed" in row:
        seed = int(row["generation_seed"])
        if not 0 <= seed <= 0x7FFFFFFF:
            raise ValueError(f"generation_seed must be between 0 and 2147483647, got {seed}")
        return seed

    seed_material = "|".join([
        str(row["sound_file"]),
        str(row["text_hash"]),
        str(row["profile_key"]),
        str(row["model_engine"]),
    ])
    return int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF

def compute_recipe_fingerprint(row, bitrate="32k"):
    """
    Computes a cryptographic fingerprint of all variables affecting the audio output:
    text hash, profile, canonical reference, reference text, model engine, exact pinned model commit revision,
    tuning mode, instruct prompt, generation temperature, max_new_tokens, post-AF, and bitrate.
    """
    ref_text = row.get("ref_text", "") or ""
    engine = row["model_engine"]
    model_rev = PINNED_MODELS.get(engine, {}).get("revision", engine)
    instruct = row.get("instruct", "") or ""
    mode = row.get("tuning_mode", "tuned")
    temp = str(row["temperature"]) if row.get("temperature") else ("0.98" if instruct else "0.95")
    generation_seed = compute_generation_seed(row)
    xvec_only = not bool(ref_text.strip())
    payload = (
        f"{row['text_hash']}|"
        f"{row['profile_key']}|"
        f"{row['canonical_reference_clip']}|"
        f"{ref_text}|"
        f"{engine}|"
        f"{model_rev}|"
        f"{mode}|"
        f"{instruct}|"
        f"{temp}|"
        f"{MAX_NEW_TOKENS}|"
        f"{generation_seed}|"
        f"{LANGUAGE}|"
        f"{xvec_only}|"
        f"{APPEND_SILENCE}|"
        f"{row.get('post_af', '') or ''}|"
        f"{str(bitrate).lower()}|"
        f"{OUTPUT_SAMPLE_RATE}|"
        f"{OUTPUT_CHANNELS}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

LEAD_KEEP_S = 0.15
QUIET_FLOOR_DB = -26.0
TARGET_RMS_DB = -18.0
# Loudness (2026-10-10: narration "louder or quieter ... not consistent"). The in-game voice-over sits at about
# -16.0 LUFS median. Until then this encoder only lifted clips under
# -26 dB RMS. Now every clip is measured (EBU R128 integrated loudness, after trim and post_af) and:
#   under TARGET - WINDOW            -> levelled to TARGET (two-pass loudnorm, linear gain, limiter);
#   over TARGET + WINDOW, no "!"     -> levelled down to TARGET;
#   over TARGET + WINDOW, with "!"   -> left loud: a shouted line is meant to be loud;
#   within the window                -> untouched.
TARGET_LUFS = -16.0
LOUDNESS_WINDOW_LU = 2.0
PEAK_CEILING_DB = -3.0  # loudnorm true-peak target; Vorbis at 32 kbps overshoots it by 1-2 dB
LIMIT_LINEAR = 0.7      # alimiter ceiling (-3.1 dBFS) after loudnorm
LEVEL_RECIPE = "lufs-16-w2-shout-v1"


def loudness_plan(measured_i, shout):
    """'up', 'down' or None (leave) for a clip at measured_i LUFS."""
    if measured_i is None or measured_i < -70:
        return None
    if measured_i < TARGET_LUFS - LOUDNESS_WINDOW_LU:
        return "up"
    if measured_i > TARGET_LUFS + LOUDNESS_WINDOW_LU and not shout:
        return "down"
    return None


def _loudnorm_measure(in_path, pre_filters):
    """First loudnorm pass over the processed audio: the measured_* values loudnorm wants back, or None."""
    chain = ",".join(list(pre_filters) + [f"loudnorm=I={TARGET_LUFS}:TP={PEAK_CEILING_DB}:LRA=11:print_format=json"])
    r = _run_with_retry(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(in_path), "-af", chain, "-f", "null", "-"],
                        timeout=90)
    import re as _re
    m = _re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr or "", _re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
        float(j["input_i"])
        return j
    except (ValueError, KeyError):
        return None


def loudnorm_filter(j):
    return (f"loudnorm=I={TARGET_LUFS}:TP={PEAK_CEILING_DB}:LRA=11:measured_I={j['input_i']}:"
            f"measured_TP={j['input_tp']}:measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}:"
            f"offset={j['target_offset']}:linear=true,alimiter=limit={LIMIT_LINEAR}:level=false")


RETRY_TIMEOUT = 300


def _run_with_retry(cmd, timeout=90):
    """subprocess.run with no stdin and a timeout; on a timeout it runs ONCE more with RETRY_TIMEOUT before giving up.
    2026-11-07: a 90 s ffmpeg timeout fired on one clip of a 132-clip batch only
    while the GPU and CPU were shared with other jobs; the clip failed, the batch exited 1 and nothing installed. The
    retry is a plain re-run of the same command (subprocess.run has already killed the first ffmpeg)."""
    kw = dict(stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return subprocess.run(cmd, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        print(f"[ffmpeg] timed out after {timeout}s, retrying once with {RETRY_TIMEOUT}s: {Path(str(cmd[-1])).name}",
              flush=True)
        return subprocess.run(cmd, timeout=max(RETRY_TIMEOUT, timeout), **kw)


def _ffmpeg(args, timeout=90):
    """ffmpeg with no stdin (a backgrounded ffmpeg that inherits a terminal stdin stops on its first read) and a
    timeout, so one bad clip can never freeze a worker again. A timeout is retried once with a longer one
    (_run_with_retry, 2026-11-07)."""
    return _run_with_retry(["ffmpeg", "-nostdin", "-hide_banner", "-y"] + args, timeout=timeout)


def apply_post_fx_and_encode(in_wav, out_ogg, post_af=None, bitrate="32k", shout=False):
    """
    Trim leading silence, apply the profile's filters (pitch, formant, bass), level the loudness (see TARGET_LUFS
    above) and encode to fixed 32 kbps 24 kHz mono Vorbis OGG. Returns {"measured_lufs", "level"} for provenance.

    2026-10-10: silenceremove and a measured loudnorm in ONE filter chain hung ffmpeg on some clips (all workers on
    two pods froze ~0.2 s into the output). Now three steps: trim + post_af into a temp WAV; measure that WAV; encode
    it with a plain gain to the target plus a limiter (what loudnorm's linear mode does anyway).
    """
    # Owner 2026-10-04 (3.0.0 listening sample): "long gap at start" (Constable Zo'ardaz 90127_progress, 2.86 s;
    # worst found 6.45 s) and "quiet clip compared to the rest" (Zapetta, -30 dB vs library median -17.8 dB).
    pre = [f"silenceremove=start_periods=1:start_threshold=-45dB:start_silence={LEAD_KEEP_S}"]
    if post_af:
        pre.append(post_af)
    mid = str(out_ogg) + ".pre.wav"
    info = {"measured_lufs": None, "level": None, "level_recipe": LEVEL_RECIPE}
    try:
        r = _ffmpeg(["-i", str(in_wav), "-af", ",".join(pre), "-c:a", "pcm_s16le", mid])
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg pre-filter failed: {r.stderr[-500:]}")
        j = _loudnorm_measure(mid, [])
        filters = []
        if j is not None:
            mi = float(j["input_i"])
            info["measured_lufs"] = round(mi, 2)
            plan = loudness_plan(mi, shout)
            if plan:
                filters.append(f"volume={TARGET_LUFS - mi:.2f}dB,alimiter=limit={LIMIT_LINEAR}:level=false")
                info["level"] = plan
        args = ["-i", mid] + (["-af", ",".join(filters)] if filters else [])
        r = _ffmpeg(args + ["-c:a", "libvorbis", "-b:a", str(bitrate), "-ar", str(OUTPUT_SAMPLE_RATE),
                            "-ac", str(OUTPUT_CHANNELS), str(out_ogg)])
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg encoding failed: {r.stderr[-500:]}")
    finally:
        try:
            os.remove(mid)
        except OSError:
            pass
    return info

class HardenedRunpodWorker:
    def __init__(self, refs_dir, device="cuda", bitrate="32k"):
        self.device = device
        self.bitrate = bitrate
        self.refs_dir = Path(refs_dir)
        self.hostname = socket.gethostname()
        self.torch_version = torch.__version__

        if device == "cuda" and not torch.cuda.is_available():
            print("[FATAL] CUDA requested but not available. Aborting.", file=sys.stderr)
            sys.exit(1)

        if not self.refs_dir.exists():
            print(f"[FATAL] References directory not found: {self.refs_dir}", file=sys.stderr)
            sys.exit(1)

        self.current_model = None
        self.current_model_type = None

    def load_model(self, model_engine):
        """Ensure only one model occupies VRAM at any time."""
        if self.current_model_type == model_engine and self.current_model is not None:
            return self.current_model

        if self.current_model is not None:
            print(f"[Worker] Unloading {self.current_model_type} from VRAM...", flush=True)
            del self.current_model
            self.current_model = None
            self.current_model_type = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        if FasterQwen3TTS is None:
            raise RuntimeError("faster_qwen3_tts is not installed. Cannot load model.")

        if model_engine not in PINNED_MODELS:
            raise ValueError(f"Unsupported model engine: {model_engine}")

        cfg = PINNED_MODELS[model_engine]
        repo_id = cfg["repo_id"]
        revision = cfg["revision"]

        print(f"[Worker] Resolving pinned model '{model_engine}' ({repo_id}@{revision[:8]})...", flush=True)
        from huggingface_hub import snapshot_download
        local_model_dir = snapshot_download(
            repo_id=repo_id,
            revision=revision,
            local_files_only=False
        )

        print(f"[Worker] Loading model from {local_model_dir} onto {self.device}...", flush=True)
        model = FasterQwen3TTS.from_pretrained(
            str(local_model_dir),
            device=self.device,
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
            max_seq_len=2048,
        )
        model.warmup(50)
        self.current_model = model
        self.current_model_type = model_engine
        print(f"[Worker] Successfully warmed up {model_engine} ({repo_id}@{revision[:8]}).", flush=True)
        return self.current_model

    def load_provenance(self, prov_path):
        """Load existing provenance records to support verified resume."""
        prov_map = {}
        if prov_path.exists():
            with open(prov_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rec = json.loads(line)
                            prov_map[rec["sound_file"]] = rec
                        except Exception:
                            pass
        return prov_map

    def process_batch(self, batch_file, output_dir):
        batch_path = Path(batch_file)
        if not batch_path.exists():
            print(f"[FATAL] Batch file not found: {batch_path}", file=sys.stderr)
            sys.exit(1)

        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        tmp_dir = out_path / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        prov_path = out_path / "generation_provenance.jsonl"
        existing_prov = self.load_provenance(prov_path)
        # Takes the quality gate held in an earlier run of this folder: each retry uses the next seed, and after
        # QC_TRIES holds the line waits for a listen instead of using the GPU every cycle.
        qc_log_path = out_path / QC_LOG
        qc_held = {}
        if qc_log_path.exists():
            for qline in qc_log_path.read_text(encoding="utf-8").splitlines():
                try:
                    qk = json.loads(qline).get("sound_file")
                except ValueError:
                    continue
                if qk:
                    qc_held[qk] = qc_held.get(qk, 0) + 1

        rows = json.loads(batch_path.read_text(encoding="utf-8"))
        other = []
        # Rows of another engine (indextts2) are made by their own worker from the same file (2026-11-02).
        other = [r for r in rows if r.get("model_engine") in ALT_ENGINES]
        if other:
            rows = [r for r in rows if r.get("model_engine") not in ALT_ENGINES]
            print(f"[Worker] WARNING: {len(other)} row(s) of {sorted(ALT_ENGINES)} are NOT made here (their own worker makes "
                  f"them); a caller that does not run it ends with those clips missing.", file=sys.stderr, flush=True)
        print(f"[Worker] Starting batch: {batch_path.name} ({len(rows)} passages)", flush=True)

        # Pre-validate references existence
        missing_refs = set()
        for r in rows:
            ref_clip = r["canonical_reference_clip"]
            if not (self.refs_dir / ref_clip).exists():
                missing_refs.add(ref_clip)

        if missing_refs:
            print(f"[FATAL] Missing {len(missing_refs)} reference files in {self.refs_dir}:", file=sys.stderr)
            for m in sorted(list(missing_refs))[:10]:
                print(f"  * {m}", file=sys.stderr)
            sys.exit(1)

        # Sort/group rows by model engine so we load each model only once
        rows_by_engine = {}
        for r in rows:
            eng = r.get("model_engine", "qwen_06b")
            rows_by_engine.setdefault(eng, []).append(r)

        success_count = 0
        skipped_count = 0
        failed_count = 0
        failed_items = []
        held_count = 0

        total_start = time.time()

        with open(prov_path, "a", encoding="utf-8") as prov_file:
            for engine, engine_rows in rows_by_engine.items():
                # Pre-filter rows that genuinely need generation using full recipe fingerprint
                pending_rows = []
                for r in engine_rows:
                    sf_name = r["sound_file"]
                    final_path = out_path / sf_name
                    expected_fp = compute_recipe_fingerprint(r, self.bitrate)

                    if final_path.exists():
                        if is_valid_output(final_path) and sf_name in existing_prov:
                            rec = existing_prov[sf_name]
                            if rec.get("recipe_fingerprint") == expected_fp:
                                skipped_count += 1
                                continue
                    if qc_held.get(sf_name, 0) >= QC_TRIES:
                        held_count += 1     # held QC_TRIES times: waits in _held_qc/ for a listen
                        continue
                    pending_rows.append(r)

                if not pending_rows:
                    print(f"[{engine}] All {len(engine_rows)} items already generated and verified with matching recipe. Skipping model load.", flush=True)
                    continue

                model = self.load_model(engine)

                for i, row in enumerate(pending_rows, start=1):
                    sound_file = row["sound_file"]
                    final_path = out_path / sound_file
                    text_hash = row["text_hash"]
                    profile_key = row["profile_key"]
                    expected_fp = compute_recipe_fingerprint(row, self.bitrate)

                    # Generation parameters
                    cleaned_text = row["cleaned_text"]
                    ref_clip = row["canonical_reference_clip"]
                    ref_path = self.refs_dir / ref_clip
                    ref_text = row.get("ref_text", "").strip()
                    xvec_mode = False if ref_text else True
                    tuning_mode = row.get("tuning_mode", "tuned")
                    instruct = row.get("instruct", "") if tuning_mode == "tuned" else ""
                    post_af = row.get("post_af", "")
                    temp = float(row["temperature"]) if row.get("temperature") else (0.98 if instruct else 0.95)
                    generation_seed = compute_generation_seed(row) + qc_held.get(sound_file, 0)   # a held take retries on the next seed

                    t0 = time.time()
                    tmp_wav = tmp_dir / f"tmp_{os.getpid()}_{time.time_ns()}.wav"
                    tmp_ogg = tmp_dir / f"tmp_{os.getpid()}_{time.time_ns()}.ogg"

                    try:
                        # Reset every RNG immediately before inference so retries and
                        # resumed runs reproduce the same stochastic sample.
                        random.seed(generation_seed)
                        np.random.seed(generation_seed)
                        torch.manual_seed(generation_seed)
                        if torch.cuda.is_available():
                            torch.cuda.manual_seed_all(generation_seed)

                        # 1. Model inference
                        wavs, sr = model.generate_voice_clone(
                            text=cleaned_text,
                            language=LANGUAGE,
                            ref_audio=str(ref_path),
                            ref_text=ref_text,
                            instruct=instruct,
                            temperature=temp,
                            max_new_tokens=MAX_NEW_TOKENS,
                            xvec_only=xvec_mode,
                            append_silence=APPEND_SILENCE
                        )

                        audio = np.concatenate([np.asarray(w, dtype=np.float32).reshape(-1) for w in wavs])
                        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
                        if peak > 1.0:
                            audio = audio / peak * 0.98

                        sf.write(str(tmp_wav), audio, sr)

                        # 2. Post-processing & FFmpeg validation (Fixed 32 kbps mono 24 kHz)
                        level_info = apply_post_fx_and_encode(tmp_wav, tmp_ogg, post_af, bitrate=self.bitrate,
                                                              shout="!" in (cleaned_text or ""))

                        # Verify encoded audio
                        out_dur, out_sr, out_ch = get_audio_info(tmp_ogg)
                        if out_dur <= 0.05 or out_sr != OUTPUT_SAMPLE_RATE or out_ch != OUTPUT_CHANNELS:
                            raise ValueError(
                                "Encoded audio failed validation "
                                f"(duration={out_dur}s, sample_rate={out_sr}, channels={out_ch})"
                            )

                        # 2b. The quality gate every path shares (review 2026-10-27): a take far longer than its
                        # words (a loop or runaway) or under 0.3 s never takes the clip's name. It waits in
                        # _held_qc/ for a listen and the next run retries it on the next seed.
                        qc_why = qc_clip(out_dur, cleaned_text)
                        if qc_why:
                            held_dir = out_path / QC_HELD_DIR
                            held_dir.mkdir(exist_ok=True)
                            os.replace(str(tmp_ogg), str(held_dir / sound_file))
                            with open(qc_log_path, "a", encoding="utf-8") as qfh:
                                qfh.write(json.dumps({
                                    "sound_file": sound_file, "why": qc_why, "duration_sec": round(out_dur, 3),
                                    "generation_seed": generation_seed, "profile_key": profile_key,
                                    "npc_name": row.get("npc_name", ""), "hostname": self.hostname,
                                    "timestamp": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
                            held_count += 1
                            print(f"[QC] held {sound_file}: {qc_why}", flush=True)
                            continue

                        # 3. Atomic rename to target
                        os.replace(str(tmp_ogg), str(final_path))

                        gen_time = time.time() - t0
                        rtf = gen_time / out_dur if out_dur > 0 else 0.0

                        # 4. Record provenance with cryptographic recipe fingerprint
                        prov_record = {
                            "sound_file": sound_file,
                            "text_hash": text_hash,
                            "recipe_fingerprint": expected_fp,
                            "profile_key": profile_key,
                            "npc_name": row.get("npc_name", ""),
                            "canonical_reference_clip": ref_clip,
                            "ref_text": ref_text,
                            "model_engine": engine,
                            "model_revision": PINNED_MODELS[engine]["revision"],
                            "temperature": temp,
                            "max_new_tokens": MAX_NEW_TOKENS,
                            "generation_seed": generation_seed,
                            "language": LANGUAGE,
                            "xvec_only": xvec_mode,
                            "append_silence": APPEND_SILENCE,
                            "tuning_mode": tuning_mode,
                            "instruct": instruct,
                            "post_af": post_af,
                            "bitrate": self.bitrate,
                            "measured_lufs": (level_info or {}).get("measured_lufs"),
                            "level": (level_info or {}).get("level"),
                            "level_recipe": (level_info or {}).get("level_recipe"),
                            "duration_sec": round(out_dur, 3),
                            "gen_time_sec": round(gen_time, 3),
                            "rtf": round(rtf, 3),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "hostname": self.hostname,
                            "torch_version": self.torch_version,
                            "faster_qwen_version": FASTER_QWEN_VERSION
                        }
                        prov_file.write(json.dumps(prov_record, ensure_ascii=False) + "\n")
                        prov_file.flush()
                        existing_prov[sound_file] = prov_record

                        success_count += 1

                    except Exception as e:
                        failed_count += 1
                        failed_items.append({"sound_file": sound_file, "error": str(e)})
                        print(f"[Error] Failed {sound_file}: {e}", file=sys.stderr)
                        if is_gpu_fatal(e):
                            fatal_path = out_path / f"fatal_{batch_path.stem}.json"
                            fatal_path.write_text(
                                json.dumps(
                                    {
                                        "sound_file": sound_file,
                                        "error": str(e),
                                        "generated_before_failure": success_count,
                                        "skipped_valid": skipped_count,
                                        "hostname": self.hostname,
                                        "timestamp": datetime.now(timezone.utc).isoformat(),
                                    },
                                    indent=2,
                                    ensure_ascii=False,
                                ),
                                encoding="utf-8",
                            )
                            print(
                                "[FATAL] GPU state is unsafe; stopping immediately so the "
                                "shard can be resumed in a fresh process.",
                                file=sys.stderr,
                            )
                            raise
                    finally:
                        if tmp_wav.exists():
                            tmp_wav.unlink()
                        if tmp_ogg.exists():
                            tmp_ogg.unlink()

                    if (success_count + skipped_count) % 25 == 0:
                        elapsed = time.time() - total_start
                        rate = (success_count) / elapsed * 3600 if elapsed > 0 else 0
                        print(f"[{engine}] Progress: {i}/{len(pending_rows)} | Done: {success_count} | Skipped: {skipped_count} | Rate: {rate:.1f} clips/hr", flush=True)

        # Cleanup tmp directory
        shutil.rmtree(tmp_dir, ignore_errors=True)

        total_elapsed = time.time() - total_start
        print(f"\n=======================================================")
        print(f"BATCH EXECUTION SUMMARY: {batch_path.name}")
        print(f"  - Total Items:      {len(rows)}")
        print(f"  - Newly Generated:  {success_count}")
        print(f"  - Skipped (Valid):  {skipped_count}")
        print(f"  - Failed:           {failed_count}")
        print(f"  - Held by QC:       {held_count}" + (f"  (in {QC_HELD_DIR}/, listed in {QC_LOG})" if held_count else ""))
        print(f"  - Total Walltime:   {total_elapsed:.1f}s")
        print(f"=======================================================\n")

        summary = {
            "batch_file": str(batch_path.resolve()),
            "output_dir": str(out_path.resolve()),
            "total_items": len(rows),
            "left_to_other_engine": len(other),
            "newly_generated": success_count,
            "skipped_valid": skipped_count,
            "failed": failed_count,
            "held_qc": held_count,
            "walltime_sec": round(total_elapsed, 3),
            "completed": failed_count == 0 and success_count + skipped_count + held_count == len(rows),
            "hostname": self.hostname,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        summary_path = out_path / f"summary_{batch_path.stem}.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if failed_count > 0:
            failed_log = out_path / f"failed_{batch_path.stem}.json"
            failed_log.write_text(json.dumps(failed_items, indent=2), encoding="utf-8")
            print(f"[FATAL] Batch finished with {failed_count} failures. Details logged to {failed_log}", file=sys.stderr)
            sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Hardened RunPod TTS Fleet Worker")
    parser.add_argument("--batch-file", required=True, help="Path to prepared batch JSON (e.g. prepared_batch_06b.json)")
    parser.add_argument("--output-dir", required=True, help="Directory to save generated audio and provenance")
    parser.add_argument("--references", default=str(DEFAULT_REFS), help="Path to references directory")
    parser.add_argument("--device", default="cuda", help="Device ('cuda' required for production, 'cpu' for tests)")
    parser.add_argument("--bitrate", default="32k", help="Vorbis OGG encoding bitrate (default 32k)")
    args = parser.parse_args()

    if FasterQwen3TTS is None:
        print("[FATAL] faster_qwen3_tts is not installed. Aborting.", file=sys.stderr)
        sys.exit(1)

    worker = HardenedRunpodWorker(args.references, args.device, args.bitrate)
    worker.process_batch(args.batch_file, args.output_dir)

if __name__ == "__main__":
    main()
