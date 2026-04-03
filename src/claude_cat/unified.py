"""Unified terminal mode — Claude Code PTY + dashboard in one window."""

import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
import uuid

from .shared import (
    CSI, HIDE, SHOW, BOLD, DIM, RST,
    STATE_DIR, STATE_PREFIX,
    state_file_for, find_session_files, project_dir_from_transcript,
)
from .log import _log, _init_logging, _close_logging, _register_cat_log
from .registry import (
    PALETTE,
    _load_registry, registry_lookup, registry_touch,
    registry_set_wrapped, registry_set_cat_id, registry_set_name,
    registry_set_approve_mode, registry_get_approve_mode,
    registry_find_by_cat_id, registry_rebind_cat,
    registry_flush, registry_flush_force,
    _random_cat_name, is_generated_name,
    _registry,
)
from .cat import Cat, TOOL_STATES

_ANSI_RE = re.compile(r'\x1b\[[^m]*m')

DASH_HEIGHT = 8
MIN_HEIGHT = 28
MIN_WIDTH = 60

# Stdout patterns for state detection (reused from litter)
SPINNER_CHARS = set("\u00b7\u273b\u273d\u2736\u2733\u2722")


class ManagedCat:
    """One Claude Code PTY session managed by the unified process."""

    def __init__(self, cat_id, name, color, child_pid, master_fd, sprite_data):
        self.cat_id = cat_id
        self.name = name
        self.color = color
        self.child_pid = child_pid
        self.master_fd = master_fd
        self.alive = True
        self.session_id = ""
        self.cat = Cat(sprite_data)
        self.cat.cat_id = cat_id
        self.cat.name = name
        self.cat.color = color
        self.replay_buf = b""
        self.dead_since = None


class UnifiedMode:
    """Single-process unified terminal: Claude Code PTY + compact dashboard."""

    def __init__(self, sprite_data):
        self.sprite_data = sprite_data
        self.cats = {}          # cat_id -> ManagedCat
        self.cat_order = []     # cat_ids in display order
        self.active_cat_id = None
        self.stdin_fd = sys.stdin.fileno()
        self.stdout_fd = sys.stdout.fileno()
        self.running = True
        self.prefix_mode = False
        self.name_prompt = False
        self.name_buffer = ""
        self.old_term = None
        self.term_h = 24
        self.term_w = 80
        self.pty_rows = 16
        self.pty_cols = 80

    # ── Terminal setup ──────────────────────────────────────────────

    def _compute_layout(self):
        try:
            size = os.get_terminal_size()
            self.term_h = size.lines
            self.term_w = size.columns
        except OSError:
            self.term_h = 24
            self.term_w = 80
        self.pty_rows = max(10, self.term_h - DASH_HEIGHT)
        self.pty_cols = self.term_w

    def _setup_scroll_region(self):
        sys.stdout.write(CSI + "1;%dr" % self.pty_rows)
        sys.stdout.flush()

    def _reset_scroll_region(self):
        sys.stdout.write(CSI + "r")
        sys.stdout.flush()

    def _setup_terminal(self):
        self.old_term = termios.tcgetattr(self.stdin_fd)
        tty.setraw(self.stdin_fd)
        self._compute_layout()
        # Clear screen, hide cursor, set scroll region
        sys.stdout.write(CSI + "2J" + CSI + "H" + HIDE)
        sys.stdout.flush()
        self._setup_scroll_region()

    def _restore_terminal(self):
        self._reset_scroll_region()
        sys.stdout.write(SHOW + "\r\n")
        sys.stdout.flush()
        if self.old_term:
            termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.old_term)

    # ── PTY management ──────────────────────────────────────────────

    def _get_winsize(self):
        return struct.pack("HHHH", self.pty_rows, self.pty_cols, 0, 0)

    def spawn_cat(self, name, resume_sid=None):
        """Fork a new Claude Code PTY and register it."""
        cat_id = str(uuid.uuid4())

        child_args = ["claude"]
        if resume_sid:
            child_args.extend(["--resume", resume_sid, "--name", name])
        else:
            child_args.extend(["--name", name])

        # Set env var so hooks embed cat_id in state files
        os.environ["CLAUDE_CAT_ID"] = cat_id

        child_pid, master_fd = pty.fork()

        if child_pid == 0:
            # Child process
            os.execvp(child_args[0], child_args)
            sys.exit(127)

        # Parent: restore env (each child gets its own via fork snapshot)
        os.environ.pop("CLAUDE_CAT_ID", None)

        # Set child PTY size
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, self._get_winsize())
        except OSError:
            pass

        # Pick color
        used = {mc.color for mc in self.cats.values()}
        available = [c for c in PALETTE if c not in used]
        color = available[0] if available else PALETTE[len(self.cats) % len(PALETTE)]

        mc = ManagedCat(cat_id, name, color, child_pid, master_fd, self.sprite_data)

        # Set approve callback so Cat writes directly to master_fd
        def _approve(sid, _fd=master_fd):
            try:
                os.write(_fd, b"1")
            except OSError:
                pass
        mc.cat.approve_callback = _approve

        self.cats[cat_id] = mc
        self.cat_order.append(cat_id)

        # Register in registry
        # Session ID will be discovered from state files later
        _log("[unified] spawned cat %s name=%s pid=%d", cat_id[:8], name, child_pid)

        return cat_id

    def _kill_cat(self, cat_id):
        mc = self.cats.get(cat_id)
        if not mc or not mc.alive:
            return
        try:
            os.kill(mc.child_pid, signal.SIGTERM)
        except OSError:
            pass
        mc.alive = False
        mc.dead_since = time.time()
        _log("[unified] killed cat %s", cat_id[:8])

    def _reap_children(self):
        for mc in self.cats.values():
            if not mc.alive:
                continue
            try:
                pid, status = os.waitpid(mc.child_pid, os.WNOHANG)
                if pid != 0:
                    mc.alive = False
                    mc.dead_since = time.time()
                    try:
                        os.close(mc.master_fd)
                    except OSError:
                        pass
                    _log("[unified] child exited cat=%s pid=%d", mc.cat_id[:8], mc.child_pid)
            except ChildProcessError:
                mc.alive = False
                mc.dead_since = time.time()

    def _cleanup_dead(self):
        """Remove dead cats after a delay."""
        now = time.time()
        for cat_id in list(self.cat_order):
            mc = self.cats.get(cat_id)
            if mc and not mc.alive and mc.dead_since and now - mc.dead_since > 5:
                self.cat_order.remove(cat_id)
                del self.cats[cat_id]
                if self.active_cat_id == cat_id:
                    self.active_cat_id = self.cat_order[0] if self.cat_order else None

    # ── Cat switching ───────────────────────────────────────────────

    def _switch_to(self, cat_id):
        if cat_id == self.active_cat_id:
            return
        mc = self.cats.get(cat_id)
        if not mc or not mc.alive:
            return

        self.active_cat_id = cat_id

        # Clear the scroll region and replay buffer
        sys.stdout.write(CSI + "1;1H" + CSI + "J")
        if mc.replay_buf:
            os.write(self.stdout_fd, mc.replay_buf)

        # Send SIGWINCH to force Claude Code to redraw
        try:
            os.kill(mc.child_pid, signal.SIGWINCH)
        except OSError:
            pass

        _log("[unified] switched to cat %s (%s)", cat_id[:8], mc.name)

    def _cycle_cat(self, direction=1):
        alive = [cid for cid in self.cat_order
                 if cid in self.cats and self.cats[cid].alive]
        if len(alive) < 2:
            return
        try:
            idx = alive.index(self.active_cat_id)
        except ValueError:
            idx = 0
        idx = (idx + direction) % len(alive)
        self._switch_to(alive[idx])

    # ── Input handling ──────────────────────────────────────────────

    def _handle_stdin(self, data):
        if self.name_prompt:
            self._handle_name_input(data)
            return

        if self.prefix_mode:
            self.prefix_mode = False
            self._handle_prefix_command(data)
            return

        if data == b"\x1c":  # Ctrl+backslash
            self.prefix_mode = True
            self._render_dashboard()  # show prefix indicator
            return

        # Normal mode: passthrough to active cat
        mc = self.cats.get(self.active_cat_id)
        if mc and mc.alive:
            try:
                os.write(mc.master_fd, data)
            except OSError:
                pass

    def _handle_prefix_command(self, data):
        ch = data[0:1]
        if ch == b"\x1c":
            # Double Ctrl+\ -> send literal to child
            mc = self.cats.get(self.active_cat_id)
            if mc and mc.alive:
                try:
                    os.write(mc.master_fd, b"\x1c")
                except OSError:
                    pass
        elif ch == b"\t":
            self._cycle_cat(1)
        elif ch == b"\x1b":
            # Shift+Tab (ESC [ Z)
            if len(data) >= 3 and data[1:3] == b"[Z":
                self._cycle_cat(-1)
        elif ch in (b"n", b"N"):
            self._start_name_prompt()
        elif ch in (b"d", b"D"):
            if self.active_cat_id:
                self._kill_cat(self.active_cat_id)
                alive = [cid for cid in self.cat_order
                         if cid in self.cats and self.cats[cid].alive]
                if alive:
                    self._switch_to(alive[0])
                else:
                    self.active_cat_id = None
        elif ch in (b"m", b"M"):
            mc = self.cats.get(self.active_cat_id)
            if mc and mc.session_id:
                cur = registry_get_approve_mode(mc.session_id)
                nxt = {"manual": "guarded", "guarded": "automatic",
                       "automatic": "manual"}.get(cur, "manual")
                registry_set_approve_mode(mc.session_id, nxt)
                registry_flush_force()
        elif ch in (b"q", b"Q"):
            self.running = False
        self._render_dashboard()

    def _start_name_prompt(self):
        self.name_prompt = True
        self.name_buffer = ""
        self._render_dashboard()

    def _handle_name_input(self, data):
        for b in data:
            ch = bytes([b])
            if ch in (b"\r", b"\n"):
                name = self.name_buffer.strip()
                if not name:
                    name = _random_cat_name()
                name = re.sub(r"[^a-z0-9-]", "-", name.lower())
                name = re.sub(r"-+", "-", name).strip("-") or _random_cat_name()
                self.name_prompt = False
                self.name_buffer = ""
                cat_id = self.spawn_cat(name)
                self._switch_to(cat_id)
                return
            elif ch == b"\x1b" or ch == b"\x03":
                self.name_prompt = False
                self.name_buffer = ""
                self._render_dashboard()
                return
            elif ch in (b"\x7f", b"\x08"):
                self.name_buffer = self.name_buffer[:-1]
            elif b >= 32:
                self.name_buffer += chr(b)
        self._render_dashboard()

    # ── Hook processing ─────────────────────────────────────────────

    def _process_hooks(self):
        """Scan state files and feed events to Cat state machines."""
        files = find_session_files()
        now = time.time()
        for path in files:
            try:
                basename = os.path.basename(path)
                sid = basename[len(STATE_PREFIX):-len(".json")]
                mtime = os.path.getmtime(path)
                with open(path) as f:
                    data = json.loads(f.read())
            except (OSError, json.JSONDecodeError):
                continue

            file_cat_id = data.get("cat_id", "")
            if not file_cat_id:
                continue

            mc = self.cats.get(file_cat_id)
            if not mc:
                continue

            # Associate session_id with this cat
            if mc.session_id != sid:
                old_sid = mc.session_id
                mc.session_id = sid
                mc.cat.session_id = sid
                mc.cat.state_file = path
                mc.cat.out_file = ""  # not used in unified mode
                # Register in registry
                if old_sid:
                    registry_rebind_cat(old_sid, sid, file_cat_id)
                else:
                    registry_lookup(sid)
                    registry_set_wrapped(sid)
                    registry_set_cat_id(sid, file_cat_id)
                    registry_set_name(sid, mc.name)
                registry_flush_force()
                _register_cat_log(sid)

            # Feed event to state machine
            if not hasattr(mc.cat, '_last_hook_mtime'):
                mc.cat._last_hook_mtime = 0.0
            if mtime > mc.cat._last_hook_mtime:
                mc.cat._last_hook_mtime = mtime
                mc.cat.cwd = data.get("cwd", mc.cat.cwd)
                tp = data.get("transcript_path", "")
                if tp and tp != mc.cat.transcript_path:
                    mc.cat.transcript_path = tp
                    mc.cat.project_dir = project_dir_from_transcript(tp)
                mc.cat.last_event = now
                mc.cat._process_event(data)
                registry_touch(sid)

    # ── Dashboard rendering ─────────────────────────────────────────

    def _render_dashboard(self):
        """Render compact dashboard below the scroll region."""
        out = ""
        # Save cursor position (inside Claude Code's area)
        out += "\033[s"

        dash_start = self.pty_rows + 1
        now = time.time()

        # Separator line
        sep_color = ""
        mc = self.cats.get(self.active_cat_id)
        if mc:
            sep_color = CSI + "38;5;%dm" % mc.color
        out += CSI + "%d;1H" % dash_start
        label = " " + (mc.name if mc else "clat") + " "
        pad = max(0, self.term_w - len(label) - 2)
        out += sep_color + DIM + "\u2500\u2500" + RST + sep_color + BOLD + label + RST
        out += sep_color + DIM + "\u2500" * pad + RST
        out += CSI + "K"

        # Status bar
        out += CSI + "%d;1H" % (dash_start + 1)
        total_input = sum(mc.cat.total_input for mc in self.cats.values())
        total_output = sum(mc.cat.total_output for mc in self.cats.values())
        total_cache = sum(mc.cat.total_cache for mc in self.cats.values())
        total_tok = total_input + total_output + total_cache
        total_cost = sum(mc.cat.est_cost() for mc in self.cats.values())
        if total_tok >= 1_000_000:
            tok_s = "%.1fM" % (total_tok / 1_000_000)
        elif total_tok >= 1000:
            tok_s = "%dk" % (total_tok // 1000)
        else:
            tok_s = "%d" % total_tok
        out += DIM + "  %s tok  $%.2f total" % (tok_s, total_cost) + RST + CSI + "K"

        # Cat strip
        out += CSI + "%d;1H" % (dash_start + 2)
        cat_parts = []
        for cid in self.cat_order:
            c = self.cats.get(cid)
            if not c:
                continue
            fg = CSI + "38;5;%dm" % c.color
            is_active = cid == self.active_cat_id
            prefix = "> " if is_active else "  "
            # Approve mode badge
            mode = registry_get_approve_mode(c.session_id) if c.session_id else "manual"
            badge = ""
            if mode == "automatic":
                badge = " " + CSI + "32m" + "[A]" + RST
            elif mode == "guarded":
                badge = " " + CSI + "33m" + "[G]" + RST

            # State dot
            DOT = "\u25cf"
            SQR = "\u25a0"
            if not c.alive:
                dot = CSI + "31m" + SQR + RST
                state_s = "dead"
            elif c.cat.permission_pending:
                dot = CSI + "38;5;208m" + SQR + RST
                state_s = "waiting"
            elif c.cat.state in ("thinking", "cooking", "reading", "browsing"):
                dot = CSI + "32m" + DOT + RST
                state_s = c.cat.state
            elif c.cat.state == "compacting":
                dot = CSI + "38;5;117m" + DOT + RST
                state_s = "compact"
            else:
                dot = CSI + "31m" + SQR + RST
                state_s = c.cat.state

            part = prefix + fg + BOLD + c.name + RST + badge + "  " + dot + " " + DIM + state_s + RST
            cat_parts.append(part)

        strip = "  ".join(cat_parts) if cat_parts else DIM + "  no cats -- ctrl+\\ n to create" + RST
        out += strip + CSI + "K"

        # Extra cat lines (if more than ~4 cats, we'd wrap — punt for now)
        for i in range(3, 6):
            out += CSI + "%d;1H" % (dash_start + i) + CSI + "K"

        # Help line
        out += CSI + "%d;1H" % (dash_start + 6)
        if self.prefix_mode:
            out += CSI + "33m" + BOLD + "  cmd: " + RST + DIM
            out += "tab=next  n=new  d=kill  m=mode  q=quit  esc=cancel" + RST
        elif self.name_prompt:
            out += DIM + "  name: " + RST + BOLD + self.name_buffer + RST + DIM + "_" + RST
        else:
            out += DIM + "  ctrl+\\=cmd  tab/shift+tab=switch" + RST
        out += CSI + "K"

        # Clear remaining dashboard lines
        out += CSI + "%d;1H" % (dash_start + 7) + CSI + "K"

        # Re-assert scroll region (safety)
        out += CSI + "1;%dr" % self.pty_rows

        # Restore cursor
        out += "\033[u"

        sys.stdout.write(out)
        sys.stdout.flush()

    # ── SIGWINCH ────────────────────────────────────────────────────

    def _handle_winch(self, signum=None, frame=None):
        self._compute_layout()
        self._setup_scroll_region()
        winsize = self._get_winsize()
        for mc in self.cats.values():
            if mc.alive:
                try:
                    fcntl.ioctl(mc.master_fd, termios.TIOCSWINSZ, winsize)
                    os.kill(mc.child_pid, signal.SIGWINCH)
                except OSError:
                    pass
        self._render_dashboard()

    # ── Main loop ───────────────────────────────────────────────────

    def run(self, initial_name=None, resume_sid=None):
        self._setup_terminal()
        signal.signal(signal.SIGWINCH, self._handle_winch)

        # Spawn initial cat if requested
        if initial_name or resume_sid:
            name = initial_name or _random_cat_name()
            cat_id = self.spawn_cat(name, resume_sid=resume_sid)
            self.active_cat_id = cat_id

        self._render_dashboard()

        last_dashboard = 0.0
        last_hook_scan = 0.0

        try:
            while self.running:
                # Build fd list
                fds = [self.stdin_fd]
                for mc in self.cats.values():
                    if mc.alive:
                        fds.append(mc.master_fd)

                try:
                    rlist, _, _ = select.select(fds, [], [], 0.05)
                except (select.error, OSError):
                    continue

                now = time.time()

                # Handle stdin
                if self.stdin_fd in rlist:
                    try:
                        data = os.read(self.stdin_fd, 4096)
                        if not data:
                            self.running = False
                            continue
                        self._handle_stdin(data)
                    except OSError:
                        self.running = False
                        continue

                # Handle PTY output from all cats
                for mc in list(self.cats.values()):
                    if not mc.alive:
                        continue
                    if mc.master_fd in rlist:
                        try:
                            data = os.read(mc.master_fd, 16384)
                            if not data:
                                mc.alive = False
                                mc.dead_since = now
                                continue
                            # Relay active cat's output to stdout
                            if mc.cat_id == self.active_cat_id:
                                os.write(self.stdout_fd, data)
                            # Buffer for replay on switch
                            mc.replay_buf = (mc.replay_buf + data)[-4096:]
                        except OSError:
                            mc.alive = False
                            mc.dead_since = now

                # Periodic: dashboard refresh
                if now - last_dashboard > 0.5:
                    self._render_dashboard()
                    last_dashboard = now

                # Periodic: hook scan
                if now - last_hook_scan > 0.3:
                    self._process_hooks()
                    registry_flush()
                    last_hook_scan = now

                # Reap dead children
                self._reap_children()
                self._cleanup_dead()

        finally:
            # Kill all children
            for mc in self.cats.values():
                if mc.alive:
                    try:
                        os.kill(mc.child_pid, signal.SIGTERM)
                    except OSError:
                        pass
            # Brief drain
            try:
                time.sleep(0.1)
                for mc in self.cats.values():
                    if mc.alive:
                        try:
                            os.waitpid(mc.child_pid, os.WNOHANG)
                        except (ChildProcessError, OSError):
                            pass
            except Exception:
                pass
            self._restore_terminal()


def unified_mode(sprite_data=None, initial_name=None, resume_sid=None):
    """Entry point for unified mode."""
    _init_logging()
    um = UnifiedMode(sprite_data)
    um.run(initial_name=initial_name, resume_sid=resume_sid)
    _close_logging()
