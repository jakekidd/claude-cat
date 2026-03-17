#!/usr/bin/env python3
"""Sprite editor for claude-cat.

Cursor moves in character space. Each cell stores one hex char
from the extended hex palette (0-F + I). Not true hex -- 17
symbols total.

Controls:
  WASD / arrows   Move cursor
  SPACE            Paint brush (toggle: same char clears to empty)
  [ / ]            Cycle brush
  R                Toggle row (fill/clear)
  X                Clear row
  F                Fill row with current brush
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
from claude_cat.__main__ import render_hex_line
from claude_cat.sprites import load, BLOCKS, REQUIRED_MOODS, _convert_legacy

MOODS = REQUIRED_MOODS
CSI = "\033["
RST = "\033[0m"

# 17 brush options: 0-F + I
BRUSHES = "0123456789ABCDEFI"

MAX_W = 18  # max char width
MAX_H = 12  # max char height


def read_key(fd):
    ch = os.read(fd, 1).decode("utf-8", errors="ignore")
    if ch == "\033":
        ch2 = os.read(fd, 1).decode("utf-8", errors="ignore")
        if ch2 == "[":
            ch3 = os.read(fd, 1).decode("utf-8", errors="ignore")
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "Z": "SHIFT_TAB"}.get(ch3, "ESC")
        return "ESC"
    return ch


def pad_grid(rows, target_w, target_h):
    cur_h = len(rows)
    cur_w = len(rows[0]) if rows else 0
    pad_l = (target_w - cur_w) // 2
    pad_r = target_w - cur_w - pad_l
    padded = [list("0" * pad_l + r + "0" * pad_r) for r in rows]
    pad_t = (target_h - cur_h) // 2
    pad_b = target_h - cur_h - pad_t
    empty = ["0"] * target_w
    return [list(empty) for _ in range(pad_t)] + padded + [list(empty) for _ in range(pad_b)]


def trim_grid(rows):
    if not rows:
        return rows
    h = len(rows)
    w = len(rows[0])
    top, bot, left, right = h, -1, w, -1
    for y in range(h):
        for x in range(w):
            if rows[y][x] != "0":
                top = min(top, y)
                bot = max(bot, y)
                left = min(left, x)
                right = max(right, x)
    if bot < 0:
        return [["0", "0"]]
    return [row[left:right + 1] for row in rows[top:bot + 1]]


def brush_display(ch):
    ch = ch.upper()
    if ch == "0":
        return CSI + "2m\u00b7" + RST
    elif ch == "I":
        return CSI + "7m " + RST
    elif ch == "F":
        return CSI + "1m\u2588" + RST
    else:
        idx = int(ch, 16)
        return CSI + "1m" + BLOCKS[idx] + RST


def render(grid, mood_idx, cx, cy, char_w, char_h, brush_idx, saved=False, clipboard=False):
    mood = MOODS[mood_idx]
    rows = grid[mood]
    preview_rows = ["".join(r) for r in rows]

    out = CSI + "H"

    # Mood tabs
    for i, m in enumerate(MOODS):
        if i == mood_idx:
            out += CSI + "7m " + m + " " + RST + " "
        else:
            out += CSI + "2m " + m + " " + RST + " "
    out += CSI + "K\n\n"

    # Grid + preview side by side
    for y in range(char_h):
        for x in range(char_w):
            ch = rows[y][x].upper()
            at_cursor = x == cx and y == cy

            if at_cursor:
                if ch == "0":
                    out += CSI + "48;5;23m " + RST
                elif ch == "I":
                    out += CSI + "48;5;37m " + RST
                elif ch == "F":
                    out += CSI + "48;5;37m" + CSI + "30m\u2588" + RST
                else:
                    idx = int(ch, 16)
                    out += CSI + "48;5;37m" + CSI + "30m" + BLOCKS[idx] + RST
            else:
                if ch == "I":
                    out += CSI + "7m " + RST
                elif ch == "0":
                    cell_shade = (x + y) % 2
                    out += (CSI + "90m\u00b7" + RST) if cell_shade else " "
                elif ch == "F":
                    out += CSI + "1m\u2588" + RST
                else:
                    idx = int(ch, 16)
                    out += CSI + "1m" + BLOCKS[idx] + RST

        # Preview
        if y < len(preview_rows):
            out += "    " + render_hex_line(preview_rows[y])

        out += CSI + "K\n"

    # Brush palette
    out += CSI + "K\n"
    out += "brush: "
    for i, b in enumerate(BRUSHES):
        if i == brush_idx:
            out += CSI + "43m"
        out += brush_display(b)
        if i == brush_idx:
            out += RST
        out += " "
    out += CSI + "K\n"

    # Status
    out += CSI + "2m"
    out += "wasd:move  space:paint  []:brush  R:row  X:clear  F:fill  tab:mood  Y/P:copy  S:save  Q:quit"
    out += RST + CSI + "K\n"
    out += "char (%d, %d)  brush: %s" % (cx, cy, BRUSHES[brush_idx])
    if clipboard:
        out += "  " + CSI + "33m[clipboard]" + RST
    if saved:
        out += "  " + CSI + "32m[saved]" + RST
    out += CSI + "K\n" + CSI + "J"

    sys.stdout.write(out)
    sys.stdout.flush()


def save(grid, path):
    trimmed = {}
    for mood in MOODS:
        trimmed[mood] = ["".join(row) for row in trim_grid(grid[mood])]

    max_w = max(len(trimmed[m][0]) for m in MOODS)
    max_h = max(len(trimmed[m]) for m in MOODS)

    moods = {}
    for mood in MOODS:
        rows = list(trimmed[mood])
        padded = pad_grid(rows, max_w, max_h)
        moods[mood] = ["".join(row) for row in padded]

    data = {
        "name": os.path.splitext(os.path.basename(path))[0],
        "author": "",
        "description": "",
        "format": "hex",
        "width": max_w,
        "height": max_h,
        "moods": moods,
    }

    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
            for key in ("name", "author", "description", "eyes"):
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
        raw_moods = data.get("moods", data)

        # Auto-detect legacy format
        if data.get("format") != "hex":
            sample = list(raw_moods.values())[0][0]
            if set(sample) <= {"#", "."}:
                converted = {}
                for mood, rows in raw_moods.items():
                    converted[mood] = _convert_legacy(rows)
                raw_moods = converted
    else:
        raw_moods = {m: ["0" * 14] * 7 for m in MOODS}

    # Pad all moods to max grid size
    grid = {}
    for mood in MOODS:
        grid[mood] = pad_grid(raw_moods[mood], MAX_W, MAX_H)

    char_h = MAX_H
    char_w = MAX_W

    mood_idx = 0
    cx, cy = 0, 0
    brush_idx = len(BRUSHES) - 1  # default: I (inverse video)
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
                mood = MOODS[mood_idx]
                current = grid[mood][cy][cx].upper()
                brush = BRUSHES[brush_idx]
                if current == brush:
                    grid[mood][cy][cx] = "0"  # toggle off
                else:
                    grid[mood][cy][cx] = brush
            elif key == "[":
                brush_idx = (brush_idx - 1) % len(BRUSHES)
            elif key == "]":
                brush_idx = (brush_idx + 1) % len(BRUSHES)
            elif key == "\t":
                mood_idx = (mood_idx + 1) % len(MOODS)
            elif key == "SHIFT_TAB":
                mood_idx = (mood_idx - 1) % len(MOODS)
            elif key in "1234567":
                mood_idx = int(key) - 1
            elif key in ("r", "R"):
                mood = MOODS[mood_idx]
                row = grid[mood][cy]
                filled = sum(1 for c in row if c != "0")
                fill = "0" if filled > len(row) // 2 else BRUSHES[brush_idx]
                grid[mood][cy] = [fill] * char_w
            elif key in ("x",):
                grid[MOODS[mood_idx]][cy] = ["0"] * char_w
            elif key == "f":
                grid[MOODS[mood_idx]][cy] = [BRUSHES[brush_idx]] * char_w
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
