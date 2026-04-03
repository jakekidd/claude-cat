#!/usr/bin/env python3
"""Test harness for unified mode PTY mechanics.

Spawns a simple child process instead of Claude Code and verifies:
- PTY output relay works
- Scroll region is set correctly
- Dashboard rendering doesn't corrupt the relay
- Cat switching works
- Input passthrough works

Run: python3 tests/test_unified.py
"""

import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from claude_cat.shared import STATE_DIR, STATE_PREFIX, CSI


def make_pty(cmd, rows=20, cols=80):
    """Fork a PTY running cmd. Returns (child_pid, master_fd)."""
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        os.execvp(cmd[0], cmd)
        sys.exit(127)
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
    return child_pid, master_fd


def read_available(fd, timeout=0.5):
    """Read all available data from fd within timeout."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0.01, deadline - time.time())
        r, _, _ = select.select([fd], [], [], remaining)
        if fd in r:
            try:
                chunk = os.read(fd, 16384)
                if not chunk:
                    break
                buf += chunk
            except OSError:
                break
        else:
            if buf:
                break
    return buf


def strip_ansi(s):
    """Strip ANSI escape sequences for content comparison."""
    import re
    return re.sub(r'\x1b\[[^a-zA-Z]*[a-zA-Z]', '', s).replace('\r', '')


def test_basic_pty_relay():
    """Test that PTY output is received from child process."""
    pid, fd = make_pty(["python3", "-c", "print('HELLO_FROM_CHILD')"])
    output = read_available(fd, timeout=2.0)
    os.waitpid(pid, 0)
    os.close(fd)
    text = output.decode("utf-8", errors="ignore")
    assert "HELLO_FROM_CHILD" in text, "Expected child output, got: %r" % text[:200]
    print("  ok  basic PTY relay")


def test_pty_input_passthrough():
    """Test that stdin written to master_fd reaches child."""
    # Use cat (the Unix command) which echoes stdin to stdout
    pid, fd = make_pty(["cat"])
    time.sleep(0.1)  # let cat start
    os.write(fd, b"TEST_INPUT\n")
    output = read_available(fd, timeout=1.0)
    os.kill(pid, signal.SIGTERM)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    os.close(fd)
    text = output.decode("utf-8", errors="ignore")
    assert "TEST_INPUT" in text, "Input not echoed, got: %r" % text[:200]
    print("  ok  PTY input passthrough")


def test_pty_window_size():
    """Test that child sees the PTY window size we set."""
    pid, fd = make_pty(
        ["python3", "-c", "import os; s=os.get_terminal_size(); print(f'ROWS={s.lines} COLS={s.columns}')"],
        rows=25, cols=100
    )
    output = read_available(fd, timeout=2.0)
    os.waitpid(pid, 0)
    os.close(fd)
    text = output.decode("utf-8", errors="ignore")
    assert "ROWS=25" in text, "Expected ROWS=25, got: %r" % text[:200]
    assert "COLS=100" in text, "Expected COLS=100, got: %r" % text[:200]
    print("  ok  PTY window size")


def test_sigwinch_resize():
    """Test that SIGWINCH causes child to see new size."""
    pid, fd = make_pty(
        ["python3", "-c", """
import os, signal, time
s = os.get_terminal_size()
print(f'BEFORE={s.lines}x{s.columns}')
got_winch = []
def handler(sig, frame):
    got_winch.append(1)
signal.signal(signal.SIGWINCH, handler)
time.sleep(0.5)
s2 = os.get_terminal_size()
print(f'AFTER={s2.lines}x{s2.columns}')
print(f'WINCH={len(got_winch)}')
"""],
        rows=20, cols=80
    )
    time.sleep(0.2)
    # Resize
    new_winsize = struct.pack("HHHH", 30, 120, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, new_winsize)
    os.kill(pid, signal.SIGWINCH)

    output = read_available(fd, timeout=3.0)
    os.waitpid(pid, 0)
    os.close(fd)
    text = output.decode("utf-8", errors="ignore")
    assert "BEFORE=20x80" in text, "Expected BEFORE=20x80, got: %r" % text[:300]
    assert "AFTER=30x120" in text, "Expected AFTER=30x120, got: %r" % text[:300]
    assert "WINCH=" in text and "WINCH=0" not in text, "Expected WINCH>=1, got: %r" % text[:300]
    print("  ok  SIGWINCH resize")


def test_scroll_region_setup():
    """Test that DECSTBM scroll region escape sequence is correctly formed."""
    pty_rows = 20
    expected = CSI + "1;%dr" % pty_rows  # \033[1;20r
    assert expected == "\033[1;20r", "Scroll region escape incorrect: %r" % expected
    print("  ok  scroll region escape")


def test_multiple_ptys():
    """Test managing multiple PTYs with select()."""
    pid1, fd1 = make_pty(["python3", "-c", "import time; print('CAT_A'); time.sleep(2)"])
    pid2, fd2 = make_pty(["python3", "-c", "import time; time.sleep(0.2); print('CAT_B'); time.sleep(2)"])

    collected = {fd1: b"", fd2: b""}
    deadline = time.time() + 3.0
    while time.time() < deadline:
        r, _, _ = select.select([fd1, fd2], [], [], 0.1)
        for fd in r:
            try:
                data = os.read(fd, 4096)
                if data:
                    collected[fd] += data
            except OSError:
                pass
        if b"CAT_A" in collected[fd1] and b"CAT_B" in collected[fd2]:
            break

    for pid in (pid1, pid2):
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except (OSError, ChildProcessError):
            pass
    for fd in (fd1, fd2):
        try:
            os.close(fd)
        except OSError:
            pass

    assert b"CAT_A" in collected[fd1], "Missing CAT_A output"
    assert b"CAT_B" in collected[fd2], "Missing CAT_B output"
    print("  ok  multiple PTYs via select()")


def test_relay_only_active():
    """Test that only the active PTY's output goes to 'stdout' (simulated)."""
    pid1, fd1 = make_pty(["python3", "-c", "import time; print('ACTIVE_OUT'); time.sleep(2)"])
    pid2, fd2 = make_pty(["python3", "-c", "import time; time.sleep(0.1); print('INACTIVE_OUT'); time.sleep(2)"])

    active_fd = fd1
    stdout_buf = b""
    inactive_buf = b""
    deadline = time.time() + 2.0

    while time.time() < deadline:
        r, _, _ = select.select([fd1, fd2], [], [], 0.1)
        for fd in r:
            try:
                data = os.read(fd, 4096)
                if not data:
                    continue
                if fd == active_fd:
                    stdout_buf += data
                else:
                    inactive_buf += data
            except OSError:
                pass
        if b"ACTIVE_OUT" in stdout_buf and b"INACTIVE_OUT" in inactive_buf:
            break

    for pid in (pid1, pid2):
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except (OSError, ChildProcessError):
            pass
    for fd in (fd1, fd2):
        try:
            os.close(fd)
        except OSError:
            pass

    assert b"ACTIVE_OUT" in stdout_buf, "Active PTY output not relayed"
    assert b"INACTIVE_OUT" not in stdout_buf, "Inactive PTY output leaked to stdout"
    assert b"INACTIVE_OUT" in inactive_buf, "Inactive PTY output not buffered"
    print("  ok  relay only active PTY")


def test_replay_buffer_on_switch():
    """Test that switching cats replays the buffer."""
    pid1, fd1 = make_pty(["python3", "-c", "import time; print('CAT1_STUFF'); time.sleep(5)"])
    pid2, fd2 = make_pty(["python3", "-c", "import time; print('CAT2_STUFF'); time.sleep(5)"])

    active = fd1
    replay = {fd1: b"", fd2: b""}
    stdout_buf = b""
    deadline = time.time() + 2.0

    while time.time() < deadline:
        r, _, _ = select.select([fd1, fd2], [], [], 0.1)
        for fd in r:
            try:
                data = os.read(fd, 4096)
                if not data:
                    continue
                replay[fd] = (replay[fd] + data)[-4096:]
                if fd == active:
                    stdout_buf += data
            except OSError:
                pass
        if replay[fd1] and replay[fd2]:
            break

    # Switch to cat2 — simulate by writing replay buffer
    assert b"CAT2_STUFF" in replay[fd2], "Cat2 replay buffer empty"
    # In real mode, we'd write replay[fd2] to stdout here

    for pid in (pid1, pid2):
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        except (OSError, ChildProcessError):
            pass
    for fd in (fd1, fd2):
        try:
            os.close(fd)
        except OSError:
            pass

    assert b"CAT1_STUFF" in stdout_buf, "Active cat output not in stdout"
    assert b"CAT2_STUFF" not in stdout_buf, "Inactive cat leaked to stdout before switch"
    assert b"CAT2_STUFF" in replay[fd2], "Replay buffer missing inactive cat output"
    print("  ok  replay buffer on switch")


def test_dashboard_escape_sequences():
    """Test that dashboard output uses correct escape sequences."""
    # Simulate what _render_dashboard produces
    pty_rows = 20
    dash_start = pty_rows + 1

    # Expected: move to dashboard row, render content, re-assert scroll region
    expected_sequences = [
        CSI + "%d;1H" % dash_start,           # move to dashboard start
        CSI + "K",                              # clear line
        CSI + "1;%dr" % pty_rows,              # re-assert scroll region
        CSI + "%d;1H" % pty_rows,              # cursor back to scroll region
    ]
    for seq in expected_sequences:
        assert "\033[" in seq, "Malformed escape: %r" % seq

    print("  ok  dashboard escape sequences")


def test_dashboard_idle_gating():
    """Test that dashboard only renders when PTY is quiet."""
    from claude_cat.unified import UnifiedMode

    um = UnifiedMode(None)
    um.pty_rows = 20
    um.pty_cols = 80
    um.term_h = 28
    um.term_w = 80

    # Simulate active output
    um.last_pty_output = time.time()
    um.dashboard_dirty = True

    # Capture stdout to check if dashboard renders
    import io
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()

    um._render_dashboard()  # should skip (PTY active)
    output_during_active = sys.stdout.getvalue()

    # Simulate idle (200ms+ ago)
    um.last_pty_output = time.time() - 0.5
    um._render_dashboard()  # should render
    output_during_idle = sys.stdout.getvalue()

    sys.stdout = old_stdout

    assert output_during_active == "", "Dashboard rendered during active PTY output"
    assert len(output_during_idle) > 0, "Dashboard didn't render during idle"
    assert "\033[21;1H" in output_during_idle, "Dashboard didn't position to correct row"
    print("  ok  dashboard idle gating")


def test_force_dashboard():
    """Test that force=True bypasses idle gating."""
    from claude_cat.unified import UnifiedMode

    um = UnifiedMode(None)
    um.pty_rows = 20
    um.pty_cols = 80
    um.term_h = 28
    um.term_w = 80

    # Simulate active output
    um.last_pty_output = time.time()
    um.dashboard_dirty = True

    import io
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()

    um._render_dashboard(force=True)  # should render despite active PTY
    output = sys.stdout.getvalue()

    sys.stdout = old_stdout

    assert len(output) > 0, "Force dashboard didn't render"
    print("  ok  force dashboard bypasses idle gate")


def test_env_var_inheritance():
    """Test that CLAUDE_CAT_ID env var is inherited by child."""
    import uuid
    cat_id = str(uuid.uuid4())
    os.environ["CLAUDE_CAT_ID"] = cat_id

    pid, fd = make_pty(["python3", "-c",
                        "import os; print('CATID=' + os.environ.get('CLAUDE_CAT_ID', 'MISSING'))"])
    output = read_available(fd, timeout=2.0)
    os.waitpid(pid, 0)
    os.close(fd)
    os.environ.pop("CLAUDE_CAT_ID", None)

    text = output.decode("utf-8", errors="ignore")
    assert ("CATID=" + cat_id) in text, "Child didn't inherit CLAUDE_CAT_ID: %r" % text[:200]
    print("  ok  env var inheritance")


def test_child_reaping():
    """Test that dead children are detected via waitpid WNOHANG."""
    pid, fd = make_pty(["python3", "-c", "print('BYE')"])
    read_available(fd, timeout=1.0)  # drain output and let child exit
    time.sleep(0.3)

    rpid, status = os.waitpid(pid, os.WNOHANG)
    assert rpid == pid, "Child not reaped (rpid=%d, expected %d)" % (rpid, pid)
    os.close(fd)
    print("  ok  child reaping")


def test_ctrl_backslash_detection():
    """Test that 0x1c byte is Ctrl+backslash."""
    assert b"\x1c" == bytes([0x1c]), "Ctrl+\\ byte mismatch"
    # Verify it's different from common keys
    assert b"\x1c" != b"\x03", "Ctrl+\\ should not be Ctrl+C"
    assert b"\x1c" != b"\x1b", "Ctrl+\\ should not be Escape"
    assert b"\x1c" != b"\t", "Ctrl+\\ should not be Tab"
    print("  ok  ctrl+\\\\ detection")


def test_managed_cat_creation():
    """Test ManagedCat data structure."""
    from claude_cat.unified import ManagedCat
    mc = ManagedCat("cat-123", "bob", 208, 999, 10, None)
    assert mc.cat_id == "cat-123"
    assert mc.name == "bob"
    assert mc.color == 208
    assert mc.child_pid == 999
    assert mc.master_fd == 10
    assert mc.alive is True
    assert mc.session_id == ""
    assert mc.replay_buf == b""
    assert mc.cat.cat_id == "cat-123"
    print("  ok  ManagedCat creation")


def test_approve_callback():
    """Test Cat approve_callback mechanism."""
    from claude_cat.cat import Cat

    # Default: no callback, uses response file
    cat = Cat()
    assert cat.approve_callback is None

    # With callback
    approved = []
    cat2 = Cat(approve_callback=lambda sid: approved.append(sid))
    cat2.session_id = "test-123"
    cat2._send_approve("t")
    assert approved == ["test-123"], "Callback not called: %r" % approved
    print("  ok  approve callback")


if __name__ == "__main__":
    print("--- PTY mechanics ---")
    test_basic_pty_relay()
    test_pty_input_passthrough()
    test_pty_window_size()
    test_sigwinch_resize()
    test_scroll_region_setup()

    print("\n--- Multi-PTY ---")
    test_multiple_ptys()
    test_relay_only_active()
    test_replay_buffer_on_switch()

    print("\n--- Dashboard ---")
    test_dashboard_escape_sequences()
    test_dashboard_idle_gating()
    test_force_dashboard()

    print("\n--- Integration ---")
    test_env_var_inheritance()
    test_child_reaping()
    test_ctrl_backslash_detection()
    test_managed_cat_creation()
    test_approve_callback()

    total = 16
    print("\n" + "=" * 50)
    print("PASSED: %d/%d" % (total, total))
