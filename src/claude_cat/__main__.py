#!/usr/bin/env python3
"""claude-cat -- a 1-bit companion cat for Claude Code."""

import glob
import json
import os
import random
import re
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sprites as sprites_mod

VERSION = "0.3.0"
DEBUG = False
TRACE = False  # Dense state machine trace logging (--trace flag)

# Logging: always-on per-cat + combined litter log in ~/.claude-cat/logs/
LOG_DIR = os.path.join(Path.home(), ".claude-cat", "logs")
_litter_log = None
_trace_log = None  # trace.jsonl — one JSON object per state change
_cat_logs = {}  # session_id -> file handle
_cat_last_log = {}  # session_id -> last log line (for UI)
_log_t0 = 0.0
MAX_LITTER_LOG = 1_000_000  # rotate litter.log above 1MB


def _init_logging():
    global _litter_log, _trace_log, _log_t0
    os.makedirs(LOG_DIR, exist_ok=True)
    litter_path = os.path.join(LOG_DIR, "litter.log")
    # Rotate if too large
    try:
        if os.path.exists(litter_path) and os.path.getsize(litter_path) > MAX_LITTER_LOG:
            prev = litter_path + ".prev"
            if os.path.exists(prev):
                os.remove(prev)
            os.rename(litter_path, prev)
    except OSError:
        pass
    _litter_log = open(litter_path, "a")
    _log_t0 = time.time()
    # Trace log (--trace): one JSON per line, machine-parseable state changes
    if TRACE:
        trace_path = os.path.join(LOG_DIR, "trace.jsonl")
        try:
            if os.path.exists(trace_path) and os.path.getsize(trace_path) > MAX_LITTER_LOG * 2:
                prev = trace_path + ".prev"
                if os.path.exists(prev):
                    os.remove(prev)
                os.rename(trace_path, prev)
        except OSError:
            pass
        _trace_log = open(trace_path, "a")


def _trace(sid, trigger_type, trigger_detail, state_before, state_after, **extra):
    """Write a trace entry. Only active when --trace is set."""
    if not _trace_log:
        return
    try:
        entry = {
            "t": round(time.time(), 3),
            "sid": sid[:8],
            "trigger": trigger_type,
            "detail": trigger_detail[:200],
            "before": state_before,
            "after": state_after,
        }
        if extra:
            entry.update(extra)
        _trace_log.write(json.dumps(entry) + "\n")
        _trace_log.flush()
    except Exception:
        pass


def _cat_log_handle(session_id):
    if session_id not in _cat_logs:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, session_id + ".log")
        _cat_logs[session_id] = open(path, "a")
    return _cat_logs[session_id]


def _log(msg, *args):
    """Write to litter log (always). If msg starts with [sid], also write to per-cat log."""
    if not _litter_log:
        return
    try:
        elapsed = time.time() - _log_t0
        line = "+%07.2f  %s" % (elapsed, msg % args if args else msg)
        _litter_log.write(line + "\n")
        _litter_log.flush()
        # Extract session_id prefix like [4a2abe2c] and write to per-cat log
        if line and "[" in line:
            import re
            m = re.search(r"\[([0-9a-f]{8})\]", line)
            if m:
                short = m.group(1)
                full_sid = _sid_map.get(short)
                if full_sid:
                    fh = _cat_log_handle(full_sid)
                    fh.write(line + "\n")
                    fh.flush()
                    # Store stripped line for UI — skip noisy routine lines
                    content = line.split("  ", 1)[1] if "  " in line else line
                    skip = ("stats refresh", "reaction expired", "cleared permission dot")
                    if not any(s in content for s in skip):
                        _cat_last_log[full_sid] = content
        if DEBUG:
            sys.stderr.write(line + "\n")
    except Exception:
        pass


def _log_cat(session_id, msg, *args):
    """Write to both litter log and a specific cat's log. Use when session_id is known."""
    if not _litter_log:
        return
    try:
        elapsed = time.time() - _log_t0
        short = session_id[:8]
        text = msg % args if args else msg
        line = "+%07.2f  [%s] %s" % (elapsed, short, text)
        _litter_log.write(line + "\n")
        _litter_log.flush()
        fh = _cat_log_handle(session_id)
        fh.write(line + "\n")
        fh.flush()
        _cat_last_log[session_id] = "[%s] %s" % (short, text)
        if DEBUG:
            sys.stderr.write(line + "\n")
    except Exception:
        pass


_sid_map = {}  # short (8-char) -> full session_id


def _register_cat_log(session_id):
    """Register a session_id so _log can route [sid_short] lines to per-cat logs."""
    _sid_map[session_id[:8]] = session_id
    _cat_log_handle(session_id)  # open file handle eagerly


def _close_logging():
    global _litter_log
    for fh in _cat_logs.values():
        try:
            fh.close()
        except Exception:
            pass
    _cat_logs.clear()
    if _litter_log:
        try:
            _litter_log.close()
        except Exception:
            pass
        _litter_log = None


def cat_last_log(session_id):
    """Get the last log line for a cat (for UI display)."""
    return _cat_last_log.get(session_id, "")


def _load_graveyard():
    """Load graveyard from graveyard.json (only cats that actually died).
    Deduplicates by name, keeping the highest-token entry per name."""
    entries = []
    try:
        with open(GRAVEYARD_FILE) as f:
            entries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # Deduplicate by name, keep highest tokens per name
    best = {}
    for e in entries:
        name = e.get("name", "")
        if name not in best or e.get("tokens", 0) > best[name].get("tokens", 0):
            best[name] = e
    entries = sorted(best.values(), key=lambda t: t.get("tokens", 0), reverse=True)
    return entries[:GRAVEYARD_MAX]


def _save_graveyard(graveyard):
    """Write graveyard to disk. Keeps top GRAVEYARD_MAX by tokens."""
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    top = sorted(graveyard, key=lambda t: t.get("tokens", 0), reverse=True)[:GRAVEYARD_MAX]
    tmp = GRAVEYARD_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(top, f, separators=(",", ":"))
    os.replace(tmp, GRAVEYARD_FILE)


STATE_DIR = os.path.join(Path.home(), ".claude-cat", "state")
STATE_PREFIX = "claude-cat-"
STATE_FILE = os.path.join(STATE_DIR, "claude-cat.json")

# Registry: persistent cat identity (name, color) across restarts
REGISTRY_DIR = os.path.join(Path.home(), ".claude-cat")
REGISTRY_FILE = os.path.join(REGISTRY_DIR, "registry.json")
REGISTRY_MAX_AGE = 30 * 86400  # prune after 30 days
GRAVEYARD_FILE = os.path.join(REGISTRY_DIR, "graveyard.json")
GRAVEYARD_MAX = 5


def _load_registry():
    """Load registry from disk. Returns dict of session_id -> {name, color, last_seen}."""
    try:
        with open(REGISTRY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_registry(reg):
    """Write registry to disk."""
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    tmp = REGISTRY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, separators=(",", ":"))
    os.replace(tmp, REGISTRY_FILE)


def _prune_registry(reg):
    """Remove entries older than REGISTRY_MAX_AGE. Also prunes stale log files. Mutates and returns reg."""
    cutoff = time.time() - REGISTRY_MAX_AGE
    stale = [sid for sid, v in reg.items() if v.get("last_seen", 0) < cutoff]
    for sid in stale:
        del reg[sid]
        # Clean up per-cat log file
        log_path = os.path.join(LOG_DIR, sid + ".log")
        try:
            if os.path.exists(log_path):
                os.remove(log_path)
        except OSError:
            pass
    return reg


_registry = {}
_registry_dirty = False
_registry_last_flush = 0.0


def registry_lookup(session_id):
    """Get (name, color) for session_id. Creates entry if new."""
    global _registry, _registry_dirty
    if not _registry:
        _registry = _prune_registry(_load_registry())
        _registry_dirty = True
    entry = _registry.get(session_id)
    if entry:
        return entry["name"], entry["color"]
    # Re-check disk: another process (clat code wrapper) may have registered it
    disk_reg = _load_registry()
    disk_entry = disk_reg.get(session_id)
    if disk_entry:
        _registry[session_id] = disk_entry
        return disk_entry["name"], disk_entry["color"]
    # New session: generate name, pick a color not already in use
    name = cat_name(session_id)
    used_colors = {e.get("color") for e in _registry.values()}
    available = [c for c in PALETTE if c not in used_colors]
    if available:
        # Pick deterministically from available colors
        import hashlib
        h = int(hashlib.md5(session_id.encode()).hexdigest()[8:16], 16)
        color = available[h % len(available)]
    else:
        # All colors used, fall back to deterministic
        color = cat_color(session_id)
    _registry[session_id] = {"name": name, "color": color, "last_seen": time.time()}
    _registry_dirty = True
    return name, color


def registry_set_color(session_id, color):
    """Update stored color for a session."""
    global _registry_dirty
    if session_id in _registry:
        _registry[session_id]["color"] = color
        _registry_dirty = True


def registry_set_name(session_id, name):
    """Override the display name for a session (from --name flag)."""
    global _registry_dirty
    if session_id in _registry:
        _registry[session_id]["name"] = name
        _registry_dirty = True


def registry_set_wrapped(session_id, wrapped=True):
    """Mark a session as wrapped (launched via clat code)."""
    global _registry_dirty
    if session_id in _registry:
        _registry[session_id]["wrapped"] = wrapped
        _registry_dirty = True


def registry_is_wrapped(session_id):
    """Check if a session was launched via clat code."""
    entry = _registry.get(session_id, {})
    return entry.get("wrapped", False)


def registry_get_approve_mode(session_id):
    """Get approve mode for a session: 'manual' (default), 'guarded', or 'automatic'."""
    entry = _registry.get(session_id, {})
    return entry.get("approve_mode", "manual")


def registry_set_approve_mode(session_id, mode):
    """Set approve mode for a session."""
    global _registry_dirty
    if session_id in _registry and mode in ("manual", "guarded", "automatic"):
        _registry[session_id]["approve_mode"] = mode
        _registry_dirty = True


# Guarded mode: commands/patterns that require manual approval even within repo
GUARDED_BLACKLIST = (
    "rm -rf", "rm -r /", "kill ", "pkill ", "killall ",
    "git push --force", "git push -f", "git reset --hard",
    "chmod ", "chown ", "curl ", "wget ",
    "sudo ", "su ", "eval ", "> /dev/",
)


def _is_guarded_safe(tool, tool_input, cwd):
    """Check if a tool call is safe under guarded mode (within repo, not blacklisted)."""
    if tool in ("Read", "Grep", "Glob", "Agent"):
        return True  # read-only or delegation tools always safe
    if tool == "Bash":
        cmd = tool_input.get("command", "")
        # Check blacklist
        for pat in GUARDED_BLACKLIST:
            if pat in cmd:
                return False
        # Check if command references paths outside cwd
        # Simple heuristic: if absolute paths appear that aren't under cwd
        import shlex
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split()
        for part in parts:
            if part.startswith("/") and cwd and not part.startswith(cwd):
                return False
        return True
    if tool in ("Edit", "Write"):
        path = tool_input.get("file_path", "")
        if path and cwd and not path.startswith(cwd):
            return False
        return True
    # Unknown tools: ask
    return False


def registry_touch(session_id):
    """Bump last_seen for a session."""
    global _registry_dirty
    if session_id in _registry:
        _registry[session_id]["last_seen"] = time.time()
        _registry_dirty = True


def registry_update_stats(session_id, tokens, turns, duration, project):
    """Store latest stats on a registry entry for graveyard history."""
    global _registry_dirty
    if session_id in _registry:
        entry = _registry[session_id]
        entry["tokens"] = tokens
        entry["turns"] = turns
        entry["duration"] = duration
        entry["project"] = project
        _registry_dirty = True


def registry_flush():
    """Write registry to disk if dirty. Debounced to avoid thrashing."""
    global _registry_dirty, _registry_last_flush
    if not _registry_dirty:
        return
    now = time.time()
    if now - _registry_last_flush < 5.0:
        return
    _save_registry(_registry)
    _registry_dirty = False
    _registry_last_flush = now


def registry_flush_force():
    """Write registry to disk unconditionally."""
    global _registry_dirty, _registry_last_flush
    if _registry_dirty:
        _save_registry(_registry)
        _registry_dirty = False
        _registry_last_flush = time.time()
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
    "SessionEnd",
]

BLOCKS = " \u2597\u2596\u2584\u259d\u2590\u259e\u259f\u2598\u259a\u258c\u2599\u2580\u259c\u259b\u2588"

# Stdout spinner chars emitted by Claude Code during thinking
SPINNER_CHARS = set("\u00b7\u273b\u273d\u2736\u2733\u2722")  # ·✻✽✶✳✢
_ANSI_RE = re.compile(r'\x1b\[[^m]*m')

# Adjacency map for idle gaze drift (used in Cat.tick)
_NEIGHBORS = {
    "center": ["up", "down", "left", "right"],
    "up": ["center", "up-left", "up-right"],
    "down": ["center", "down-left", "down-right"],
    "left": ["center", "up-left", "down-left"],
    "right": ["center", "up-right", "down-right"],
    "up-left": ["up", "left", "center"],
    "up-right": ["up", "right", "center"],
    "down-left": ["down", "left", "center"],
    "down-right": ["down", "right", "center"],
}
# Error patterns in Claude Code stdout
_ERROR_PATTERNS = (b"API Error", b"Rate limit", b"Request too large", b"Overloaded")

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

OVERLAYS = {
    "bulb": {"art": [" \u259e\u259a", " \u259c\u259b"], "duration": 3.0},
    "plug": {"art": [" \u2596\u2597", " \u259c\u259b"], "duration": 4.0},
}

# Vertical block elements for context bar (1/8 to 8/8 fill)
CTX_BLOCKS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

# Cat name generator — deterministic from session_id
_NAME_ADJ = [
    "fuzzy", "sleepy", "sneaky", "rowdy", "dusty", "chunky", "peppy", "scrappy",
    "zippy", "wily", "toasty", "rusty", "plucky", "muggy", "grumpy", "perky",
    "lanky", "pudgy", "feisty", "dinky", "snappy", "gritty", "spunky", "nifty",
    "cranky", "frisky", "chewy", "dizzy", "lumpy", "salty", "husky", "breezy",
]
_NAME_NOUN = [
    "beans", "mochi", "pixel", "toast", "gizmo", "nacho", "biscuit", "waffle",
    "pickle", "nugget", "turnip", "pretzel", "sprout", "muffin", "crumble", "tater",
    "dumpling", "noodle", "radish", "cobbler", "truffle", "pepper", "rascal", "clover",
    "pebble", "thistle", "widget", "morsel", "crouton", "cheddar", "brisket", "juniper",
]


def cat_name(session_id):
    """Deterministic cat name from session_id. Uses md5 for cross-process stability."""
    import hashlib
    h = int(hashlib.md5(session_id.encode()).hexdigest()[:8], 16)
    return _NAME_ADJ[h % len(_NAME_ADJ)] + " " + _NAME_NOUN[(h >> 8) % len(_NAME_NOUN)]


_NAME_ADJ_SET = set(_NAME_ADJ)
_NAME_NOUN_SET = set(_NAME_NOUN)


def is_generated_name(name):
    """Check if a name was auto-generated (adj + noun from word lists)."""
    parts = name.split(" ")
    if len(parts) == 2 and parts[0] in _NAME_ADJ_SET and parts[1] in _NAME_NOUN_SET:
        return True
    # Also check hyphenated form (sanitized names)
    parts = name.split("-")
    if len(parts) == 2 and parts[0] in _NAME_ADJ_SET and parts[1] in _NAME_NOUN_SET:
        return True
    return False


def cat_color(session_id):
    """Deterministic color from session_id."""
    import hashlib
    h = int(hashlib.md5(session_id.encode()).hexdigest()[8:16], 16)
    return PALETTE[h % len(PALETTE)]


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
# Three states:
#   IDLE       — not doing anything. Sleeping is visual-only after 10min.
#   ACTIVE     — Claude is working. Substates: thinking, reading, cooking, browsing.
#   COMPACTING — separate because it never times out.
#
# Orange dot: PermissionRequest sets a cosmetic indicator, cleared on next state change.
#
#   EVENTS:
#     UserPromptSubmit   => active/thinking
#     PostToolUse        => active/reading|cooking|browsing (by tool)
#     SubagentStart      => active/thinking
#     PreCompact         => compacting
#     PostCompact        => active/thinking
#     Stop               => idle + reaction:happy
#     PermissionRequest  => orange dot (cosmetic)
#     PostToolUseFailure => reaction:error (state unchanged)
#     SessionEnd         => dead
#
#   TIMEOUTS (in tick):
#     active/reading|cooking|browsing + 15s quiet => active/thinking
#     active/thinking + 2min quiet => idle + reaction:interrupted
#     idle + 10min => sleeping (visual only, label stays "idle")
#     compacting => NEVER times out
#
#   LIFECYCLE:
#     state file age > 1hr => dead
#     transcript file gone => dead
#     dead => 30s death display => remove state file + cat
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
        if session_id:
            reg_name, reg_color = registry_lookup(session_id)
            self.name = reg_name
            self.color = color if color is not None else reg_color
        else:
            self.name = ""
            self.color = color if color is not None else 208
        self.cwd = ""
        self.project_dir = ""
        self.state_file = state_file_for(session_id) if session_id else STATE_FILE
        # State: idle | thinking | reading | cooking | browsing | compacting
        self.state = "idle"
        self.sleeping = False  # visual only, label still says "idle"
        self.permission_pending = False  # orange dot indicator
        self.permission_tool = ""  # tool name for pending permission
        self.permission_input = {}  # tool_input for pending permission
        self.flashing = False  # meow flash (5s color cycling)
        self.flash_end = 0.0
        self.last_wrapper_ts = 0.0  # last WrapperState event timestamp
        self.subagent_depth = 0    # number of active subagents
        # Stdout tee parsing state (litter-side)
        self.out_file = os.path.join(STATE_DIR, STATE_PREFIX + session_id + ".out") if session_id else ""
        self.last_out_mtime = 0.0
        self.last_out_content = ""
        self.last_spinner_ts = 0.0
        self.spinner_active = False
        self.pending_idle = False
        self.pending_idle_ts = 0.0
        self.thought_seconds = 0
        # Pending question (planning questions with numbered options)
        self.pending_question = None  # {type, text, options} or None
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
        self.blinks_since_long = 0  # escalating long-blink probability
        # Idle gaze: occasionally hold a direction or drift to neighbors
        self.gaze_hold = 0  # remaining ticks to hold current direction
        # Overlay
        self.overlay = None
        self.overlay_end = 0.0
        # Timing
        self.last_event = time.time()
        self.last_raw = ""
        self.last_mtime = 0.0
        self.last_tool = ""
        self.last_message = ""
        self.transcript_path = ""
        # Session stats (from transcript)
        self.total_input = 0
        self.total_output = 0
        self.total_cache = 0
        self.context_k = 0
        self.compactions = 0
        self.human_turns = 0
        self.session_start = 0.0  # timestamp of first transcript entry
        self.model = ""  # e.g. "claude-opus-4-6"
        self.stats_read = False
        self.last_transcript_read = 0.0
        self._last_stats_read = 0.0
        self.event_count = 0
        # Lifecycle
        self.dead = False
        self.dead_since = 0.0
        self.death_reason = ""  # "ended" (SessionEnd) or "killed" (stale/gone)

    def _read_last_message(self, transcript_path):
        """Read the last assistant message from transcript JSONL."""
        try:
            if not os.path.exists(transcript_path):
                return
            with open(transcript_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = min(16384, size)
                f.seek(size - chunk)
                lines = f.read().decode("utf-8", errors="ignore").strip().split("\n")

            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "assistant":
                        text = self._extract_text(entry)
                        if text:
                            self.last_message = text
                            return
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            pass

    def _check_error_tail(self, transcript_path):
        """Check if the last entry in transcript is a system error."""
        try:
            if not transcript_path or not os.path.exists(transcript_path):
                return False
            with open(transcript_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                chunk = min(4096, size)
                f.seek(size - chunk)
                lines = f.read().decode("utf-8", errors="ignore").strip().split("\n")
            # Check last few entries for error types
            for line in reversed(lines[-5:]):
                try:
                    entry = json.loads(line)
                    t = entry.get("type", "").lower()
                    if t in ("error", "api_error"):
                        return True
                    # Check for error in message content
                    msg = entry.get("message", "")
                    if isinstance(msg, str) and "api error" in msg.lower():
                        return True
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            pass
        return False

    def _check_waiting(self, transcript_path):
        """Check if the session is waiting for user input.

        Returns a dict with question info, or None if not waiting.
        Returns: {type: "question", text: str, options: [str, ...]} or None.
        """
        try:
            if not transcript_path or not os.path.exists(transcript_path):
                return None
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
                    if t in ("human", "user"):
                        return None
                    if t == "assistant":
                        text = self._extract_text(entry)
                        if not text:
                            continue
                        text = text.rstrip()
                        # Check for numbered options at line starts (1. ... 2. ...)
                        # Must have at least "1." and "2." at start of lines to avoid
                        # false positives from prose like "Step 1. do X"
                        if re.search(r"^\s*1\.", text, re.MULTILINE) and \
                           re.search(r"^\s*2\.", text, re.MULTILINE):
                            return self._parse_question(text)
                        # Check for question ending with ?
                        if text.endswith("?"):
                            return {"type": "question", "text": text[-500:], "options": []}
                        return None
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            pass
        return None

    def _parse_question(self, text):
        """Extract question text and numbered options from assistant message."""
        lines = text.split("\n")
        question_lines = []
        options = []
        current_option = []
        current_num = None
        for line in lines:
            m = re.match(r"^\s*(\d+)\.\s+(.+)", line)
            if m:
                if current_num is not None and current_option:
                    options.append("%d. %s" % (current_num, " ".join(current_option)))
                current_num = int(m.group(1))
                current_option = [m.group(2).strip()]
            elif current_num is not None:
                stripped = line.strip()
                if stripped:
                    current_option.append(stripped)
                elif current_option:
                    options.append("%d. %s" % (current_num, " ".join(current_option)))
                    current_num = None
                    current_option = []
            else:
                stripped = line.strip()
                if stripped:
                    question_lines.append(stripped)
        if current_num is not None and current_option:
            options.append("%d. %s" % (current_num, " ".join(current_option)))
        question_text = "\n".join(question_lines[-6:]) if question_lines else ""
        return {"type": "question", "text": question_text, "options": options}

    def _read_stats(self, transcript_path):
        """Sum token usage from transcript for session cost/context display."""
        import datetime
        try:
            if not os.path.exists(transcript_path):
                return
            total_in = 0
            total_out = 0
            total_cache = 0
            last_ctx = 0
            compactions = 0
            human_turns = 0
            first_ts = 0.0
            with open(transcript_path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        t = entry.get("type", "")
                        # Capture first entry timestamp for session age
                        if not first_ts:
                            ts = entry.get("timestamp")
                            if ts:
                                if isinstance(ts, str):
                                    try:
                                        dt = datetime.datetime.fromisoformat(
                                            ts.replace("Z", "+00:00")
                                        )
                                        first_ts = dt.timestamp()
                                    except (ValueError, AttributeError):
                                        pass
                                elif isinstance(ts, (int, float)):
                                    first_ts = ts / 1000 if ts > 1e12 else ts
                        if t in ("human", "user"):
                            human_turns += 1
                        model = entry.get("message", {}).get("model", "")
                        if model:
                            self.model = model
                        usage = entry.get("message", {}).get("usage", {})
                        if usage:
                            total_in += usage.get("input_tokens", 0)
                            total_out += usage.get("output_tokens", 0)
                            total_cache += usage.get("cache_read_input_tokens", 0)
                            last_ctx = (
                                usage.get("input_tokens", 0)
                                + usage.get("cache_read_input_tokens", 0)
                                + usage.get("cache_creation_input_tokens", 0)
                            )
                        if t == "summary":
                            compactions += 1
                    except (json.JSONDecodeError, KeyError):
                        continue
            self.total_input = total_in
            self.total_output = total_out
            self.total_cache = total_cache
            self.context_k = last_ctx // 1000
            self.compactions = compactions
            self.human_turns = human_turns
            if first_ts:
                self.session_start = first_ts
            self.stats_read = True
        except Exception:
            pass

    def est_cost(self):
        """Rough cost estimate using Opus pricing."""
        # input $15/M, output $75/M, cache read $1.5/M
        return (
            self.total_input * 15 / 1_000_000
            + self.total_output * 75 / 1_000_000
            + self.total_cache * 1.5 / 1_000_000
        )

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
        sid_short = self.session_id[:8]
        old_state = self.state

        _log("[%s] event: %s%s  state=%s", sid_short, ev,
             " tool=%s" % tool if tool else "", old_state)

        # Wake from sleep on meaningful events (not stale SubagentStop/PostToolUseFailure)
        wake_events = ("UserPromptSubmit", "PostToolUse", "SubagentStart", "PreCompact", "Stop")
        if self.sleeping and ev in wake_events:
            self.sleeping = False
            self.reaction = "surprised"
            self.reaction_end = time.time() + 0.5
            _log("[%s] woke from sleep -> reaction:surprised", sid_short)
        elif ev in wake_events:
            self.sleeping = False

        # Clear permission/question state on any non-PermissionRequest event
        if ev != "PermissionRequest":
            if self.permission_pending:
                _log("[%s] cleared permission dot", sid_short)
            self.permission_pending = False
            self.permission_tool = ""
            self.permission_input = {}
            self.pending_question = None

        if ev == "UserPromptSubmit":
            self.state = "thinking"
            self.subagent_depth = 0  # reset on new turn
            self.frame_idx = 0
            self.next_frame = time.time() + 0.5
        elif ev == "Stop":
            self.state = "idle"
            tp = data.get("transcript_path", "") or self.transcript_path
            if self._check_error_tail(tp):
                self.reaction = "error"
                self.reaction_end = time.time() + self.reactions.get("error", {}).get("hold", 4.0)
                self.reaction_msg = "crashed"
                _log("[%s] Stop with error tail -> reaction:error/crashed", sid_short)
            else:
                waiting = self._check_waiting(tp)
                if waiting:
                    self.permission_pending = True
                    self.pending_question = waiting
                    _log("[%s] Stop with question -> permission dot (waiting, %d options)",
                         sid_short, len(waiting.get("options", [])))
                else:
                    self.reaction = "happy"
                    self.reaction_end = time.time() + self.reactions.get("happy", {}).get("hold", 4.0)
                    self.reaction_msg = "done!"
                    self.overlay = "bulb"
                    self.overlay_end = time.time() + OVERLAYS["bulb"]["duration"]
        elif ev == "PermissionRequest":
            if tool == "AskUserQuestion":
                # Not a permission — it's a question needing typed input in the session window
                self.state = "idle"
                self.reaction = "surprised"
                self.reaction_end = time.time() + 8.0
                self.reaction_msg = "asking..."
                _log("[%s] AskUserQuestion -> idle/asking (answer in session window)", sid_short)
            elif self.subagent_depth > 0:
                # Subagent permission — can't route response to subagent via main stdin
                _log("[%s] subagent permission (depth=%d) tool=%s — skipping prompt", sid_short, self.subagent_depth, tool)
            else:
                # Check auto-approve mode
                mode = registry_get_approve_mode(self.session_id)
                if mode == "automatic":
                    # Auto-approve: write "2" (Always) to response file
                    try:
                        resp_path = os.path.join(STATE_DIR, STATE_PREFIX + self.session_id + "-response")
                        with open(resp_path, "w") as f:
                            f.write("2")
                    except OSError:
                        pass
                    _log("[%s] auto-approved (automatic mode) tool=%s", sid_short, tool)
                elif mode == "guarded" and _is_guarded_safe(tool, data.get("tool_input", {}), self.cwd):
                    # Guarded: safe operation, auto-approve
                    try:
                        resp_path = os.path.join(STATE_DIR, STATE_PREFIX + self.session_id + "-response")
                        with open(resp_path, "w") as f:
                            f.write("1")
                    except OSError:
                        pass
                    _log("[%s] guarded-approved tool=%s", sid_short, tool)
                else:
                    self.permission_pending = True
                    self.permission_tool = tool
                    self.permission_input = data.get("tool_input", {})
                    _log("[%s] permission dot ON tool=%s", sid_short, tool)
        elif ev == "SessionEnd":
            self.dead = True
            self.dead_since = time.time()
            self.death_reason = "ended"
            self.reaction = "error"
            self.reaction_end = time.time() + 3.0
            self.reaction_msg = ""
            _log("[%s] SessionEnd -> dead", sid_short)
        elif ev == "SubagentStop":
            self.subagent_depth = max(0, self.subagent_depth - 1)
            _log("[%s] SubagentStop depth=%d", sid_short, self.subagent_depth)
            # Only react if cat is actively working (not idle/sleeping)
            if old_state not in ("idle", "compacting"):
                self.reaction = "happy"
                self.reaction_end = time.time() + self.reactions.get("happy", {}).get("hold", 2.0)
                self.reaction_msg = "returned"
        elif ev == "PostToolUseFailure":
            self.reaction = "error"
            self.reaction_end = time.time() + self.reactions.get("error", {}).get("hold", 4.0)
            self.reaction_msg = "womp womp"
        elif ev == "PostToolUse":
            new_state = TOOL_STATES.get(tool, "cooking")
            if new_state != self.state:
                self.state = new_state
                self.frame_idx = 0
                self.next_frame = time.time() + 0.5
        elif ev == "SubagentStart":
            self.subagent_depth += 1
            self.state = "thinking"
            self.frame_idx = 0
            _log("[%s] SubagentStart depth=%d", sid_short, self.subagent_depth)
        elif ev == "PreCompact":
            self.state = "compacting"
            self.frame_idx = 0
        elif ev == "PostCompact":
            self.state = "thinking"
            self.frame_idx = 0
        elif ev == "Interrupted":
            self.state = "idle"
            self.sleeping = False
            self.reaction = "interrupted"
            self.reaction_end = time.time() + self.reactions.get("interrupted", {}).get("hold", 7.0)
            self.reaction_msg = "interrupted"
            _log("[%s] Interrupted event -> idle/interrupted", sid_short)
        elif ev == "WrapperState":
            ws = data.get("wrapper_state", "")
            self.last_wrapper_ts = time.time()
            if ws == "interrupted":
                self.state = "idle"
                self.sleeping = False
                self.reaction = "interrupted"
                self.reaction_end = time.time() + self.reactions.get("interrupted", {}).get("hold", 7.0)
                self.reaction_msg = "interrupted"
            _log("[%s] WrapperState: %s", sid_short, ws)
        elif ev == "Meow":
            self.flashing = True
            self.flash_end = time.time() + 5.0
            self.reaction = "happy"
            self.reaction_end = time.time() + 5.0
            self.reaction_msg = "meow!"
            _log("[%s] Meow -> flashing for 5s", sid_short)

        if self.state != old_state:
            _log("[%s] state: %s -> %s  (trigger: %s)", sid_short, old_state, self.state, ev)
            _trace(self.session_id, "hook", "%s/%s" % (ev, tool), old_state, self.state,
                   reaction=self.reaction_msg or "")
        if self.reaction and self.reaction_msg:
            _log("[%s] reaction: %s msg=%s", sid_short, self.reaction, self.reaction_msg)

        # Try to read last message from transcript
        transcript = data.get("transcript_path", "")
        if transcript:
            self.transcript_path = transcript
            if not self.project_dir:
                self.project_dir = project_dir_from_transcript(transcript)
            self._read_last_message(transcript)
            self.last_transcript_read = time.time()

        if tool:
            self.last_tool = tool
        self.event_count += 1
        self.last_event = time.time()

    def handle_event(self, data):
        """Process event with wake-up animation (for target mode)."""
        ev = data.get("event", "")
        wake_events = ("UserPromptSubmit", "PostToolUse", "SubagentStart", "PreCompact", "Stop")
        if self.sleeping and ev in wake_events:
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
            _log("[%s] reaction expired: %s", self.session_id[:8], self.reaction)
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
                # Idle gaze: hold direction, then drift to neighbor
                if self.gaze_hold > 0:
                    self.gaze_hold -= 1
                    # Stay on current frame (hold the look)
                else:
                    cur_label = labels[self.frame_idx] if self.frame_idx < len(labels) else ""
                    neighbors = _NEIGHBORS.get(cur_label)
                    if labels and neighbors and random.random() < 0.65:
                        # Drift to a neighbor direction
                        target = random.choice(neighbors)
                        if target in labels:
                            self.frame_idx = labels.index(target)
                    else:
                        # Pure random jump
                        self.frame_idx = random.randint(start, len(frames) - 1)
                    # 25% chance to hold this direction for 1-3 extra ticks
                    if random.random() < 0.25:
                        self.gaze_hold = random.randint(1, 3)
            self.next_frame = now + ms / 1000.0
            dirty = True

        # Blink — escalating long-blink probability
        if (
            not self.blinking
            and now >= self.next_blink
            and not self.reaction
        ):
            self.blinking = True
            # Long blink: P = blinks_since_long * 0.15, guaranteed by 7th
            if random.random() < self.blinks_since_long * 0.15:
                self.blink_end = now + 0.30  # long blink
                self.blinks_since_long = 0
            else:
                self.blink_end = now + 0.15  # normal blink
                self.blinks_since_long += 1
            self.next_blink = now + random.uniform(2, 7)
            dirty = True
        elif self.blinking and now >= self.blink_end:
            self.blinking = False
            dirty = True

        # ── Timeouts ──
        quiet = now - self.last_event
        active = self.state not in ("idle", "compacting")
        is_wrapped = registry_is_wrapped(self.session_id) if self.session_id else False

        # Timeout-based state transitions only for unwrapped sessions.
        # Wrapped sessions get state from stdout parsing (WrapperState events).
        if not is_wrapped:
            # active/reading|cooking|browsing + 15s quiet => active/thinking
            if self.state in ("reading", "cooking", "browsing") and not self.reaction and quiet > 15:
                old_s = self.state
                _log("[%s] timeout: %s -> thinking (%.0fs quiet)", self.session_id[:8], self.state, quiet)
                self.state = "thinking"
                self.frame_idx = 0
                dirty = True
                _trace(self.session_id, "timeout", "15s_quiet", old_s, "thinking", quiet=round(quiet, 1))

            # active/thinking + 45s quiet => idle + interrupted reaction
            if self.state == "thinking" and not self.reaction and quiet > 45:
                _log("[%s] timeout: thinking -> idle/interrupted (%.0fs quiet)", self.session_id[:8], quiet)
                self.reaction = "interrupted"
                self.reaction_end = now + self.reactions.get("interrupted", {}).get("hold", 7.0)
                self.reaction_msg = "interrupted"
                self.state = "idle"
                self.sleeping = False
                dirty = True
                _trace(self.session_id, "timeout", "45s_quiet", "thinking", "idle", quiet=round(quiet, 1))

        # idle + 10min => sleeping (visual only, applies to all sessions)
        # Guard: don't sleep if we just set a reaction (e.g. interrupted)
        if self.state == "idle" and not self.sleeping and not self.reaction and quiet > 600:
            _log("[%s] timeout: idle -> sleeping (%.0fs quiet)", self.session_id[:8], quiet)
            self.sleeping = True
            _trace(self.session_id, "timeout", "600s_quiet", "idle", "sleeping", quiet=round(quiet, 1))
            self.frame_idx = 0
            dirty = True

        # ── Transcript refresh ──
        # active: refresh every 2s for last message display
        if active and self.transcript_path and now - self.last_transcript_read > 2.0:
            self._read_last_message(self.transcript_path)
            # Refresh stats every 30s (full file scan is heavier)
            if not self.stats_read or now - self._last_stats_read > 30:
                self._read_stats(self.transcript_path)
                self._last_stats_read = now
                _log("[%s] stats refresh: %dk ctx, $%.2f, %d turns",
                     self.session_id[:8], self.context_k, self.est_cost(), self.human_turns)
                # Persist stats to registry for graveyard history
                if self.stats_read and self.session_id:
                    dur = now - self.session_start if self.session_start else 0
                    proj = os.path.basename((self.project_dir or self.cwd or "").rstrip("/"))
                    total = self.total_input + self.total_output + self.total_cache
                    registry_update_stats(self.session_id, total, self.human_turns, dur, proj)
            self.last_transcript_read = now
            dirty = True

        # Expire flash
        if self.flashing and now >= self.flash_end:
            self.flashing = False
            dirty = True

        # Expire overlay
        if self.overlay and self.overlay_end and now >= self.overlay_end:
            self.overlay = None
            self.overlay_end = 0
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
        self.graveyard = _load_graveyard()
        self.prompt_queue = []  # [{session_id, name, color, tool, input, ts}]
        # Cat selector state
        self.selected_idx = 0       # index into visible wrapped cats
        self.input_mode = False     # typing into input buffer
        self.input_buffer = ""      # accumulated text for input mode
        self.input_target_sid = ""  # session_id receiving input

    def scan(self):
        files = find_session_files()
        seen = set()
        now = time.time()
        for path in files:
            basename = os.path.basename(path)
            sid = basename[len(STATE_PREFIX) : -len(".json")]
            seen.add(sid)
            if sid not in self.cats:
                # Ancient state files (>24h): just delete, don't create a cat
                try:
                    age = now - os.path.getmtime(path)
                except OSError:
                    continue
                if age > 86400:
                    _log("[scan] pruned ancient state file: %s (%.0fh old)", sid[:8], age / 3600)
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue

                cat = Cat(self.sprite_data, session_id=sid)
                cat.state_file = path
                _register_cat_log(sid)
                try:
                    with open(path) as f:
                        data = json.loads(f.read())
                    cat.cwd = data.get("cwd", "")
                    cat.last_mtime = os.path.getmtime(path)
                    cat.last_event = os.path.getmtime(path)
                    cat.last_raw = json.dumps(data)
                    ev = data.get("event", "")
                    tool = data.get("tool", "")
                    tp = data.get("transcript_path", "")
                    if tp:
                        cat.transcript_path = tp
                        cat.project_dir = project_dir_from_transcript(tp)
                        cat._read_last_message(tp)
                        cat._read_stats(tp)
                        cat._last_stats_read = now
                    # Boot state
                    if ev == "SessionEnd":
                        cat.dead = True
                        cat.dead_since = now
                        cat.death_reason = "ended"
                        _log("[scan] new cat %s: dead (SessionEnd)", sid[:8])
                    elif ev == "PreCompact" and age < 60:
                        cat.state = "compacting"
                        _log("[scan] new cat %s: compacting (PreCompact %.0fs ago)", sid[:8], age)
                    elif ev == "PostToolUse" and age < 30:
                        cat.state = TOOL_STATES.get(tool, "cooking")
                        _log("[scan] new cat %s: %s (PostToolUse/%s %.0fs ago)", sid[:8], cat.state, tool, age)
                    elif ev == "UserPromptSubmit":
                        # No age gate: UserPromptSubmit without a Stop means still thinking.
                        # The timeout fallback (45s) handles stale cases.
                        cat.state = "thinking"
                        _log("[scan] new cat %s: thinking (UserPromptSubmit %.0fs ago)", sid[:8], age)
                    else:
                        cat.state = "idle"
                        _log("[scan] new cat %s: idle (last_ev=%s %.0fs ago)", sid[:8], ev, age)
                    if cat.state == "idle" and age > 600:
                        cat.sleeping = True
                    # Lifecycle: dead if stale >1hr
                    if not cat.dead and age > 3600:
                        cat.dead = True
                        cat.dead_since = now
                        cat.death_reason = "killed"
                        _log("[scan] %s: dead (stale %.0fh)", sid[:8], age / 3600)
                    # Lifecycle: dead if transcript gone
                    if not cat.dead and tp and not os.path.exists(tp):
                        cat.dead = True
                        cat.dead_since = now
                        cat.death_reason = "killed"
                        _log("[scan] %s: dead (transcript gone)", sid[:8])
                except Exception:
                    pass
                self.cats[sid] = cat
                self.cat_order.append(sid)
            else:
                # Existing cat: check lifecycle
                cat = self.cats[sid]
                if not cat.dead:
                    try:
                        age = now - os.path.getmtime(cat.state_file)
                        if age > 3600:
                            cat.dead = True
                            cat.dead_since = now
                            cat.death_reason = "killed"
                            cat.reaction = "error"
                            cat.reaction_end = now + 3.0
                            _log("[lifecycle] %s: dead (stale %.0fh)", sid[:8], age / 3600)
                        elif cat.transcript_path and not os.path.exists(cat.transcript_path):
                            cat.dead = True
                            cat.dead_since = now
                            cat.death_reason = "killed"
                            cat.reaction = "error"
                            cat.reaction_end = now + 3.0
                            _log("[lifecycle] %s: dead (transcript gone)", sid[:8])
                    except OSError:
                        cat.dead = True
                        cat.dead_since = now
                        cat.death_reason = "killed"
                        cat.reaction = "error"
                        cat.reaction_end = now + 3.0
                        _log("[lifecycle] %s: dead (state file OSError)", sid[:8])

        # Bump registry last_seen for alive cats
        for sid, cat in self.cats.items():
            if not cat.dead:
                registry_touch(sid)

        # Remove cats whose state files are gone
        for sid in list(self.cat_order):
            if sid not in seen:
                if sid in self.cats:
                    del self.cats[sid]
                self.cat_order.remove(sid)

        # Clean up dead cats after 30s death display -> graveyard
        for sid in list(self.cat_order):
            if sid not in self.cats:
                continue
            cat = self.cats[sid]
            if cat.dead and cat.dead_since and now - cat.dead_since > 30:
                _log("[cleanup] removing dead cat %s (dead %.0fs) -> graveyard", sid[:8], now - cat.dead_since)
                # Capture tombstone
                duration = 0.0
                if cat.session_start:
                    duration = cat.dead_since - cat.session_start
                total_tok = cat.total_input + cat.total_output + cat.total_cache
                tomb = {
                    "name": cat.name,
                    "color": cat.color,
                    "tokens": total_tok,
                    "turns": cat.human_turns,
                    "duration": duration,
                    "project": os.path.basename((cat.project_dir or cat.cwd or "").rstrip("/")),
                }
                # Replace existing entry with same name if new one has more tokens
                replaced = False
                for i, existing in enumerate(self.graveyard):
                    if existing.get("name") == cat.name:
                        if total_tok > existing.get("tokens", 0):
                            self.graveyard[i] = tomb
                        replaced = True
                        break
                if not replaced:
                    self.graveyard.append(tomb)
                self.graveyard.sort(key=lambda t: t.get("tokens", 0), reverse=True)
                self.graveyard = self.graveyard[:GRAVEYARD_MAX]
                _save_graveyard(self.graveyard)
                try:
                    os.remove(cat.state_file)
                except OSError:
                    pass
                del self.cats[sid]
                self.cat_order.remove(sid)

    def tick(self):
        now = time.time()
        dirty = False
        for cat in self.cats.values():
            if cat.dead:
                continue
            try:
                mtime = os.path.getmtime(cat.state_file)
                if mtime > cat.last_mtime:
                    cat.last_mtime = mtime
                    with open(cat.state_file) as f:
                        raw = f.read()
                    if raw != cat.last_raw:
                        cat.last_raw = raw
                        data = json.loads(raw)
                        cat.cwd = data.get("cwd") or cat.cwd
                        _log("[tick] state file changed for %s, processing event", cat.session_id[:8])
                        cat._process_event(data)
                        dirty = True
            except (OSError, json.JSONDecodeError):
                pass
            if cat.tick(now):
                dirty = True
        # ── Stdout tee parsing (wrapped sessions only) ──
        for cat in self.cats.values():
            if cat.dead or not cat.out_file or not registry_is_wrapped(cat.session_id):
                continue
            try:
                mtime = os.path.getmtime(cat.out_file)
                if mtime <= cat.last_out_mtime:
                    continue
                cat.last_out_mtime = mtime
                with open(cat.out_file) as f:
                    content = f.read()
                if content == cat.last_out_content:
                    continue
                # Extract new text (delta from last read)
                old_len = len(cat.last_out_content)
                if old_len and content.startswith(cat.last_out_content[:min(old_len, 256)]):
                    new_text = content[old_len:]
                elif not cat.last_out_content:
                    # First read — skip if file is stale (>30s), else use last 500 chars
                    if now - mtime > 30:
                        cat.last_out_content = content
                        continue
                    new_text = content[-500:] if len(content) > 500 else content
                else:
                    # Buffer wrapped — skip this tick (can't compute reliable delta)
                    cat.last_out_content = content
                    continue
                cat.last_out_content = content

                # Spinner detection
                if SPINNER_CHARS & set(new_text):
                    cat.last_spinner_ts = now
                    cat.pending_idle = False
                    if not cat.spinner_active:
                        cat.spinner_active = True
                        old_s = cat.state
                        if cat.state != "thinking":
                            cat.state = "thinking"
                            cat.sleeping = False
                            cat.frame_idx = 0
                            cat.last_wrapper_ts = now
                            dirty = True
                            _log("[stdout] %s -> thinking (spinner)", cat.session_id[:8])
                            _trace(cat.session_id, "stdout", "spinner_start", old_s, "thinking")

                # Error detection — brief grumpy face, no state change
                for pat_s in ("API Error", "Rate limit", "Request too large", "Overloaded"):
                    if pat_s in new_text:
                        cat.reaction = "error"
                        cat.reaction_end = now + 2.0
                        cat.reaction_msg = pat_s.lower().replace(" ", "_")
                        dirty = True
                        _log("[stdout] %s error: %s", cat.session_id[:8], pat_s)
                        break

                # Compaction detection — match spinner char + "Compacting" (Claude Code's format)
                if re.search(r"[·✻✽✶✳✢]\s*Compacting", new_text):
                    if cat.state != "compacting":
                        old_s = cat.state
                        cat.state = "compacting"
                        cat.frame_idx = 0
                        cat.last_wrapper_ts = now
                        dirty = True
                        _log("[stdout] %s -> compacting (spinner+Compacting)", cat.session_id[:8])
                        _trace(cat.session_id, "stdout", "spinner+Compacting", old_s, "compacting")

                # "Thought for Ns" detection
                m = re.search(r"Thought for (\d+)s", content[-200:])
                if m:
                    cat.thought_seconds = int(m.group(1))

            except (OSError, ValueError):
                pass

        # Spinner stop -> idle (requires BOTH: no spinners for 3s AND no hook events for 5s)
        # This prevents spurious idle transitions between tool calls where hooks keep firing
        for cat in self.cats.values():
            if cat.dead or not registry_is_wrapped(cat.session_id):
                continue
            if cat.spinner_active and cat.last_spinner_ts and now - cat.last_spinner_ts > 3.0:
                cat.spinner_active = False
                if not cat.pending_idle:
                    cat.pending_idle = True
                    cat.pending_idle_ts = now
            # Cancel pending idle if a hook event arrived recently (tool use in progress)
            if cat.pending_idle and now - cat.last_event < 5.0:
                cat.pending_idle = False
            if cat.pending_idle and now - cat.pending_idle_ts > 3.0:
                cat.pending_idle = False
                old_s = cat.state
                if cat.state != "idle":
                    cat.state = "idle"
                    cat.last_wrapper_ts = now
                    _trace(cat.session_id, "timeout", "spinner_stop+hook_quiet",
                           old_s, "idle", spinner_silence=round(now - cat.last_spinner_ts, 1),
                           hook_silence=round(now - cat.last_event, 1))
                    if cat.thought_seconds:
                        cat.reaction = "happy"
                        cat.reaction_end = now + 4.0
                        cat.reaction_msg = "thought %ds" % cat.thought_seconds
                        cat.thought_seconds = 0
                    else:
                        cat.reaction = "happy"
                        cat.reaction_end = now + 4.0
                        cat.reaction_msg = "done!"
                    cat.overlay = "bulb"
                    cat.overlay_end = now + 3.0
                    dirty = True
                    _log("[stdout] %s -> idle", cat.session_id[:8])

        # Sync cat names from registry (picks up renames from wrap/--rename)
        if not hasattr(self, "_last_name_sync") or now - self._last_name_sync > 10:
            self._last_name_sync = now
            disk_reg = _load_registry()
            # Merge disk into in-memory _registry so flush won't overwrite
            for sid, disk_entry in disk_reg.items():
                if sid in _registry:
                    for key in ("name", "color", "wrapped"):
                        dv = disk_entry.get(key)
                        if dv is not None and dv != _registry[sid].get(key):
                            _registry[sid][key] = dv
                else:
                    _registry[sid] = disk_entry
            for cat in self.cats.values():
                entry = _registry.get(cat.session_id, {})
                disk_name = entry.get("name", "")
                if disk_name and disk_name != cat.name:
                    _log("[sync] %s name: %s -> %s", cat.session_id[:8], cat.name, disk_name)
                    cat.name = disk_name
        # Update prompt queue from cat permission states
        self._update_prompt_queue()
        return dirty

    def _update_prompt_queue(self):
        """Sync prompt queue with cat permission/question states."""
        now = time.time()
        active_sids = set()
        for cat in self.cats.values():
            if not cat.permission_pending:
                continue
            active_sids.add(cat.session_id)
            if any(p["session_id"] == cat.session_id for p in self.prompt_queue):
                continue
            if cat.pending_question:
                # Planning question with numbered options
                self.prompt_queue.append({
                    "session_id": cat.session_id,
                    "name": cat.name,
                    "color": cat.color,
                    "type": "question",
                    "text": cat.pending_question.get("text", ""),
                    "options": cat.pending_question.get("options", []),
                    "tool": "",
                    "input": {},
                    "ts": now,
                })
            elif cat.permission_tool:
                # Permission prompt (Y/A/N)
                self.prompt_queue.append({
                    "session_id": cat.session_id,
                    "name": cat.name,
                    "color": cat.color,
                    "type": "permission",
                    "tool": cat.permission_tool,
                    "input": cat.permission_input,
                    "ts": now,
                })
        self.prompt_queue = [
            p for p in self.prompt_queue
            if p["session_id"] in active_sids and now - p["ts"] < 120
        ]

    def _format_ago(self, elapsed):
        if elapsed < 60:
            return "%ds ago" % int(elapsed)
        elif elapsed < 3600:
            return "%dm ago" % int(elapsed / 60)
        return "%dh ago" % int(elapsed / 3600)

    def _format_duration(self, seconds):
        """Format duration as compact string: 5m, 1h 23m, 2d 5h."""
        if seconds < 60:
            return "%ds" % int(seconds)
        elif seconds < 3600:
            return "%dm" % int(seconds / 60)
        elif seconds < 86400:
            h = int(seconds / 3600)
            m = int((seconds % 3600) / 60)
            return "%dh %02dm" % (h, m) if m else "%dh" % h
        else:
            d = int(seconds / 86400)
            h = int((seconds % 86400) / 3600)
            return "%dd %dh" % (d, h) if h else "%dd" % d

    def _context_bar(self, cat, height):
        """Build vertical context % bar (one char per sprite row).

        Shows remaining context capacity. Full bar = plenty left, empty = running out.
        Uses cat's own color. Auto-detects 1M context models.
        """
        if not cat.stats_read or cat.context_k <= 0:
            return ["  "] * height

        # Detect context window from model
        if "opus" in cat.model and ("4-6" in cat.model or "1m" in cat.model.lower()):
            ctx_max = 1000.0  # Opus 4.6 = 1M context
        elif "opus" in cat.model:
            ctx_max = 200.0
        elif "sonnet" in cat.model:
            ctx_max = 200.0
        elif "haiku" in cat.model:
            ctx_max = 200.0
        else:
            # Fallback: if context exceeds 200k, assume 1M model
            ctx_max = 1000.0 if cat.context_k > 200 else 200.0
        pct_used = min(1.0, cat.context_k / ctx_max)
        remaining = max(0.0, 1.0 - pct_used)

        fg = CSI + "38;5;%dm" % cat.color if cat.color else ""

        fill = remaining * height
        full_rows = int(fill)
        partial = fill - full_rows

        bar = []
        for row in range(height):
            row_from_bottom = height - 1 - row
            if row_from_bottom < full_rows:
                bar.append(fg + "\u2588" + RST + " ")
            elif row_from_bottom == full_rows and partial > 0.0625:
                level = min(8, max(1, int(partial * 8)))
                bar.append(fg + CTX_BLOCKS[level] + RST + " ")
            else:
                bar.append("  ")
        return bar

    def _render_status_bar(self, valid, now):
        """Compact burn rate / cost rate / prediction bar at the top."""
        import datetime
        total_cost = 0.0
        total_tok = 0
        earliest_start = now
        alive_count = 0
        for sid, cat in valid:
            if cat.dead or not cat.stats_read:
                continue
            total_cost += cat.est_cost()
            total_tok += cat.total_input + cat.total_output + cat.total_cache
            if cat.session_start and cat.session_start < earliest_start:
                earliest_start = cat.session_start
            alive_count += 1
        if not alive_count or earliest_start >= now:
            return ""
        elapsed_min = max(1, (now - earliest_start) / 60)
        tok_per_min = total_tok / elapsed_min
        cost_per_min = total_cost / elapsed_min

        # Color coding for burn rate
        if tok_per_min > 500:
            rate_color = CSI + "38;5;167m"  # clay red (hot)
        elif tok_per_min > 200:
            rate_color = CSI + "38;5;179m"  # muted gold (warm)
        else:
            rate_color = CSI + "38;5;109m"  # muted teal (cool)

        # Tokens burned today: alive cats + today's graveyard deaths
        today_tok = total_tok
        today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        for tomb in self.graveyard:
            # Graveyard entries don't have timestamps, so just include alive totals
            pass
        if today_tok >= 1_000_000:
            today_s = "%.1fM" % (today_tok / 1_000_000)
        elif today_tok >= 1000:
            today_s = "%dk" % (today_tok // 1000)
        else:
            today_s = "%d" % today_tok

        # Format: "12.4M today  28k tok/m  $0.05/m  $59.79 total"
        parts = []
        parts.append(BOLD + today_s + RST + DIM + " today" + RST)
        if tok_per_min >= 1000:
            tok_s = "%dk" % (tok_per_min // 1000)
        else:
            tok_s = "%d" % tok_per_min
        parts.append(rate_color + BOLD + tok_s + RST + DIM + " tok/m" + RST)
        parts.append(rate_color + BOLD + "$%.2f" % cost_per_min + RST + DIM + "/m" + RST)
        parts.append(DIM + "$%.2f total" % total_cost + RST)

        # Runout prediction (assume 5h window, ~$50 opus limit rough guess)
        # Simple: just show session age and cost trajectory
        elapsed_h = elapsed_min / 60
        if elapsed_h > 0.05:  # at least 3 min of data
            cost_per_h = total_cost / elapsed_h
            if cost_per_h > 0:
                # Rough API limit: use 5h window from earliest session
                reset_time = earliest_start + 5 * 3600
                reset_dt = datetime.datetime.fromtimestamp(reset_time)
                reset_str = reset_dt.strftime("%-I:%M%p").lower()
                parts.append(DIM + "reset " + RST + CSI + "38;5;109m" + reset_str + RST)

        try:
            term_w = os.get_terminal_size().columns
        except OSError:
            term_w = 80

        bar = "  ".join(parts)
        return bar + CLRL + "\n"

    def _format_prompt_content(self, prompt):
        """Format tool_input into displayable lines."""
        tool = prompt.get("tool", "")
        inp = prompt.get("input", {})
        lines = []
        if tool == "Bash":
            cmd = inp.get("command", "")
            desc = inp.get("description", "")
            lines.append("Bash command")
            if desc:
                lines.append("  " + desc)
            for l in cmd.split("\n"):
                lines.append("  " + l)
        elif tool in ("Read", "Edit", "Write"):
            fp = inp.get("file_path", "")
            lines.append("%s  %s" % (tool, fp))
            if tool == "Edit":
                old = inp.get("old_string", "")
                if old:
                    lines.append("  replacing:")
                    for l in old.split("\n")[:5]:
                        lines.append("    " + l)
        elif tool == "WebFetch":
            lines.append("WebFetch  " + inp.get("url", ""))
        elif tool == "WebSearch":
            lines.append("WebSearch  " + inp.get("query", ""))
        else:
            lines.append(tool)
            for k, v in inp.items():
                if isinstance(v, str) and v:
                    lines.append("  %s: %s" % (k, v[:80]))
        return lines if lines else [tool or "unknown tool"]

    def _center_truncate(self, lines, max_lines):
        """Truncate lines from the center, keeping top and bottom."""
        if len(lines) <= max_lines:
            return lines
        if max_lines < 3:
            return lines[:max_lines]
        # Split space: top gets slightly more than bottom
        top_n = (max_lines - 1) // 2 + (max_lines - 1) % 2
        bot_n = (max_lines - 1) // 2
        result = lines[:top_n]
        result.append("  ...")
        result.extend(lines[-bot_n:] if bot_n > 0 else [])
        return result

    PROMPT_LINES = 20  # constant allocation for prompt area (never shifts)

    def _render_prompt_widget(self, now):
        """Render the interaction area. Always PROMPT_LINES tall (constant allocation)."""
        try:
            term_w = os.get_terminal_size().columns
        except OSError:
            term_w = 80

        total = self.PROMPT_LINES

        if self.input_mode:
            return self._render_input_widget(term_w, total)

        if not self.prompt_queue:
            return (CLRL + "\n") * total

        prompt = self.prompt_queue[0]
        ptype = prompt.get("type", "permission")

        if ptype == "question":
            return self._render_question_widget(prompt, term_w, total)
        else:
            return self._render_permission_widget(prompt, term_w, total)

    def _render_input_widget(self, term_w, total):
        """Render text input mode."""
        target_cat = self.cats.get(self.input_target_sid)
        target_name = target_cat.name if target_cat else self.input_target_sid[:16]
        target_color = target_cat.color if target_cat else 208
        tfg = CSI + "38;5;%dm" % target_color
        out = ""
        header = " send to %s " % target_name
        pad = max(0, term_w - len(header) - 2)
        out += tfg + DIM + "\u2500\u2500" + RST + tfg + BOLD + header + RST + tfg + DIM + "\u2500" * pad + RST + CLRL + "\n"
        cursor = "\u2588"
        buf_display = self.input_buffer
        max_buf = term_w - 6
        if len(buf_display) > max_buf:
            buf_display = buf_display[-(max_buf - 1):]
        out += "  > " + buf_display + cursor + CLRL + "\n"
        for _ in range(total - 3):
            out += CLRL + "\n"
        out += "  " + DIM + "enter=send  esc=cancel" + RST + CLRL + "\n"
        return out

    def _render_permission_widget(self, prompt, term_w, total):
        """Render Y/A/N permission prompt."""
        fg = CSI + "38;5;%dm" % prompt["color"] if prompt["color"] else ""
        name = prompt["name"]
        queue_info = " (%d pending)" % len(self.prompt_queue) if len(self.prompt_queue) > 1 else ""
        out = ""
        # Header
        header = " %s wants to run%s " % (name, queue_info)
        pad = max(0, term_w - len(header) - 2)
        out += fg + DIM + "\u2500\u2500" + RST + fg + BOLD + header + RST + fg + DIM + "\u2500" * pad + RST + CLRL + "\n"
        # Content (6 lines for command details)
        content_lines = 6
        raw_lines = self._format_prompt_content(prompt)
        trimmed = []
        for l in raw_lines:
            if len(l) > term_w - 4:
                trimmed.append(l[:term_w - 7] + "...")
            else:
                trimmed.append(l)
        display = self._center_truncate(trimmed, content_lines)
        for i in range(content_lines):
            if i < len(display):
                out += "  " + DIM + display[i] + RST + CLRL + "\n"
            else:
                out += CLRL + "\n"
        # Pad middle
        used = 1 + content_lines + 2  # header + content + options + pad
        for _ in range(total - used):
            out += CLRL + "\n"
        # Options
        out += "  " + CSI + "32m" + BOLD + "> [enter]=Always" + RST + "  [Y]es  [N]o" + CLRL + "\n"
        out += CLRL + "\n"
        return out

    def _render_question_widget(self, prompt, term_w, total):
        """Render planning question with numbered options."""
        fg = CSI + "38;5;%dm" % prompt["color"] if prompt["color"] else ""
        name = prompt["name"]
        options = prompt.get("options", [])
        question_text = prompt.get("text", "")
        out = ""
        # Header
        header = " %s is asking " % name
        pad = max(0, term_w - len(header) - 2)
        out += fg + DIM + "\u2500\u2500" + RST + fg + BOLD + header + RST + fg + DIM + "\u2500" * pad + RST + CLRL + "\n"
        lines_used = 1  # header
        # Question text (up to 6 lines)
        max_question = 6
        q_lines = question_text.split("\n") if question_text else []
        q_display = []
        for l in q_lines:
            if len(l) > term_w - 4:
                q_display.append(l[:term_w - 7] + "...")
            else:
                q_display.append(l)
        if len(q_display) > max_question:
            q_display = q_display[:max_question - 1] + ["..."]
        for l in q_display:
            out += "  " + l + CLRL + "\n"
            lines_used += 1
        # Pad to fill max_question lines
        for _ in range(max_question - len(q_display)):
            out += CLRL + "\n"
            lines_used += 1
        # Options area: divide remaining space equally among options
        # Reserve 1 line for controls at bottom
        options_area = total - lines_used - 1
        if options:
            lines_per = max(1, options_area // len(options))
            for i, opt in enumerate(options):
                # Truncate option to allocated lines
                opt_lines = opt.split("\n") if "\n" in opt else [opt]
                # Wrap long lines
                wrapped = []
                for ol in opt_lines:
                    while len(ol) > term_w - 6:
                        wrapped.append(ol[:term_w - 6])
                        ol = ol[term_w - 6:]
                    wrapped.append(ol)
                if len(wrapped) > lines_per:
                    wrapped = wrapped[:lines_per - 1] + [wrapped[lines_per - 1][:term_w - 9] + "..."]
                for j, wl in enumerate(wrapped):
                    prefix = CSI + "33m" + BOLD + "  " + RST if j == 0 else "    "
                    out += prefix + DIM + wl + RST + CLRL + "\n"
                    lines_used += 1
                # Pad remaining lines for this option
                for _ in range(lines_per - len(wrapped)):
                    out += CLRL + "\n"
                    lines_used += 1
        # Pad any remaining space
        while lines_used < total - 1:
            out += CLRL + "\n"
            lines_used += 1
        # Controls
        nums = "  ".join("[%d]" % (i + 1) for i in range(min(len(options), 9)))
        out += "  " + DIM + nums + RST + CLRL + "\n"
        return out

    def _get_selectable_sids(self):
        """Get ordered list of session_ids that can be selected (wrapped, alive)."""
        return [sid for sid in self.cat_order
                if sid in self.cats and self.cats[sid].cwd
                and not self.cats[sid].dead and registry_is_wrapped(sid)]

    def cycle_cat(self, direction):
        """Move cat selector up (+1) or down (-1)."""
        sids = self._get_selectable_sids()
        if not sids:
            return
        self.selected_idx = (self.selected_idx + direction) % len(sids)

    def get_selected_sid(self):
        """Get the currently selected session_id."""
        sids = self._get_selectable_sids()
        if not sids:
            return None
        self.selected_idx = min(self.selected_idx, len(sids) - 1)
        return sids[self.selected_idx]

    def start_input(self):
        """Enter input mode for the selected cat."""
        sid = self.get_selected_sid()
        if sid:
            self.input_mode = True
            self.input_buffer = ""
            self.input_target_sid = sid

    def send_input(self):
        """Send input buffer to the target cat via response file."""
        if self.input_target_sid and self.input_buffer:
            resp_path = os.path.join(STATE_DIR, STATE_PREFIX + self.input_target_sid + "-response")
            try:
                with open(resp_path, "w") as f:
                    f.write(self.input_buffer)
            except OSError:
                pass
            _log("[input] sent '%s' to %s", self.input_buffer[:40], self.input_target_sid[:8])
        self.input_mode = False
        self.input_buffer = ""
        self.input_target_sid = ""

    def cancel_input(self):
        """Cancel input mode without sending."""
        self.input_mode = False
        self.input_buffer = ""
        self.input_target_sid = ""

    def toggle_approve_mode(self, mode):
        """Set approve mode on the selected cat."""
        sid = self.get_selected_sid()
        if sid:
            registry_set_approve_mode(sid, mode)
            registry_flush_force()
            _log("[approve] %s -> %s", sid[:8], mode)

    def handle_prompt_response(self, key):
        """Handle user input for the active prompt. Returns session_id if responded."""
        if not self.prompt_queue:
            return None
        prompt = self.prompt_queue[0]
        sid = prompt["session_id"]
        ptype = prompt.get("type", "permission")
        response = None
        if ptype == "question":
            # Planning question: number keys select option
            if key in "123456789":
                response = key
            elif key in ("\r", "\n"):
                response = "1"  # Enter = first option
        else:
            # Permission: Enter=Always, Y=Yes, A=Always, N=No
            if key in ("\r", "\n"):
                response = "2"  # Enter = Always
            elif key in ("y", "Y", "1"):
                response = "1"  # Yes
            elif key in ("a", "A", "2"):
                response = "2"  # Always
            elif key in ("n", "N", "3"):
                response = "3"  # No
        if response:
            resp_path = os.path.join(STATE_DIR, STATE_PREFIX + sid + "-response")
            try:
                with open(resp_path, "w") as f:
                    f.write(response)
            except OSError:
                pass
            cat = self.cats.get(sid)
            if cat:
                cat.permission_pending = False
                cat.permission_tool = ""
                cat.permission_input = {}
                cat.pending_question = None
            _log("[prompt] responded %s for %s (%s/%s)", response, prompt["name"],
                 ptype, prompt.get("tool", ""))
            self.prompt_queue.pop(0)
            return sid
        return None

    def _render_cat(self, cat, now, show_dir=True):
        """Render one cat. Returns string. show_dir=False when grouped."""
        sprite = cat._get_sprite()
        # Flash: rapid color cycling during meow
        if cat.flashing:
            flash_color = PALETTE[int(now * 8) % len(PALETTE)]
            fg = CSI + "38;5;%dm" % flash_color
        else:
            fg = CSI + "38;5;%dm" % cat.color if cat.color else ""
        cwd_short = os.path.basename(cat.cwd.rstrip("/")) if cat.cwd else ""
        ago = self._format_ago(now - cat.last_event)

        # State indicator: at-a-glance triage
        #   green dot    = working (leave alone)
        #   light blue   = compacting (maintenance, leave alone)
        #   orange square = needs help (permission pending)
        #   red square   = idle/sleeping/dead (not productive)
        DOT = "\u25cf "   # circle
        SQR = "\u25a0 "   # square (stop symbol)
        if cat.dead:
            remaining = max(0, 30 - int(now - cat.dead_since))
            indicator = CSI + "31m" + SQR + RST
            death_label = "killed" if cat.death_reason == "killed" else "session ended"
            state_text = indicator + CSI + "31m" + BOLD + death_label + RST + "  " + DIM + "%ds" % remaining + RST
        else:
            if cat.permission_pending:
                indicator = CSI + "38;5;208m" + SQR + RST  # orange square: needs help
                display_state = "waiting..."
            elif cat.state == "compacting":
                indicator = CSI + "38;5;117m" + DOT + RST  # light blue: maintenance
                display_state = cat.state
            elif cat.state in ("thinking", "cooking", "reading", "browsing"):
                indicator = CSI + "32m" + DOT + RST  # green: working
                display_state = cat.state
            else:
                indicator = CSI + "31m" + SQR + RST  # red square: idle/sleeping
                display_state = cat.state
            state_text = indicator + fg + BOLD + display_state + RST + "  " + DIM + ago + RST
            if cat.reaction_msg:
                msg_color = CSI + "31m" if cat.reaction == "error" else CSI + "33m"
                state_text += "  " + msg_color + BOLD + cat.reaction_msg + RST

        id_text = DIM + cat.session_id[:16] + RST
        if cat.state == "idle" and cat.last_tool:
            id_text += "  " + DIM + "last:" + cat.last_tool + RST

        # Stats line: fixed-width columns, normal color
        stats = ""
        if cat.stats_read:
            cost = cat.est_cost()
            total_tok = cat.total_input + cat.total_output + cat.total_cache
            ctx_s = "%dk" % cat.context_k
            cost_s = "$%.2f" % cost
            if total_tok > 1_000_000:
                tok_s = "%.1fM" % (total_tok / 1_000_000)
            elif total_tok > 1000:
                tok_s = "%dk" % (total_tok // 1000)
            else:
                tok_s = "%d" % total_tok
            turns_s = "%d turns" % cat.human_turns if cat.human_turns else ""
            age_s = ""
            if cat.session_start:
                age_s = self._format_duration(now - cat.session_start)
            # Fixed-width columns
            stats = "%-8s %-10s %-10s" % (ctx_s + " ctx", cost_s, tok_s + " tok")
            if turns_s:
                stats += "  " + turns_s
            if age_s:
                stats += "  " + age_s

        raw_msg = cat.last_message or ""
        msg = ""
        if raw_msg:
            try:
                term_w = os.get_terminal_size().columns
            except OSError:
                term_w = 80
            sprite_w = len(sprite[0]) if sprite else 14
            max_msg = max(5, term_w - sprite_w - 6)
            if len(raw_msg) > max_msg:
                msg = raw_msg[:max(2, max_msg - 3)] + "..."
            else:
                msg = raw_msg

        # Name line: bold cat name, star if wrapped, approve mode badge, selector
        wrapped = registry_is_wrapped(cat.session_id) if cat.session_id else False
        star = " *" if wrapped else ""
        # Approve mode badge
        mode = registry_get_approve_mode(cat.session_id) if cat.session_id else "manual"
        mode_badge = ""
        if mode == "automatic":
            mode_badge = "  " + CSI + "32m" + "[A]" + RST
        elif mode == "guarded":
            mode_badge = "  " + CSI + "33m" + "[G]" + RST
        # Selected indicator
        is_selected = hasattr(self, "selected_idx") and cat.session_id == self.get_selected_sid()
        sel_prefix = CSI + "7m" + ">" + RST + " " if is_selected else ""
        name_text = sel_prefix + fg + BOLD + cat.name + RST + DIM + star + RST + mode_badge if cat.name else ""

        # Per-cat burn rate for the cwd line
        rate_s = ""
        if cat.stats_read and cat.session_start and now - cat.session_start > 60:
            total_tok_rate = cat.total_input + cat.total_output + cat.total_cache
            elapsed_min = (now - cat.session_start) / 60
            cat_tok_m = total_tok_rate / elapsed_min
            if cat_tok_m >= 1000:
                rate_s = "%dk tok/m" % (cat_tok_m // 1000)
            else:
                rate_s = "%d tok/m" % cat_tok_m

        labels = [name_text, state_text]
        if show_dir:
            # Merge cwd + rate + session_id + last:tool into one line
            cwd_line = fg + cwd_short + RST if cwd_short else ""
            if rate_s:
                cwd_line += "  " + DIM + rate_s + RST
            cwd_line += "  " + id_text
            labels.append(cwd_line)
        else:
            labels.append(id_text)
        if stats:
            labels.append(stats)
        if msg:
            labels.append(DIM + msg + RST)
        # Separator + debug info below
        try:
            sep_w = os.get_terminal_size().columns
        except OSError:
            sep_w = 80
        sprite_w = len(sprite[0]) if sprite else 14
        sep_len = max(5, sep_w - sprite_w - 4)
        labels.append(DIM + "\u2501" * sep_len + RST)
        # Debug log line
        last_log = cat_last_log(cat.session_id)
        if last_log:
            import re
            log_display = re.sub(r"^\[[0-9a-f]{8}\] ", "", last_log)
            max_log = max(5, sep_len)
            if len(log_display) > max_log:
                log_display = log_display[:max(2, max_log - 3)] + "..."
            labels.append(DIM + log_display + RST)

        # Context bar on left side
        sprite_height = len(sprite)
        ctx_bar = self._context_bar(cat, sprite_height)

        render_color = PALETTE[int(now * 8) % len(PALETTE)] if cat.flashing else cat.color

        out = ""
        for i, line in enumerate(sprite):
            bar_ch = ctx_bar[i] if i < len(ctx_bar) else " "
            out += bar_ch + render_hex_line(line, color=render_color)
            if i < len(labels) and labels[i]:
                out += "  " + labels[i]
            out += CLRL + "\n"
        return out

    def render(self, now=None):
        if now is None:
            now = time.time()
        out = HOME + HIDE

        # Filter valid cats — split wrapped (full rendering) vs unwrapped (text-only)
        # Dead cats still render (show "rip" countdown) until moved to graveyard
        valid = [(sid, self.cats[sid]) for sid in self.cat_order
                 if sid in self.cats and self.cats[sid].cwd
                 and registry_is_wrapped(sid)]
        unmonitored = [(sid, self.cats[sid]) for sid in self.cat_order
                       if sid in self.cats and self.cats[sid].cwd and not self.cats[sid].dead
                       and not registry_is_wrapped(sid)]

        # Burn rate status bar (includes all sessions for total)
        all_active = valid + unmonitored
        if all_active:
            out += self._render_status_bar(all_active, now)

        # Title bar: shows active prompt cat or selected cat
        title_cat = None
        if self.prompt_queue:
            title_cat = self.cats.get(self.prompt_queue[0]["session_id"])
        if not title_cat:
            sel_sid = self.get_selected_sid()
            title_cat = self.cats.get(sel_sid) if sel_sid else None
        if title_cat:
            tfg = CSI + "38;5;%dm" % title_cat.color
            title_name = (title_cat.name or title_cat.session_id[:16]).upper()
            try:
                term_w = os.get_terminal_size().columns
            except OSError:
                term_w = 80
            pad = max(0, term_w - len(title_name) - 4)
            out += tfg + BOLD + "  " + title_name + RST + tfg + DIM + " " + "\u2500" * pad + RST + CLRL + "\n"

        # Prompt widget (constant height, always visible)
        out += self._render_prompt_widget(now)

        if not valid and not unmonitored:
            out += CLRL + "\n"
            out += DIM + "  no active sessions" + RST + CLRL + "\n"
            out += DIM + "  start claude code to wake a cat" + RST + CLRL + "\n"
        else:
            # Group by project root dir (not cwd)
            from collections import OrderedDict
            groups = OrderedDict()
            for sid, cat in valid:
                d = cat.project_dir or cat.cwd or "unknown"
                groups.setdefault(d, []).append((sid, cat))

            for proj_dir, members in groups.items():
                proj_short = os.path.basename(proj_dir.rstrip("/"))
                base_color = members[0][1].color or 208
                fg = CSI + "38;5;%dm" % base_color
                try:
                    term_w = os.get_terminal_size().columns
                except OSError:
                    term_w = 80
                header = " " + proj_short + " "
                pad = max(0, term_w - len(header) - 2)
                out += fg + DIM + "\u2500\u2500" + RST + fg + BOLD + header + RST + fg + DIM + "\u2500" * pad + RST + CLRL + "\n"
                for i, (sid, cat) in enumerate(members):
                    out += self._render_cat(cat, now, show_dir=True)
                    out += CLRL + "\n"

        # Graveyard — hide cats that are currently in the active display (alive or dying)
        alive_names = {cat.name for cat in self.cats.values()}
        visible_graves = [t for t in self.graveyard if t.get("name") not in alive_names]
        if visible_graves:
            try:
                term_w = os.get_terminal_size().columns
            except OSError:
                term_w = 80
            pad = max(0, term_w - 8)
            out += DIM + "\u2500\u2500 rip " + "\u2500" * pad + RST + CLRL + "\n"
            for tomb in visible_graves:
                fg = CSI + "38;5;%dm" % tomb["color"] if tomb["color"] else ""
                dur = self._format_duration(tomb["duration"]) if tomb["duration"] else ""
                tok = tomb["tokens"]
                if tok >= 1_000_000:
                    tok_s = "%.1fM tok" % (tok / 1_000_000)
                elif tok >= 1_000:
                    tok_s = "%dk tok" % (tok // 1000)
                else:
                    tok_s = "%d tok" % tok
                parts = []
                if tomb["project"]:
                    parts.append(tomb["project"])
                parts.append(tok_s)
                if tomb["turns"]:
                    parts.append("%d turns" % tomb["turns"])
                if dur:
                    parts.append(dur)
                out += "  " + fg + tomb["name"] + RST + "  " + DIM + "  ".join(parts) + RST + CLRL + "\n"

        # Unmonitored section — unwrapped sessions (text-only, no sprites)
        if unmonitored:
            try:
                term_w = os.get_terminal_size().columns
            except OSError:
                term_w = 80
            pad = max(0, term_w - 18)
            out += DIM + "\u2500\u2500 unmonitored " + "\u2500" * pad + RST + CLRL + "\n"
            for sid, cat in unmonitored:
                cwd_short = os.path.basename((cat.project_dir or cat.cwd or "").rstrip("/"))
                ago = self._format_ago(now - cat.last_event)
                out += "  " + DIM + (cat.name or sid[:16]) + "  " + cwd_short + "  " + ago + RST + CLRL + "\n"

        # Controls footer
        if not self.prompt_queue and not self.input_mode:
            out += DIM + "  tab=select  m/g/a=mode  enter=input  q=quit" + RST + CLRL + "\n"

        out += CLRB
        sys.stdout.write(out)
        sys.stdout.flush()


# ── Commands ─────────────────────────────────────────────────────────


def meow_mode():
    """Identify which cat this session is. Writes a Meow event to flash the cat."""
    files = find_session_files()
    if not files:
        print("No active cats found.")
        sys.exit(1)
    # Strategy: find state file matching our cwd + most recently modified
    # When Claude runs `--meow`, its cwd matches the session's cwd in the state file
    my_cwd = os.getcwd()
    candidates = []
    for path in files:
        try:
            with open(path) as f:
                data = json.loads(f.read())
            file_cwd = data.get("cwd", "")
            mt = os.path.getmtime(path)
            # Prefer cwd match, then recency
            cwd_match = os.path.realpath(file_cwd) == os.path.realpath(my_cwd) if file_cwd else False
            candidates.append((cwd_match, mt, path, data))
        except (OSError, json.JSONDecodeError):
            continue
    if not candidates:
        print("No active cats found.")
        sys.exit(1)
    # Sort: cwd matches first, then most recent
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    _, _, best, _ = candidates[0]
    if not best:
        print("No active cats found.")
        sys.exit(1)
    # Read session_id and write Meow event
    try:
        with open(best) as f:
            data = json.loads(f.read())
        sid = data.get("session_id", "")
        if not sid:
            # Extract from filename
            bn = os.path.basename(best)
            sid = bn[len(STATE_PREFIX):-len(".json")]
        name, color = registry_lookup(sid) if sid else ("unknown", 208)
        # Write Meow event to trigger flash
        data["event"] = "Meow"
        data["ts"] = int(time.time() * 1000)
        with open(best, "w") as f:
            json.dump(data, f)
        fg = CSI + "38;5;%dm" % color
        print("%s%s%s  (%s)" % (fg + BOLD, name, RST, sid[:16]))
    except Exception as e:
        print("Error: %s" % e)
        sys.exit(1)


def hook_mode():
    try:
        data = json.loads(sys.stdin.read())
        session_id = data.get("session_id", "")
        state_path = state_file_for(session_id) if session_id else STATE_FILE
        state = {
            "event": data.get("hook_event_name", "unknown"),
            "tool": data.get("tool_name", ""),
            "ts": int(time.time() * 1000),
            "session_id": session_id,
            "cwd": data.get("cwd", ""),
            "transcript_path": data.get("transcript_path", ""),
        }
        # Save tool_input for permission prompts
        tool_input = data.get("tool_input")
        if tool_input and data.get("hook_event_name") == "PermissionRequest":
            state["tool_input"] = tool_input
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(state_path, "w") as f:
            json.dump(state, f)
    except Exception:
        pass
    sys.exit(0)


def _hook_command():
    """Get the full path to the claude-cat binary for hook commands."""
    import shutil
    # Try to find the installed binary
    for name in ("claude-cat", "clat"):
        path = shutil.which(name)
        if path:
            return path + " --hook"
    # Fallback: use the script location directly
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "__main__.py"))
    return "python3 %s --hook" % script


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
    cmd = _hook_command()
    added = 0
    for event in HOOK_EVENTS:
        rules = hooks.setdefault(event, [])
        already = any(
            any("claude-cat" in h.get("command", "") for h in rule.get("hooks", []))
            for rule in rules
        )
        if not already:
            rules.append({"matcher": "", "hooks": [{"type": "command", "command": cmd, "async": True, "timeout": 5}]})
            added += 1
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    if added:
        print("Installed %d hook(s) in %s" % (added, settings_path))
        print("Hook command: %s" % cmd)
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


def _random_cat_name():
    """Generate a random hyphenated cat name for session naming."""
    adj = random.choice(_NAME_ADJ)
    noun = random.choice(_NAME_NOUN)
    return adj + "-" + noun


def _session_selector(stdin_fd):
    """Interactive session picker. Returns (action, value) or None on cancel.
    action: "new" (value=name) or "resume" (value=session_id).
    """
    import re
    import termios
    import tty

    # Build session list from registry (sorted by last_seen, most recent first)
    reg = _load_registry()
    # Detect currently running sessions from state files
    active_sids = set()
    for path in find_session_files():
        try:
            age = time.time() - os.path.getmtime(path)
            if age < 3600:
                bn = os.path.basename(path)
                sid = bn[len(STATE_PREFIX):-len(".json")]
                active_sids.add(sid)
        except OSError:
            pass

    # All sessions not currently running, sorted by last_seen desc
    # Display session name if set, otherwise session_id prefix
    sessions = []
    for sid, entry in reg.items():
        if sid in active_sids:
            continue
        name = entry.get("name", "") or sid[:16]
        sessions.append({
            "sid": sid,
            "name": name,
            "has_name": bool(entry.get("name")),
            "last_seen": entry.get("last_seen", 0),
            "tokens": entry.get("tokens", 0),
            "turns": entry.get("turns", 0),
            "project": entry.get("project", ""),
        })
    sessions.sort(key=lambda s: s["last_seen"], reverse=True)

    # Menu: (new) + existing sessions
    HIGHLIGHT = CSI + "38;5;117m"  # light blue (compacting color)
    MAX_VISIBLE = 5
    cursor = 0  # 0 = (new), 1+ = sessions
    scroll_offset = 0
    total = 1 + len(sessions)

    old_term = termios.tcgetattr(stdin_fd)
    lines_drawn = 0
    try:
        tty.setcbreak(stdin_fd)
        while True:
            # Move cursor back up to start of menu, then clear
            if lines_drawn > 0:
                out = CSI + "%dA" % lines_drawn  # move up
            else:
                out = ""
            out += "\r" + CSI + "J"  # go to col 0, clear to end of screen
            lines_drawn = 0

            visible = min(MAX_VISIBLE, total)
            for i in range(visible):
                idx = scroll_offset + i
                if idx >= total:
                    break
                selected = idx == cursor
                prefix = HIGHLIGHT + "> " + RST if selected else "  "
                if idx == 0:
                    label = "(new)"
                    detail = ""
                else:
                    s = sessions[idx - 1]
                    label = s["name"]
                    tok = s["tokens"]
                    if tok >= 1_000_000:
                        tok_s = "%.1fM tok" % (tok / 1_000_000)
                    elif tok >= 1000:
                        tok_s = "%dk tok" % (tok // 1000)
                    else:
                        tok_s = ""
                    turns_s = "%d turns" % s["turns"] if s["turns"] else ""
                    proj_s = s.get("project", "")
                    parts = [p for p in (proj_s, turns_s, tok_s) if p]
                    detail = "  " + DIM + "  ".join(parts) + RST if parts else ""

                if selected:
                    out += prefix + HIGHLIGHT + BOLD + label + RST + detail + CLRL + "\n"
                elif idx > 0 and not sessions[idx - 1].get("has_name"):
                    out += prefix + DIM + label + RST + detail + CLRL + "\n"
                else:
                    out += prefix + label + detail + CLRL + "\n"
                lines_drawn += 1

            # Scroll indicator
            if total > MAX_VISIBLE:
                pos = "(%d/%d)" % (cursor + 1, total)
                out += DIM + "  " + pos + RST + CLRL + "\n"
                lines_drawn += 1

            sys.stdout.write(out)
            sys.stdout.flush()

            # Read key
            ch = os.read(stdin_fd, 3).decode("utf-8", errors="ignore")
            if ch == "\x1b[A":  # up arrow
                if cursor > 0:
                    cursor -= 1
                    if cursor < scroll_offset:
                        scroll_offset = cursor
            elif ch == "\x1b[B":  # down arrow
                if cursor < total - 1:
                    cursor += 1
                    if cursor >= scroll_offset + MAX_VISIBLE:
                        scroll_offset = cursor - MAX_VISIBLE + 1
            elif ch in ("\r", "\n"):  # enter
                # Clear the menu
                sys.stdout.write("\r" + CSI + "J")
                sys.stdout.flush()
                if cursor == 0:
                    # New session: prompt for name
                    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
                    default_name = _random_cat_name()
                    try:
                        user_input = input("session name (\"%s\"): " % default_name).strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return None
                    name = user_input if user_input else default_name
                    name = re.sub(r"[^a-z0-9-]", "-", name.lower())
                    name = re.sub(r"-+", "-", name).strip("-")
                    if not name:
                        name = default_name
                    return ("new", name)
                else:
                    s = sessions[cursor - 1]
                    return ("resume", s["sid"])
            elif ch in ("\x03", "\x1b", "q"):  # ctrl-c, esc, q
                sys.stdout.write("\r" + CSI + "J")
                sys.stdout.flush()
                return None
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)


def _write_wrapper_state(session_id, wrapper_state, **extra):
    """Write a WrapperState event to the session's state file.
    Preserves cwd and transcript_path from existing state to avoid clearing them."""
    try:
        state_path = state_file_for(session_id)
        os.makedirs(STATE_DIR, exist_ok=True)
        # Read existing state to preserve cwd and transcript_path
        existing_cwd = ""
        existing_tp = ""
        try:
            with open(state_path) as f:
                old = json.loads(f.read())
            existing_cwd = old.get("cwd", "")
            existing_tp = old.get("transcript_path", "")
        except (OSError, json.JSONDecodeError):
            pass
        event = {
            "event": "WrapperState",
            "wrapper_state": wrapper_state,
            "source": "wrapper",
            "tool": "",
            "ts": int(time.time() * 1000),
            "session_id": session_id,
            "cwd": existing_cwd,
            "transcript_path": existing_tp,
        }
        event.update(extra)
        with open(state_path, "w") as sf:
            json.dump(event, sf)
    except OSError:
        pass


def code_mode(child_args):
    """PTY wrapper for Claude Code. Transparent passthrough with stdin control."""
    import fcntl
    import pty
    import select
    import struct
    import termios
    import tty

    if not child_args:
        child_args = ["claude"]

    # Save original terminal state
    stdin_fd = sys.stdin.fileno()
    if not os.isatty(stdin_fd):
        print("wrap requires a terminal (tty)")
        sys.exit(1)

    # Session selector: pick existing session or create new
    has_name = any(a in ("--name", "-n") for a in child_args)
    has_resume = "--resume" in child_args or "-c" in child_args or "--continue" in child_args
    if not has_name and not has_resume:
        result = _session_selector(stdin_fd)
        if result is None:
            sys.exit(0)
        action, value = result
        if action == "resume":
            # Check if session has a generated name — offer to rename
            reg = _load_registry()
            entry = reg.get(value, {})
            cur_name = entry.get("name", "")
            if cur_name and is_generated_name(cur_name):
                import re as _re_sel
                try:
                    user_input = input("rename \"%s\"? (enter to keep): " % cur_name).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    sys.exit(0)
                if user_input:
                    new_name = _re_sel.sub(r"[^a-z0-9-]", "-", user_input.lower())
                    new_name = _re_sel.sub(r"-+", "-", new_name).strip("-")
                    if new_name:
                        registry_lookup(value)
                        registry_set_name(value, new_name)
                        registry_flush_force()
                        cur_name = new_name
            if cur_name and not is_generated_name(cur_name):
                child_args.extend(["--resume", value, "--name", cur_name])
            else:
                child_args.extend(["--resume", value])
        elif action == "new":
            child_args.extend(["--name", value])

    old_term = termios.tcgetattr(stdin_fd)

    # Get current terminal size
    def get_winsize(fd):
        try:
            return struct.pack("HHHH", *struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)))
        except Exception:
            return struct.pack("HHHH", 24, 80, 0, 0)

    # Fork with pty
    child_pid, master_fd = pty.fork()

    if child_pid == 0:
        # Child: exec the command
        os.execvp(child_args[0], child_args)
        # If exec fails
        sys.exit(127)

    # Parent: set up transparent passthrough
    # Set child pty size to match our terminal
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, get_winsize(stdin_fd))
    except Exception:
        pass

    # Forward SIGWINCH to child pty
    def handle_winch(*_):
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, get_winsize(stdin_fd))
            os.kill(child_pid, signal.SIGWINCH)
        except Exception:
            pass
    signal.signal(signal.SIGWINCH, handle_winch)

    # Detect session_id and session name from args
    wrap_session_id = None
    wrap_session_name = None
    for i, arg in enumerate(child_args):
        if arg == "--resume" and i + 1 < len(child_args):
            val = child_args[i + 1]
            # UUID: use as session ID. Name: infer as session name, detect real ID from state files.
            import re as _re2
            if _re2.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-', val):
                wrap_session_id = val
            else:
                wrap_session_name = wrap_session_name or val
        elif arg in ("--name", "-n") and i + 1 < len(child_args):
            wrap_session_name = child_args[i + 1]
    # Track existing state files to detect new sessions
    existing_files = set(find_session_files()) if not wrap_session_id else set()

    # Save session name + wrapped flag to registry
    if wrap_session_id:
        registry_lookup(wrap_session_id)  # ensure entry exists
        registry_set_wrapped(wrap_session_id)
        if wrap_session_name:
            registry_set_name(wrap_session_id, wrap_session_name)
        registry_flush_force()

    # Set our terminal to raw mode
    tty.setraw(stdin_fd)

    # Minimal wrapper state — all parsing logic lives in the litter
    _last_escape_ts = 0.0       # when user last pressed Escape
    _output_buf = b""           # rolling buffer for interrupt detection
    _out_tee_buf = ""           # rolling decoded ANSI-stripped stdout for litter to read
    _out_tee_ts = 0.0           # last time .out file was written (debounce 200ms)
    _heartbeat_ts = 0.0         # last heartbeat write (keeps .out mtime fresh)

    try:
        while True:
            try:
                rlist, _, _ = select.select([stdin_fd, master_fd], [], [], 0.1)
            except select.error:
                break

            if stdin_fd in rlist:
                # User input -> child
                try:
                    data = os.read(stdin_fd, 4096)
                    if not data:
                        break
                    os.write(master_fd, data)
                    # Track Escape (0x1b) and Ctrl-C (0x03) for interrupt detection
                    if len(data) == 1 and (data == b"\x1b" or data == b"\x03"):
                        _last_escape_ts = time.time()
                except OSError:
                    break

            if master_fd in rlist:
                # Child output -> user
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)

                    if wrap_session_id:
                        now = time.time()

                        # Interrupt detection (Escape + "Interrupted" — needs stdin timing)
                        if _last_escape_ts and now - _last_escape_ts < 3.0:
                            _output_buf = (_output_buf + data)[-4096:]
                            if b"Interrupted" in _output_buf:
                                _last_escape_ts = 0.0
                                _output_buf = b""
                                _write_wrapper_state(wrap_session_id, "interrupted")
                        else:
                            _output_buf = b""

                        # Tee stdout for litter to parse (decoded, ANSI-stripped, rolling 4KB)
                        chunk_text = data.decode("utf-8", errors="ignore")
                        clean_text = _ANSI_RE.sub("", chunk_text)
                        _out_tee_buf = (_out_tee_buf + clean_text)[-4096:]
                        # Write .out file debounced at 200ms
                        if now - _out_tee_ts > 0.2:
                            try:
                                out_path = os.path.join(STATE_DIR, STATE_PREFIX + wrap_session_id + ".out")
                                os.makedirs(STATE_DIR, exist_ok=True)
                                tmp = out_path + ".tmp"
                                with open(tmp, "w") as f:
                                    f.write(_out_tee_buf)
                                os.replace(tmp, out_path)
                                _out_tee_ts = now
                            except OSError:
                                pass

                except OSError:
                    break

            # Detect session_id from new state files
            if not wrap_session_id:
                current = set(find_session_files())
                new_files = current - existing_files
                if new_files:
                    # Newest file is likely ours
                    newest = max(new_files, key=lambda f: os.path.getmtime(f))
                    bn = os.path.basename(newest)
                    wrap_session_id = bn[len(STATE_PREFIX):-len(".json")]
                    # Save session name + wrapped flag to registry
                    registry_lookup(wrap_session_id)  # ensure entry exists
                    registry_set_wrapped(wrap_session_id)
                    if wrap_session_name:
                        registry_set_name(wrap_session_id, wrap_session_name)
                        registry_flush_force()

            # Check for response files from litter
            if wrap_session_id:
                resp_path = os.path.join(STATE_DIR, STATE_PREFIX + wrap_session_id + "-response")
                try:
                    if os.path.exists(resp_path):
                        with open(resp_path) as rf:
                            response = rf.read().strip()
                        os.remove(resp_path)
                        if response in ("1", "2", "3"):
                            os.write(master_fd, response.encode())
                        elif response:
                            # Arbitrary text input — send with Enter
                            os.write(master_fd, (response + "\r").encode())
                except OSError:
                    pass

            # Heartbeat: touch .out file every 15s so litter knows we're alive
            if wrap_session_id:
                _now_hb = time.time()
                if _now_hb - _heartbeat_ts > 15.0:
                    _heartbeat_ts = _now_hb
                    try:
                        out_path = os.path.join(STATE_DIR, STATE_PREFIX + wrap_session_id + ".out")
                        if _out_tee_buf:
                            tmp = out_path + ".tmp"
                            with open(tmp, "w") as f:
                                f.write(_out_tee_buf)
                            os.replace(tmp, out_path)
                        else:
                            # No content yet, just touch the file
                            Path(out_path).touch()
                    except OSError:
                        pass

            # Check if child is still alive
            try:
                pid, status = os.waitpid(child_pid, os.WNOHANG)
                if pid != 0:
                    # Child exited, drain remaining output
                    try:
                        while True:
                            rlist, _, _ = select.select([master_fd], [], [], 0.1)
                            if not rlist:
                                break
                            data = os.read(master_fd, 4096)
                            if not data:
                                break
                            os.write(sys.stdout.fileno(), data)
                    except OSError:
                        pass
                    break
            except ChildProcessError:
                break

    finally:
        # Restore terminal
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
        try:
            os.close(master_fd)
        except OSError:
            pass
        # Clean up .out tee file
        if wrap_session_id:
            try:
                out_path = os.path.join(STATE_DIR, STATE_PREFIX + wrap_session_id + ".out")
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass

    # Exit with child's exit code
    try:
        if os.WIFEXITED(status):
            sys.exit(os.WEXITSTATUS(status))
        else:
            sys.exit(1)
    except NameError:
        sys.exit(1)


def litter_mode(sprite_data=None):
    import fcntl
    import termios
    import tty
    # Single-instance enforcement: only one clat can interact with sessions
    lock_path = os.path.join(STATE_DIR, "clat.lock")
    os.makedirs(STATE_DIR, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        print("Another clat instance is already running.")
        print("Only one interactive clat session is allowed (it sends responses to clat code).")
        lock_fd.close()
        sys.exit(1)
    _init_logging()
    _log("claude-cat v%s litter started", VERSION)
    _log("state_dir=%s  prefix=%s", STATE_DIR, STATE_PREFIX)
    sys.stdout.write(CLR)
    sys.stdout.flush()
    litter = Litter(sprite_data)
    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
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
            registry_flush()
            # Non-blocking key check using select
            import select
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    raw = os.read(fd, 1)
                    # Read escape sequences (arrow keys, etc.)
                    if raw == b"\x1b":
                        import select as _sel2
                        if _sel2.select([fd], [], [], 0.05)[0]:
                            raw += os.read(fd, 4)
                    ch = raw.decode("utf-8", errors="ignore")

                    if litter.input_mode:
                        # Input mode: all keys go to buffer
                        if ch in ("\r", "\n"):
                            litter.send_input()
                        elif ch == "\x1b" or ch == "\x03":
                            litter.cancel_input()
                        elif ch == "\x7f" or ch == "\x08":  # backspace
                            litter.input_buffer = litter.input_buffer[:-1]
                        elif len(ch) == 1 and ch >= " ":
                            litter.input_buffer += ch
                    elif litter.prompt_queue and ch in ("y", "Y", "n", "N", "a", "A",
                                                        "1", "2", "3", "4", "5", "6", "7", "8", "9",
                                                        "\r", "\n"):
                        # Prompt response (permission or question)
                        litter.handle_prompt_response(ch)
                    elif ch == "\x1b[A" or ch == "\x1b[Z":  # up arrow or shift-tab
                        litter.cycle_cat(-1)
                    elif ch == "\x1b[B" or ch == "\t":  # down arrow or tab
                        litter.cycle_cat(1)
                    elif ch in ("\r", "\n"):
                        # Enter on selected cat: open input mode
                        litter.start_input()
                    elif ch in ("m", "M"):
                        litter.toggle_approve_mode("manual")
                    elif ch in ("g", "G"):
                        litter.toggle_approve_mode("guarded")
                    elif ch in ("a", "A"):
                        litter.toggle_approve_mode("automatic")
                    elif ch in ("c", "C"):
                        # Spread colors: shuffle palette, assign evenly
                        cats = [c for c in litter.cats.values() if not c.dead]
                        if cats:
                            shuffled = list(PALETTE)
                            random.shuffle(shuffled)
                            for i, cat in enumerate(cats):
                                cat.color = shuffled[i % len(shuffled)]
                                registry_set_color(cat.session_id, cat.color)
                            registry_flush_force()
                    elif ch in ("q", "Q", "\x03"):
                        break
                except OSError:
                    pass
            else:
                pass  # select handled the 0.1s sleep
    finally:
        registry_flush_force()
        _close_logging()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()
        # Release lock
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.remove(lock_path)
        except OSError:
            pass


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
    subprocess.run(["tmux", "select-pane", "-t", session + ":0.1"])  # focus clat pane (bottom)
    subprocess.run(["tmux", "attach", "-t", session])


def print_help():
    print(
        "claude-cat v%s\n"
        "A 1-bit companion cat for Claude Code\n\n"
        "Usage:\n"
        "  clat                             Monitor all sessions\n"
        "  clat code                        New session (prompts for name)\n"
        "  clat code my-feature             Resume 'my-feature' or create new\n"
        "  clat code --resume <id>          Resume by session id\n"
        "  clat --rename <name> [new-name]  Rename a session\n"
        "  clat install                     Set up Claude Code hooks\n"
        "  clat uninstall                   Remove Claude Code hooks\n"
        "  clat --sprite <name|path>        Use a custom sprite\n"
        "  clat --demo                      Preview all states + reactions\n"
        "  clat list-sprites                Show available sprites\n"
        "  clat --meow                      Identify this session's cat (flash it)\n"
        "  clat --tmux-ccm                  Dashboard: CCM + litter in tmux\n"
        "  clat --debug                     Verbose logging (also prints to stderr)\n"
        "  clat --trace                     Dense state machine trace (logs/trace.jsonl)\n"
        "  clat --version                   Show version" % VERSION
    )


def main():
    global DEBUG, TRACE
    args = sys.argv[1:]
    sprite_name = None
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--sprite" and i + 1 < len(args):
            sprite_name = args[i + 1]
            i += 2
        elif args[i] == "--debug":
            DEBUG = True
            i += 1
        elif args[i] == "--trace":
            TRACE = True
            i += 1
        else:
            filtered.append(args[i])
            i += 1
    cmd = filtered[0] if filtered else ""
    sprite_data = None
    if cmd in ("", "--watch", "watch", "--demo", "demo"):
        sprite_data = sprites_mod.load(sprite_name)
    if cmd == "--rename" or (cmd == "code" and len(filtered) > 1 and filtered[1] == "--rename"):
        # clat --rename <name-or-id> [new-name]
        import re as _re
        rename_args = filtered[1:] if cmd == "--rename" else filtered[2:]
        if not rename_args:
            print("Usage: clat --rename <session-name-or-id> [new-name]")
            sys.exit(1)
        target = rename_args[0]
        reg = _load_registry()
        # Find by name or session_id
        found_sid = None
        found_name = None
        is_uuid = bool(_re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", target))
        if is_uuid and target in reg:
            found_sid = target
            found_name = reg[target].get("name", "")
        else:
            for sid, entry in reg.items():
                if entry.get("name") == target:
                    found_sid = sid
                    found_name = target
                    break
        if not found_sid:
            print("Session '%s' not found in registry." % target)
            sys.exit(1)
        # Get new name: from arg or prompt
        if len(rename_args) > 1:
            new_name = rename_args[1]
        else:
            try:
                new_name = input("session name (\"%s\"): " % found_name).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)
            if not new_name:
                new_name = found_name
        new_name = _re.sub(r"[^a-z0-9-]", "-", new_name.lower())
        new_name = _re.sub(r"-+", "-", new_name).strip("-")
        if not new_name:
            print("Invalid name.")
            sys.exit(1)
        registry_lookup(found_sid)
        registry_set_name(found_sid, new_name)
        registry_flush_force()
        print("%s -> %s" % (found_name or found_sid[:16], new_name))
        sys.exit(0)
    elif cmd == "code":
        # Everything after "--" is the child command, OR remaining args passed to claude
        child_args = []
        if "--" in sys.argv:
            dash_idx = sys.argv.index("--")
            child_args = sys.argv[dash_idx + 1:]
        elif len(filtered) > 1:
            code_args = filtered[1:]
            if code_args and not code_args[0].startswith("-"):
                # Positional arg: name or session UUID
                import re as _re
                val = code_args[0]
                rest = code_args[1:]
                is_uuid = bool(_re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", val))
                if is_uuid:
                    # UUID: resume directly, prompt for name if missing or auto-generated
                    reg = _load_registry()
                    entry = reg.get(val, {})
                    name = entry.get("name", "")
                    if not name or is_generated_name(name):
                        default_name = name or _random_cat_name()
                        try:
                            user_input = input("name this session (\"%s\"): " % default_name).strip()
                        except (EOFError, KeyboardInterrupt):
                            print()
                            sys.exit(0)
                        name = user_input if user_input else default_name
                        name = _re.sub(r"[^a-z0-9-]", "-", name.lower())
                        name = _re.sub(r"-+", "-", name).strip("-") or default_name
                        registry_lookup(val)
                        registry_set_name(val, name)
                        registry_flush_force()
                    # Pass --name so Claude Code shows it in the UI too
                    child_args = ["claude", "--resume", val, "--name", name] + rest
                else:
                    # Name: look up in registry — resume if found, new if not
                    reg = _load_registry()
                    found_sid = None
                    for sid, entry in reg.items():
                        if entry.get("name") == val:
                            found_sid = sid
                            break
                    if found_sid:
                        child_args = ["claude", "--resume", found_sid, "--name", val] + rest
                    else:
                        child_args = ["claude", "--name", val] + rest
            elif "--resume" in code_args:
                # Explicit --resume: resolve UUID or name
                idx = code_args.index("--resume")
                if idx + 1 < len(code_args):
                    val = code_args[idx + 1]
                    rest = code_args[:idx] + code_args[idx + 2:]
                    reg = _load_registry()
                    if val in reg:
                        # Direct UUID match — prompt if name is auto-generated
                        name = reg[val].get("name", "")
                        if not name or is_generated_name(name):
                            default_name = name or _random_cat_name()
                            try:
                                user_input = input("name this session (\"%s\"): " % default_name).strip()
                            except (EOFError, KeyboardInterrupt):
                                print()
                                sys.exit(0)
                            import re as _re_uuid
                            name = user_input if user_input else default_name
                            name = _re_uuid.sub(r"[^a-z0-9-]", "-", name.lower())
                            name = _re_uuid.sub(r"-+", "-", name).strip("-") or default_name
                            registry_set_name(val, name)
                            registry_flush_force()
                        child_args = ["claude", "--resume", val, "--name", name] + rest
                    else:
                        # Search by name or partial session_id
                        found_sid = None
                        found_name = None
                        matches = []
                        for sid, entry in reg.items():
                            if entry.get("name") == val:
                                found_sid = sid
                                found_name = val
                                break
                            if val in sid or val in entry.get("name", ""):
                                matches.append((sid, entry.get("name", "")))
                        if found_sid:
                            child_args = ["claude", "--resume", found_sid, "--name", found_name] + rest
                        elif len(matches) == 1:
                            # Unambiguous partial match — use it
                            child_args = ["claude", "--resume", matches[0][0], "--name", matches[0][1] or matches[0][0][:16]] + rest
                        elif matches:
                            print("Session '%s' not found. Did you mean:" % val)
                            for sid, n in matches[:3]:
                                print("  %s  (%s)" % (n or sid[:16], sid[:16]))
                            sys.exit(1)
                        else:
                            # No match at all — pass through, let Claude Code resolve
                            child_args = ["claude", "--resume", val, "--name", val] + rest
                else:
                    child_args = ["claude"] + code_args
            else:
                child_args = ["claude"] + code_args
        code_mode(child_args)
    elif cmd == "--tmux-ccm":
        tmux_ccm_mode()
    elif cmd in ("--hook", "hook"):
        hook_mode()
    elif cmd in ("--meow", "meow"):
        meow_mode()
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
        litter_mode(sprite_data)
    else:
        print("Unknown command: %s" % cmd)
        print_help()
        sys.exit(1)




if __name__ == "__main__":
    main()
