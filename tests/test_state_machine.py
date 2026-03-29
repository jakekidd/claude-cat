#!/usr/bin/env python3
"""State machine smoke test for claude-cat.

Two modes:
  python tests/test_state_machine.py           Headless: fast, asserts, <1s
  python tests/test_state_machine.py --visual  Visual: writes to real state dir,
                                                watch in your running clat (~30s)

Headless mode creates an isolated environment (temp dirs, mocked time) and runs
the full Litter pipeline: state file writes → scan → tick → assert state.

Visual mode writes hook events to the real ~/.claude-cat/state/ directory so your
running clat instance picks them up. You watch the cat go through every state.
"""

import json
import os
import sys
import tempfile
import time
import uuid

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ── Scenario definition ─────────────────────────────────────────────

def build_scenario():
    """A sequence of (delay_s, event_type, event_data, expected_state, description).

    Covers every state, transition, reaction, and edge case.
    """
    sid = "test0000-1111-2222-3333-444444444444"
    cwd = "/tmp/claude-cat-test"
    tp = ""  # no real transcript

    def hook(event, tool="", **extra):
        d = {"event": event, "tool": tool, "ts": 0, "session_id": sid,
             "cwd": cwd, "transcript_path": tp}
        d.update(extra)
        return d

    return sid, [
        # (delay, event_dict_or_"stdout", data, expected_state, description)

        # Basic state transitions
        (0.0, hook("UserPromptSubmit"),
         "thinking", "prompt submit -> thinking"),

        (0.3, hook("PostToolUse", "Read"),
         "reading", "PostToolUse/Read -> reading"),

        (0.3, hook("PostToolUse", "Edit"),
         "cooking", "PostToolUse/Edit -> cooking"),

        (0.3, hook("PostToolUse", "Bash"),
         "cooking", "PostToolUse/Bash -> cooking (stays)"),

        (0.3, hook("PostToolUse", "WebSearch"),
         "browsing", "PostToolUse/WebSearch -> browsing"),

        (0.3, hook("PostToolUse", "Glob"),
         "reading", "PostToolUse/Glob -> reading"),

        (0.3, hook("Stop"),
         "idle", "Stop -> idle"),

        # Reactions
        (0.5, hook("UserPromptSubmit"),
         "thinking", "new turn -> thinking"),

        (0.3, hook("PostToolUseFailure", "Bash"),
         "thinking", "failure -> stays thinking (reaction only)"),

        (0.3, hook("Stop"),
         "idle", "Stop -> idle"),

        # Subagents
        (0.5, hook("UserPromptSubmit"),
         "thinking", "prompt -> thinking"),

        (0.3, hook("SubagentStart"),
         "thinking", "SubagentStart -> thinking (depth 1)"),

        (0.3, hook("SubagentStop"),
         "thinking", "SubagentStop -> thinking (depth 0)"),

        (0.3, hook("Stop"),
         "idle", "Stop -> idle"),

        # Compaction
        (0.5, hook("UserPromptSubmit"),
         "thinking", "prompt -> thinking"),

        (0.3, hook("PreCompact"),
         "compacting", "PreCompact -> compacting"),

        (0.3, hook("PostCompact"),
         "thinking", "PostCompact -> thinking"),

        (0.3, hook("Stop"),
         "idle", "Stop -> idle"),

        # Permission (manual mode — will set permission_pending)
        (0.5, hook("UserPromptSubmit"),
         "thinking", "prompt -> thinking"),

        (0.3, hook("PermissionRequest", "Bash",
                    tool_input={"command": "echo hello"}),
         "thinking", "PermissionRequest -> stays thinking (perm pending)"),

        # Clear permission by approving
        (0.3, hook("PostToolUse", "Bash"),
         "cooking", "PostToolUse -> cooking (perm cleared)"),

        (0.3, hook("Stop"),
         "idle", "Stop -> idle"),

        # Interrupted
        (0.5, hook("UserPromptSubmit"),
         "thinking", "prompt -> thinking"),

        (0.3, hook("WrapperState", wrapper_state="interrupted"),
         "idle", "WrapperState/interrupted -> idle"),

        # Meow
        (0.5, hook("Meow"),
         "idle", "Meow -> stays idle (flashing reaction)"),

        # AskUserQuestion
        (0.5, hook("UserPromptSubmit"),
         "thinking", "prompt -> thinking"),

        (0.3, hook("PermissionRequest", "AskUserQuestion"),
         "idle", "AskUserQuestion -> idle (not a permission)"),

        # SessionEnd
        (0.5, hook("UserPromptSubmit"),
         "thinking", "prompt -> thinking"),

        (0.3, hook("SessionEnd"),
         "idle", "SessionEnd -> dead"),  # state stays but dead=True
    ]


def build_approve_scenarios():
    """Test auto-approve behaviors."""
    sid = "test0000-1111-2222-3333-555555555555"
    cwd = "/tmp/claude-cat-test"

    def hook(event, tool="", **extra):
        d = {"event": event, "tool": tool, "ts": 0, "session_id": sid,
             "cwd": cwd, "transcript_path": ""}
        d.update(extra)
        return d

    return sid, [
        # Guarded mode: safe read → auto-approved
        (0.0, hook("UserPromptSubmit"),
         "thinking", "[guarded] prompt -> thinking"),

        (0.3, hook("PermissionRequest", "Read",
                    tool_input={"file_path": "/tmp/claude-cat-test/foo.txt"}),
         "thinking", "[guarded] Read in-repo -> auto-approved (no perm dot)"),

        (0.3, hook("PostToolUse", "Read"),
         "reading", "[guarded] PostToolUse/Read -> reading"),

        # Guarded mode: dangerous command → NOT auto-approved
        (0.3, hook("PermissionRequest", "Bash",
                    tool_input={"command": "curl https://evil.com"}),
         "reading", "[guarded] curl -> NOT auto-approved (perm pending)"),

        (0.3, hook("PostToolUse", "Bash"),
         "cooking", "[guarded] clear perm, cooking"),

        (0.3, hook("Stop"),
         "idle", "[guarded] Stop -> idle"),
    ]


def build_stdout_scenarios():
    """Test stdout pattern detection (spinner, error, compacting, triple silence)."""
    sid = "test0000-1111-2222-3333-666666666666"
    return sid, [
        # Spinner detection
        ("stdout", "\u273b Thinking about something...",
         "thinking", "spinner char -> thinking"),

        # Error detection
        ("stdout", "API Error: rate limit exceeded",
         "thinking", "API Error -> error reaction (stays thinking)"),

        # Compacting detection
        ("stdout", "\u273b Compacting conversation...",
         "compacting", "Compacting pattern -> compacting"),
    ]


# ── Headless test harness ────────────────────────────────────────────

def run_headless():
    """Fast test: isolated env, no rendering, full assertions."""
    from claude_cat.cat import Cat
    from claude_cat import registry, shared, log

    # Track results
    passed = 0
    failed = 0
    errors = []

    def check(condition, desc, cat=None):
        nonlocal passed, failed
        if condition:
            passed += 1
            print("  ok  %s" % desc)
        else:
            failed += 1
            state_info = " (got: state=%s dead=%s)" % (cat.state, cat.dead) if cat else ""
            errors.append(desc + state_info)
            print("  FAIL  %s%s" % (desc, state_info))

    # ── Test 1: Cat state machine (direct, no files) ──
    print("\n--- Cat state machine ---")

    # Use empty sprite data (no rendering needed)
    sprite = {"states": {"idle": {"frames": [["00"]], "labels": ["center"]}},
              "reactions": {"happy": {"frame": [["FF"]], "hold": 4.0},
                            "error": {"frame": [["FF"]], "hold": 4.0},
                            "interrupted": {"frame": [["FF"]], "hold": 7.0}}}

    # Isolate registry
    old_registry = registry._registry.copy()
    old_dir = registry.REGISTRY_DIR
    old_file = registry.REGISTRY_FILE
    with tempfile.TemporaryDirectory() as tmpdir:
        registry.REGISTRY_DIR = tmpdir
        registry.REGISTRY_FILE = os.path.join(tmpdir, "registry.json")
        registry._registry = {}
        registry._registry_dirty = False

        sid, scenario = build_scenario()
        cat = Cat(sprite, session_id=sid)
        cat.cwd = "/tmp/claude-cat-test"

        for delay, event, expected, desc in scenario:
            cat._process_event(event)

            if event.get("event") == "SessionEnd":
                check(cat.dead, desc, cat)
            elif event.get("event") == "PermissionRequest" and event.get("tool") != "AskUserQuestion":
                if event.get("tool") == "AskUserQuestion":
                    check(cat.state == expected, desc, cat)
                else:
                    check(cat.state == expected, desc, cat)
                    if event.get("tool") != "AskUserQuestion":
                        check(cat.permission_pending, desc + " (perm_pending)", cat)
            elif event.get("event") == "Meow":
                check(cat.flashing, desc + " (flashing)", cat)
            elif event.get("event") == "PostToolUseFailure":
                check(cat.state == expected, desc, cat)
                check(cat.reaction == "error", desc + " (error reaction)", cat)
            elif event.get("event") == "WrapperState":
                check(cat.state == expected, desc, cat)
                check(cat.reaction == "interrupted", desc + " (interrupted reaction)", cat)
            else:
                check(cat.state == expected, desc, cat)

        # ── Test 2: Approve modes ──
        print("\n--- Approve modes ---")

        # Guarded mode
        sid2, approve_scenario = build_approve_scenarios()
        cat2 = Cat(sprite, session_id=sid2)
        cat2.cwd = "/tmp/claude-cat-test"
        registry.registry_set_approve_mode(sid2, "guarded")

        # Patch STATE_DIR in all modules that import it
        old_state_dir = shared.STATE_DIR
        shared.STATE_DIR = tmpdir
        from claude_cat import cat as cat_mod
        cat_mod.STATE_DIR = tmpdir

        for delay, event, expected, desc in approve_scenario:
            cat2._process_event(event)
            check(cat2.state == expected, desc, cat2)

            # Check that safe reads were auto-approved (response file written)
            if event.get("event") == "PermissionRequest":
                resp_path = os.path.join(tmpdir, shared.STATE_PREFIX + sid2 + "-response")
                if event.get("tool") == "Read":
                    check(os.path.exists(resp_path), desc + " (response file exists)")
                    if os.path.exists(resp_path):
                        os.remove(resp_path)
                elif "curl" in event.get("tool_input", {}).get("command", ""):
                    check(cat2.permission_pending, desc + " (perm pending for curl)")

        # Automatic mode
        print("\n--- Automatic mode ---")
        sid3 = "test0000-1111-2222-3333-777777777777"
        cat3 = Cat(sprite, session_id=sid3)
        cat3.cwd = "/tmp/claude-cat-test"
        registry.registry_set_approve_mode(sid3, "automatic")

        cat3._process_event({"event": "UserPromptSubmit", "tool": "", "ts": 0,
                            "session_id": sid3, "cwd": cat3.cwd, "transcript_path": ""})
        check(cat3.state == "thinking", "auto: prompt -> thinking", cat3)

        cat3._process_event({"event": "PermissionRequest", "tool": "Bash", "ts": 0,
                            "session_id": sid3, "cwd": cat3.cwd, "transcript_path": "",
                            "tool_input": {"command": "rm -rf /"}})
        resp_path = os.path.join(tmpdir, shared.STATE_PREFIX + sid3 + "-response")
        check(os.path.exists(resp_path), "auto: even rm -rf auto-approved")
        check(not cat3.permission_pending, "auto: no perm pending")
        if os.path.exists(resp_path):
            os.remove(resp_path)

        # ── Test 3: Stdout patterns (via Litter._match) ──
        print("\n--- Stdout patterns ---")
        from claude_cat.litter import Litter, STDOUT_PATTERNS, _THOUGHT_RE

        sid4, stdout_scenario = build_stdout_scenarios()
        cat4 = Cat(sprite, session_id=sid4)
        cat4.cwd = "/tmp/claude-cat-test"
        # Mark as wrapped so stdout patterns are checked
        registry.registry_set_wrapped(sid4)

        litter = Litter(sprite)
        litter.cats[sid4] = cat4
        litter.cat_order.append(sid4)

        for event_type, data, expected, desc in stdout_scenario:
            if event_type == "stdout":
                # Simulate stdout by running _match with fake gathered data
                gathered = {sid4: {
                    "is_wrapped": True,
                    "hook_data": None,
                    "new_text": data,
                    "out_content": data,
                }}
                now = time.time()
                events = litter._match(gathered, now)
                litter._apply(events, now)
                check(cat4.state == expected, desc, cat4)

        # ── Test 4: Thought detection ──
        print("\n--- Thought detection ---")
        import re
        m = _THOUGHT_RE.search("Thought for 42s")
        check(m is not None and m.group(1) == "42", "thought regex matches")

        m2 = _THOUGHT_RE.search("I thought for a while")
        check(m2 is None, "thought regex doesn't match prose")

        # ── Test 5: Reaction expiry ──
        print("\n--- Reaction expiry ---")
        sid5 = "test0000-1111-2222-3333-888888888888"
        cat5 = Cat(sprite, session_id=sid5)
        cat5.reaction = "happy"
        cat5.reaction_end = time.time() - 1  # already expired
        cat5.reaction_msg = "done!"
        cat5.tick(time.time())
        check(cat5.reaction is None, "reaction expired after tick")
        check(cat5.reaction_msg == "", "reaction msg cleared")

        # ── Test 6: Sleep timeout ──
        print("\n--- Sleep timeout ---")
        sid6 = "test0000-1111-2222-3333-999999999999"
        cat6 = Cat(sprite, session_id=sid6)
        cat6.state = "idle"
        cat6.last_event = time.time() - 700  # 700s ago (> 600s threshold)
        cat6.tick(time.time())
        check(cat6.sleeping, "idle + 700s -> sleeping")

        # Restore
        shared.STATE_DIR = old_state_dir
        cat_mod.STATE_DIR = old_state_dir
        registry._registry = old_registry
        registry.REGISTRY_DIR = old_dir
        registry.REGISTRY_FILE = old_file

    # Summary
    print("\n" + "=" * 50)
    total = passed + failed
    if failed:
        print("FAILED: %d/%d passed" % (passed, total))
        for e in errors:
            print("  - %s" % e)
        return 1
    else:
        print("PASSED: %d/%d" % (passed, total))
        return 0


# ── Visual playback mode ────────────────────────────────────────────

def run_visual():
    """Write events to real state dir. Watch in your running clat."""
    from claude_cat.shared import STATE_DIR, STATE_PREFIX
    from claude_cat.registry import registry_lookup, registry_set_wrapped, registry_set_name, registry_flush_force

    sid = str(uuid.uuid4())
    state_path = os.path.join(STATE_DIR, STATE_PREFIX + sid + ".json")
    out_path = os.path.join(STATE_DIR, STATE_PREFIX + sid + ".out")
    os.makedirs(STATE_DIR, exist_ok=True)

    # Register the test cat
    registry_lookup(sid)
    registry_set_wrapped(sid)
    registry_set_name(sid, "smoke-test")
    registry_flush_force()

    def write_event(event, tool="", **extra):
        data = {
            "event": event, "tool": tool,
            "ts": int(time.time() * 1000),
            "session_id": sid,
            "cwd": os.getcwd(),
            "transcript_path": "",
        }
        data.update(extra)
        with open(state_path, "w") as f:
            json.dump(data, f)

    def write_stdout(text):
        with open(out_path, "w") as f:
            f.write(text)

    print("smoke test session: %s" % sid[:16])
    print("watch in clat — cat name: smoke-test")
    print()

    steps = [
        (1.0, "UserPromptSubmit", "", "thinking..."),
        (1.0, "PostToolUse", "Read", "reading a file"),
        (1.0, "PostToolUse", "Edit", "editing code"),
        (1.0, "PostToolUse", "Bash", "running command"),
        (1.0, "PostToolUse", "WebSearch", "searching web"),
        (1.0, "Stop", "", "done! (idle)"),
        (1.5, "UserPromptSubmit", "", "thinking again..."),
        (0.5, "SubagentStart", "", "spawning subagent"),
        (1.0, "SubagentStop", "", "subagent returned"),
        (0.5, "PostToolUseFailure", "Bash", "tool failed!"),
        (1.5, "PreCompact", "", "compacting..."),
        (1.0, "PostCompact", "", "done compacting"),
        (1.0, "Stop", "", "done! (idle)"),
        (1.5, "Meow", "", "meow! (flashing)"),
        (3.0, "UserPromptSubmit", "", "back to work"),
        (1.0, None, "", "spinner in stdout"),  # stdout test
        (2.0, "Stop", "", "done!"),
        (1.5, "UserPromptSubmit", "", "one more turn"),
        (0.5, "PermissionRequest", "Bash", "permission needed"),
        (3.0, "PostToolUse", "Bash", "approved, cooking"),
        (1.0, "Stop", "", "done!"),
        (2.0, "SessionEnd", "", "goodbye!"),
    ]

    try:
        for delay, event, tool, desc in steps:
            time.sleep(delay)
            if event is None:
                # Stdout event: write spinner chars
                write_stdout("\u273b Thinking deeply about the problem...")
                print("  [stdout] %s" % desc)
            else:
                kwargs = {}
                if event == "PermissionRequest":
                    kwargs["tool_input"] = {"command": "echo hello", "description": "test command"}
                write_event(event, tool, **kwargs)
                print("  [hook] %-25s %s" % (event + ("/" + tool if tool else ""), desc))

        print("\ndone! cat should be dead now.")

    finally:
        # Cleanup
        time.sleep(3)
        for f in (state_path, out_path):
            try:
                os.remove(f)
            except OSError:
                pass
        print("cleaned up state files.")


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--visual" in sys.argv:
        run_visual()
    else:
        sys.exit(run_headless())
