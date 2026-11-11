"""Parchment colours, taken from the add-on's Theme.lua (parchment.ui), as tk hex strings."""


def _hex(r, g, b):
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


PANEL = _hex(0.80, 0.69, 0.50)
INSET = _hex(0.88, 0.80, 0.62)
EDGE = _hex(0.42, 0.27, 0.13)
TITLE = _hex(0.27, 0.13, 0.05)
TEXT = _hex(0.20, 0.12, 0.06)
DIM = _hex(0.40, 0.28, 0.17)
ACCENT = _hex(0.50, 0.22, 0.10)
BTN_BG = _hex(0.42, 0.26, 0.14)
BTN_HOVER = _hex(0.52, 0.32, 0.17)
BTN_TEXT = _hex(0.97, 0.90, 0.72)
BTN_OFF = _hex(0.68, 0.58, 0.42)
BOX_BG = _hex(0.90, 0.82, 0.64)
# state dot colours: idle / making / paused / error
DOT = {"idle": "#8a7a5c", "making": "#3f7a2f", "paused": "#b8860b", "error": "#9b2d1f"}
FONT = "Georgia"          # serif, close to the add-on's parchment look; tk falls back if missing
FONT_UI = "Segoe UI"
