# PyInstaller spec (console app: the command-line menu). Build: see app/README.md. Bundles the three worker scripts from the repo root under worker/.
from pathlib import Path
root = Path(SPECPATH).parent
a = Analysis(['run_app.py'], pathex=[SPECPATH],
             datas=[(str(root / n), 'worker') for n in ('net_worker.py', 'indextts2_worker.py', 'runpod_worker.py')],
             hiddenimports=['pystray._win32', 'ssapp.main_gui'], excludes=['numpy', 'torch', 'matplotlib', 'scipy'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='SpeakStoneWorker', console=True, upx=False)
