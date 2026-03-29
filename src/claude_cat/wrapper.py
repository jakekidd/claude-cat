"""PTY wrapper for Claude Code (clat code)."""

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from .shared import STATE_DIR, STATE_PREFIX, state_file_for, find_session_files
from .registry import (
    _load_registry, registry_lookup, registry_set_wrapped, registry_set_name,
    registry_flush_force, is_generated_name, _random_cat_name, _NAME_ADJ, _NAME_NOUN,
)

_ANSI_RE = re.compile(r'\x1b\[[^m]*m')


def _write_wrapper_state(session_id, wrapper_state, **extra):
    """Write a WrapperState event to the session's state file.
    Preserves cwd and transcript_path from existing state to avoid clearing them."""
    try:
        state_path = state_file_for(session_id)
        os.makedirs(STATE_DIR, exist_ok=True)
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


def _session_selector(stdin_fd):
    """Interactive session picker. Returns (action, value) or None on cancel.
    action: "new" (value=name) or "resume" (value=session_id).
    """
    import termios
    import tty

    from .shared import CSI, BOLD, DIM, RST, CLRL

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
    HIGHLIGHT = CSI + "38;5;117m"  # light blue
    MAX_VISIBLE = 5
    cursor = 0  # 0 = (new), 1+ = sessions
    scroll_offset = 0
    total = 1 + len(sessions)

    old_term = termios.tcgetattr(stdin_fd)
    lines_drawn = 0
    try:
        tty.setcbreak(stdin_fd)
        while True:
            if lines_drawn > 0:
                out = CSI + "%dA" % lines_drawn
            else:
                out = ""
            out += "\r" + CSI + "J"
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

            if total > MAX_VISIBLE:
                pos = "(%d/%d)" % (cursor + 1, total)
                out += DIM + "  " + pos + RST + CLRL + "\n"
                lines_drawn += 1

            sys.stdout.write(out)
            sys.stdout.flush()

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
                sys.stdout.write("\r" + CSI + "J")
                sys.stdout.flush()
                if cursor == 0:
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
            elif ch in ("\x03", "\x1b", "q"):
                sys.stdout.write("\r" + CSI + "J")
                sys.stdout.flush()
                return None
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)


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
            reg = _load_registry()
            entry = reg.get(value, {})
            cur_name = entry.get("name", "")
            if cur_name and is_generated_name(cur_name):
                try:
                    user_input = input("rename \"%s\"? (enter to keep): " % cur_name).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    sys.exit(0)
                if user_input:
                    new_name = re.sub(r"[^a-z0-9-]", "-", user_input.lower())
                    new_name = re.sub(r"-+", "-", new_name).strip("-")
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

    def get_winsize(fd):
        try:
            return struct.pack("HHHH", *struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)))
        except Exception:
            return struct.pack("HHHH", 24, 80, 0, 0)

    child_pid, master_fd = pty.fork()

    if child_pid == 0:
        os.execvp(child_args[0], child_args)
        sys.exit(127)

    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, get_winsize(stdin_fd))
    except Exception:
        pass

    def handle_winch(*_):
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, get_winsize(stdin_fd))
            os.kill(child_pid, signal.SIGWINCH)
        except Exception:
            pass
    signal.signal(signal.SIGWINCH, handle_winch)

    wrap_session_id = None
    wrap_session_name = None
    for i, arg in enumerate(child_args):
        if arg == "--resume" and i + 1 < len(child_args):
            val = child_args[i + 1]
            if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-', val):
                wrap_session_id = val
            else:
                wrap_session_name = wrap_session_name or val
        elif arg in ("--name", "-n") and i + 1 < len(child_args):
            wrap_session_name = child_args[i + 1]
    existing_files = set(find_session_files()) if not wrap_session_id else set()

    if wrap_session_id:
        registry_lookup(wrap_session_id)
        registry_set_wrapped(wrap_session_id)
        if wrap_session_name:
            registry_set_name(wrap_session_id, wrap_session_name)
        registry_flush_force()

    tty.setraw(stdin_fd)

    _last_escape_ts = 0.0
    _output_buf = b""
    _out_tee_buf = ""
    _out_tee_ts = 0.0
    _heartbeat_ts = 0.0

    try:
        while True:
            try:
                rlist, _, _ = select.select([stdin_fd, master_fd], [], [], 0.1)
            except select.error:
                break

            if stdin_fd in rlist:
                try:
                    data = os.read(stdin_fd, 4096)
                    if not data:
                        break
                    os.write(master_fd, data)
                    if len(data) == 1 and (data == b"\x1b" or data == b"\x03"):
                        _last_escape_ts = time.time()
                except OSError:
                    break

            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)

                    if wrap_session_id:
                        now = time.time()

                        if _last_escape_ts and now - _last_escape_ts < 3.0:
                            _output_buf = (_output_buf + data)[-4096:]
                            if b"Interrupted" in _output_buf:
                                _last_escape_ts = 0.0
                                _output_buf = b""
                                _write_wrapper_state(wrap_session_id, "interrupted")
                        else:
                            _output_buf = b""

                        chunk_text = data.decode("utf-8", errors="ignore")
                        clean_text = _ANSI_RE.sub("", chunk_text)
                        _out_tee_buf = (_out_tee_buf + clean_text)[-4096:]
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

            if not wrap_session_id:
                current = set(find_session_files())
                new_files = current - existing_files
                if new_files:
                    newest = max(new_files, key=lambda f: os.path.getmtime(f))
                    bn = os.path.basename(newest)
                    wrap_session_id = bn[len(STATE_PREFIX):-len(".json")]
                    registry_lookup(wrap_session_id)
                    registry_set_wrapped(wrap_session_id)
                    if wrap_session_name:
                        registry_set_name(wrap_session_id, wrap_session_name)
                        registry_flush_force()

            if wrap_session_id:
                resp_path = os.path.join(STATE_DIR, STATE_PREFIX + wrap_session_id + "-response")
                try:
                    if os.path.exists(resp_path):
                        resp_mtime = os.path.getmtime(resp_path)
                        if time.time() - resp_mtime < 0.5:
                            pass
                        else:
                            with open(resp_path) as rf:
                                response = rf.read().strip()
                            os.remove(resp_path)
                            if response in ("1", "2", "3"):
                                os.write(master_fd, response.encode())
                            elif response:
                                os.write(master_fd, (response + "\r").encode())
                except OSError:
                    pass

            if wrap_session_id:
                _now_hb = time.time()
                if _now_hb - _heartbeat_ts > 15.0:
                    _heartbeat_ts = _now_hb
                    try:
                        out_path = os.path.join(STATE_DIR, STATE_PREFIX + wrap_session_id + ".out")
                        Path(out_path).touch()
                    except OSError:
                        pass

            try:
                pid, status = os.waitpid(child_pid, os.WNOHANG)
                if pid != 0:
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
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if wrap_session_id:
            try:
                out_path = os.path.join(STATE_DIR, STATE_PREFIX + wrap_session_id + ".out")
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass

    try:
        if os.WIFEXITED(status):
            sys.exit(os.WEXITSTATUS(status))
        else:
            sys.exit(1)
    except NameError:
        sys.exit(1)
