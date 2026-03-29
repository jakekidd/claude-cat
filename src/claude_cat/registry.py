"""Cat identity, naming, colors, graveyard, and approve mode."""

import hashlib
import json
import os
import random
import shlex
import time
from pathlib import Path

from . import log as _log_mod

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

# Registry paths
REGISTRY_DIR = os.path.join(Path.home(), ".claude-cat")
REGISTRY_FILE = os.path.join(REGISTRY_DIR, "registry.json")
REGISTRY_MAX_AGE = 30 * 86400  # prune after 30 days
GRAVEYARD_FILE = os.path.join(REGISTRY_DIR, "graveyard.json")
GRAVEYARD_MAX = 5


# ── Name generation ──────────────────────────────────────────────────

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
_NAME_ADJ_SET = set(_NAME_ADJ)
_NAME_NOUN_SET = set(_NAME_NOUN)


def cat_name(session_id):
    """Deterministic cat name from session_id. Uses md5 for cross-process stability."""
    h = int(hashlib.md5(session_id.encode()).hexdigest()[:8], 16)
    return _NAME_ADJ[h % len(_NAME_ADJ)] + " " + _NAME_NOUN[(h >> 8) % len(_NAME_NOUN)]


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
    h = int(hashlib.md5(session_id.encode()).hexdigest()[8:16], 16)
    return PALETTE[h % len(PALETTE)]


def _random_cat_name():
    """Generate a random hyphenated cat name for session naming."""
    adj = random.choice(_NAME_ADJ)
    noun = random.choice(_NAME_NOUN)
    return adj + "-" + noun


# ── Graveyard ────────────────────────────────────────────────────────

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


# ── Registry CRUD ────────────────────────────────────────────────────

_registry = {}
_registry_dirty = False
_registry_last_flush = 0.0


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
        log_path = os.path.join(_log_mod.LOG_DIR, sid + ".log")
        try:
            if os.path.exists(log_path):
                os.remove(log_path)
        except OSError:
            pass
    return reg


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


# ── Guarded mode ─────────────────────────────────────────────────────

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
