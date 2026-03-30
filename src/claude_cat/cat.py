"""Cat state machine — the core state management class."""

import datetime
import json
import os
import random
import re
import time

from .shared import STATE_DIR, STATE_PREFIX, state_file_for, project_dir_from_transcript
from .log import _log, _trace
from .registry import (
    registry_lookup, registry_get_approve_mode, registry_update_stats,
    _is_guarded_safe, PALETTE,
)

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
    "ToolSearch": "reading",
    "EnterPlanMode": "thinking",
    "ExitPlanMode": "thinking",
    "TaskCreate": "thinking",
    "TaskUpdate": "thinking",
    "TaskGet": "reading",
    "TaskList": "reading",
    "AskUserQuestion": "thinking",
    "SendMessage": "thinking",
}

# Adjacency map for idle gaze drift
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

OVERLAYS = {
    "bulb": {"art": [" \u259e\u259a", " \u259c\u259b"], "duration": 3.0},
    "plug": {"art": [" \u2596\u2597", " \u259c\u259b"], "duration": 4.0},
}


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
        self.state_file = state_file_for(session_id) if session_id else ""
        # State: idle | thinking | reading | cooking | browsing | compacting
        self.state = "idle"
        self.sleeping = False  # visual only, label still says "idle"
        self.permission_pending = False  # orange dot indicator
        self.permission_tool = ""  # tool name for pending permission
        self.permission_input = {}  # tool_input for pending permission
        self.flashing = False  # meow flash (5s color cycling)
        self.flash_end = 0.0
        self.subagent_depth = 0    # number of active subagents
        # Stdout tee parsing state (litter-side)
        self.out_file = os.path.join(STATE_DIR, STATE_PREFIX + session_id + ".out") if session_id else ""
        self.last_out_mtime = 0.0
        self.last_out_content = ""
        self.last_out_change = 0.0  # when .out content last changed (for idle detection)
        self.last_spinner_seen = 0.0  # when spinner chars last appeared in stdout
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
                        if re.search(r"^\s*1\.", text, re.MULTILINE) and \
                           re.search(r"^\s*2\.", text, re.MULTILINE):
                            result = self._parse_question(text)
                            if result and result.get("options"):
                                return result
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

    # (input $/M, output $/M, cache_read $/M)
    MODEL_PRICING = {
        "opus":   (15.0, 75.0, 1.50),
        "sonnet": (3.0,  15.0, 0.30),
        "haiku":  (0.80, 4.0,  0.08),
    }

    def est_cost(self):
        """Rough cost estimate based on detected model."""
        inp, out, cache = self.MODEL_PRICING["opus"]  # default
        for key, rates in self.MODEL_PRICING.items():
            if key in self.model:
                inp, out, cache = rates
                break
        return (
            self.total_input * inp / 1_000_000
            + self.total_output * out / 1_000_000
            + self.total_cache * cache / 1_000_000
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
                    if self.thought_seconds:
                        self.reaction_msg = "thought %ds" % self.thought_seconds
                        self.thought_seconds = 0
                    else:
                        self.reaction_msg = "done!"
                    self.overlay = "bulb"
                    self.overlay_end = time.time() + OVERLAYS["bulb"]["duration"]
        elif ev == "PermissionRequest":
            if tool == "AskUserQuestion":
                self.state = "idle"
                self.reaction = "surprised"
                self.reaction_end = time.time() + 8.0
                self.reaction_msg = "asking..."
                _log("[%s] AskUserQuestion -> idle/asking (answer in session window)", sid_short)
            else:
                mode = registry_get_approve_mode(self.session_id)
                if mode == "automatic":
                    try:
                        resp_path = os.path.join(STATE_DIR, STATE_PREFIX + self.session_id + "-response")
                        with open(resp_path, "w") as f:
                            f.write("1")
                    except OSError:
                        pass
                    _log("[%s] auto-approved (automatic mode) tool=%s depth=%d",
                         sid_short, tool, self.subagent_depth)
                elif mode == "guarded" and _is_guarded_safe(tool, data.get("tool_input", {}), self.cwd):
                    try:
                        resp_path = os.path.join(STATE_DIR, STATE_PREFIX + self.session_id + "-response")
                        with open(resp_path, "w") as f:
                            f.write("1")
                    except OSError:
                        pass
                    _log("[%s] guarded-approved tool=%s depth=%d", sid_short, tool, self.subagent_depth)
                elif self.subagent_depth > 0:
                    _log("[%s] subagent permission (depth=%d) tool=%s — skipping prompt",
                         sid_short, self.subagent_depth, tool)
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
            if old_state not in ("idle", "compacting"):
                self.reaction = "happy"
                self.reaction_end = time.time() + self.reactions.get("happy", {}).get("hold", 2.0)
                self.reaction_msg = "returned"
        elif ev == "PostToolUseFailure":
            self.reaction = "error"
            self.reaction_end = time.time() + self.reactions.get("error", {}).get("hold", 4.0)
            self.reaction_msg = "%s failed" % tool if tool else "tool failed"
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
        self.last_event = time.time()


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
                else:
                    cur_label = labels[self.frame_idx] if self.frame_idx < len(labels) else ""
                    neighbors = _NEIGHBORS.get(cur_label)
                    if labels and neighbors and random.random() < 0.65:
                        target = random.choice(neighbors)
                        if target in labels:
                            self.frame_idx = labels.index(target)
                    else:
                        self.frame_idx = random.randint(start, len(frames) - 1)
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

        # idle + 10min => sleeping (visual only, applies to all sessions)
        if self.state == "idle" and not self.sleeping and not self.reaction and quiet > 600:
            _log("[%s] timeout: idle -> sleeping (%.0fs quiet)", self.session_id[:8], quiet)
            self.sleeping = True
            _trace(self.session_id, "timeout", "600s_quiet", "idle", "sleeping", quiet=round(quiet, 1))
            self.frame_idx = 0
            dirty = True

        # ── Transcript refresh ──
        active = self.state not in ("idle", "compacting")
        if active and self.transcript_path and now - self.last_transcript_read > 2.0:
            self._read_last_message(self.transcript_path)
            if not self.stats_read or now - self._last_stats_read > 30:
                self._read_stats(self.transcript_path)
                self._last_stats_read = now
                _log("[%s] stats refresh: %dk ctx, $%.2f, %d turns",
                     self.session_id[:8], self.context_k, self.est_cost(), self.human_turns)
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
