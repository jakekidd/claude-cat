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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sprites as sprites_mod

VERSION = "0.2.0"
STATE_DIR = tempfile.gettempdir()
STATE_PREFIX = "claude-cat-"
STATE_FILE = os.path.join(STATE_DIR, "claude-cat.json")
HOOK_EVENTS = [
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "UserPromptSubmit",
]

BLOCKS = " \u2597\u2596\u2584\u259d\u2590\u259e\u259f\u2598\u259a\u258c\u2599\u2580\u259c\u259b\u2588"

# Tool name -> cat state
TOOL_STATES = {
    "Read": "reading",
    "Edit": "cooking",
    "Write": "cooking",
    "Bash": "cooking",
    "Grep": "reading",
    "Glob": "reading",
    "Agent": "thinking",
    "WebFetch": "browsing",
    "WebSearch": "browsing",
    "Skill": "cooking",
    "NotebookEdit": "cooking",
}

# Muted rainbow — all colors share Claude orange's warmth and saturation.
# Not neon, not pastel. Each one belongs in the same room as the orange.
PALETTE = [
    208,  # claude orange
    174,  # dusty rose
    137,  # warm tan
    143,  # sage green
    109,  # muted teal
    67,   # steel blue
    133,  # soft purple
    167,  # clay red
    179,  # muted gold
    73,   # dusty cyan
]
random.shuffle(PALETTE)

OVERLAYS = {
    "bulb": {"art": [" \u259e\u259a", " \u259c\u259b"], "duration": 3.0},
    "plug": {"art": [" \u2596\u2597", " \u259c\u259b"], "duration": 4.0},
}

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


def state_file_for(session_id):
    return os.path.join(STATE_DIR, STATE_PREFIX + session_id + ".json")


def find_session_files():
    return glob.glob(os.path.join(STATE_DIR, STATE_PREFIX + "*.json"))


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


class Cat:
    def __init__(self, sprite_data=None, session_id=None, color=None):
        if sprite_data and isinstance(sprite_data, dict) and "states" in sprite_data:
            self.states = sprite_data["states"]
            self.reactions = sprite_data.get("reactions", {})
        else:
            self.states = {}
            self.reactions = {}
        self.session_id = session_id or ""
        self.color = color
        self.cwd = ""
        self.state_file = state_file_for(session_id) if session_id else STATE_FILE
        # State = what Claude is doing (persists, animated)
        self.state = "idle"
        # Reaction = brief face override from events (expires)
        self.reaction = None
        self.reaction_end = 0.0
        self.reaction_msg = ""  # expressive message shown separately from state
        # Animation
        self.frame_idx = 0
        self.next_frame = time.time() + random.uniform(0.5, 2.0)
        self.blinking = False
        self.next_blink = time.time() + random.uniform(2, 7)
        self.blink_end = 0.0
        # Bubble (text label)
        self.bubble = ""
        self.bubble_end = 0.0
        # Overlay
        self.overlay = None
        self.overlay_end = 0.0
        # Timing
        self.last_event = time.time()
        self.last_raw = ""
        self.last_mtime = 0.0
        self.last_tool = ""
        self.last_message = ""
        self.event_count = 0

    def _read_last_message(self, transcript_path):
        """Try to read the last assistant message from the transcript JSONL."""
        try:
            if not os.path.exists(transcript_path):
                return
            # Read last few lines (tail) to find the latest assistant message
            with open(transcript_path, "rb") as f:
                # Seek to end, scan backwards for last few lines
                f.seek(0, 2)
                size = f.tell()
                # Read last 8KB at most
                chunk = min(8192, size)
                f.seek(size - chunk)
                lines = f.read().decode("utf-8", errors="ignore").strip().split("\n")

            # Scan backwards for last assistant text
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "assistant" and entry.get("message", {}).get("content"):
                        content = entry["message"]["content"]
                        # Content can be a list of blocks or a string
                        if isinstance(content, list):
                            for block in reversed(content):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "").strip()
                                    if text:
                                        # First line, truncated
                                        first = text.split("\n")[0]
                                        self.last_message = first[:60] + ("..." if len(first) > 60 else "")
                                        return
                        elif isinstance(content, str) and content.strip():
                            first = content.strip().split("\n")[0]
                            self.last_message = first[:60] + ("..." if len(first) > 60 else "")
                            return
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            pass

    def _get_sprite(self):
        """Get the current sprite to display."""
        # Reaction overrides everything
        if self.reaction and self.reaction in self.reactions:
            return self.reactions[self.reaction]["frame"]

        state_cfg = self.states.get(self.state)
        if not state_cfg:
            # waiting uses idle animation, unknown states fall back to idle
            state_cfg = self.states.get("idle", {})
        if not state_cfg:
            return []

        # Blink: use blink key if present, or frame 0 if labeled "blink"
        if self.blinking:
            if "blink" in state_cfg:
                return state_cfg["blink"]
            labels = state_cfg.get("labels", [])
            if labels and labels[0] == "blink":
                return state_cfg["frames"][0]

        frames = state_cfg.get("frames", [])
        if not frames:
            return state_cfg.get("blink", [])
        return frames[self.frame_idx % len(frames)]

    def _process_event(self, data):
        """Update state and reaction from hook event.

        State = what Claude is doing (persists, shown as label).
        Reaction = brief face + message (expires, shown separately).
        """
        ev = data.get("event", "")
        tool = data.get("tool", "")
        was_sleeping = self.state == "sleeping"

        # Wake from sleep on any event
        if was_sleeping:
            self.reaction = "surprised"
            self.reaction_end = time.time() + 0.5

        if ev == "UserPromptSubmit":
            self.state = "thinking"
            self.frame_idx = 0
            self.next_frame = time.time() + 0.5
        elif ev in ("Stop", "SubagentStop"):
            self.state = "waiting"  # done, needs user input
            self.reaction = "happy"
            self.reaction_end = time.time() + self.reactions.get("happy", {}).get("hold", 4.0)
            self.reaction_msg = "done!" if ev == "Stop" else "returned"
            self.overlay = "bulb"
            self.overlay_end = time.time() + OVERLAYS["bulb"]["duration"]
        elif ev == "PostToolUseFailure":
            self.reaction = "error"
            self.reaction_end = time.time() + self.reactions.get("error", {}).get("hold", 4.0)
            self.reaction_msg = "oops"
        elif ev in ("PostToolUse", "PreToolUse"):
            new_state = TOOL_STATES.get(tool, "cooking")
            if new_state != self.state:
                self.state = new_state
                self.frame_idx = 0
                self.next_frame = time.time() + 0.5
        elif ev == "SubagentStart":
            self.state = "thinking"
            self.frame_idx = 0

        # Try to read last message from transcript
        transcript = data.get("transcript_path", "")
        if transcript:
            self._read_last_message(transcript)

        if tool:
            self.last_tool = tool
        self.event_count += 1
        self.last_event = time.time()

    def handle_event(self, data):
        """Process event with wake-up animation (for target mode)."""
        if self.state == "sleeping":
            self.reaction = "surprised"
            self.reaction_end = time.time() + 0.5
            self.state = "idle"
            self.render()
            time.sleep(0.4)
        self._process_event(data)
        self.render()

    def tick(self, now):
        """Advance animation timers. Returns True if display changed."""
        dirty = False

        # Expire reaction
        if self.reaction and now >= self.reaction_end:
            self.reaction = None
            self.reaction_msg = ""
            dirty = True

        # Advance state animation frame
        state_cfg = self.states.get(self.state, {})
        frames = state_cfg.get("frames", [])
        mode = state_cfg.get("mode", "shuffle")
        ms = state_cfg.get("ms", 2000)

        if not self.reaction and not self.blinking and frames and now >= self.next_frame:
            labels = state_cfg.get("labels", [])
            # Skip blink frame (idx 0) during shuffle — blink timer handles it
            skip_blink = labels and labels[0] == "blink"
            start = 1 if skip_blink else 0
            if mode == "loop":
                self.frame_idx = (self.frame_idx + 1) % len(frames)
                if skip_blink and self.frame_idx == 0:
                    self.frame_idx = 1
            elif mode == "shuffle" and len(frames) > start:
                self.frame_idx = random.randint(start, len(frames) - 1)
            self.next_frame = now + ms / 1000.0
            dirty = True

        # Blink
        if (
            not self.blinking
            and now >= self.next_blink
            and not self.reaction
        ):
            self.blinking = True
            self.blink_end = now + 0.15
            self.next_blink = now + random.uniform(2, 7)
            dirty = True
        elif self.blinking and now >= self.blink_end:
            self.blinking = False
            dirty = True

        # Active state quiet for 15s -> drop to thinking (still processing)
        if self.state in ("reading", "cooking", "browsing") and now - self.last_event > 15:
            self.state = "thinking"
            self.frame_idx = 0
            dirty = True

        # Idle/waiting for 2 min -> sleeping
        if self.state in ("idle", "waiting") and not self.reaction and now - self.last_event > 120:
            self.state = "sleeping"
            self.frame_idx = 0
            self.overlay = "plug"
            self.overlay_end = now + OVERLAYS["plug"]["duration"]
            dirty = True

        # Was working, went quiet for 2 min -> interrupted then idle
        if self.state not in ("idle", "waiting", "sleeping") and not self.reaction and now - self.last_event > 120:
            self.reaction = "interrupted"
            self.reaction_end = now + self.reactions.get("interrupted", {}).get("hold", 10.0)
            self.reaction_msg = "interrupted"
            self.state = "idle"
            self.overlay = "plug"
            self.overlay_end = now + OVERLAYS["plug"]["duration"]
            dirty = True

        # Expire overlay
        if self.overlay and self.overlay_end and now >= self.overlay_end:
            self.overlay = None
            self.overlay_end = 0
            dirty = True

        # Expire bubble (legacy, kept for target mode speech bubble)
        if self.bubble and self.bubble_end and now >= self.bubble_end:
            self.bubble = ""
            self.bubble_end = 0
            dirty = True

        return dirty

    def render(self):
        """Full render for target mode."""
        sprite = self._get_sprite()
        cat_w = len(sprite[0]) if sprite else 14

        out = HOME + HIDE

        bubble_text = self.reaction_msg or ""
        if bubble_text:
            inner = " " + bubble_text + " "
            horiz = "\u2500" * len(inner)
            pad = " " * max(0, (cat_w - len(inner) - 2) // 2)
            out += pad + DIM + "\u256d" + horiz + "\u256e" + RST + CLRL + "\n"
            out += pad + DIM + "\u2502" + RST + inner + DIM + "\u2502" + RST + CLRL + "\n"
            out += pad + DIM + "\u2570" + horiz + "\u256f" + RST + CLRL + "\n"
        else:
            out += CLRL + "\n" + CLRL + "\n" + CLRL + "\n"

        for line in sprite:
            out += render_hex_line(line, color=self.color) + CLRL + "\n"

        if self.overlay and self.overlay in OVERLAYS:
            ov = OVERLAYS[self.overlay]
            for i, art_line in enumerate(ov["art"]):
                r = 3 + i
                c = cat_w + 1
                if r > 0:
                    out += CSI + "%d;%dH" % (r, c) + BOLD + art_line + RST

        out += CLRL + "\n" + DIM + self.state + RST + CLRL + "\n" + CLRB
        sys.stdout.write(out)
        sys.stdout.flush()


# ── Litter ───────────────────────────────────────────────────────────


class Litter:
    def __init__(self, sprite_data):
        self.sprite_data = sprite_data
        self.cats = {}
        self.cat_order = []
        self.color_idx = 0

    def _next_color(self):
        c = PALETTE[self.color_idx % len(PALETTE)]
        self.color_idx += 1
        return c

    def scan(self):
        files = find_session_files()
        seen = set()
        for path in files:
            basename = os.path.basename(path)
            sid = basename[len(STATE_PREFIX) : -len(".json")]
            seen.add(sid)
            if sid not in self.cats:
                cat = Cat(self.sprite_data, session_id=sid, color=self._next_color())
                cat.state_file = path
                self.cats[sid] = cat
                self.cat_order.append(sid)
        for sid in list(self.cat_order):
            if sid not in seen:
                del self.cats[sid]
                self.cat_order.remove(sid)

    def tick(self):
        now = time.time()
        dirty = False
        for cat in self.cats.values():
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
            if cat.tick(now):
                dirty = True
        return dirty

    def render(self, now=None):
        if now is None:
            now = time.time()
        out = HOME + HIDE
        if not self.cats:
            out += CLRL + "\n"
            out += DIM + "  no active sessions" + RST + CLRL + "\n"
            out += DIM + "  start claude code to wake a cat" + RST + CLRL + "\n"
        else:
            for sid in self.cat_order:
                if sid not in self.cats:
                    continue
                cat = self.cats[sid]
                sprite = cat._get_sprite()
                fg = CSI + "38;5;%dm" % cat.color if cat.color else ""
                cwd_short = os.path.basename(cat.cwd.rstrip("/")) if cat.cwd else ""
                # Time since last activity
                elapsed = now - cat.last_event
                if elapsed < 60:
                    ago = "%ds ago" % int(elapsed)
                elif elapsed < 3600:
                    ago = "%dm ago" % int(elapsed / 60)
                else:
                    ago = "%dh ago" % int(elapsed / 3600)
                # Line 0: state + time ago + optional reaction message
                state_text = fg + BOLD + cat.state + RST + "  " + DIM + ago + RST
                if cat.reaction_msg:
                    state_text += "  " + CSI + "33m" + cat.reaction_msg + RST
                # Line 1: project dir
                line1 = fg + cwd_short + RST if cwd_short else ""
                # Line 2: session ID + last tool
                id_text = DIM + cat.session_id[:16] + RST
                if cat.state in ("idle", "waiting", "sleeping") and cat.last_tool:
                    id_text += "  " + DIM + "last:" + cat.last_tool + RST
                # Line 3: last message
                msg = ""
                if cat.last_message:
                    max_w = 40
                    msg = cat.last_message[:max_w]
                    if len(cat.last_message) > max_w:
                        msg += "..."
                labels = [
                    state_text,
                    line1,
                    id_text,
                    DIM + msg + RST if msg else "",
                ]
                for i, line in enumerate(sprite):
                    out += render_hex_line(line, color=cat.color)
                    if i < len(labels) and labels[i]:
                        out += "  " + labels[i]
                    out += CLRL + "\n"
                out += CLRL + "\n"
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
                    "transcript_path": data.get("transcript_path", ""),
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
            rules.append({"matcher": "", "hooks": [{"type": "command", "command": "claude-cat --hook", "async": True, "timeout": 5}]})
            added += 1
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    if added:
        print("Installed %d hook(s) in %s" % (added, settings_path))
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
        hooks[event] = [r for r in hooks[event] if not any("claude-cat" in h.get("command", "") for h in r.get("hooks", []))]
        removed += before - len(hooks[event])
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    print("Removed %d hook(s) from %s" % (removed, settings_path))


def litter_mode(sprite_data=None):
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
        litter.tick()
        litter.render()
        time.sleep(0.1)


def target_mode(session_id, sprite_data=None):
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
        if cat.tick(now):
            dirty = True
        if dirty:
            cat.render()
        time.sleep(0.1)


def demo_mode(sprite_data=None):
    sys.stdout.write(CLR)
    sys.stdout.flush()
    cat = Cat(sprite_data)
    all_states = list((sprite_data or {}).get("states", {}).keys())
    all_reactions = list((sprite_data or {}).get("reactions", {}).keys())
    def cleanup(*_):
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    for s in all_states:
        cat.state = s
        cat.reaction = None
        cat.bubble = s
        cat.frame_idx = 0
        cat.render()
        time.sleep(1.5)
    for r in all_reactions:
        cat.reaction = r
        cat.bubble = r
        cat.render()
        time.sleep(1.5)
    cleanup()


def tmux_ccm_mode():
    """Launch tmux with CCM on top and clat on bottom."""
    import shutil
    import subprocess
    if not shutil.which("tmux"):
        print("tmux not found. Install tmux first.")
        sys.exit(1)
    ccm = shutil.which("ccm") or shutil.which("claude-monitor") or shutil.which("claude-code-monitor")
    clat = shutil.which("clat") or shutil.which("claude-cat")
    if not ccm:
        print("Claude Code Monitor not found (ccm/claude-monitor). Install it first:")
        print("  pip install claude-monitor")
        sys.exit(1)
    if not clat:
        print("claude-cat not in PATH. Run: pip install -e .")
        sys.exit(1)
    # Create tmux session with CCM on top, clat on bottom
    session = "claude-dashboard"
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, ccm])
    subprocess.run(["tmux", "split-window", "-v", "-t", session, clat])
    subprocess.run(["tmux", "select-pane", "-t", session + ":0.0"])  # focus CCM pane
    subprocess.run(["tmux", "attach", "-t", session])


def print_help():
    print(
        "claude-cat v%s\n"
        "A 1-bit companion cat for Claude Code\n\n"
        "Usage:\n"
        "  claude-cat                       Litter mode (all sessions)\n"
        "  claude-cat --target <session_id> Single cat for one session\n"
        "  claude-cat --tmux-ccm            Dashboard: CCM + litter in tmux\n"
        "  claude-cat --sprite <name|path>  Use a custom sprite\n"
        "  claude-cat install               Set up Claude Code hooks\n"
        "  claude-cat uninstall             Remove Claude Code hooks\n"
        "  claude-cat --demo                Preview all states + reactions\n"
        "  claude-cat list-sprites          Show available sprites\n"
        "  claude-cat --version             Show version" % VERSION
    )


def main():
    args = sys.argv[1:]
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
    sprite_data = None
    if cmd in ("", "--watch", "watch", "--demo", "demo"):
        sprite_data = sprites_mod.load(sprite_name)
    if cmd == "--tmux-ccm":
        tmux_ccm_mode()
    elif cmd in ("--hook", "hook"):
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
