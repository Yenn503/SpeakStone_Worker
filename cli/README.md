# SpeakStone Worker (command line)

Run: `SpeakStoneWorker.exe` (Windows) or `./run.sh` / `python -m speakstone_worker` (Linux, `pip install rich`).
Menu: [1] Start now, [2] Install as a service (runs when your PC is idle), [3] Settings, [Q] Quit.
First run sets itself up (about 20 GB free, 8 GB graphics card), joins the queue automatically and needs no key.
Everything lives in `%LOCALAPPDATA%\SpeakStoneWorker` (Linux: `~/.local/share/speakstone-worker`).
`SPEAKSTONE_API_BASE` overrides the site URL (testing). Tests: `pytest cli/tests`. Build: `pyinstaller SpeakStoneWorker.spec`.
Unofficial; not affiliated with Blizzard Entertainment.
