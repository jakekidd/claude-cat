#!/usr/bin/env python3
"""Unified sprite editor for claude-cat.

Edit states (animated, multi-frame) and reactions (static, single frame).
Replaces the old mood editor and eye editor.

Controls:
  -- Navigation --
  TAB / SHIFT+TAB   Next/prev state or reaction
  < / >             Prev/next frame within current state
  ENTER             Add new frame after current (duplicates it)
  BACKSPACE         Delete current frame

  -- Drawing --
  WASD / arrows     Move cursor
  SPACE             Paint brush (toggle: same = clear)
  [ / ]             Cycle brush
  R                 Toggle row fill/clear
  X                 Clear row
  F                 Fill row with brush

  -- Playback --
  P                 Play/pause animation preview
  + / -             Adjust ms timing (50ms steps)

  -- Clipboard --
  Y                 Copy current frame
  V                 Paste into current frame

  -- File --
  S                 Save
  Q / ESC           Quit
"""

import copy
import fcntl
import json
import os
import sys
import termios
import time
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from claude_cat.__main__ import render_hex_line
from claude_cat.sprites import BLOCKS

CSI = "\033["
RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BRUSHES = "0123456789ABCDEFI"

MAX_W = 18
MAX_H = 12


def read_key(fd):
    ch = os.read(fd, 1).decode("utf-8", errors="ignore")
    if ch == "\033":
        ch2 = os.read(fd, 1).decode("utf-8", errors="ignore")
        if ch2 == "[":
            ch3 = os.read(fd, 1).decode("utf-8", errors="ignore")
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "Z": "SHIFT_TAB"}.get(ch3, "ESC")
        return "ESC"
    if ch in ("\r", "\n"):
        return "ENTER"
    if ch == "\x7f":
        return "BACKSPACE"
    return ch


def pad_frame(rows, w, h):
    """Center a frame in a w x h grid."""
    cur_h = len(rows)
    cur_w = len(rows[0]) if rows else 0
    pl = (w - cur_w) // 2
    pr = w - cur_w - pl
    padded = [list("0" * pl + r + "0" * pr) for r in rows]
    pt = (h - cur_h) // 2
    pb = h - cur_h - pt
    empty = ["0"] * w
    return [list(empty) for _ in range(pt)] + padded + [list(empty) for _ in range(pb)]


def trim_frame(rows):
    """Remove empty border from a frame."""
    if not rows:
        return ["00"]
    h, w = len(rows), len(rows[0])
    top, bot, left, right = h, -1, w, -1
    for y in range(h):
        for x in range(w):
            if rows[y][x] != "0":
                top = min(top, y)
                bot = max(bot, y)
                left = min(left, x)
                right = max(right, x)
    if bot < 0:
        return ["00"]
    return ["".join(row[left:right + 1]) for row in rows[top:bot + 1]]


def brush_char(ch):
    ch = ch.upper()
    if ch == "0":
        return DIM + "\u00b7" + RST
    elif ch == "I":
        return CSI + "7m " + RST
    elif ch == "F":
        return BOLD + "\u2588" + RST
    else:
        return BOLD + BLOCKS[int(ch, 16)] + RST


class Editor:
    def __init__(self, path):
        self.path = path
        with open(path) as f:
            self.data = json.load(f)

        # Build item list: states first, then reactions
        self.items = []  # (name, kind) tuples
        for name in self.data.get("states", {}):
            self.items.append((name, "state"))
        for name in self.data.get("reactions", {}):
            self.items.append((name, "reaction"))

        # Pad all frames to MAX grid
        self.grids = {}  # (name, kind, frame_idx) -> 2D list
        for name, kind in self.items:
            if kind == "state":
                cfg = self.data["states"][name]
                for i, frame in enumerate(cfg.get("frames", [])):
                    self.grids[(name, "frame", i)] = pad_frame(frame, MAX_W, MAX_H)
                if "blink" in cfg:
                    self.grids[(name, "blink", 0)] = pad_frame(cfg["blink"], MAX_W, MAX_H)
            else:
                cfg = self.data["reactions"][name]
                self.grids[(name, "reaction", 0)] = pad_frame(cfg.get("frame", []), MAX_W, MAX_H)

        self.item_idx = 0
        self.frame_idx = 0
        self.cx = 0
        self.cy = 0
        self.brush_idx = len(BRUSHES) - 1  # default: I
        self.clipboard = None
        self.playing = False
        self.play_frame = 0
        self.play_time = 0.0
        self.saved = False

    def _current_name(self):
        return self.items[self.item_idx][0]

    def _current_kind(self):
        return self.items[self.item_idx][1]

    def _frame_count(self):
        name = self._current_name()
        kind = self._current_kind()
        if kind == "reaction":
            return 1
        cfg = self.data["states"][name]
        n = len(cfg.get("frames", []))
        if "blink" in cfg:
            n += 1  # blink is the last "frame"
        return n

    def _frame_label(self, idx):
        name = self._current_name()
        kind = self._current_kind()
        if kind == "reaction":
            return "hold"
        cfg = self.data["states"][name]
        n_frames = len(cfg.get("frames", []))
        labels = cfg.get("labels", [])
        if idx < n_frames:
            if idx < len(labels):
                return labels[idx]
            return str(idx)
        if idx == n_frames and "blink" in cfg:
            return "blink"
        return str(idx)

    def _grid_key(self):
        name = self._current_name()
        kind = self._current_kind()
        if kind == "reaction":
            return (name, "reaction", 0)
        cfg = self.data["states"][name]
        n_frames = len(cfg.get("frames", []))
        if self.frame_idx < n_frames:
            return (name, "frame", self.frame_idx)
        return (name, "blink", 0)

    def _current_grid(self):
        return self.grids.get(self._grid_key())

    def _ms(self):
        name = self._current_name()
        kind = self._current_kind()
        if kind == "reaction":
            return int(self.data["reactions"][name].get("hold", 4.0) * 1000)
        return self.data["states"][name].get("ms", 1000)

    def _set_ms(self, ms):
        name = self._current_name()
        kind = self._current_kind()
        if kind == "reaction":
            self.data["reactions"][name]["hold"] = max(0.5, ms / 1000.0)
        else:
            self.data["states"][name]["ms"] = max(50, ms)

    def render(self):
        grid = self._current_grid()
        if not grid:
            return
        name = self._current_name()
        kind = self._current_kind()
        n_frames = self._frame_count()
        ms = self._ms()

        # For playback, show the play frame
        if self.playing and kind == "state":
            cfg = self.data["states"][name]
            all_frames = list(cfg.get("frames", []))
            if all_frames:
                pf = self.play_frame % len(all_frames)
                play_key = (name, "frame", pf)
                play_grid = self.grids.get(play_key, grid)
            else:
                play_grid = grid
        else:
            play_grid = grid

        out = CSI + "H" + CSI + "?25l"

        # Item tabs
        for i, (n, k) in enumerate(self.items):
            tag = n if k == "state" else "!" + n
            if i == self.item_idx:
                out += CSI + "7m " + tag + " " + RST + " "
            else:
                out += DIM + " " + tag + " " + RST + " "
        out += CSI + "K\n"

        # Frame pips with labels
        out += "  "
        show_idx = self.play_frame % max(1, len(self.data["states"].get(name, {}).get("frames", []))) if self.playing else self.frame_idx
        for i in range(n_frames):
            label = self._frame_label(i)
            is_active = (self.playing and kind == "state" and i == show_idx) or (not self.playing and i == self.frame_idx)
            if is_active:
                out += CSI + "33m\u25cf " + label + RST + "  "
            else:
                out += DIM + "\u25cb " + label + RST + "  "
        if kind == "state":
            mode = self.data["states"][name].get("mode", "?")
            out += " " + DIM + "%s %dms" % (mode, ms) + RST
        else:
            out += " " + DIM + "hold %.1fs" % (ms / 1000.0) + RST
        out += CSI + "K\n\n"

        # Grid + preview
        display = play_grid if self.playing else grid
        preview_rows = ["".join(r) for r in display]
        for y in range(MAX_H):
            for x in range(MAX_W):
                ch = display[y][x].upper()
                at_cursor = x == self.cx and y == self.cy and not self.playing

                if at_cursor:
                    if ch == "0":
                        out += CSI + "48;5;23m " + RST
                    elif ch == "I":
                        out += CSI + "48;5;37m " + RST
                    else:
                        out += CSI + "48;5;37m" + CSI + "30m" + (BLOCKS[int(ch, 16)] if ch != "F" else "\u2588") + RST
                else:
                    if ch == "I":
                        out += CSI + "7m " + RST
                    elif ch == "0":
                        out += (DIM + "\u00b7" + RST) if (x + y) % 2 else " "
                    elif ch == "F":
                        out += BOLD + "\u2588" + RST
                    else:
                        out += BOLD + BLOCKS[int(ch, 16)] + RST

            # Preview
            if y < len(preview_rows):
                out += "    " + render_hex_line(preview_rows[y])
            out += CSI + "K\n"

        # Brush palette
        out += CSI + "K\n" + "  brush: "
        for i, b in enumerate(BRUSHES):
            if i == self.brush_idx:
                out += CSI + "43m"
            out += brush_char(b)
            if i == self.brush_idx:
                out += RST
            out += " "
        out += CSI + "K\n"

        # Status
        out += DIM
        if self.playing:
            out += "  PLAYING  "
        out += "wasd:move space:paint []:brush </>:frame enter:add bksp:del +/-:ms P:play Y/V:copy S:save"
        out += RST
        if self.saved:
            out += "  " + CSI + "32m[saved]" + RST
        out += CSI + "K\n"
        out += "  char(%d,%d) frame:%s" % (self.cx, self.cy, self._frame_label(self.frame_idx))
        out += CSI + "K\n" + CSI + "J"

        sys.stdout.write(out)
        sys.stdout.flush()

    def save(self):
        # Trim and write back
        for name, kind in self.items:
            if kind == "state":
                cfg = self.data["states"][name]
                new_frames = []
                for i in range(len(cfg.get("frames", []))):
                    key = (name, "frame", i)
                    if key in self.grids:
                        new_frames.append(trim_frame(self.grids[key]))
                cfg["frames"] = new_frames
                blink_key = (name, "blink", 0)
                if blink_key in self.grids:
                    cfg["blink"] = trim_frame(self.grids[blink_key])
            else:
                key = (name, "reaction", 0)
                if key in self.grids:
                    self.data["reactions"][name]["frame"] = trim_frame(self.grids[key])

        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)
            f.write("\n")
        self.saved = True

    def add_frame(self):
        if self._current_kind() != "state":
            return
        name = self._current_name()
        cfg = self.data["states"][name]
        frames = cfg.get("frames", [])
        n = len(frames)
        if self.frame_idx >= n:
            return  # can't add after blink
        # Duplicate current frame
        src_key = (name, "frame", self.frame_idx)
        new_grid = copy.deepcopy(self.grids[src_key])
        # Shift all frame keys after insertion point
        for i in range(n - 1, self.frame_idx, -1):
            self.grids[(name, "frame", i + 1)] = self.grids.pop((name, "frame", i))
        self.frame_idx += 1
        self.grids[(name, "frame", self.frame_idx)] = new_grid
        frames.insert(self.frame_idx, ["0"] * 7)  # placeholder, will be overwritten on save

    def del_frame(self):
        if self._current_kind() != "state":
            return
        name = self._current_name()
        cfg = self.data["states"][name]
        frames = cfg.get("frames", [])
        n = len(frames)
        if n <= 1:
            return  # keep at least one
        if self.frame_idx >= n:
            return  # can't delete blink
        del self.grids[(name, "frame", self.frame_idx)]
        # Shift keys down
        for i in range(self.frame_idx + 1, n):
            if (name, "frame", i) in self.grids:
                self.grids[(name, "frame", i - 1)] = self.grids.pop((name, "frame", i))
        frames.pop(self.frame_idx)
        if self.frame_idx >= len(frames):
            self.frame_idx = len(frames) - 1

    def run(self):
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        orig_fl = fcntl.fcntl(fd, fcntl.F_GETFL)

        try:
            tty.setcbreak(fd)
            sys.stdout.write(CSI + "2J")

            while True:
                self.render()
                self.saved = False

                # Animate in play mode
                if self.playing:
                    fcntl.fcntl(fd, fcntl.F_SETFL, orig_fl | os.O_NONBLOCK)
                    try:
                        key = read_key(fd)
                    except (BlockingIOError, OSError):
                        key = None
                    fcntl.fcntl(fd, fcntl.F_SETFL, orig_fl)

                    now = time.time()
                    ms = self._ms()
                    if now - self.play_time >= ms / 1000.0:
                        name = self._current_name()
                        n = len(self.data["states"].get(name, {}).get("frames", []))
                        if n > 0:
                            self.play_frame = (self.play_frame + 1) % n
                        self.play_time = now

                    if key is None:
                        time.sleep(0.03)
                        continue
                else:
                    key = read_key(fd)

                if key in ("q", "Q", "ESC"):
                    break
                elif key in ("p", "P"):
                    self.playing = not self.playing
                    self.play_frame = 0
                    self.play_time = time.time()
                elif key in ("w", "UP"):
                    self.cy = max(0, self.cy - 1)
                elif key in ("s", "DOWN"):
                    self.cy = min(MAX_H - 1, self.cy + 1)
                elif key in ("a", "A", "LEFT"):
                    self.cx = max(0, self.cx - 1)
                elif key in ("d", "D", "RIGHT"):
                    self.cx = min(MAX_W - 1, self.cx + 1)
                elif key == " ":
                    grid = self._current_grid()
                    if grid:
                        current = grid[self.cy][self.cx].upper()
                        brush = BRUSHES[self.brush_idx]
                        grid[self.cy][self.cx] = "0" if current == brush else brush
                elif key == "]":
                    self.brush_idx = (self.brush_idx + 1) % len(BRUSHES)
                elif key == "[":
                    self.brush_idx = (self.brush_idx - 1) % len(BRUSHES)
                elif key in ("<", ","):
                    n = self._frame_count()
                    if n > 0:
                        self.frame_idx = (self.frame_idx - 1) % n
                elif key in (">", "."):
                    n = self._frame_count()
                    if n > 0:
                        self.frame_idx = (self.frame_idx + 1) % n
                elif key == "ENTER":
                    self.add_frame()
                elif key == "BACKSPACE":
                    self.del_frame()
                elif key == "+":
                    self._set_ms(self._ms() + 50)
                elif key == "-":
                    self._set_ms(self._ms() - 50)
                elif key == "\t":
                    self.item_idx = (self.item_idx + 1) % len(self.items)
                    self.frame_idx = 0
                    self.playing = False
                elif key == "SHIFT_TAB":
                    self.item_idx = (self.item_idx - 1) % len(self.items)
                    self.frame_idx = 0
                    self.playing = False
                elif key in ("r", "R"):
                    grid = self._current_grid()
                    if grid:
                        row = grid[self.cy]
                        filled = sum(1 for c in row if c != "0")
                        fill = "0" if filled > len(row) // 2 else BRUSHES[self.brush_idx]
                        grid[self.cy][:] = [fill] * MAX_W
                elif key in ("x",):
                    grid = self._current_grid()
                    if grid:
                        grid[self.cy][:] = ["0"] * MAX_W
                elif key == "f":
                    grid = self._current_grid()
                    if grid:
                        grid[self.cy][:] = [BRUSHES[self.brush_idx]] * MAX_W
                elif key in ("y", "Y"):
                    grid = self._current_grid()
                    if grid:
                        self.clipboard = copy.deepcopy(grid)
                elif key in ("v", "V"):
                    if self.clipboard:
                        key = self._grid_key()
                        self.grids[key] = copy.deepcopy(self.clipboard)
                elif key == "S":
                    self.save()
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, orig_fl)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write(CSI + "?25h\n")
            sys.stdout.flush()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "src/claude_cat/sprites/default.json"
    Editor(path).run()
