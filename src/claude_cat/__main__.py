#!/usr/bin/env python3
"""claude-cat -- a 1-bit companion cat for Claude Code."""

import json
import os
import random
import signal
import sys
import tempfile
import time
from pathlib import Path

# Allow running directly: python3 __main__.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sprites as sprites_mod

VERSION = "0.1.0"
STATE_FILE = os.path.join(tempfile.gettempdir(), "claude-cat.json")
HOOK_EVENTS = [
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SubagentStart",
    "SubagentStop",
]

# Quadrant block lookup (extended hex: 0-F + I for inverse video)
BLOCKS = " \u2597\u2596\u2584\u259d\u2590\u259e\u259f\u2598\u259a\u258c\u2599\u2580\u259c\u259b\u2588"

TOOL_LABELS = {
    "Read": "reading",
    "Edit": "editing",
    "Write": "writing",
    "Bash": "hacking",
    "Grep": "searching",
    "Glob": "looking",
    "Agent": "thinking",
    "WebFetch": "fetching",
    "WebSearch": "googling",
    "Skill": "casting",
}

# ANSI sequences
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


def render_hex_line(hex_row):
    """Render a hex-format sprite row to terminal output.

    0=space, 1-E=quadrant blocks, F=foreground block, I=inverse video.
    """
    out = ""
    i = 0
    while i < len(hex_row):
        ch = hex_row[i].upper()
        if ch == "I":
            j = i
            while j < len(hex_row) and hex_row[j].upper() == "I":
                j += 1
            out += CSI + "7m" + " " * (j - i) + RST
            i = j
        elif ch == "0":
            out += " "
            i += 1
        elif ch == "F":
            out += BOLD + "\u2588" + RST
            i += 1
        else:
            idx = int(ch, 16)
            out += BOLD + BLOCKS[idx] + RST
            i += 1
    return out


class Cat:
    def __init__(self, sprite_data=None):
        self.sprites = sprite_data or sprites_mod.BUILTIN
        self.mood = "idle"
        self.bubble = ""
        self.blinking = False
        self.last_event = time.time()
        self.last_raw = ""
        self.last_mtime = 0.0
        self.next_blink = time.time() + random.uniform(2, 7)
        self.blink_end = 0.0
        self.bubble_end = 0.0

    def render(self):
        mood = self.mood
        if self.blinking and mood not in ("sleeping", "surprised"):
            mood = "blink"
        cat = self.sprites[mood]
        cat_w = len(cat[0]) if cat else 12

        out = HOME + HIDE

        if self.bubble:
            inner = " " + self.bubble + " "
            horiz = "\u2500" * len(inner)
            pad = " " * max(0, (cat_w - len(inner) - 2) // 2)
            out += pad + DIM + "\u256d" + horiz + "\u256e" + RST + CLRL + "\n"
            out += pad + DIM + "\u2502" + RST + inner + DIM + "\u2502" + RST + CLRL + "\n"
            out += pad + DIM + "\u2570" + horiz + "\u256f" + RST + CLRL + "\n"
        else:
            out += CLRL + "\n" + CLRL + "\n" + CLRL + "\n"

        for line in cat:
            out += render_hex_line(line) + CLRL + "\n"

        out += CLRL + "\n" + DIM + self.mood + RST + CLRL + "\n" + CLRB
        sys.stdout.write(out)
        sys.stdout.flush()

    def handle_event(self, data):
        if self.mood == "sleeping":
            self.mood = "surprised"
            self.bubble = "!"
            self.render()
            time.sleep(0.4)
            self.mood = "idle"

        ev = data.get("event", "")
        tool = data.get("tool", "")

        if ev in ("Stop", "SubagentStop"):
            self.mood = "happy"
            self.bubble = "done!" if ev == "Stop" else "returned"
        elif ev == "PostToolUseFailure":
            self.mood = "error"
            self.bubble = "oops"
        elif ev in ("PostToolUse", "PreToolUse"):
            self.mood = "working"
            self.bubble = TOOL_LABELS.get(tool, tool.lower() or "working")
        elif ev == "SubagentStart":
            self.mood = "working"
            self.bubble = "spawning"
        else:
            self.mood = "idle"
            self.bubble = ev.lower() or ""

        self.last_event = time.time()
        self.bubble_end = time.time() + 4
        self.render()


# ── Commands ─────────────────────────────────────────────────────────


def hook_mode():
    try:
        data = json.loads(sys.stdin.read())
        with open(STATE_FILE, "w") as f:
            json.dump(
                {
                    "event": data.get("hook_event_name", "unknown"),
                    "tool": data.get("tool_name", ""),
                    "ts": int(time.time() * 1000),
                },
                f,
            )
    except Exception:
        pass
    sys.exit(0)


def install_hooks():
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception:
            pass

    hooks = settings.setdefault("hooks", {})
    added = 0

    for event in HOOK_EVENTS:
        rules = hooks.setdefault(event, [])
        already = any(
            any("claude-cat" in h.get("command", "") for h in rule.get("hooks", []))
            for rule in rules
        )
        if not already:
            rules.append(
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "claude-cat --hook",
                            "async": True,
                            "timeout": 5,
                        }
                    ],
                }
            )
            added += 1

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    if added:
        print("Installed %d hook(s) in %s" % (added, settings_path))
        print("Run claude-cat in a side terminal to see your cat.")
    else:
        print("Hooks already installed.")


def uninstall_hooks():
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print("No settings found.")
        return

    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        return

    hooks = settings.get("hooks", {})
    if not hooks:
        print("No hooks found.")
        return

    removed = 0
    for event in HOOK_EVENTS:
        if event not in hooks:
            continue
        before = len(hooks[event])
        hooks[event] = [
            rule
            for rule in hooks[event]
            if not any(
                "claude-cat" in h.get("command", "") for h in rule.get("hooks", [])
            )
        ]
        removed += before - len(hooks[event])
        if not hooks[event]:
            del hooks[event]

    if not hooks:
        del settings["hooks"]

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    print("Removed %d hook(s) from %s" % (removed, settings_path))


def watch_mode(sprite_data=None):
    sys.stdout.write(CLR)
    sys.stdout.flush()

    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, "w") as f:
            f.write("{}")

    cat = Cat(sprite_data)
    cat.render()

    def cleanup(*_):
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    while True:
        now = time.time()
        dirty = False

        # Poll state file
        try:
            mtime = os.path.getmtime(STATE_FILE)
            if mtime > cat.last_mtime:
                cat.last_mtime = mtime
                with open(STATE_FILE) as f:
                    raw = f.read()
                if raw != cat.last_raw:
                    cat.last_raw = raw
                    cat.handle_event(json.loads(raw))
                    dirty = True
        except (OSError, json.JSONDecodeError):
            pass

        # Blink
        if (
            not cat.blinking
            and now >= cat.next_blink
            and cat.mood not in ("sleeping", "surprised")
        ):
            cat.blinking = True
            cat.blink_end = now + 0.15
            cat.next_blink = now + random.uniform(2, 7)
            dirty = True
        elif cat.blinking and now >= cat.blink_end:
            cat.blinking = False
            dirty = True

        # Sleep after 2 minutes idle
        if cat.mood != "sleeping" and now - cat.last_event > 120:
            cat.mood = "sleeping"
            cat.bubble = "zzz"
            cat.bubble_end = now + 3
            dirty = True

        # Clear bubble
        if cat.bubble and cat.bubble_end and now >= cat.bubble_end:
            cat.bubble = ""
            cat.bubble_end = 0
            if cat.mood != "sleeping":
                cat.mood = "idle"
            dirty = True

        if dirty:
            cat.render()

        time.sleep(0.1)


def demo_mode(sprite_data=None):
    sys.stdout.write(CLR)
    sys.stdout.flush()

    cat = Cat(sprite_data)
    moods = ["idle", "blink", "working", "happy", "error", "sleeping", "surprised"]

    def cleanup(*_):
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)

    for mood in moods:
        cat.mood = mood
        cat.bubble = mood
        cat.render()
        time.sleep(1.5)

    cleanup()


def print_help():
    print(
        "claude-cat v%s\n"
        "A 1-bit companion cat for Claude Code\n\n"
        "Usage:\n"
        "  claude-cat                       Start the cat\n"
        "  claude-cat --sprite <name|path>  Use a custom sprite\n"
        "  claude-cat install               Set up Claude Code hooks\n"
        "  claude-cat uninstall             Remove Claude Code hooks\n"
        "  claude-cat --demo                Preview all expressions\n"
        "  claude-cat list-sprites          Show available sprites\n"
        "  claude-cat --version             Show version" % VERSION
    )


def main():
    args = sys.argv[1:]

    # Extract --sprite flag from anywhere in args
    sprite_name = None
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--sprite" and i + 1 < len(args):
            sprite_name = args[i + 1]
            i += 2
        else:
            filtered.append(args[i])
            i += 1

    cmd = filtered[0] if filtered else ""

    # Load sprites (only needed for display modes)
    sprite_data = None
    if cmd in ("", "--watch", "watch", "--demo", "demo"):
        sprite_data = sprites_mod.load(sprite_name)

    if cmd in ("--hook", "hook"):
        hook_mode()
    elif cmd in ("--demo", "demo"):
        demo_mode(sprite_data)
    elif cmd == "install":
        install_hooks()
    elif cmd == "uninstall":
        uninstall_hooks()
    elif cmd in ("list-sprites", "sprites"):
        sprites_mod.list_sprites()
    elif cmd in ("--help", "-h", "help"):
        print_help()
    elif cmd in ("--version", "-v"):
        print(VERSION)
    elif cmd in ("", "--watch", "watch"):
        watch_mode(sprite_data)
    else:
        print("Unknown command: %s" % cmd)
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
