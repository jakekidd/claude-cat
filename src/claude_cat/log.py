"""Logging infrastructure for claude-cat."""

import json
import os
import re
import sys
import time
from pathlib import Path

LOG_DIR = os.path.join(Path.home(), ".claude-cat", "logs")
MAX_LITTER_LOG = 1_000_000  # rotate litter.log above 1MB

# Module-level flags (set by main before _init_logging)
DEBUG = False
TRACE = False

# Mutable state
_litter_log = None
_trace_log = None  # trace.jsonl — one JSON object per state change
_cat_logs = {}  # session_id -> file handle
_cat_last_log = {}  # session_id -> last log line (for UI)
_log_t0 = 0.0
_sid_map = {}  # short (8-char) -> full session_id


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
