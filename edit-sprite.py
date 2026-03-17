#!/usr/bin/env python3
"""Sprite editor for claude-cat.

Cursor moves in character space. Each cell is a 2x2 subpixel block.
Paint cells with a brush selected from the 16 quadrant block types.

Controls:
  WASD / arrows   Move cursor (character space)
  SPACE            Paint current brush at cursor
  [ / ]            Cycle brush
  R                Toggle entire row (fill/clear)
  X                Clear row
  F                Fill row
  1-7 / TAB        Switch mood
  Y                Copy current mood
  P                Paste copied mood into current
  S                Save to JSON
  Q / ESC          Quit
"""

import copy
import json
import os
import sys
import termios
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from claude_cat.__main__ import to_blocks
from claude_cat.sprites import BUILTIN, REQUIRED_MOODS

MOODS = REQUIRED_MOODS
CSI = "\033["
RST = "\033[0m"

# All 16 quadrant patterns: (TL, TR, BL, BR)
# Index matches BLOCKS lookup: TL*8 + TR*4 + BL*2 + BR
PATTERNS = [
    (0, 0, 0, 0),  # 0:  empty
    (0, 0, 0, 1),  # 1:  ▗
    (0, 0, 1, 0),  # 2:  ▖
    (0, 0, 1, 1),  # 3:  ▄
    (0, 1, 0, 0),  # 4:  ▝
    (0, 1, 0, 1),  # 5:  ▐
    (0, 1, 1, 0),  # 6:  ▞
    (0, 1, 1, 1),  # 7:  ▟
    (1, 0, 0, 0),  # 8:  ▘
    (1, 0, 0, 1),  # 9:  ▚
    (1, 0, 1, 0),  # 10: ▌
    (1, 0, 1, 1),  # 11: ▙
    (1, 1, 0, 0),  # 12: ▀
    (1, 1, 0, 1),  # 13: ▜
    (1, 1, 1, 0),  # 14: ▛
    (1, 1, 1, 1),  # 15: █
]

BLOCKS = " \u2597\u2596\u2584\u259d\u2590\u259e\u259f\u2598\u259a\u258c\u2599\u2580\u259c\u259b\u2588"


def read_key(fd):
    ch = os.read(fd, 1).decode("utf-8", errors="ignore")
    if ch == "\033":
        ch2 = os.read(fd, 1).decode("utf-8", errors="ignore")
        if ch2 == "[":
            ch3 = os.read(fd, 1).decode("utf-8", errors="ignore")
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "Z": "SHIFT_TAB"}.get(ch3, "ESC")
        return "ESC"
    return ch


def get_cell(grid, mood, cx, cy):
    """Read the 4 subpixels at char position (cx, cy) and return brush index."""
    rows = grid[mood]
    px, py = cx * 2, cy * 2
    tl = 1 if rows[py][px] == "#" else 0
    tr = 1 if rows[py][px + 1] == "#" else 0
    bl = 1 if rows[py + 1][px] == "#" else 0
    br = 1 if rows[py + 1][px + 1] == "#" else 0
    return tl * 8 + tr * 4 + bl * 2 + br


def set_cell(grid, mood, cx, cy, brush_idx):
    """Write a brush pattern to the 4 subpixels at char position (cx, cy)."""
    rows = grid[mood]
    px, py = cx * 2, cy * 2
    tl, tr, bl, br = PATTERNS[brush_idx]
    rows[py][px] = "#" if tl else "."
    rows[py][px + 1] = "#" if tr else "."
    rows[py + 1][px] = "#" if bl else "."
    rows[py + 1][px + 1] = "#" if br else "."


def render_cat_char(ch):
    """Render a single cat character with inverse video for full blocks."""
    if ch == "\u2588":
        return CSI + "7m " + RST
    elif ch == " ":
        return " "
    else:
        return CSI + "1m" + ch + RST


def render(grid, mood_idx, cx, cy, char_w, char_h, brush_idx, saved=False, clipboard=False):
    mood = MOODS[mood_idx]
    preview = to_blocks(["".join(r) for r in grid[mood]])

    out = CSI + "H"

    # Mood tabs
    for i, m in enumerate(MOODS):
        if i == mood_idx:
            out += CSI + "7m " + m + " " + RST + " "
        else:
            out += CSI + "2m " + m + " " + RST + " "
    out += CSI + "K\n\n"

    # Grid (character space) + preview side by side
    for y in range(char_h):
        for x in range(char_w):
            cell_idx = get_cell(grid, mood, x, y)
            ch = BLOCKS[cell_idx]
            at_cursor = x == cx and y == cy

            if at_cursor:
                # Red background cursor
                if ch == "\u2588" or ch == " ":
                    out += CSI + "48;5;196m" + CSI + "7m " + RST
                else:
                    out += CSI + "48;5;196m" + CSI + "1;30m" + ch + RST
            else:
                out += render_cat_char(ch)

        # Preview on right
        if y < len(preview):
            out += "    "
            pline = preview[y]
            pi = 0
            while pi < len(pline):
                out += render_cat_char(pline[pi])
                pi += 1

        out += CSI + "K\n"

    # Brush palette
    out += CSI + "K\n"
    out += "brush: "
    for i in range(16):
        if i == brush_idx:
            out += CSI + "7;33m"
        if i == 15:
            out += CSI + "7m " + RST
        elif i == 0:
            out += CSI + "2m\u00b7" + RST
        else:
            out += BLOCKS[i]
        if i == brush_idx:
            out += RST
        out += " "
    out += CSI + "K\n"

    # Status
    out += CSI + "2m"
    out += "wasd:move  space:paint  []:brush  R:row  X:clear  F:fill  tab:mood  Y/P:copy  S:save  Q:quit"
    out += RST + CSI + "K\n"
    out += "char (%d, %d)  brush: %s" % (cx, cy, BLOCKS[brush_idx] if brush_idx > 0 else "empty")
    if clipboard:
        out += "  " + CSI + "33m[clipboard]" + RST
    if saved:
        out += "  " + CSI + "32m[saved]" + RST
    out += CSI + "K\n" + CSI + "J"

    sys.stdout.write(out)
    sys.stdout.flush()


def save(grid, path):
    moods = {}
    for mood in MOODS:
        moods[mood] = ["".join(row) for row in grid[mood]]

    data = {
        "name": os.path.splitext(os.path.basename(path))[0],
        "author": "",
        "description": "",
        "width": len(grid[MOODS[0]][0]),
        "height": len(grid[MOODS[0]]),
        "moods": moods,
    }

    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
            for key in ("name", "author", "description"):
                if key in existing:
                    data[key] = existing[key]
        except Exception:
            pass

    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "src/claude_cat/sprites/default.json"

    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        sprites = data.get("moods", data)
    else:
        sprites = copy.deepcopy(BUILTIN)

    grid = {}
    for mood in MOODS:
        grid[mood] = [list(row) for row in sprites[mood]]

    sub_h = len(grid[MOODS[0]])
    sub_w = len(grid[MOODS[0]][0])
    char_h = sub_h // 2
    char_w = sub_w // 2

    mood_idx = 0
    cx, cy = 0, 0
    brush_idx = 15  # default: full block
    clipboard = None
    just_saved = False

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    try:
        tty.setcbreak(fd)
        sys.stdout.write(CSI + "2J" + CSI + "?25l")

        while True:
            render(grid, mood_idx, cx, cy, char_w, char_h, brush_idx,
                   saved=just_saved, clipboard=clipboard is not None)
            just_saved = False

            key = read_key(fd)

            if key in ("q", "Q", "ESC"):
                break
            elif key in ("w", "UP"):
                cy = max(0, cy - 1)
            elif key in ("s", "DOWN"):
                cy = min(char_h - 1, cy + 1)
            elif key in ("a", "A", "LEFT"):
                cx = max(0, cx - 1)
            elif key in ("d", "D", "RIGHT"):
                cx = min(char_w - 1, cx + 1)
            elif key == " ":
                set_cell(grid, MOODS[mood_idx], cx, cy, brush_idx)
            elif key == "[":
                brush_idx = (brush_idx - 1) % 16
            elif key == "]":
                brush_idx = (brush_idx + 1) % 16
            elif key == "\t":
                mood_idx = (mood_idx + 1) % len(MOODS)
            elif key == "SHIFT_TAB":
                mood_idx = (mood_idx - 1) % len(MOODS)
            elif key in "1234567":
                mood_idx = int(key) - 1
            elif key in ("r", "R"):
                mood = MOODS[mood_idx]
                # Toggle row: if mostly filled, clear it; else fill it
                row_filled = sum(1 for x in range(char_w) if get_cell(grid, mood, x, cy) == 15)
                fill = 0 if row_filled > char_w // 2 else 15
                for x in range(char_w):
                    set_cell(grid, mood, x, cy, fill)
            elif key in ("x",):
                for x in range(char_w):
                    set_cell(grid, MOODS[mood_idx], x, cy, 0)
            elif key in ("f",):
                for x in range(char_w):
                    set_cell(grid, MOODS[mood_idx], x, cy, 15)
            elif key in ("Y", "y"):
                clipboard = copy.deepcopy(grid[MOODS[mood_idx]])
            elif key in ("P", "p"):
                if clipboard:
                    grid[MOODS[mood_idx]] = copy.deepcopy(clipboard)
            elif key == "S":
                save(grid, path)
                just_saved = True
            elif key == "\x13":
                save(grid, path)
                just_saved = True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(CSI + "?25h\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
