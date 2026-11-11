"""When to run. `decide` is a pure function; the readers below it are the only Windows-specific part."""
import ctypes
import sys

LEAVE_SECONDS = 15      # input younger than this counts as "the user is back"


def decide(mode, running, idle_seconds, idle_minutes, fullscreen, on_battery, paused,
           block_fullscreen=True, block_on_battery=True):
    """Return 'start', 'stop' or 'keep'.

    mode 'off'  : never run (stop if running).
    mode 'now'  : run unless paused (the user asked for it, so fullscreen/battery are the user's call).
    mode 'idle' : start after idle_minutes without input; stop as soon as input returns, a full-screen app takes
                  over, the PC goes on battery, or the user paused.
    """
    if mode == "off" or paused:
        return "stop" if running else "keep"
    if mode == "now":
        return "keep" if running else "start"
    blocked = (block_fullscreen and fullscreen) or (block_on_battery and on_battery)
    if running:
        return "stop" if (blocked or idle_seconds < LEAVE_SECONDS) else "keep"
    if not blocked and idle_seconds >= idle_minutes * 60:
        return "start"
    return "keep"


def state_dot(paused, running, making, error=False):
    if error:
        return "error"
    if paused:
        return "paused"
    return "making" if (running and making) else "idle"


# ---------------------------------------------------------------- Windows readers (best effort, never raise)
def idle_seconds():
    if sys.platform != "win32":
        return 0.0
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
        ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


def fullscreen_busy():
    """True for a full-screen game or presentation (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)."""
    if sys.platform != "win32":
        return False
    try:
        state = ctypes.c_int(0)
        ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        return state.value in (2, 3, 4)
    except Exception:
        return False


def on_battery():
    if sys.platform != "win32":
        return False
    try:
        class SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        s = SPS()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
        return s.ACLineStatus == 0
    except Exception:
        return False
