"""Shared state between the background loop and the tray icon: what the user asked for, and what the loop is doing.
Plain attributes behind a lock; the loop reads the requests, the tray reads the status."""
import threading

WORKING, WAITING_WORK, PAUSED, WAITING_IDLE, UPDATING, STOPPED = (
    "Working", "Waiting for work", "Paused", "Waiting for the PC to be idle", "Updating", "Stopped")
PAUSING, UPDATING_AFTER = "Pausing after this job", "Updating after this job"


class Control:
    def __init__(self):
        self.lock = threading.Lock()
        self.paused = False
        self.quit = False
        self.check_now = False           # "Check for updates now": look straight away and install even if auto is off
        self.status = WAITING_IDLE
        self.update_version = None       # a newer version that is known but not installed yet
        self.drain_reason = None         # None, "pause" or "update": why the worker was asked to finish its job and leave
        self.drain_since = 0.0

    def set_paused(self, value):
        with self.lock:
            self.paused = bool(value)

    def toggle_pause(self):
        with self.lock:
            self.paused = not self.paused
            return self.paused

    def request_quit(self):
        with self.lock:
            self.quit = True

    def request_check(self):
        with self.lock:
            self.check_now = True

    def take_check(self):
        with self.lock:
            v, self.check_now = self.check_now, False
            return v


def status_for(running, making, paused, updating=False, drain=None):
    """The one-line status the tray shows. Pure. `drain` is control.drain_reason."""
    if updating:
        return UPDATING
    if drain == "update" and running:
        return UPDATING_AFTER
    if paused:
        return PAUSING if running else PAUSED
    if running:
        return WORKING if making else WAITING_WORK
    return WAITING_IDLE
