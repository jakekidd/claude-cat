"""Litter — the TUI dashboard for monitoring Claude Code sessions."""

import datetime
import json
import os
import random
import re
import signal
import sys
import time

from .shared import (
    CSI, HIDE, SHOW, CLR, CLRL, CLRB, BOLD, DIM, RST, HOME,
    STATE_DIR, STATE_PREFIX,
    find_session_files, project_dir_from_transcript, render_hex_line,
)
from .log import (
    _log, _trace, _init_logging, _close_logging, _register_cat_log, cat_last_log,
    DEBUG,
)
from .registry import (
    PALETTE, GRAVEYARD_MAX,
    _load_registry, _load_graveyard, _save_graveyard,
    registry_lookup, registry_touch, registry_is_wrapped,
    registry_get_approve_mode, registry_set_approve_mode,
    registry_set_color, registry_flush, registry_flush_force,
    _registry,
)
from .cat import Cat, TOOL_STATES

# Stdout spinner chars emitted by Claude Code during thinking
SPINNER_CHARS = set("\u00b7\u273b\u273d\u2736\u2733\u2722")  # ·✻✽✶✳✢

# Declarative stdout pattern table
STDOUT_PATTERNS = [
    ("spinner", SPINNER_CHARS, None),
    ("error", None, [
        ("API Error", "api_error"),
        ("Rate limit", "rate_limit"),
        ("Request too large", "request_too_large"),
        ("Overloaded", "overloaded"),
    ]),
    ("compacting", None, re.compile(r"Compacting conversation")),
]
_THOUGHT_RE = re.compile(r"Thought for (\d+)s")

# Vertical block elements for context bar (1/8 to 8/8 fill)
CTX_BLOCKS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


class Litter:
    def __init__(self, sprite_data):
        self.sprite_data = sprite_data
        self.cats = {}
        self.cat_order = []
        self.graveyard = _load_graveyard()
        self.prompt_queue = []  # [{session_id, name, color, tool, input, ts}]
        # Cat selector state
        self.selected_idx = 0
        self.input_mode = False
        self.input_buffer = ""
        self.input_target_sid = ""

    def scan(self):
        files = find_session_files()
        seen = set()
        now = time.time()
        for path in files:
            basename = os.path.basename(path)
            sid = basename[len(STATE_PREFIX) : -len(".json")]
            seen.add(sid)
            if sid not in self.cats:
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
                        cat.state = "thinking"
                        _log("[scan] new cat %s: thinking (UserPromptSubmit %.0fs ago)", sid[:8], age)
                    else:
                        cat.state = "idle"
                        _log("[scan] new cat %s: idle (last_ev=%s %.0fs ago)", sid[:8], ev, age)
                    if cat.state == "idle" and age > 600:
                        cat.sleeping = True
                    if not cat.dead and age > 3600:
                        cat.dead = True
                        cat.dead_since = now
                        cat.death_reason = "killed"
                        _log("[scan] %s: dead (stale %.0fh)", sid[:8], age / 3600)
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

        for sid, cat in self.cats.items():
            if not cat.dead:
                registry_touch(sid)

        for sid in list(self.cat_order):
            if sid not in seen:
                if sid in self.cats:
                    del self.cats[sid]
                self.cat_order.remove(sid)

        # Clean up dead cats after 30s -> graveyard
        for sid in list(self.cat_order):
            if sid not in self.cats:
                continue
            cat = self.cats[sid]
            if cat.dead and cat.dead_since and now - cat.dead_since > 30:
                _log("[cleanup] removing dead cat %s (dead %.0fs) -> graveyard", sid[:8], now - cat.dead_since)
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

    # ── Unified tick pipeline: gather → match → apply → sync ──

    def tick(self):
        now = time.time()
        gathered = self._gather(now)
        events = self._match(gathered, now)
        dirty = self._apply(events, now)
        self._sync(now)
        return dirty

    def _gather(self, now):
        """Phase 1: Read all data sources once per tick. No display mutations."""
        result = {}
        for sid, cat in self.cats.items():
            if cat.dead:
                result[sid] = {"is_wrapped": False, "hook_data": None,
                               "new_text": None, "out_content": None}
                continue
            is_wrapped = registry_is_wrapped(cat.session_id) if cat.session_id else False
            hook_data = None
            try:
                mtime = os.path.getmtime(cat.state_file)
                if mtime > cat.last_mtime:
                    cat.last_mtime = mtime
                    with open(cat.state_file) as f:
                        raw = f.read()
                    if raw != cat.last_raw:
                        cat.last_raw = raw
                        hook_data = json.loads(raw)
            except (OSError, json.JSONDecodeError):
                pass
            new_text = None
            out_content = None
            if is_wrapped and cat.out_file:
                try:
                    mtime = os.path.getmtime(cat.out_file)
                    if mtime > cat.last_out_mtime:
                        cat.last_out_mtime = mtime
                        with open(cat.out_file) as f:
                            content = f.read()
                        if content != cat.last_out_content:
                            cat.last_out_change = now
                            old_len = len(cat.last_out_content)
                            if old_len and content.startswith(cat.last_out_content[:min(old_len, 256)]):
                                new_text = content[old_len:]
                            elif not cat.last_out_content:
                                if now - mtime > 30:
                                    cat.last_out_content = content
                                else:
                                    new_text = content[-500:] if len(content) > 500 else content
                            else:
                                cat.last_out_content = content  # buffer wrapped, skip
                            if new_text is not None:
                                cat.last_out_content = content
                                out_content = content
                except (OSError, ValueError):
                    pass
            result[sid] = {"is_wrapped": is_wrapped, "hook_data": hook_data,
                           "new_text": new_text, "out_content": out_content}
        return result

    def _match(self, gathered, now):
        """Phase 2: Pattern matching on gathered data. No mutations. No I/O."""
        events = []
        for sid, g in gathered.items():
            cat = self.cats[sid]
            if cat.dead:
                continue
            if g["hook_data"] is not None:
                events.append(("hook", sid, g["hook_data"]))
            new_text = g["new_text"]
            if new_text is not None and g["is_wrapped"]:
                for pat_type, char_set, match_spec in STDOUT_PATTERNS:
                    if char_set is not None:
                        if char_set & set(new_text):
                            events.append(("stdout_" + pat_type, sid, None))
                    elif isinstance(match_spec, list):
                        for pat_str, key in match_spec:
                            if pat_str in new_text:
                                events.append(("stdout_" + pat_type, sid, key))
                                break
                    elif hasattr(match_spec, "search"):
                        if match_spec.search(new_text):
                            events.append(("stdout_" + pat_type, sid, None))
                if g["out_content"]:
                    m = _THOUGHT_RE.search(g["out_content"][-200:])
                    if m:
                        events.append(("stdout_thought", sid, int(m.group(1))))
            # Three-way silence → idle (wrapped sessions, backstop for missed Stop)
            if g["is_wrapped"] and cat.state not in ("idle", "compacting"):
                spinner_quiet = (now - cat.last_spinner_seen) if cat.last_spinner_seen else 999
                hook_quiet = now - cat.last_event
                content_quiet = (now - cat.last_out_change) if cat.last_out_change else 999
                if spinner_quiet > 15 and hook_quiet > 15 and content_quiet > 15:
                    events.append(("stdout_idle", sid, None))
        return events

    def _apply(self, events, now):
        """Phase 3: Process events sequentially. All display mutations here."""
        dirty = False
        hook_sids = set()
        for etype, sid, detail in events:
            cat = self.cats.get(sid)
            if not cat or cat.dead:
                continue
            if etype == "hook":
                hook_sids.add(sid)
                cat.cwd = detail.get("cwd") or cat.cwd
                _log("[tick] processing event for %s", sid[:8])
                cat._process_event(detail)
                dirty = True
            elif etype == "stdout_spinner":
                if sid in hook_sids:
                    continue
                cat.last_spinner_seen = now
                # Only set thinking from idle/sleeping — don't override tool states
                # (reading/cooking/browsing) set by PostToolUse. Those are ground truth.
                if cat.state == "idle" or cat.sleeping:
                    old_s = cat.state
                    cat.state = "thinking"
                    cat.sleeping = False
                    cat.frame_idx = 0
                    dirty = True
                    _log("[stdout] %s -> thinking (spinner)", sid[:8])
                    _trace(sid, "stdout", "spinner_start", old_s, "thinking")
            elif etype == "stdout_error":
                cat.reaction = "error"
                cat.reaction_end = now + 2.0
                cat.reaction_msg = detail
                dirty = True
                _log("[stdout] %s error: %s", sid[:8], detail)
            elif etype == "stdout_compacting":
                if sid in hook_sids:
                    continue
                if cat.state != "compacting":
                    old_s = cat.state
                    cat.state = "compacting"
                    cat.frame_idx = 0
                    dirty = True
                    _log("[stdout] %s -> compacting", sid[:8])
                    _trace(sid, "stdout", "compacting", old_s, "compacting")
            elif etype == "stdout_thought":
                cat.thought_seconds = detail
            elif etype == "stdout_idle":
                if sid in hook_sids:
                    continue
                old_s = cat.state
                cat.state = "idle"
                _log("[stdout] %s -> idle (all signals quiet 15s)", sid[:8])
                _trace(sid, "stdout", "triple_silence", old_s, "idle",
                       spinner=round(now - cat.last_spinner_seen, 1) if cat.last_spinner_seen else -1,
                       hook=round(now - cat.last_event, 1),
                       content=round(now - cat.last_out_change, 1) if cat.last_out_change else -1)
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
        for cat in self.cats.values():
            if not cat.dead and cat.tick(now):
                dirty = True
        return dirty

    def _sync(self, now):
        """Phase 4: Registry sync + prompt queue update."""
        if not hasattr(self, "_last_name_sync") or now - self._last_name_sync > 10:
            self._last_name_sync = now
            disk_reg = _load_registry()
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
        self._update_prompt_queue()

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
        if not cat.stats_read or cat.context_k <= 0:
            return ["  "] * height
        if "opus" in cat.model and ("4-6" in cat.model or "1m" in cat.model.lower()):
            ctx_max = 1000.0
        elif "opus" in cat.model:
            ctx_max = 200.0
        elif "sonnet" in cat.model:
            ctx_max = 200.0
        elif "haiku" in cat.model:
            ctx_max = 200.0
        else:
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
        if tok_per_min > 500:
            rate_color = CSI + "38;5;167m"
        elif tok_per_min > 200:
            rate_color = CSI + "38;5;179m"
        else:
            rate_color = CSI + "38;5;109m"
        today_tok = total_tok
        if today_tok >= 1_000_000:
            today_s = "%.1fM" % (today_tok / 1_000_000)
        elif today_tok >= 1000:
            today_s = "%dk" % (today_tok // 1000)
        else:
            today_s = "%d" % today_tok
        parts = []
        parts.append(BOLD + today_s + RST + DIM + " today" + RST)
        if tok_per_min >= 1000:
            tok_s = "%dk" % (tok_per_min // 1000)
        else:
            tok_s = "%d" % tok_per_min
        parts.append(rate_color + BOLD + tok_s + RST + DIM + " tok/m" + RST)
        parts.append(rate_color + BOLD + "$%.2f" % cost_per_min + RST + DIM + "/m" + RST)
        parts.append(DIM + "$%.2f total" % total_cost + RST)
        elapsed_h = elapsed_min / 60
        if elapsed_h > 0.05:
            cost_per_h = total_cost / elapsed_h
            if cost_per_h > 0:
                reset_time = earliest_start + 5 * 3600
                reset_dt = datetime.datetime.fromtimestamp(reset_time)
                reset_str = reset_dt.strftime("%-I:%M%p").lower()
                parts.append(DIM + "reset " + RST + CSI + "38;5;109m" + reset_str + RST)
        bar = "  ".join(parts)
        return bar + CLRL + "\n"

    def _format_prompt_content(self, prompt):
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
        if len(lines) <= max_lines:
            return lines
        if max_lines < 3:
            return lines[:max_lines]
        top_n = (max_lines - 1) // 2 + (max_lines - 1) % 2
        bot_n = (max_lines - 1) // 2
        result = lines[:top_n]
        result.append("  ...")
        result.extend(lines[-bot_n:] if bot_n > 0 else [])
        return result

    PROMPT_LINES = 20

    def _render_prompt_widget(self, now):
        term_w = getattr(self, "_term_w", 80)
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
        fg = CSI + "38;5;%dm" % prompt["color"] if prompt["color"] else ""
        name = prompt["name"]
        queue_info = " (%d pending)" % len(self.prompt_queue) if len(self.prompt_queue) > 1 else ""
        out = ""
        header = " %s wants to run%s " % (name, queue_info)
        pad = max(0, term_w - len(header) - 2)
        out += fg + DIM + "\u2500\u2500" + RST + fg + BOLD + header + RST + fg + DIM + "\u2500" * pad + RST + CLRL + "\n"
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
        used = 1 + content_lines + 2
        for _ in range(total - used):
            out += CLRL + "\n"
        out += "  " + CSI + "32m" + BOLD + "[1/enter] Yes" + RST + DIM + "  [2] Always  [3] No" + RST + CLRL + "\n"
        out += CLRL + "\n"
        return out

    def _render_question_widget(self, prompt, term_w, total):
        fg = CSI + "38;5;%dm" % prompt["color"] if prompt["color"] else ""
        name = prompt["name"]
        options = prompt.get("options", [])
        question_text = prompt.get("text", "")
        out = ""
        header = " %s is asking " % name
        pad = max(0, term_w - len(header) - 2)
        out += fg + DIM + "\u2500\u2500" + RST + fg + BOLD + header + RST + fg + DIM + "\u2500" * pad + RST + CLRL + "\n"
        lines_used = 1
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
        for _ in range(max_question - len(q_display)):
            out += CLRL + "\n"
            lines_used += 1
        options_area = total - lines_used - 1
        if options:
            lines_per = max(1, options_area // len(options))
            for i, opt in enumerate(options):
                opt_lines = opt.split("\n") if "\n" in opt else [opt]
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
                for _ in range(lines_per - len(wrapped)):
                    out += CLRL + "\n"
                    lines_used += 1
        while lines_used < total - 1:
            out += CLRL + "\n"
            lines_used += 1
        nums = "  ".join("[%d]" % (i + 1) for i in range(min(len(options), 9)))
        out += "  " + DIM + nums + RST + CLRL + "\n"
        return out

    def _get_selectable_sids(self):
        return [sid for sid in self.cat_order
                if sid in self.cats and self.cats[sid].cwd
                and not self.cats[sid].dead and registry_is_wrapped(sid)]

    def cycle_cat(self, direction):
        sids = self._get_selectable_sids()
        if not sids:
            return
        self.selected_idx = (self.selected_idx + direction) % len(sids)

    def get_selected_sid(self):
        sids = self._get_selectable_sids()
        if not sids:
            return None
        self.selected_idx = min(self.selected_idx, len(sids) - 1)
        return sids[self.selected_idx]

    def start_input(self):
        sid = self.get_selected_sid()
        if sid:
            self.input_mode = True
            self.input_buffer = ""
            self.input_target_sid = sid

    def send_input(self):
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
        self.input_mode = False
        self.input_buffer = ""
        self.input_target_sid = ""

    def toggle_approve_mode(self, mode):
        sid = self.get_selected_sid()
        if sid:
            registry_set_approve_mode(sid, mode)
            registry_flush_force()
            _log("[approve] %s -> %s", sid[:8], mode)

    def handle_prompt_response(self, key):
        if not self.prompt_queue:
            return None
        prompt = self.prompt_queue[0]
        sid = prompt["session_id"]
        ptype = prompt.get("type", "permission")
        response = None
        if ptype == "question":
            if key in "123456789":
                response = key
            elif key in ("\r", "\n"):
                response = "1"
        else:
            if key in ("\r", "\n"):
                response = "1"
            elif key == "1":
                response = "1"
            elif key == "2":
                response = "2"
            elif key == "3":
                response = "3"
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
        sprite = cat._get_sprite()
        if cat.flashing:
            flash_color = PALETTE[int(now * 8) % len(PALETTE)]
            fg = CSI + "38;5;%dm" % flash_color
        else:
            fg = CSI + "38;5;%dm" % cat.color if cat.color else ""
        cwd_short = os.path.basename(cat.cwd.rstrip("/")) if cat.cwd else ""
        ago = self._format_ago(now - cat.last_event)
        DOT = "\u25cf "
        SQR = "\u25a0 "
        if cat.dead:
            remaining = max(0, 30 - int(now - cat.dead_since))
            indicator = CSI + "31m" + SQR + RST
            death_label = "killed" if cat.death_reason == "killed" else "session ended"
            state_text = indicator + CSI + "31m" + BOLD + death_label + RST + "  " + DIM + "%ds" % remaining + RST
        else:
            if cat.permission_pending:
                indicator = CSI + "38;5;208m" + SQR + RST
                display_state = "waiting..."
            elif cat.state == "compacting":
                indicator = CSI + "38;5;117m" + DOT + RST
                display_state = cat.state
            elif cat.state in ("thinking", "cooking", "reading", "browsing"):
                indicator = CSI + "32m" + DOT + RST
                display_state = cat.state
            else:
                indicator = CSI + "31m" + SQR + RST
                display_state = cat.state
            state_text = indicator + fg + BOLD + display_state + RST + "  " + DIM + ago + RST
            if cat.reaction_msg:
                msg_color = CSI + "31m" if cat.reaction == "error" else CSI + "33m"
                state_text += "  " + msg_color + BOLD + cat.reaction_msg + RST
        id_text = DIM + cat.session_id[:16] + RST
        if cat.state == "idle" and cat.last_tool:
            id_text += "  " + DIM + "last:" + cat.last_tool + RST
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
            stats = "%-8s %-10s %-10s" % (ctx_s + " ctx", cost_s, tok_s + " tok")
            if turns_s:
                stats += "  " + turns_s
            if age_s:
                stats += "  " + age_s
        raw_msg = cat.last_message or ""
        msg = ""
        if raw_msg:
            term_w = getattr(self, "_term_w", 80)
            sprite_w = len(sprite[0]) if sprite else 14
            max_msg = max(5, term_w - sprite_w - 6)
            if len(raw_msg) > max_msg:
                msg = raw_msg[:max(2, max_msg - 3)] + "..."
            else:
                msg = raw_msg
        wrapped = registry_is_wrapped(cat.session_id) if cat.session_id else False
        star = " *" if wrapped else ""
        mode = registry_get_approve_mode(cat.session_id) if cat.session_id else "manual"
        mode_badge = ""
        if mode == "automatic":
            mode_badge = "  " + CSI + "32m" + "[A]" + RST
        elif mode == "guarded":
            mode_badge = "  " + CSI + "33m" + "[G]" + RST
        is_selected = hasattr(self, "selected_idx") and cat.session_id == self.get_selected_sid()
        sel_prefix = CSI + "7m" + ">" + RST + " " if is_selected else ""
        name_text = sel_prefix + fg + BOLD + cat.name + RST + DIM + star + RST + mode_badge if cat.name else ""
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
            cwd_line = fg + cwd_short + RST if cwd_short else ""
            if rate_s:
                cwd_line += "  " + DIM + rate_s + RST
            cwd_line += "  " + id_text
            labels.append(cwd_line)
        else:
            labels.append(id_text)
        if stats:
            labels.append(stats)
        sep_w = getattr(self, "_term_w", 80)
        sprite_w = len(sprite[0]) if sprite else 14
        sep_len = max(5, sep_w - sprite_w - 4)
        labels.append(DIM + "\u2501" * sep_len + RST)
        if msg:
            labels.append(msg)
        if DEBUG:
            last_log = cat_last_log(cat.session_id)
            if last_log:
                log_display = re.sub(r"^\[[0-9a-f]{8}\] ", "", last_log)
                max_log = max(5, sep_len)
                if len(log_display) > max_log:
                    log_display = log_display[:max(2, max_log - 3)] + "..."
                labels.append(DIM + log_display + RST)
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
        try:
            self._term_w = os.get_terminal_size().columns
        except OSError:
            self._term_w = 80
        valid = [(sid, self.cats[sid]) for sid in self.cat_order
                 if sid in self.cats and self.cats[sid].cwd
                 and registry_is_wrapped(sid)]
        unmonitored = [(sid, self.cats[sid]) for sid in self.cat_order
                       if sid in self.cats and self.cats[sid].cwd and not self.cats[sid].dead
                       and not registry_is_wrapped(sid)]
        all_active = valid + unmonitored
        if all_active:
            out += self._render_status_bar(all_active, now)
        title_cat = None
        if self.prompt_queue:
            title_cat = self.cats.get(self.prompt_queue[0]["session_id"])
        if not title_cat:
            sel_sid = self.get_selected_sid()
            title_cat = self.cats.get(sel_sid) if sel_sid else None
        if title_cat:
            tfg = CSI + "38;5;%dm" % title_cat.color
            title_name = (title_cat.name or title_cat.session_id[:16]).upper()
            term_w = self._term_w
            pad = max(0, term_w - len(title_name) - 4)
            out += tfg + BOLD + "  " + title_name + RST + tfg + DIM + " " + "\u2500" * pad + RST + CLRL + "\n"
        out += self._render_prompt_widget(now)
        if not valid and not unmonitored:
            out += CLRL + "\n"
            out += DIM + "  no active sessions" + RST + CLRL + "\n"
            out += DIM + "  start claude code to wake a cat" + RST + CLRL + "\n"
        else:
            from collections import OrderedDict
            groups = OrderedDict()
            for sid, cat in valid:
                d = cat.project_dir or cat.cwd or "unknown"
                groups.setdefault(d, []).append((sid, cat))
            for proj_dir, members in groups.items():
                proj_short = os.path.basename(proj_dir.rstrip("/"))
                base_color = members[0][1].color or 208
                fg = CSI + "38;5;%dm" % base_color
                term_w = self._term_w
                header = " " + proj_short + " "
                pad = max(0, term_w - len(header) - 2)
                out += fg + DIM + "\u2500\u2500" + RST + fg + BOLD + header + RST + fg + DIM + "\u2500" * pad + RST + CLRL + "\n"
                for i, (sid, cat) in enumerate(members):
                    out += self._render_cat(cat, now, show_dir=True)
                    out += CLRL + "\n"
        alive_names = {cat.name for cat in self.cats.values()}
        visible_graves = [t for t in self.graveyard if t.get("name") not in alive_names]
        if visible_graves:
            term_w = self._term_w
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
        if unmonitored:
            term_w = self._term_w
            pad = max(0, term_w - 18)
            out += DIM + "\u2500\u2500 unmonitored " + "\u2500" * pad + RST + CLRL + "\n"
            for sid, cat in unmonitored:
                cwd_short = os.path.basename((cat.project_dir or cat.cwd or "").rstrip("/"))
                ago = self._format_ago(now - cat.last_event)
                out += "  " + DIM + (cat.name or sid[:16]) + "  " + cwd_short + "  " + ago + RST + CLRL + "\n"
        if not self.prompt_queue and not self.input_mode:
            out += DIM + "  tab=select  \\=mode  C=color  enter=input  1-9=respond  Q=quit" + RST + CLRL + "\n"
        out += CLRB
        sys.stdout.write(out)
        sys.stdout.flush()


def litter_mode(sprite_data=None):
    import fcntl
    import termios
    import tty

    from . import __main__ as _main

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
    _log("claude-cat v%s litter started", _main.VERSION)
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
            import select
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    raw = os.read(fd, 1)
                    if raw == b"\x1b":
                        import select as _sel2
                        if _sel2.select([fd], [], [], 0.05)[0]:
                            raw += os.read(fd, 4)
                    ch = raw.decode("utf-8", errors="ignore")

                    if litter.input_mode:
                        if ch in ("\r", "\n"):
                            litter.send_input()
                        elif ch == "\x1b" or ch == "\x03":
                            litter.cancel_input()
                        elif ch == "\x7f" or ch == "\x08":
                            litter.input_buffer = litter.input_buffer[:-1]
                        elif len(ch) == 1 and ch >= " ":
                            litter.input_buffer += ch
                    elif litter.prompt_queue and ch in ("1", "2", "3", "4", "5",
                                                        "6", "7", "8", "9", "\r", "\n"):
                        litter.handle_prompt_response(ch)
                    elif ch == "\x1b[A" or ch == "\x1b[Z":
                        litter.cycle_cat(1)
                    elif ch == "\x1b[B" or ch == "\t":
                        litter.cycle_cat(-1)
                    elif ch in ("\r", "\n"):
                        litter.start_input()
                    elif ch == "\\":
                        sid = litter.get_selected_sid()
                        if sid:
                            cur = registry_get_approve_mode(sid)
                            nxt = {"manual": "guarded", "guarded": "automatic",
                                   "automatic": "manual"}.get(cur, "manual")
                            litter.toggle_approve_mode(nxt)
                    elif ch == "C":
                        sid = litter.get_selected_sid()
                        cat = litter.cats.get(sid) if sid else None
                        if cat:
                            cat.color = random.choice(PALETTE)
                            registry_set_color(cat.session_id, cat.color)
                            registry_flush_force()
                    elif ch == "Q":
                        break
                    elif ch == "\x03":
                        break
                except OSError:
                    pass
    finally:
        registry_flush_force()
        _close_logging()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.remove(lock_path)
        except OSError:
            pass
