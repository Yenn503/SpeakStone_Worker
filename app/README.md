# SpeakStone Worker app (Windows 11, NVIDIA)

One program that sets up and runs the SpeakStone worker for you. Unofficial; not affiliated with Blizzard Entertainment.

## For a home PC

1. Download `SpeakStoneWorker.exe` (from the releases page, or from the owner) and double-click it.
2. Pick an install folder (about 20 GB free) and paste your worker key (typing is hidden). Press Install.
   It checks your graphics card (8 GB or more), then downloads everything it needs (about 6 GB). This takes a while;
   if it stops, run the program again and it carries on where it left off.
3. Choose how it runs in the stats window:
   - **Run now**: starts at once and shows live figures (voice lines made, speed, queue, time left, card temperature).
   - **Run when idle**: starts after the PC has had no mouse or keyboard use for N minutes (you set N), never during a
     full-screen game or on battery, and stops the moment you come back (the current job is handed back, nothing is lost).
     Choosing it adds a per-user log-on task so the tray icon is there after a restart; choosing another mode removes it.
4. The tray icon (bottom right) has: Open stats, Start now, Pause 15 / 30 / 60 / 120 minutes, Quit.
   Dot colours: grey waiting, green making, amber paused.

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

Result: `app\dist\SpeakStoneWorker.exe` (about 18 MB; the three worker scripts are inside it and copied to the install
folder by setup). Tests need no GPU or network.

## Updates

Off by default (`update_check` in `%APPDATA%\SpeakStoneWorkerApp\app.json`). When on, the app checks the latest release of
this repo; a release needs `SpeakStoneWorker.exe` and `SpeakStoneWorker.exe.sha256` (the SHA-256 in hex). A download is
kept only if the hash matches. See `ssapp/updater.py`.
