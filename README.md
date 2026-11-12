# SpeakStone Worker

A small client that lets the owner's machines and invited helpers make SpeakStone voice lines for the SpeakStone
add-on. It asks the SpeakStone site (https://speakstone.beanw.co.uk) for a job, makes the audio on your GPU,
uploads the finished clips and asks for the next job. The voices are similar to the in-game characters.

Unofficial; not affiliated with Blizzard Entertainment.

All rights reserved until a licence is chosen.

## Requirements

- Python 3.10 or newer (`net_worker.py` uses only the standard library).
- An NVIDIA GPU with about 6 GB of VRAM free.
- IndexTTS-2 weights in a plain folder, and the Python environment IndexTTS-2 runs in
  (`indextts2_worker.py` needs torch, numpy, soundfile and the `indextts` package). Run the worker with that
  environment's Python, or set `INDEXTTS2_PYTHON` to it.
- ffmpeg on your PATH.
- A worker key from the owner. Keys are invite-only for now.

## Setup

```
python net_worker.py init --model-dir D:\models\IndexTTS-2
```

`init` asks for your worker key (typing is hidden) and saves the settings in a per-user config file with
user-only permissions. Optional: `--machine-name`, `--work-dir`, `--api-base`.

Settings can also come from environment variables, which override the file: `SSW_WORKER_KEY`, `SSW_API_BASE`,
`SSW_WORK_DIR`, `SSW_MACHINE_NAME`, `INDEXTTS2_DIR`, `INDEXTTS2_PYTHON`, `SSW_NEED_FREE_MIB`, `SSW_YIELD_LOCK`.

## Sharing the GPU with another program

If something else on your PC uses the GPU and marks it with a lock file, point the worker at it with `"yield_lock"`
in the config file (or `SSW_YIELD_LOCK`). While that lock is held by a running program the worker takes no jobs, and
if it appears mid-job the worker stops, gives the job back and waits.

## Run

```
python net_worker.py run
```

The worker waits until enough GPU memory is free, claims a job, makes the clips, uploads them and repeats.

## Stop

Press Ctrl+C, or create a file named `STOP` in the work folder. The worker finishes cleanly and exits.

## Rented cloud GPUs (container)

For a rented Linux machine with an NVIDIA card, use the container in `docker/`. Everything is put in the image
when it is built, so a new machine pulls the image and starts work: no setup, nothing downloaded at start.

```
docker build -f docker/Dockerfile -t speakstone-worker .
docker run --gpus all -e SSW_WORKER_KEY=sswk_... speakstone-worker
```

- `--build-arg BAKE_MODELS=0` builds a slim image without the model files (about 8 GB smaller). It fetches them
  once into `/data` (or `/workspace/speakstone`); mount a disk that outlives the machine there and later machines
  start without downloading.
- Optional settings: `SSW_MACHINE_NAME`, `SSW_DATA` (where work files and fetched model files go).
- Stopping the container gives the current job back to the queue.

## Model hash

```
python net_worker.py --print-model-hash
```

Prints the fingerprint of your IndexTTS-2 folder. The site only gives jobs to workers whose model matches the
one it expects.

## What it sends and receives

- Receives: a job (lines of text, small voice sample clips, settings) from the site.
- Sends: the finished audio clips, a result list, and basic machine facts (machine name, GPU name and free memory,
  worker version).
- Privacy: the worker key is never logged or printed (only `sswk_` plus the last four characters). The text of a
  line is never logged. Nothing else on your machine is read or uploaded.

Exit codes: 0 stopped, 2 config error, 3 key refused, 4 protocol refused, 5 model folder missing.
