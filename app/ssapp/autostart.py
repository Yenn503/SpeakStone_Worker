"""'Run when idle' registration: a per-user Scheduled Task that starts the tray app at log-on.

Why not a Windows service, and why not the Task Scheduler's own idle trigger?
  * A service runs in session 0 with no desktop: it cannot read keyboard/mouse idle time, see a full-screen game, show
    a tray icon, and needs administrator rights to install.
  * The Task Scheduler idle trigger fires once and its stop-on-return rules cannot see full-screen apps or hand the job
    back (it kills the process).
  The tray app does the watching itself (idle.decide), starts the worker after N idle minutes and stops it by the
  worker's STOP file, so the job is given back. The only thing the OS must do is start the tray app at log-on, which a
  per-user task does with no administrator rights and no service. `build_task_xml` is pure; `register`/`unregister`
  call schtasks and are never run by tests.
"""
import subprocess
import sys
from xml.sax.saxutils import escape

TASK_NAME = "SpeakStone Worker"


def build_task_xml(exe_path, args="--tray"):
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts the SpeakStone Worker tray app when you sign in.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(str(exe_path))}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>
"""


def register(exe_path, xml_path, runner=subprocess.run):
    from pathlib import Path
    Path(xml_path).write_text(build_task_xml(exe_path), encoding="utf-16")
    return runner(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
                  capture_output=True, text=True).returncode == 0


def unregister(runner=subprocess.run):
    return runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True).returncode == 0


def exe_path():
    return sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
