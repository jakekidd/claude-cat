"""Shared constants and utility functions used across modules."""

import glob
import os
from pathlib import Path

# ANSI escape sequences
CSI = "\033["
HIDE = CSI + "?25l"
SHOW = CSI + "?25h"
HOME = CSI + "H"
CLR = CSI + "2J"
CLRL = CSI + "K"
CLRB = CSI + "J"
BOLD = CSI + "1m"
DIM = CSI + "2m"
RST = CSI + "0m"

# Unicode block characters (indexed by hex digit 0-F)
BLOCKS = " \u2597\u2596\u2584\u259d\u2590\u259e\u259f\u2598\u259a\u258c\u2599\u2580\u259c\u259b\u2588"

# State file paths
STATE_DIR = os.path.join(Path.home(), ".claude-cat", "state")
STATE_PREFIX = "claude-cat-"
STATE_FILE = os.path.join(STATE_DIR, "claude-cat.json")


def state_file_for(session_id):
    return os.path.join(STATE_DIR, STATE_PREFIX + session_id + ".json")


def find_session_files():
    return glob.glob(os.path.join(STATE_DIR, STATE_PREFIX + "*.json"))


def project_dir_from_transcript(transcript_path):
    """Extract project root dir from transcript path.

    Transcript lives at ~/.claude/projects/-Users-foo-Code-bar/session.jsonl
    The dir name encodes the project path with - as separator.
    """
    try:
        encoded = os.path.basename(os.path.dirname(transcript_path))
        if encoded.startswith("-"):
            return "/" + encoded.lstrip("-").replace("-", "/")
    except Exception:
        pass
    return ""


def render_hex_line(hex_row, color=None):
    fg = CSI + "38;5;%dm" % color if color else ""
    out = ""
    i = 0
    while i < len(hex_row):
        ch = hex_row[i].upper()
        if ch == "I":
            j = i
            while j < len(hex_row) and hex_row[j].upper() == "I":
                j += 1
            out += fg + CSI + "7m" + " " * (j - i) + RST
            i = j
        elif ch == "0":
            out += " "
            i += 1
        elif ch == "F":
            out += fg + BOLD + "\u2588" + RST
            i += 1
        else:
            idx = int(ch, 16)
            out += fg + BOLD + BLOCKS[idx] + RST
            i += 1
    return out
