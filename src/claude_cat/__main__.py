#!/usr/bin/env python3
"""claude-cat -- a 1-bit companion cat for Claude Code."""

import glob
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

VERSION = "0.2.0"
STATE_DIR = tempfile.gettempdir()
STATE_PREFIX = "claude-cat-"
STATE_FILE = os.path.join(STATE_DIR, "claude-cat.json")  # legacy single mode
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

# Warm palette for litter mode — orange/amber/rust family, shuffled each boot
PALETTE = [208, 209, 215, 216, 173, 179, 180, 137, 172, 214]
random.shuffle(PALETTE)

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

# Event-driven overlays rendered beside the cat
OVERLAYS = {
    "bulb": {
        "art": [" \u259e\u259a", " \u259c\u259b"],
        "duration": 3.0,
    },
    "plug": {
        "art": [" \u2596\u2597", " \u259c\u259b"],
        "duration": 4.0,
    },
}


def state_file_for(session_id):
    return os.path.join(STATE_DIR, STATE_PREFIX + session_id + ".json")


def find_session_files():
    return glob.glob(os.path.join(STATE_DIR, STATE_PREFIX + "*.json"))


def render_hex_line(hex_row, color=None):
    """Render a hex-format sprite row to terminal output.

    0=space, 1-E=quadrant blocks, F=foreground block, I=inverse video.
    Optional color (256-color index) tints all filled elements.
    """
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


class Cat:
    def __init__(self, sprite_data=None, session_id=None, color=None):
        if sprite_data and isinstance(sprite_data, dict) and "moods" in sprite_data:
            self.sprites = sprite_data["moods"]
            self.eyes_config = sprite_data.get("eyes", {})
        else:
            self.sprites = sprite_data or {}
            self.eyes_config = {}
        self.session_id = session_id or ""
        self.color = color
        self.cwd = ""
        self.state_file = state_file_for(session_id) if session_id else STATE_FILE
        self.mood = "idle"
        self.bubble = ""
        self.blinking = False
        self.last_event = time.time()
        self.last_raw = ""
        self.last_mtime = 0.0
        self.next_blink = time.time() + random.uniform(2, 7)
        self.blink_end = 0.0
        self.bubble_end = 0.0
        self.eye_frame = 0
        self.next_eye_shift = time.time() + random.uniform(1.0, 3.0)
        self.overlay = None
        self.overlay_end = 0.0

    def _apply_eyes(self, mood, rows):
        cfg = self.eyes_config.get(mood)
        if not cfg:
            return rows
        frames = cfg["frames"]
        if not frames:
            return rows
        frame = frames[self.eye_frame % len(frames)]
        slots = cfg["slots"]
        patched = [list(r) for r in rows]
        for i, (r, c) in enumerate(slots):
            if i < len(frame) and r < len(patched) and c < len(patched[r]):
                patched[r][c] = frame[i]
        return ["".join(r) for r in patched]

    def _resolve_sprite(self):
        mood = self.mood
        if self.blinking and mood not in ("sleeping", "surprised", "interrupted"):
            mood = "blink"
        return self._apply_eyes(mood, self.sprites.get(mood, []))

    def _process_event(self, data):
        """Update internal state from event data (no render)."""
        prev_mood = self.mood
        ev = data.get("event", "")
        tool = data.get("tool", "")

        if ev in ("Stop", "SubagentStop"):
            self.mood = "happy"
            self.bubble = "done!" if ev == "Stop" else "returned"
            self.overlay = "bulb"
            self.overlay_end = time.time() + OVERLAYS["bulb"]["duration"]
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

        if self.mood != prev_mood:
            self.eye_frame = 0
            self.next_eye_shift = time.time() + random.uniform(1.0, 3.0)

        self.last_event = time.time()
        self.bubble_end = time.time() + 4

    def render(self):
        """Full render for single/target mode (writes to stdout)."""
        cat = self._resolve_sprite()
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
            out += render_hex_line(line, color=self.color) + CLRL + "\n"

        # Overlay (absolute cursor positioning)
        if self.overlay and self.overlay in OVERLAYS:
            ov = OVERLAYS[self.overlay]
            base_row = 4
            cat_w_actual = len(cat[0]) if cat else 14
            for i, art_line in enumerate(ov["art"]):
                r = base_row - 1 + i
                c = cat_w_actual + 1
                if r > 0:
                    out += CSI + "%d;%dH" % (r, c) + BOLD + art_line + RST

        out += CLRL + "\n" + DIM + self.mood + RST + CLRL + "\n" + CLRB
        sys.stdout.write(out)
        sys.stdout.flush()

    def handle_event(self, data):
        """Process event with wake-up animation (for single/target mode)."""
        if self.mood == "sleeping":
            self.mood = "surprised"
            self.bubble = "!"
            self.render()
            time.sleep(0.4)
            self.mood = "idle"
        self._process_event(data)
        self.render()


# ── Litter mode ──────────────────────────────────────────────────────


class Litter:
    """Manages multiple cats, one per Claude Code session."""

    def __init__(self, sprite_data):
        self.sprite_data = sprite_data
        self.cats = {}  # session_id -> Cat
        self.cat_order = []  # ordered session IDs
        self.color_idx = 0

    def _next_color(self):
        c = PALETTE[self.color_idx % len(PALETTE)]
        self.color_idx += 1
        return c

    def scan(self):
        """Discover new sessions, track existing ones."""
        files = find_session_files()
        seen = set()
        for path in files:
            basename = os.path.basename(path)
            sid = basename[len(STATE_PREFIX) : -len(".json")]
            seen.add(sid)
            if sid not in self.cats:
                cat = Cat(
                    self.sprite_data, session_id=sid, color=self._next_color()
                )
                cat.state_file = path
                self.cats[sid] = cat
                self.cat_order.append(sid)

        # Remove cats whose state files are gone
        for sid in list(self.cat_order):
            if sid not in seen:
                del self.cats[sid]
                self.cat_order.remove(sid)

    def tick(self):
        """Update all cats. Returns True if any changed."""
        now = time.time()
        dirty = False

        for cat in self.cats.values():
            # Poll state file
            try:
                mtime = os.path.getmtime(cat.state_file)
                if mtime > cat.last_mtime:
                    cat.last_mtime = mtime
                    with open(cat.state_file) as f:
                        raw = f.read()
                    if raw != cat.last_raw:
                        cat.last_raw = raw
                        data = json.loads(raw)
                        cat.cwd = data.get("cwd", cat.cwd)
                        cat._process_event(data)
                        dirty = True
            except (OSError, json.JSONDecodeError):
                pass

            # Blink
            if (
                not cat.blinking
                and now >= cat.next_blink
                and cat.mood not in ("sleeping", "surprised", "interrupted")
            ):
                cat.blinking = True
                cat.blink_end = now + 0.15
                cat.next_blink = now + random.uniform(2, 7)
                dirty = True
            elif cat.blinking and now >= cat.blink_end:
                cat.blinking = False
                dirty = True

            # Eye animation
            if (
                not cat.blinking
                and now >= cat.next_eye_shift
                and cat.mood not in ("sleeping", "blink")
            ):
                cfg = cat.eyes_config.get(cat.mood)
                if cfg and cfg.get("frames"):
                    cat.eye_frame = (cat.eye_frame + 1) % len(cfg["frames"])
                    cat.next_eye_shift = now + cfg.get("ms", 2000) / 1000.0
                    dirty = True
                else:
                    cat.next_eye_shift = now + 2.0

            # Sleep after 2 minutes idle
            if cat.mood not in ("sleeping", "interrupted") and now - cat.last_event > 120:
                if cat.mood == "working":
                    # Was mid-task when session went quiet
                    cat.mood = "interrupted"
                    cat.bubble = "interrupted"
                    cat.bubble_end = now + 5
                else:
                    cat.mood = "sleeping"
                    cat.bubble = "zzz"
                    cat.bubble_end = now + 3
                cat.overlay = "plug"
                cat.overlay_end = now + OVERLAYS["plug"]["duration"]
                dirty = True

            # Expire overlay
            if cat.overlay and cat.overlay_end and now >= cat.overlay_end:
                cat.overlay = None
                cat.overlay_end = 0
                dirty = True

            # Clear bubble
            if cat.bubble and cat.bubble_end and now >= cat.bubble_end:
                cat.bubble = ""
                cat.bubble_end = 0
                if cat.mood != "sleeping":
                    cat.mood = "idle"
                dirty = True

        return dirty

    def render(self):
        out = HOME + HIDE

        if not self.cats:
            out += CLRL + "\n"
            out += DIM + "  no active sessions" + RST + CLRL + "\n"
            out += DIM + "  start claude code to wake a cat" + RST + CLRL + "\n"
            out += CLRL + "\n"
        else:
            for sid in self.cat_order:
                if sid not in self.cats:
                    continue
                cat = self.cats[sid]
                sprite = cat._resolve_sprite()
                cat_w = len(sprite[0]) if sprite else 14

                # Label info for right side
                cwd_short = os.path.basename(cat.cwd.rstrip("/")) if cat.cwd else ""
                fg = CSI + "38;5;%dm" % cat.color if cat.color else ""
                labels = [
                    fg + BOLD + (cat.bubble or cat.mood) + RST,
                    fg + cwd_short + RST if cwd_short else "",
                    DIM + cat.session_id[:16] + RST,
                ]

                for i, line in enumerate(sprite):
                    out += render_hex_line(line, color=cat.color)
                    if i < len(labels) and labels[i]:
                        out += "  " + labels[i]
                    out += CLRL + "\n"

                out += CLRL + "\n"  # separator between cats

        out += CLRB
        sys.stdout.write(out)
        sys.stdout.flush()


# ── Commands ─────────────────────────────────────────────────────────


def hook_mode():
    try:
        data = json.loads(sys.stdin.read())
        session_id = data.get("session_id", "")
        state_path = state_file_for(session_id) if session_id else STATE_FILE
        with open(state_path, "w") as f:
            json.dump(
                {
                    "event": data.get("hook_event_name", "unknown"),
                    "tool": data.get("tool_name", ""),
                    "ts": int(time.time() * 1000),
                    "session_id": session_id,
                    "cwd": data.get("cwd", ""),
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
        print("Run claude-cat in a side terminal to see your cats.")
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


def litter_mode(sprite_data=None):
    """Default mode: watch all Claude Code sessions."""
    sys.stdout.write(CLR)
    sys.stdout.flush()

    litter = Litter(sprite_data)

    def cleanup(*_):
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    while True:
        litter.scan()
        dirty = litter.tick()

        # Always render periodically (scan may find new cats)
        litter.render()

        time.sleep(0.1)


def target_mode(session_id, sprite_data=None):
    """Single-cat mode: watch one specific session."""
    sys.stdout.write(CLR)
    sys.stdout.flush()

    cat = Cat(sprite_data, session_id=session_id)

    if not os.path.exists(cat.state_file):
        with open(cat.state_file, "w") as f:
            f.write("{}")

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

        try:
            mtime = os.path.getmtime(cat.state_file)
            if mtime > cat.last_mtime:
                cat.last_mtime = mtime
                with open(cat.state_file) as f:
                    raw = f.read()
                if raw != cat.last_raw:
                    cat.last_raw = raw
                    cat.handle_event(json.loads(raw))
                    dirty = True
        except (OSError, json.JSONDecodeError):
            pass

        if (
            not cat.blinking
            and now >= cat.next_blink
            and cat.mood not in ("sleeping", "surprised", "interrupted")
        ):
            cat.blinking = True
            cat.blink_end = now + 0.15
            cat.next_blink = now + random.uniform(2, 7)
            dirty = True
        elif cat.blinking and now >= cat.blink_end:
            cat.blinking = False
            dirty = True

        if (
            not cat.blinking
            and now >= cat.next_eye_shift
            and cat.mood not in ("sleeping", "blink")
        ):
            cfg = cat.eyes_config.get(cat.mood)
            if cfg and cfg.get("frames"):
                cat.eye_frame = (cat.eye_frame + 1) % len(cfg["frames"])
                cat.next_eye_shift = now + cfg.get("ms", 2000) / 1000.0
                dirty = True
            else:
                cat.next_eye_shift = now + 2.0

        if cat.mood not in ("sleeping", "interrupted") and now - cat.last_event > 120:
            if cat.mood == "working":
                cat.mood = "interrupted"
                cat.bubble = "interrupted"
                cat.bubble_end = now + 5
            else:
                cat.mood = "sleeping"
                cat.bubble = "zzz"
                cat.bubble_end = now + 3
            cat.overlay = "plug"
            cat.overlay_end = now + OVERLAYS["plug"]["duration"]
            dirty = True

        if cat.overlay and cat.overlay_end and now >= cat.overlay_end:
            cat.overlay = None
            cat.overlay_end = 0
            dirty = True

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
    moods = ["idle", "blink", "working", "happy", "error", "sleeping", "surprised", "interrupted"]

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
        "  claude-cat                       Litter mode (all sessions)\n"
        "  claude-cat --target <session_id> Single cat for one session\n"
        "  claude-cat --sprite <name|path>  Use a custom sprite\n"
        "  claude-cat install               Set up Claude Code hooks\n"
        "  claude-cat uninstall             Remove Claude Code hooks\n"
        "  claude-cat --demo                Preview all expressions\n"
        "  claude-cat list-sprites          Show available sprites\n"
        "  claude-cat --version             Show version\n\n"
        "  /meow                            (stub) Wake a cat from Claude Code" % VERSION
    )


def main():
    args = sys.argv[1:]

    # Extract flags
    sprite_name = None
    target_session = None
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--sprite" and i + 1 < len(args):
            sprite_name = args[i + 1]
            i += 2
        elif args[i] == "--target" and i + 1 < len(args):
            target_session = args[i + 1]
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
        if target_session:
            target_mode(target_session, sprite_data)
        else:
            litter_mode(sprite_data)
    else:
        print("Unknown command: %s" % cmd)
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
