"""One `tick()` every few seconds: read the PC's state, ask idle.decide what to do, act through the controller."""
import time

from . import idle


class Supervisor:
    def __init__(self, settings, worker, readers=None, clock=time.time, stats_reader=None):
        self.s = settings
        self.w = worker
        self.clock = clock
        r = readers or {}
        self.idle_seconds = r.get("idle_seconds", idle.idle_seconds)
        self.fullscreen = r.get("fullscreen", idle.fullscreen_busy)
        self.battery = r.get("battery", idle.on_battery)
        self.stats_reader = stats_reader          # () -> {"making": bool, ...} from the worker log (optional)
        self.stopping = False
        self.last_action = "keep"

    def tick(self):
        paused = self.s.paused(self.clock())
        action = idle.decide(self.s["mode"], self.w.running, self.idle_seconds(), self.s["idle_minutes"],
                             self.fullscreen(), self.battery(), paused,
                             self.s["block_fullscreen"], self.s["block_on_battery"])
        if action == "start":
            self.stopping = False
            self.w.start()
        elif action == "stop":
            self.stopping = True
            self.w.request_stop()
        if self.stopping and self.w.poll_stop():
            self.stopping = False
        self.last_action = action
        return self.dot()

    def dot(self):
        making = bool(self.stats_reader and self.stats_reader().get("making"))
        return idle.state_dot(self.s.paused(self.clock()), self.w.running, making)

    # ---- tray / window commands
    def pause(self, minutes):
        self.s.pause_for(minutes, self.clock())
        self.w.request_stop()
        self.stopping = True

    def start_now(self):
        self.s.resume()
        self.s["mode"] = "now"
        self.stopping = False
        self.w.start()

    def set_mode(self, mode):
        self.s["mode"] = mode
        if mode == "off":
            self.w.request_stop()
            self.stopping = True
