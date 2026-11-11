"""Tray icon (pystray + Pillow). Menu actions are handed to the tk thread through `post`."""
from PIL import Image, ImageDraw

from . import APP_NAME, theme


def make_icon(state):
    """Parchment disc with a state dot in the corner."""
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((4, 4, 60, 60), fill=theme.PANEL, outline=theme.EDGE, width=3)
    d.text((22, 20), "S", fill=theme.TITLE)
    d.ellipse((38, 38, 62, 62), fill=theme.DOT[state], outline=theme.EDGE, width=2)
    return im


class Tray:
    def __init__(self, supervisor, post, open_stats, quit_app):
        import pystray
        self.pystray = pystray
        self.sup, self.post = supervisor, post
        self.state = "idle"
        item, menu = pystray.MenuItem, pystray.Menu
        pause_menu = menu(*[item(f"Pause {m} minutes", self._pause(m)) for m in (15, 30, 60, 120)],
                          item("Resume", lambda: self.post(self.sup.s.resume)))
        self.icon = pystray.Icon("speakstone_worker", make_icon("idle"), APP_NAME, menu(
            item("Open stats", lambda: self.post(open_stats), default=True),
            item("Start now", lambda: self.post(self.sup.start_now)),
            item("Pause", pause_menu),
            item("Quit", lambda: self.post(quit_app))))

    def _pause(self, minutes):
        return lambda: self.post(lambda: self.sup.pause(minutes))

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.icon.icon = make_icon(state)
            self.icon.title = f"{APP_NAME}: {state}"

    def run(self):
        self.icon.run_detached()

    def stop(self):
        self.icon.stop()
