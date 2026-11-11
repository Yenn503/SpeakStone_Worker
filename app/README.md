# SpeakStone Worker app (Windows 11, NVIDIA)

One program that sets up and runs the SpeakStone worker for you. Unofficial; not affiliated with Blizzard Entertainment.

## For a home PC (v0.2.0, command-line app)

1. Double-click `SpeakStoneWorker.exe`. A console opens with the SpeakStone banner and a menu.
2. First run only: it checks your graphics card (8 GB or more), installs everything it needs (about 6 GB; torch pinned
   to 2.8.0 for CUDA 12.8) with a progress display, then **joins the network by itself** (`POST /api/worker/enroll`):
   there is no key to type or paste. If it stops, run it again and it carries on.
3. Menu: **[1] Start now** (live stats board: lines, words, audio, queue and time left, GPU temperature and load bars,
   speed sparkline, recent events, what it is making; P pause, S or Q stop and hand the job back), **[2] Install as a
   service** (per-user log-on task plus a tray icon; makes voice lines only when the PC is idle, never during a
   full-screen game or on battery, stops the moment you are back), **[3] Settings**, **[Q] Quit**.
4. The tray icon has: Open board (a read-only live board in its own window), Start now, Pause, Quit.
   `SpeakStoneWorker.exe --gui` still starts the older windowed app (kept for a later option).

## Why a log-on task and not a service

A Windows service cannot see keyboard or mouse use, full-screen games or the tray, and needs administrator rights. The
tray app does the watching and stops the worker by its STOP file, which gives the job back. Windows only has to start
the tray app at log-on, which a per-user Scheduled Task does with no administrator rights (`ssapp/autostart.py`).

## Build

```
cd app
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q tests
.venv\Scripts\python -m PyInstaller --noconfirm --clean SpeakStoneWorker.spec
```

Result: `app\dist\SpeakStoneWorker.exe` (console app, about 21 MB; the three worker scripts are inside it and copied to the install
folder by setup). Tests need no GPU or network.

## Updates

Off by default (`update_check` in `%APPDATA%\SpeakStoneWorkerApp\app.json`). When on, the app checks the latest release of
this repo; a release needs `SpeakStoneWorker.exe` and `SpeakStoneWorker.exe.sha256` (the SHA-256 in hex). A download is
kept only if the hash matches. See `ssapp/updater.py`.
