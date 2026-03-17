#!/usr/bin/env python3
"""View all states and reactions. Usage: python3 view-sprite.py [name|path]"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from claude_cat.sprites import load
from claude_cat.__main__ import render_hex_line

name = sys.argv[1] if len(sys.argv) > 1 else None
data = load(name)

print("\033[1mStates:\033[0m")
for state_name, cfg in data.get("states", {}).items():
    frames = cfg.get("frames", [])
    mode = cfg.get("mode", "?")
    ms = cfg.get("ms", 0)
    print("\n\033[1m%s\033[0m  (%s, %dms, %d frames)" % (state_name, mode, ms, len(frames)))
    # Show first frame
    if frames:
        for line in frames[0]:
            print("  " + render_hex_line(line))

print("\n\033[1mReactions:\033[0m")
for name, cfg in data.get("reactions", {}).items():
    hold = cfg.get("hold", 0)
    print("\n\033[1m%s\033[0m  (hold %.1fs)" % (name, hold))
    for line in cfg.get("frame", []):
        print("  " + render_hex_line(line))
