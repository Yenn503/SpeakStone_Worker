# PyInstaller spec. Build (from cli/): .venv\Scripts\pyinstaller SpeakStoneWorker.spec
# One-file console exe; the three worker scripts are bundled under worker/.
from pathlib import Path
root = Path(SPECPATH).parent
a = Analysis(['run_cli.py'], pathex=[SPECPATH],
             datas=[(str(root / n), 'worker') for n in ('net_worker.py', 'indextts2_worker.py', 'runpod_worker.py')],
             excludes=['numpy', 'torch', 'matplotlib', 'scipy', 'tkinter'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='SpeakStoneWorker', console=True, upx=False)
