#!/usr/bin/env python3
"""View all moods for a sprite. Usage: python3 view-sprite.py [name|path]"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from claude_cat.sprites import load, BUILTIN
from claude_cat.__main__ import to_blocks

RST = "\033[0m"
BOLD = "\033[1m"

def render_line(line):
    """Render a cat line using inverse video for full blocks."""
    out = ""
    i = 0
    while i < len(line):
        if line[i] == "\u2588":
            j = i
            while j < len(line) and line[j] == "\u2588":
                j += 1
            out += "\033[7m" + " " * (j - i) + RST
            i = j
        elif line[i] == " ":
            out += " "
            i += 1
        else:
            out += BOLD + line[i] + RST
            i += 1
    return out

name = sys.argv[1] if len(sys.argv) > 1 else None
sprites = load(name)

moods = ["idle", "blink", "working", "happy", "error", "sleeping", "surprised"]
for mood in moods:
    print(f"\033[1m{mood}\033[0m")
    for line in to_blocks(sprites[mood]):
        print("  " + render_line(line))
    print()
