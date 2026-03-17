#!/usr/bin/env python3
"""Eye animation editor for claude-cat.

Edit eye frames for a mood and preview them live.

Controls:
  TAB / 1-7        Switch mood
  LEFT / RIGHT     Select eye slot
  [ / ]            Cycle char at selected slot
  ENTER            Add current eye state as a new frame
  BACKSPACE / X    Delete last frame
  +/-              Adjust ms timing (50ms steps)
  P                Play/pause animation preview
  S                Save to JSON
  Q                Quit
"""

import copy
import json
import os
import sys
import termios
import time
import tty

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from claude_cat.__main__ import render_hex_line
from claude_cat.sprites import BLOCKS, REQUIRED_MOODS

MOODS = REQUIRED_MOODS
CSI = "\033["
RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CHARS = "0123456789ABCDEFI"


def read_key(fd):
    ch = os.read(fd, 1).decode("utf-8", errors="ignore")
    if ch == "\033":
        ch2 = os.read(fd, 1).decode("utf-8", errors="ignore")
        if ch2 == "[":
            ch3 = os.read(fd, 1).decode("utf-8", errors="ignore")
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT",
                    "Z": "SHIFT_TAB", "3": "DEL"}.get(ch3, "ESC")
        return "ESC"
    if ch == "\r" or ch == "\n":
        return "ENTER"
    if ch == "\x7f":
        return "BACKSPACE"
    return ch


def char_display(ch):
    ch = ch.upper()
    if ch == "0":
        return " "
    elif ch == "I":
        return CSI + "7m " + RST
    elif ch == "F":
        return BOLD + "\u2588" + RST
    else:
        idx = int(ch, 16)
        return BOLD + BLOCKS[idx] + RST


def render(sprite_rows, eyes_cfg, mood_idx, slot_idx, frame_idx, playing, play_frame, ms):
    mood = MOODS[mood_idx]
    slots = eyes_cfg.get("slots", [])
    frames = eyes_cfg.get("frames", [])

    out = CSI + "H" + CSI + "?25l"

    # Mood tabs
    for i, m in enumerate(MOODS):
        if i == mood_idx:
            out += CSI + "7m " + m + " " + RST + " "
        else:
            out += DIM + " " + m + " " + RST + " "
    out += CSI + "K\n\n"

    # Cat preview (with current eye state applied)
    if playing and frames:
        frame = frames[play_frame % len(frames)]
    elif frames and frame_idx < len(frames):
        frame = frames[frame_idx]
    else:
        frame = None

    preview_rows = list(sprite_rows)
    if frame and slots:
        patched = [list(r) for r in preview_rows]
        for i, (r, c) in enumerate(slots):
            if i < len(frame) and r < len(patched) and c < len(patched[r]):
                patched[r][c] = frame[i]
        preview_rows = ["".join(r) for r in patched]

    for line in preview_rows:
        out += "  " + render_hex_line(line) + CSI + "K\n"

    out += CSI + "K\n"

    # Slots display
    if slots:
        out += "  slots: "
        for i, (r, c) in enumerate(slots):
            if i == slot_idx:
                out += CSI + "43m"
            out += "[%d,%d]" % (r, c)
            if i == slot_idx:
                out += RST
            out += " "
        out += CSI + "K\n"

        # Current slot char (editable)
        if frames and frame_idx < len(frames):
            current_frame = frames[frame_idx] if not playing else frames[play_frame % len(frames)]
            out += "  editing: "
            for i, ch in enumerate(current_frame):
                if i == slot_idx:
                    out += CSI + "43m"
                out += char_display(ch)
                if i == slot_idx:
                    out += RST
                out += " "
            out += CSI + "K\n"
    else:
        out += "  no eye slots defined for this mood" + CSI + "K\n"
        out += CSI + "K\n"

    out += CSI + "K\n"

    # Frame pips
    out += "  ms: %d   " % ms
    if frames:
        for i in range(len(frames)):
            if playing and i == play_frame % len(frames):
                out += CSI + "33m\u25cf" + RST + " "
            elif not playing and i == frame_idx:
                out += "\u25cf "
            else:
                out += DIM + "\u25cb" + RST + " "
    out += CSI + "K\n"

    # Frames detail
    for i, fr in enumerate(frames):
        if not playing and i == frame_idx:
            marker = CSI + "33m>" + RST
        elif playing and i == play_frame % len(frames):
            marker = CSI + "33m>" + RST
        else:
            marker = " "
        out += "  %s %2d: " % (marker, i)
        for ch in fr:
            out += char_display(ch) + " "
        out += " " + DIM + "[%s]" % fr + RST
        out += CSI + "K\n"

    out += CSI + "K\n"

    # Controls
    out += DIM
    if playing:
        out += "  PLAYING  "
    out += "L/R:slot  U/D:frame  []:char  enter:add  bksp:del  +/-:ms  P:play  S:save  Q:quit"
    out += RST + CSI + "K\n" + CSI + "J"

    sys.stdout.write(out)
    sys.stdout.flush()


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "src/claude_cat/sprites/default.json"

    with open(path) as f:
        data = json.load(f)

    moods = data.get("moods", {})
    all_eyes = data.get("eyes", {})

    mood_idx = 0
    slot_idx = 0
    frame_idx = 0
    playing = False
    play_frame = 0
    last_play_time = 0.0

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    import fcntl
    # Set stdin to non-blocking for play mode
    orig_fl = fcntl.fcntl(fd, fcntl.F_GETFL)

    try:
        tty.setcbreak(fd)
        sys.stdout.write(CSI + "2J")

        while True:
            mood = MOODS[mood_idx]
            sprite_rows = moods.get(mood, [])
            eyes_cfg = all_eyes.get(mood, {"slots": [], "frames": [], "ms": 600})
            slots = eyes_cfg.get("slots", [])
            frames = eyes_cfg.get("frames", [])
            ms = eyes_cfg.get("ms", 600)

            # Animate in play mode
            if playing and frames:
                now = time.time()
                if now - last_play_time >= ms / 1000.0:
                    play_frame = (play_frame + 1) % len(frames)
                    last_play_time = now

            if frames:
                frame_idx = min(frame_idx, len(frames) - 1)
            else:
                frame_idx = 0

            render(sprite_rows, eyes_cfg, mood_idx, slot_idx, frame_idx, playing, play_frame, ms)

            # Non-blocking read in play mode, blocking otherwise
            if playing:
                fcntl.fcntl(fd, fcntl.F_SETFL, orig_fl | os.O_NONBLOCK)
                try:
                    key = read_key(fd)
                except (BlockingIOError, OSError):
                    key = None
                    time.sleep(0.05)
                fcntl.fcntl(fd, fcntl.F_SETFL, orig_fl)
            else:
                key = read_key(fd)

            if key is None:
                continue

            if key in ("q", "Q", "ESC"):
                break
            elif key in ("p", "P"):
                playing = not playing
                play_frame = 0
                last_play_time = time.time()
            elif key == "RIGHT":
                if slots:
                    slot_idx = (slot_idx + 1) % len(slots)
            elif key == "LEFT":
                if slots:
                    slot_idx = (slot_idx - 1) % len(slots)
            elif key == "UP":
                if frames:
                    frame_idx = (frame_idx - 1) % len(frames)
            elif key == "DOWN":
                if frames:
                    frame_idx = (frame_idx + 1) % len(frames)
            elif key == "]":
                if frames and slots and frame_idx < len(frames):
                    f = list(frames[frame_idx])
                    if slot_idx < len(f):
                        ci = CHARS.index(f[slot_idx].upper())
                        f[slot_idx] = CHARS[(ci + 1) % len(CHARS)]
                        frames[frame_idx] = "".join(f)
            elif key == "[":
                if frames and slots and frame_idx < len(frames):
                    f = list(frames[frame_idx])
                    if slot_idx < len(f):
                        ci = CHARS.index(f[slot_idx].upper())
                        f[slot_idx] = CHARS[(ci - 1) % len(CHARS)]
                        frames[frame_idx] = "".join(f)
            elif key == "ENTER":
                if slots:
                    if frames:
                        frames.insert(frame_idx + 1, frames[frame_idx])
                        frame_idx += 1
                    else:
                        frames.append("I" * len(slots))
                        frame_idx = 0
                    eyes_cfg["frames"] = frames
                    all_eyes[mood] = eyes_cfg
            elif key in ("BACKSPACE", "DEL", "x", "X"):
                if frames:
                    frames.pop(frame_idx)
                    if frame_idx >= len(frames) and frames:
                        frame_idx = len(frames) - 1
                    eyes_cfg["frames"] = frames
            elif key == "+":
                ms = min(5000, ms + 50)
                eyes_cfg["ms"] = ms
                all_eyes[mood] = eyes_cfg
            elif key == "-":
                ms = max(50, ms - 50)
                eyes_cfg["ms"] = ms
                all_eyes[mood] = eyes_cfg
            elif key == "\t":
                mood_idx = (mood_idx + 1) % len(MOODS)
                slot_idx = 0
                frame_idx = 0
                playing = False
            elif key in "1234567":
                mood_idx = int(key) - 1
                slot_idx = 0
                frame_idx = 0
                playing = False
            elif key == "S":
                data["eyes"] = all_eyes
                with open(path, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")

    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, orig_fl)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(CSI + "?25h\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
