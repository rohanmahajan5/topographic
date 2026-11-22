"""Runs the Claude Code CLI in a process group of its own that dies with the worker that started it. Also used for
`opencode serve` (providers/opencode.py), which dies with the workspace server the same way.

Ported by hand on 2026-11-21 (robust) from the unmerged branch ide-providers-2026-11-09 (5f5b8903f), unchanged but for this
paragraph; its OpenCode use came over the same day with providers/opencode.py.

Why (docs/providers-study/claude.md §4, T3 Code 5ffe04593 and 3213a0c77): the CLI finishes its turn after its pipes
close, so a worker that is killed would leave the CLI working next to the session a later resume opens (two writers
corrupt a transcript); and MCP servers are the CLI's children, so only ending the whole group reaches them. The Python
Agent SDK has no spawn hook and its atexit handler only sends SIGTERM to the CLI's pid (never on SIGKILL), so the
worker points the SDK's cli_path at a one-line launcher that runs this file:

    python -I claude_wrap.py <real claude> <args...>

What it does (stdlib only; design from claude.md §4, polling in place of T3's fd-3 pipe because the SDK's spawn closes
inherited descriptors):
- forks; the child calls setsid() (a new session and process group, led by the CLI) and execs the real CLI with the
  stdio the SDK gave it;
- the parent drops its own copies of stdin, stdout and stderr, so the SDK sees end-of-file exactly when the CLI's side
  closes;
- SIGTERM, SIGINT or SIGHUP to the wrapper (the SDK's close escalation) is passed to the CLI's group, and the group is
  sent SIGKILL GRACE seconds later if anything in it is left (before the SDK's own SIGKILL of the wrapper, 5 s);
- every POLL seconds it checks that the worker still lives (its parent pid unchanged and AGENT_IDE_WORKER_PID alive);
  when the worker is gone it sends the group SIGTERM, then SIGKILL after GRACE;
- when the CLI exits, whatever is left in its group (MCP servers it started) gets SIGTERM, then SIGKILL after GRACE, and
  the wrapper exits with the CLI's exit status (128 + n for a signal, as a shell reports it).
"""
import os
import signal
import sys
import time

GRACE = 4.0          # seconds between SIGTERM and SIGKILL to the group; below the SDK's 5 s before it kills the wrapper
POLL = 0.25


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def main(argv):
    if len(argv) < 2:
        sys.stderr.write('usage: claude_wrap.py <claude binary> [args...]\n')
        return 2
    real, args = argv[1], argv[2:]
    parent = os.getppid()
    try:
        watch = int(os.environ.get('AGENT_IDE_WORKER_PID') or parent)
    except ValueError:
        watch = parent
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            os.execv(real, [real] + args)
        finally:
            os._exit(127)
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    stopping = {'at': None}

    def on_signal(sig, frame):
        if stopping['at'] is None:
            stopping['at'] = time.monotonic()
        signal_group(pid, signal.SIGTERM)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)
    code = 1
    while True:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            break
        except InterruptedError:
            continue
        if done:
            code = os.waitstatus_to_exitcode(status)
            break
        if stopping['at'] is None and (os.getppid() != parent or not alive(watch)):
            stopping['at'] = time.monotonic()
            signal_group(pid, signal.SIGTERM)
        if stopping['at'] is not None and time.monotonic() - stopping['at'] > GRACE:
            signal_group(pid, signal.SIGKILL)
        time.sleep(POLL)
    # the CLI has exited: end what it left in its group
    if signal_group(pid, signal.SIGTERM):
        end = time.monotonic() + GRACE
        while time.monotonic() < end and signal_group(pid, 0):
            time.sleep(POLL)
        signal_group(pid, signal.SIGKILL)
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
