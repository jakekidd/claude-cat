#!/usr/bin/env python3
"""claude-cat -- a 1-bit companion cat for Claude Code."""

import json
import os
import random
import re
import signal
import sys
import time
from pathlib import Path

from . import sprites as sprites_mod
from . import __version__ as VERSION

from .shared import (
    CSI, HIDE, SHOW, CLR, CLRL, CLRB, BOLD, DIM, RST, HOME,
    STATE_DIR, STATE_PREFIX,
    state_file_for, find_session_files, render_hex_line,
)
from . import log as _log_mod
from .registry import (
    PALETTE,
    _load_registry, registry_lookup, registry_set_name, registry_flush_force,
    is_generated_name, _random_cat_name,
)

# ── Hook events ──────────────────────────────────────────────────────

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


# ── Small commands ───────────────────────────────────────────────────

def meow_mode():
    """Identify which cat this session is. Writes a Meow event to flash the cat."""
    files = find_session_files()
    if not files:
        print("No active cats found.")
        sys.exit(1)
    my_cwd = os.getcwd()
    candidates = []
    for path in files:
        try:
            with open(path) as f:
                data = json.loads(f.read())
            file_cwd = data.get("cwd", "")
            mt = os.path.getmtime(path)
            cwd_match = os.path.realpath(file_cwd) == os.path.realpath(my_cwd) if file_cwd else False
            candidates.append((cwd_match, mt, path, data))
        except (OSError, json.JSONDecodeError):
            continue
    if not candidates:
        print("No active cats found.")
        sys.exit(1)
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    _, _, best, _ = candidates[0]
    if not best:
        print("No active cats found.")
        sys.exit(1)
    try:
        with open(best) as f:
            data = json.loads(f.read())
        sid = data.get("session_id", "")
        if not sid:
            bn = os.path.basename(best)
            sid = bn[len(STATE_PREFIX):-len(".json")]
        name, color = registry_lookup(sid) if sid else ("unknown", 208)
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
        state_path = state_file_for(session_id) if session_id else os.path.join(STATE_DIR, "claude-cat.json")
        state = {
            "event": data.get("hook_event_name", "unknown"),
            "tool": data.get("tool_name", ""),
            "ts": int(time.time() * 1000),
            "session_id": session_id,
            "cwd": data.get("cwd", ""),
            "transcript_path": data.get("transcript_path", ""),
        }
        tool_input = data.get("tool_input")
        if tool_input and data.get("hook_event_name") == "PermissionRequest":
            state["tool_input"] = tool_input
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(state_path, "w") as f:
            json.dump(state, f)
    except Exception as e:
        try:
            sys.stderr.write("claude-cat hook error: %s\n" % e)
        except Exception:
            pass
    sys.exit(0)


def _hook_command():
    import shutil
    for name in ("claude-cat", "clat"):
        path = shutil.which(name)
        if path:
            return path + " --hook"
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


def demo_mode(sprite_data=None):
    from .cat import Cat

    sys.stdout.write(CLR)
    sys.stdout.flush()
    cat = Cat(sprite_data)
    all_states = list((sprite_data or {}).get("states", {}).keys())
    all_reactions = list((sprite_data or {}).get("reactions", {}).keys())
    def cleanup(*_):
        sys.stdout.write(SHOW + "\n")
        sys.stdout.flush()
        sys.exit(0)
    def render_demo(label):
        sprite = cat._get_sprite()
        out = HOME + HIDE
        out += CLRL + "\n" + CLRL + "\n" + CLRL + "\n"
        for line in sprite:
            out += render_hex_line(line, color=cat.color) + CLRL + "\n"
        out += CLRL + "\n" + DIM + label + RST + CLRL + "\n" + CLRB
        sys.stdout.write(out)
        sys.stdout.flush()
    signal.signal(signal.SIGINT, cleanup)
    for s in all_states:
        cat.state = s
        cat.reaction = None
        cat.frame_idx = 0
        render_demo(s)
        time.sleep(1.5)
    for r in all_reactions:
        cat.reaction = r
        render_demo(r)
        time.sleep(1.5)
    cleanup()


def tmux_ccm_mode():
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
    session = "claude-dashboard"
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, ccm])
    subprocess.run(["tmux", "split-window", "-v", "-t", session, clat])
    subprocess.run(["tmux", "select-pane", "-t", session + ":0.1"])
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


# ── Main entry ───────────────────────────────────────────────────────

def main():
    if sys.platform == "win32":
        print("claude-cat requires a Unix-like environment (macOS or Linux).")
        sys.exit(1)
    args = sys.argv[1:]
    sprite_name = None
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--sprite" and i + 1 < len(args):
            sprite_name = args[i + 1]
            i += 2
        elif args[i] == "--debug":
            _log_mod.DEBUG = True
            i += 1
        elif args[i] == "--trace":
            _log_mod.TRACE = True
            i += 1
        else:
            filtered.append(args[i])
            i += 1
    cmd = filtered[0] if filtered else ""
    sprite_data = None
    if cmd in ("", "--watch", "watch", "--demo", "demo"):
        sprite_data = sprites_mod.load(sprite_name)
    if cmd == "--rename" or (cmd == "code" and len(filtered) > 1 and filtered[1] == "--rename"):
        rename_args = filtered[1:] if cmd == "--rename" else filtered[2:]
        if not rename_args:
            print("Usage: clat --rename <session-name-or-id> [new-name]")
            sys.exit(1)
        target = rename_args[0]
        reg = _load_registry()
        found_sid = None
        found_name = None
        is_uuid = bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", target))
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
        new_name = re.sub(r"[^a-z0-9-]", "-", new_name.lower())
        new_name = re.sub(r"-+", "-", new_name).strip("-")
        if not new_name:
            print("Invalid name.")
            sys.exit(1)
        registry_lookup(found_sid)
        registry_set_name(found_sid, new_name)
        registry_flush_force()
        print("%s -> %s" % (found_name or found_sid[:16], new_name))
        sys.exit(0)
    elif cmd == "code":
        from .wrapper import code_mode

        child_args = []
        if "--" in sys.argv:
            dash_idx = sys.argv.index("--")
            child_args = sys.argv[dash_idx + 1:]
        elif len(filtered) > 1:
            code_args = filtered[1:]
            if code_args and not code_args[0].startswith("-"):
                val = code_args[0]
                rest = code_args[1:]
                is_uuid = bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", val))
                if is_uuid:
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
                        name = re.sub(r"[^a-z0-9-]", "-", name.lower())
                        name = re.sub(r"-+", "-", name).strip("-") or default_name
                        registry_lookup(val)
                        registry_set_name(val, name)
                        registry_flush_force()
                    child_args = ["claude", "--resume", val, "--name", name] + rest
                else:
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
                idx = code_args.index("--resume")
                if idx + 1 < len(code_args):
                    val = code_args[idx + 1]
                    rest = code_args[:idx] + code_args[idx + 2:]
                    reg = _load_registry()
                    if val in reg:
                        name = reg[val].get("name", "")
                        if not name or is_generated_name(name):
                            default_name = name or _random_cat_name()
                            try:
                                user_input = input("name this session (\"%s\"): " % default_name).strip()
                            except (EOFError, KeyboardInterrupt):
                                print()
                                sys.exit(0)
                            name = user_input if user_input else default_name
                            name = re.sub(r"[^a-z0-9-]", "-", name.lower())
                            name = re.sub(r"-+", "-", name).strip("-") or default_name
                            registry_set_name(val, name)
                            registry_flush_force()
                        child_args = ["claude", "--resume", val, "--name", name] + rest
                    else:
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
                            child_args = ["claude", "--resume", matches[0][0], "--name", matches[0][1] or matches[0][0][:16]] + rest
                        elif matches:
                            print("Session '%s' not found. Did you mean:" % val)
                            for sid, n in matches[:3]:
                                print("  %s  (%s)" % (n or sid[:16], sid[:16]))
                            sys.exit(1)
                        else:
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
        from .litter import litter_mode
        litter_mode(sprite_data)
    else:
        print("Unknown command: %s" % cmd)
        print_help()
        sys.exit(1)




if __name__ == "__main__":
    main()
