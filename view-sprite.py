#!/usr/bin/env python3
"""View all moods for a sprite. Usage: python3 view-sprite.py [name|path]"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from claude_cat.sprites import load
from claude_cat.__main__ import render_hex_line

name = sys.argv[1] if len(sys.argv) > 1 else None
sprites = load(name)

moods = ["idle", "blink", "working", "happy", "error", "sleeping", "surprised"]
for mood in moods:
    print(f"\033[1m{mood}\033[0m")
    for line in sprites[mood]:
        print("  " + render_hex_line(line))
    print()
