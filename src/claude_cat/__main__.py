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
    "PreCompact",
    "PostCompact",
    "PermissionRequest",
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


# ── State machine ────────────────────────────────────────────────────
#
# Three meta-states: idle, active, waiting.
# "active" has substates: thinking, reading, cooking, browsing, compacting.
# Sleeping is visual-only (label still says "idle").
#
#   EVENTS:
#     UserPromptSubmit   => active/thinking
#     PostToolUse        => active/reading|cooking|browsing (by tool)
#     SubagentStart      => active/thinking
#     PreCompact         => compacting
#     PostCompact        => active/thinking
#     Stop               => idle + reaction:happy
#     PermissionRequest  => waiting
#     PostToolUseFailure => reaction:error (state unchanged)
#
#   TIMEOUTS (in tick):
#     active/reading|cooking|browsing + 15s quiet => active/thinking
#     active/thinking + 2min quiet => idle + reaction:interrupted
#     idle + 2min => idle (cat sleeps visually, label stays "idle")
#     waiting => NEVER times out
#     compacting => NEVER times out (PostCompact ends it)
#
#   BOOT:
#     last event = PermissionRequest + < 5min => waiting
#     last event = PreCompact + < 1min => compacting
#     last event = tool + < 30s => that active substate
#     else => idle
#
#   REACTIONS (overlay on any state, don't change state):
#     happy, error, surprised, interrupted
#


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
        # State: idle | waiting | thinking | reading | cooking | browsing | compacting
        self.state = "idle"
        self.sleeping = False  # visual only, label still says "idle"
        # Reaction = brief face override from events (expires)
        self.reaction = None
        self.reaction_end = 0.0
        self.reaction_msg = ""
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
        self.last_user_prompt = ""
        self.transcript_path = ""
        self.last_transcript_read = 0.0
        self.event_count = 0

    def _read_last_message(self, transcript_path):
        """Read the last assistant or user message from transcript JSONL."""
        try:
            if not os.path.exists(transcript_path):
                return
            with open(transcript_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = min(16384, size)
                f.seek(size - chunk)
                lines = f.read().decode("utf-8", errors="ignore").strip().split("\n")

            # Scan backwards — find last assistant text AND last user text
            found_assistant = False
            found_user = False
            for line in reversed(lines):
                if found_assistant and found_user:
                    break
                try:
                    entry = json.loads(line)
                    msg_type = entry.get("type", "")
                    if not found_assistant and msg_type == "assistant":
                        text = self._extract_text(entry)
                        if text:
                            self.last_message = text
                            found_assistant = True
                    if not found_user and msg_type == "human":
                        text = self._extract_text(entry)
                        if text:
                            self.last_user_prompt = text
                            found_user = True
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            pass

    def _check_transcript_turn(self, transcript_path):
        """Check who spoke last in the transcript. Returns 'assistant' or 'human' or ''."""
        try:
            if not os.path.exists(transcript_path):
                return ""
            with open(transcript_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = min(8192, size)
                f.seek(size - chunk)
                lines = f.read().decode("utf-8", errors="ignore").strip().split("\n")
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                    t = entry.get("type", "")
                    if t in ("assistant", "human"):
                        return t
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            pass
        return ""

    @staticmethod
    def _extract_text(entry):
        """Extract first line of text from a transcript entry."""
        content = entry.get("message", {}).get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        return text.split("\n")[0]
        elif isinstance(content, str) and content.strip():
            return content.strip().split("\n")[0]
        return ""

    def _get_sprite(self):
        """Get the current sprite to display."""
        # Reaction overrides everything
        if self.reaction and self.reaction in self.reactions:
            return self.reactions[self.reaction]["frame"]

        # sleeping is visual-only (state is still "idle")
        visual_state = "sleeping" if self.state == "idle" and self.sleeping else self.state
        state_cfg = self.states.get(visual_state)
        if not state_cfg:
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

        # Wake from sleep on any event
        if self.sleeping:
            self.sleeping = False
            self.reaction = "surprised"
            self.reaction_end = time.time() + 0.5

        # Any active event clears sleeping
        self.sleeping = False

        if ev == "UserPromptSubmit":
            self.state = "thinking"
            self.frame_idx = 0
            self.next_frame = time.time() + 0.5
        elif ev == "Stop":
            self.state = "idle"
            self.reaction = "happy"
            self.reaction_end = time.time() + self.reactions.get("happy", {}).get("hold", 4.0)
            self.reaction_msg = "done!"
            self.overlay = "bulb"
            self.overlay_end = time.time() + OVERLAYS["bulb"]["duration"]
        elif ev == "PermissionRequest":
            self.state = "waiting"
        elif ev == "SubagentStop":
            # Subagent finished but parent may still be working
            self.reaction = "happy"
            self.reaction_end = time.time() + self.reactions.get("happy", {}).get("hold", 2.0)
            self.reaction_msg = "returned"
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
        elif ev == "PreCompact":
            self.state = "compacting"
            self.frame_idx = 0
        elif ev == "PostCompact":
            self.state = "thinking"
            self.frame_idx = 0

        # Try to read last message from transcript
        transcript = data.get("transcript_path", "")
        if transcript:
            self.transcript_path = transcript
            self._read_last_message(transcript)
            self.last_transcript_read = time.time()

        if tool:
            self.last_tool = tool
        self.event_count += 1
        self.last_event = time.time()

    def handle_event(self, data):
        """Process event with wake-up animation (for target mode)."""
        if self.sleeping:
            self.sleeping = False
            self.reaction = "surprised"
            self.reaction_end = time.time() + 0.5
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

        # ── Timeouts ──
        quiet = now - self.last_event
        active = self.state not in ("idle", "waiting", "compacting")

        # active/reading|cooking|browsing + 15s quiet => active/thinking
        if self.state in ("reading", "cooking", "browsing") and not self.reaction and quiet > 15:
            self.state = "thinking"
            self.frame_idx = 0
            dirty = True

        # active/thinking + 2min quiet => idle + interrupted reaction
        if self.state == "thinking" and not self.reaction and quiet > 120:
            self.reaction = "interrupted"
            self.reaction_end = now + self.reactions.get("interrupted", {}).get("hold", 10.0)
            self.reaction_msg = "interrupted"
            self.state = "idle"
            self.sleeping = False
            dirty = True

        # idle + 2min => sleeping (visual only, label stays "idle")
        if self.state == "idle" and not self.sleeping and quiet > 120:
            self.sleeping = True
            self.frame_idx = 0
            dirty = True

        # waiting => NEVER times out

        # ── Transcript refresh ──
        # waiting: scan every 3s to detect user responding
        if self.state == "waiting" and self.transcript_path and now - self.last_transcript_read > 3.0:
            turn = self._check_transcript_turn(self.transcript_path)
            if turn == "human":
                self.state = "thinking"
                self.frame_idx = 0
            self._read_last_message(self.transcript_path)
            self.last_transcript_read = now
            dirty = True

        # active: refresh every 2s for last message display
        if active and self.transcript_path and now - self.last_transcript_read > 2.0:
            self._read_last_message(self.transcript_path)
            self.last_transcript_read = now
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
                # Read metadata without triggering reactions
                try:
                    with open(path) as f:
                        data = json.loads(f.read())
                    cat.cwd = data.get("cwd", "")
                    cat.last_mtime = os.path.getmtime(path)
                    cat.last_event = os.path.getmtime(path)  # real timestamp
                    cat.last_raw = json.dumps(data)
                    # Restore state from last event + transcript
                    ev = data.get("event", "")
                    tool = data.get("tool", "")
                    age = time.time() - os.path.getmtime(path)
                    tp = data.get("transcript_path", "")
                    if tp:
                        cat.transcript_path = tp
                        cat._read_last_message(tp)
                    # Determine state
                    if ev in ("PostToolUse", "PreToolUse") and age < 30:
                        cat.state = TOOL_STATES.get(tool, "cooking")
                    elif ev == "UserPromptSubmit" and age < 30:
                        cat.state = "thinking"
                    elif ev == "PreCompact" and age < 60:
                        cat.state = "compacting"
                    elif ev == "PermissionRequest" and age < 300:
                        cat.state = "waiting"
                    else:
                        cat.state = "idle"
                    # Visual sleep if idle and stale
                    if cat.state == "idle" and age > 120:
                        cat.sleeping = True
                    # Read transcript for last message + user prompt
                    tp = data.get("transcript_path", "")
                    if tp:
                        cat.transcript_path = tp
                        cat._read_last_message(tp)
                except Exception:
                    pass
                self.cats[sid] = cat
                self.cat_order.append(sid)
        for sid in list(self.cat_order):
            if sid not in seen:
                del self.cats[sid]
                self.cat_order.remove(sid)

        # Prune stale session files (older than 24h)
        for path in files:
            try:
                if time.time() - os.path.getmtime(path) > 86400:
                    os.remove(path)
            except OSError:
                pass

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

    def _format_ago(self, elapsed):
        if elapsed < 60:
            return "%ds ago" % int(elapsed)
        elif elapsed < 3600:
            return "%dm ago" % int(elapsed / 60)
        return "%dh ago" % int(elapsed / 3600)

    def _render_cat(self, cat, now, show_dir=True):
        """Render one cat. Returns string. show_dir=False when grouped."""
        sprite = cat._get_sprite()
        fg = CSI + "38;5;%dm" % cat.color if cat.color else ""
        cwd_short = os.path.basename(cat.cwd.rstrip("/")) if cat.cwd else ""
        ago = self._format_ago(now - cat.last_event)

        state_text = fg + BOLD + cat.state + RST + "  " + DIM + ago + RST
        if cat.reaction_msg:
            state_text += "  " + CSI + "33m" + cat.reaction_msg + RST

        id_text = DIM + cat.session_id[:16] + RST
        if cat.state == "idle" and cat.last_tool:
            id_text += "  " + DIM + "last:" + cat.last_tool + RST

        raw_msg = cat.last_message or ""
        msg = ""
        if raw_msg:
            try:
                term_w = os.get_terminal_size().columns
            except OSError:
                term_w = 80
            sprite_w = len(sprite[0]) if sprite else 14
            max_msg = max(5, term_w - sprite_w - 4)
            if len(raw_msg) > max_msg:
                msg = raw_msg[:max(2, max_msg - 3)] + "..."
            else:
                msg = raw_msg

        labels = [state_text]
        if show_dir:
            labels.append(fg + cwd_short + RST if cwd_short else "")
        labels.append(id_text)
        if msg:
            labels.append(DIM + msg + RST)

        out = ""
        for i, line in enumerate(sprite):
            out += render_hex_line(line, color=cat.color)
            if i < len(labels) and labels[i]:
                out += "  " + labels[i]
            out += CLRL + "\n"
        return out

    def render(self, now=None):
        if now is None:
            now = time.time()
        out = HOME + HIDE

        # Filter valid cats
        valid = [(sid, self.cats[sid]) for sid in self.cat_order
                 if sid in self.cats and self.cats[sid].cwd]

        if not valid:
            out += CLRL + "\n"
            out += DIM + "  no active sessions" + RST + CLRL + "\n"
            out += DIM + "  start claude code to wake a cat" + RST + CLRL + "\n"
        else:
            # Group by directory
            from collections import OrderedDict
            groups = OrderedDict()
            for sid, cat in valid:
                d = cat.cwd or "unknown"
                groups.setdefault(d, []).append((sid, cat))

            for cwd, members in groups.items():
                cwd_short = os.path.basename(cwd.rstrip("/"))
                if len(members) > 1:
                    # Group header
                    base_color = members[0][1].color or 208
                    fg = CSI + "38;5;%dm" % base_color
                    try:
                        term_w = os.get_terminal_size().columns
                    except OSError:
                        term_w = 80
                    header = " " + cwd_short + " "
                    pad = max(0, term_w - len(header) - 2)
                    out += fg + DIM + "\u2500\u2500" + RST + fg + BOLD + header + RST + fg + DIM + "\u2500" * pad + RST + CLRL + "\n"
                    # Assign gradient colors within group
                    for i, (sid, cat) in enumerate(members):
                        cat.color = base_color + i  # slight shift for gradient
                        out += self._render_cat(cat, now, show_dir=False)
                        out += CLRL + "\n"
                else:
                    # Solo cat — show dir on the cat itself
                    sid, cat = members[0]
                    out += self._render_cat(cat, now, show_dir=True)
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
    import fcntl
    import termios
    import tty
    sys.stdout.write(CLR)
    sys.stdout.flush()
    litter = Litter(sprite_data)
    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    orig_fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    running = True
    def cleanup(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    try:
        tty.setcbreak(fd)
        while running:
            litter.scan()
            litter.tick()
            litter.render()
            # Non-blocking key check using select
            import select
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    ch = os.read(fd, 1).decode("utf-8", errors="ignore")
                    if ch in ("c", "C"):
                        random.shuffle(PALETTE)
                        for i, sid in enumerate(litter.cat_order):
                            if sid in litter.cats:
                                litter.cats[sid].color = PALETTE[i % len(PALETTE)]
                    elif ch in ("q", "Q", "\x03"):
                        break
                except OSError:
                    pass
            else:
                pass  # select handled the 0.1s sleep
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()


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
