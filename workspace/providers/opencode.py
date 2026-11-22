"""OpenCode, driven through its own HTTP + SSE server (`opencode serve`), not through ACP.

Why native: over ACP we would lose fork, the conversation snapshot, native steering and one server for every folder
(docs/providers-study/acp.md Part C, inferred from T3 Code's capability tables). Design: docs/providers-design.md "OpenCode".

Which OpenCode: version 2 (the docs site labels it "V2 (latest)"; routes under /api, events session.*), as described by
https://opencode.ai/docs/api (read 2026-11-09). OpenCode 1.x speaks a different API (/session, /event, message.part.*);
it is reported as found-but-unsupported with the upgrade hint, never driven. T3 Code supports 2.0.18 and newer.

Origins, marked where they matter below:
  [docs]  OpenCode's API reference (routes, query names, event type names);
  [T3]    T3 Code's OpenCode provider (packages/provider-opencode, clone 2da1b7431, MIT): body shapes, event payloads, and the
          fix commits named next to the code they explain (port of the design, not the code);
  [ours]  our own judgement, marked as such.

Failure classes from T3's fix history (git log -- packages/provider-opencode, 116 commits) and where each is handled:
  1 server start and auth .......... d796063f7 resolved password; b058babb1 database lock at start; c59ab7d92 slow wrappers;
                                     10cd1a534 home-relative binary; OpenCode2Server.ts "OpenCode 2 always requires one"
                                     -> Server.start, server_env, find_binary
  2 event stream health ............ 5ad00e9a0 stalled subscriptions; 45 s silence rule (OpenCode2Client.ts); unreadable
                                     frames skipped -> OpenCodeProvider._follow, sse_frames, _handle
  3 reconnect and lost ends ........ cb7d07393 a reconnect took a Stop's end; 200a373fb streamed text after reconnect;
                                     reconcile in v2/adapter.ts -> _reconcile, live text overlay in read_history
  4 turn end and stop .............. INTERRUPT_TIMEOUT 10 s, held end while steers wait (v2 endExecution), 43dfdcc84 tools
                                     stopped mid-run are interrupted -> stop, _on_exec_end, tool_status
  5 failures and retries ........... aac28657c retries shown; 2f021ee7d out-of-funds is not a usage limit; 6f4136480 Claude
                                     limits keep their reset time; 9f018954d `<none>` messages -> classify, readable_error
  6 tool rendering ................. 0992f5169 non-zero exit is failed; 28aed0dcb empty replacement kept; cfbfbca6e read
                                     output without XML envelope -> tool_status, diff_of, clean_output
  7 requests ....................... 04bd86951 question flags kept; permission reply with the agent's own decision ids;
                                     a request whose execution ended or whose reply 404s closes -> _ask_*, reply, _settle
  8 bounded history ................ 75c439f4e / b467be4ea bound history reads -> HISTORY_LIMIT
  Not taken (T3-only concerns): model catalogue caching (502ce3cf6, 807319c27: we do not cache a failed catalogue at all),
  T3's MCP tools and instructions entries (a5307c44f, f14fa03d5), usage-limit plans for OpenCode Go/Zen (9b0435369,
  f04546609), text generation (5d0de82dc, de6830f1d, 9facfc592), Windows and systemd process scopes (b6629dcd6, a0a601c26).
"""
import base64
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sys
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import uuid

from .base import Provider, relative, under
from . import core, planuse

WRAP = Path(__file__).resolve().parent.parent / 'claude_wrap.py'          # own process group, ended with this process
PREFIX = 'opencode:'
USERNAME = 'opencode'          # OpenCode accepts only HTTP Basic with this user name [T3 OpenCode2Client.ts]
INSTALL = ('OpenCode is not installed. Install OpenCode 2 with `curl -fsSL https://opencode.ai/v2/install | bash` '
           '(or `npm install -g @opencode/cli`, or Homebrew `opencode-v2`), then reload.')          # [docs] install page
UPGRADE = ('OpenCode %s is installed; this workspace drives OpenCode 2 (its 1.x API differs). Install OpenCode 2 with '
           '`curl -fsSL https://opencode.ai/v2/install | bash`; 2.x converts the shared database on first run.')
LISTENING = re.compile(r'server listening on\s+(https?://\S+)', re.I)          # [T3 OpenCodeRuntime.ts:285]
LOCKED = re.compile(r'SQLITE_BUSY|database is locked', re.I)          # [T3 b058babb1]
STARTUP_TIMEOUT = 30.0          # [T3 DEFAULT_OPENCODE_SERVER_TIMEOUT_MS]
INFO_TIMEOUT = 5.0              # [T3 OpenCode2Server.ts INFO_TIMEOUT]
SUBSCRIBE_TIMEOUT = 10.0        # [T3 5ad00e9a0]
SILENCE = 45.0                  # "Heartbeats arrive every 10-15 seconds; longer silence means stuck" [T3 OpenCode2Client.ts]
RECONNECT_ATTEMPTS, RECONNECT_DELAY = 5, 2.0          # [T3 v2/adapter.ts RECONNECT_ATTEMPTS, RECONNECT_DELAY]
STOP_WAIT = 10.0                # [T3 v2/adapter.ts INTERRUPT_TIMEOUT]
HISTORY_LIMIT = 500             # newest messages read per session [T3 75c439f4e bounds history reads; 500 is ours]
DEATHS, DEATH_WINDOW = 3, 60.0  # three server deaths in a minute and it stays down [ours, as the Codex bridge]

# ---------- tools -> the floor's kinds (harnesses.md table; OpenCode 2 names seen in T3's recordings: shell, read, glob,
# write, subagent, question; the rest from the docs' tool list) ----------
TOOL_KINDS = {'read': 'read', 'glob': 'read', 'grep': 'read', 'list': 'read', 'lsp': 'read',
              'edit': 'write', 'write': 'write', 'patch': 'write', 'apply_patch': 'write', 'multiedit': 'write',
              'shell': 'run', 'bash': 'run',
              'subagent': 'delegate', 'task': 'delegate',
              'question': 'ask'}
PATH_KEYS = ('filePath', 'path', 'file_path', 'file')


def kind_of(name):
    return TOOL_KINDS.get(str(name or '').lower(), 'tool')


def path_of(inp):
    for k in PATH_KEYS:
        v = inp.get(k) if isinstance(inp, dict) else None
        if isinstance(v, str) and v:
            return v
    return None


def summary(name, inp, cwd=None):
    """A tool call's one-line text, as the chat shows it."""
    inp = inp if isinstance(inp, dict) else {}
    k = kind_of(name)
    if k in ('read', 'write'):
        p = path_of(inp) or ''
        p = (relative(p, cwd) or p) if cwd and p else p
        return ' '.join(x for x in (name, p, inp.get('pattern') or '') if x)
    if k == 'run':
        return str(inp.get('command') or inp.get('description') or name)
    if k == 'delegate':
        return name + ': ' + str(inp.get('description') or str(inp.get('prompt', ''))[:80])
    if k == 'ask':
        qs = inp.get('questions') or []
        return 'asks: ' + (qs[0].get('question', '') if qs and isinstance(qs[0], dict) else '')
    return str(name)


def diff_of(name, inp):
    """{'old', 'new'} for a file change. An edit that deletes text keeps its empty replacement: '' is a value, not a
    missing one [T3 28aed0dcb]."""
    if kind_of(name) != 'write' or not isinstance(inp, dict):
        return None
    old = next((inp[k] for k in ('oldString', 'oldText') if isinstance(inp.get(k), str)), None)
    new = next((inp[k] for k in ('newString', 'content', 'newText') if isinstance(inp.get(k), str)), None)
    if old is None and new is None:
        return None
    return {'old': old, 'new': new}


def tool_status(name, state):
    """running | completed | failed | interrupted from a tool part's state. A shell command that exits non-zero is failed
    though OpenCode reports success [T3 0992f5169]; a tool Stop cut short is interrupted, not failed [T3 43dfdcc84]."""
    state = state if isinstance(state, dict) else {}
    st = state.get('status')
    meta = state.get('metadata') if isinstance(state.get('metadata'), dict) else {}
    if st == 'completed':
        code = meta.get('exit', meta.get('exitCode'))
        return 'failed' if kind_of(name) == 'run' and isinstance(code, int) and code != 0 else 'completed'
    if st == 'error':
        err = state.get('error')
        etype = err.get('type') if isinstance(err, dict) else None
        msg = (err.get('message') if isinstance(err, dict) else err) or ''
        aborted = etype == 'aborted' or meta.get('interrupted') is True or msg in ('Tool execution aborted', 'Tool execution interrupted', 'Task cancelled')
        return 'interrupted' if aborted else 'failed'
    return 'running' if st in ('running', 'pending', None) else str(st)


def clean_output(name, text):
    """A read tool's output without OpenCode's <path>/<type>/<content> envelope [T3 cfbfbca6e]."""
    if str(name).lower() != 'read' or not text:
        return text
    m = re.match(r'^<path>[^\r\n]+</path>\r?\n<type>(directory|file)</type>\r?\n', text)
    if not m:
        return text
    body = text[m.end():]
    env = (re.match(r'^<entries>\r?\n([\s\S]*?)\r?\n</entries>(?=\r?\n|$)', body) if m.group(1) == 'directory'
           else re.match(r'^<content>(?:\r?\n)?([\s\S]*?)\r?\n</content>(?=\r?\n|$)', body))
    return text if not env else env.group(1) + body[env.end():]


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    return ''


def readable_error(message=None, status=None, body=None):
    """OpenCode stores `<none>` when it cannot read the provider's body [T3 9f018954d, providerErrorMessage.ts]."""
    text = (message or '').strip()
    if text and text.lower() != '<none>':
        return text
    m = re.search(r'<title[^>]*>([^<]*)</title>', body or '', re.I)
    if m and m.group(1).strip():
        title = ' '.join(m.group(1).split())
        return '%s (HTTP %s).' % (title, status) if isinstance(status, int) else title
    if isinstance(status, int):
        return 'The model provider failed the request (HTTP %d).' % status
    return 'The model provider failed the request and did not include an error message.' if text else 'OpenCode ended the turn with an error.'


def classify(error):
    """A failed execution's error as {class, message, code, resetsAt} (core.FAILURE_CLASSES) [T3 v2/adapter.ts]:
    provider.quota is a usage limit unless HTTP 402 (an empty balance never resets: 2f021ee7d); provider.rate-limit is one;
    a spent Claude plan through OpenCode comes as Claude's own text and keeps its reset time (6f4136480)."""
    error = error if isinstance(error, dict) else {}
    etype, status = error.get('type'), error.get('status')
    resp = error.get('response') if isinstance(error.get('response'), dict) else {}
    message = readable_error(error.get('message'), status, resp.get('body'))
    claude = re.match(r"^You've hit your [^\n·]*\blimit\b", error.get('message') or '')
    reset = re.search(r'\bResets at (\d{4}-\d\d-\d\dT[\d:.]+Z)\.?\s*$', error.get('message') or '') if claude else None
    limited = (etype == 'provider.quota' and status != 402) or etype == 'provider.rate-limit' or bool(claude)
    return {'class': 'usage_limit' if limited else 'provider_error', 'message': message[:4096], 'code': etype,
            'resetsAt': reset.group(1) if reset else None}


def unwrap(body):
    """Responses wrap their payload as {data: ...} [T3 recordings]; lists of pending requests may come bare."""
    if isinstance(body, dict) and 'data' in body and set(body) <= {'data', 'cursor', 'location'}:
        return body['data']
    return body


def ms(v):
    return v / 1000.0 if isinstance(v, (int, float)) else None


# ---------- HTTP and SSE (stdlib) ----------
class HttpError(Exception):
    def __init__(self, status, text):
        super().__init__('OpenCode server answered HTTP %s%s' % (status, (': ' + text[:200]) if text else ''))
        self.status = status


class Http:
    """One OpenCode server: HTTP Basic `opencode:<password>`, encoded as UTF-8 (OpenCode requires UTF-8 credentials; Latin-1
    breaks non-ASCII passwords) [T3 OpenCode2Client.ts basicAuthorization]."""

    def __init__(self, host, port, password):
        self.host, self.port = host, port
        self.header = 'Basic ' + base64.b64encode(('%s:%s' % (USERNAME, password)).encode('utf-8')).decode('ascii')

    def url(self, path, query=None):
        q = urllib.parse.urlencode([(k, v) for k, v in (query or {}).items() if v is not None])
        return path + ('?' + q if q else '')

    def call(self, method, path, body=None, query=None, timeout=10.0):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            data = None if body is None else json.dumps(body).encode()
            headers = {'Authorization': self.header, 'Accept': 'application/json'}
            if data is not None:
                headers['Content-Type'] = 'application/json'
            conn.request(method, self.url(path, query), body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read()
        finally:
            conn.close()
        text = raw.decode('utf-8', 'replace')
        if r.status == 401:
            raise HttpError(401, 'The OpenCode server rejected the server password.')
        if r.status >= 400:
            raise HttpError(r.status, text)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise HttpError(r.status, 'not JSON (not an OpenCode 2 server?)')

    def subscribe(self, timeout=SUBSCRIBE_TIMEOUT, silence=SILENCE):
        """GET /api/event: (connection, response) once the server accepts; reads then time out after `silence` seconds
        without a byte (heartbeats included)."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        conn.request('GET', '/api/event', headers={'Authorization': self.header, 'Accept': 'text/event-stream'})
        r = conn.getresponse()
        if r.status != 200:
            r.read()
            conn.close()
            raise HttpError(r.status, 'event stream refused')
        if conn.sock is not None:
            conn.sock.settimeout(silence)
        return conn, r


def sse_frames(resp):
    """Yield each SSE frame's data (str), and None for a comment line (a heartbeat). Ends at EOF."""
    data = []
    while True:
        line = resp.readline()
        if not line:
            return
        line = line.decode('utf-8', 'replace').rstrip('\r\n')
        if not line:
            if data:
                yield '\n'.join(data)
                data = []
            continue
        if line.startswith(':'):
            yield None
            continue
        field, _, value = line.partition(':')
        if field == 'data':
            data.append(value[1:] if value.startswith(' ') else value)


# ---------- the server process ----------
def find_binary(env=None):
    """The `opencode` command as a list: AGENT_IDE_OPENCODE_BIN (home-relative allowed [T3 10cd1a534]), else PATH, else
    the installers' usual places [ours: guessed, not seen on this machine]."""
    env = os.environ if env is None else env
    home = env.get('HOME') or os.path.expanduser('~')

    def expand(p):          # the given environment's home, not this process's
        return home + p[1:] if p == '~' or p.startswith('~/') else p
    own = env.get('AGENT_IDE_OPENCODE_BIN')
    if own:
        p = expand(own)
        return [p] if os.path.exists(p) or shutil.which(p, path=env.get('PATH')) else None
    hit = shutil.which('opencode', path=env.get('PATH'))
    if hit:
        return [hit]
    for p in ('~/.opencode/bin/opencode', '~/.local/bin/opencode', '/opt/homebrew/bin/opencode', '/usr/local/bin/opencode'):
        p = expand(p)
        if os.access(p, os.X_OK):
            return [p]
    return None


def generation(version):
    m = re.match(r'^\s*v?(\d+)\.(\d+)', str(version or ''))
    return None if not m else ('v2' if int(m.group(1)) >= 2 else 'v1')


def server_env(base, password):
    """The server's environment: our generated password under both names. OPENCODE_PASSWORD is 2.x's and overrides;
    OPENCODE_SERVER_PASSWORD is 1.x's [T3 OpenCode2Server.ts serverEnvironment; docs server page]. An inherited user name
    is dropped so `opencode` holds [ours]."""
    env = {k: v for k, v in base.items() if k not in ('OPENCODE_SERVER_PASSWORD', 'OPENCODE_PASSWORD', 'OPENCODE_SERVER_USERNAME')}
    env['OPENCODE_PASSWORD'] = env['OPENCODE_SERVER_PASSWORD'] = password
    return env


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StartError(Exception):
    pass


class Server:
    """`opencode serve --hostname=127.0.0.1 --port=<free>` in its own process group, with our password; ready once it
    prints its listening line and answers GET /api/info within 5 s [T3 OpenCodeRuntime.ts, OpenCode2Server.ts]."""

    def __init__(self, command, env=None, cwd=None, startup_timeout=STARTUP_TIMEOUT):
        self.command, self.base_env, self.cwd, self.startup_timeout = command, dict(os.environ if env is None else env), cwd, startup_timeout
        self.proc = self.http = self.version = None
        self.inner = None          # OpenCode's own pid (its group) when it runs through the wrapper
        self.stderr = ''

    def start(self):
        for attempt in range(3):          # a held database lock at start is retried [T3 b058babb1]
            try:
                return self._start_once()
            except StartError as e:
                if 'database lock' not in str(e) or attempt == 2:
                    raise
                time.sleep(0.3 * (attempt + 1))

    def _start_once(self):
        port, password = free_port(), secrets.token_urlsafe(32)
        args = list(self.command) + ['serve', '--hostname=127.0.0.1', '--port=%d' % port]
        real = shutil.which(args[0]) or args[0]
        # through claude_wrap.py: `opencode serve` is an HTTP server that never notices its starter is gone, so a workspace
        # server that was stopped or killed left it running beside the next one, both working the same sessions (seen
        # 2026-11-10: seven left over, one resumed a stopped subagent). The wrapper ends its group when this process dies.
        if WRAP.exists() and os.path.isabs(real):
            args = [sys.executable, '-I', str(WRAP), real] + args[1:]
        env = dict(server_env(self.base_env, password), AGENT_IDE_WORKER_PID=str(os.getpid()))
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, cwd=self.cwd or os.path.expanduser('~'),
                                     start_new_session=True)          # own process group: killed whole on close
        ready, out = threading.Event(), {}
        self.stderr = ''

        def read_out():
            for raw in iter(self.proc.stdout.readline, b''):
                m = LISTENING.search(raw.decode('utf-8', 'replace'))
                if m and not ready.is_set():
                    out['url'] = m.group(1)
                    ready.set()
            ready.set()

        def read_err():
            for raw in iter(self.proc.stderr.readline, b''):
                self.stderr = (self.stderr + raw.decode('utf-8', 'replace'))[-65536:]          # [T3 64 KiB cap]
        threading.Thread(target=read_out, daemon=True, name='opencode-stdout').start()
        threading.Thread(target=read_err, daemon=True, name='opencode-stderr').start()
        ready.wait(self.startup_timeout)
        if 'url' not in out:
            try:          # its output closed: the exit follows (through the wrapper, up to a poll later)
                code = self.proc.wait(1.0) if ready.is_set() else self.proc.poll()
            except subprocess.TimeoutExpired:
                code = None
            time.sleep(0.05)          # let the last stderr lines drain [T3 OPENCODE_SERVER_EXIT_DRAIN_TIMEOUT]
            self.kill()
            if code is not None and LOCKED.search(self.stderr):
                raise StartError('OpenCode could not start: its database lock is held by another OpenCode.')
            if code is not None:
                raise StartError('OpenCode exited at start (code %s): %s' % (code, self.stderr.strip()[-300:]))
            raise StartError('Timed out waiting for the OpenCode server to start after %d s.' % self.startup_timeout)
        self.inner = self.wrapped_child(self.proc.pid) if self.proc.args[:2] == [sys.executable, '-I'] else self.proc.pid
        u = urllib.parse.urlparse(out['url'])
        self.http = Http(u.hostname or '127.0.0.1', u.port or port, password)
        try:
            info = self.http.call('GET', '/api/info', timeout=INFO_TIMEOUT)
        except HttpError as e:
            self.kill()
            raise StartError('The OpenCode server rejected the server password.' if e.status == 401
                             else 'The server is not an OpenCode 2 server (%s).' % e)
        except (OSError, socket.timeout):
            self.kill()
            raise StartError('Timed out waiting for the OpenCode server.')
        self.version = (info or {}).get('version') if isinstance(info, dict) else None
        if generation(self.version) != 'v2':
            self.kill()
            raise StartError(UPGRADE % (self.version or 'of an unknown version'))
        return self

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    @staticmethod
    def wrapped_child(pid):
        """The process the wrapper started (its only child), or None."""
        try:
            out = subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True, timeout=5).stdout.split()
            return int(out[0]) if out else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def kill(self):
        p, self.proc = self.proc, None
        inner, self.inner = self.inner, None
        if p is None:
            return
        # both groups: the wrapper's and OpenCode's own (the wrapper passes SIGTERM on, but a wrapper that is gone could not)
        groups = [g for g in (p.pid, inner) if g]
        for sig, wait in ((signal.SIGTERM, 6.0), (signal.SIGKILL, 1.0)):
            for g in groups:
                try:
                    os.killpg(g, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                p.wait(wait)
                if not inner or not _group_alive(inner):
                    break
            except subprocess.TimeoutExpired:
                continue
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except Exception:
                pass


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


# ---------- live state of one OpenCode session ----------
class Live:
    def __init__(self, sid, cwd=None, owned=False):
        self.sid, self.cwd, self.owned = sid, cwd, owned
        self.phase = 'idle'
        self.turn_open = self.exec_running = self.stop_requested = False
        self.steers = set()          # our steer inbox ids OpenCode has not delivered yet
        self.held_end = None         # an execution end waiting for those steers [T3 v2/adapter.ts endExecution]
        self.retry = None
        self.text = {}               # (assistantMessageID, kind, ordinal) -> text of a block still streaming
        self.context = None
        self.contexts = []
        self.model = self.agent = self.parent = self.title = None
        self.errors = []
        self.compacts = []
        self.tools = {}              # tool call id -> name, for this turn's tool events
        self.ended = threading.Condition()
        self.turns_ended = 0


class OpenCodeProvider(Provider):
    name = 'opencode'
    label = 'OpenCode'

    def __init__(self, publish=None, binary=None, env=None, data_dir=None, reconnect_attempts=RECONNECT_ATTEMPTS,
                 reconnect_delay=RECONNECT_DELAY, silence=SILENCE, stop_wait=STOP_WAIT, startup_timeout=STARTUP_TIMEOUT, ledger=None):
        self.publish_fn = publish
        self.env = dict(os.environ if env is None else env)
        self.binary = binary          # a command list (tests pass the fake); None: find_binary()
        self.data_dir = data_dir
        self.reconnect_attempts, self.reconnect_delay, self.silence = reconnect_attempts, reconnect_delay, silence
        self.stop_wait, self.startup_timeout = stop_wait, startup_timeout
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.server = None
        self.gen = 0
        self.connected = threading.Event()
        self.closing = False
        self.down = None              # why the server stays down
        self.deaths = []
        self.sessions = {}            # OpenCode session id -> Live
        self.pending = {}             # our request id -> {'msg', 'sid', 'native', 'kind', ...}
        self.skipped = set()          # event types skipped (unknown or unreadable), each noted once
        self.catalog = None
        self._probe = None
        # the sessions started (or forked) here, kept on disk so they stay driven here after the workspace restarts; None = kept
        # in memory only (tests). The ACP agents keep the same in their per-session records [ours, 2026-11-09: an
        # OpenCode agent turned watch-only after a restart]
        self.ledger = Path(ledger) if ledger else None
        self._load_ledger()
        self.refusals = planuse.Refusals()          # plan use: a model provider behind OpenCode refusing requests (quota, limit)

    def plan_usage(self):
        """No plan of its own (it uses the person's own keys and sign-ins), but its refusals show as its state."""
        r = planuse.reading(planuse.OPENCODE_NOTE)
        r['state'] = self.refusals.current()
        return r

    def _load_ledger(self):
        if not self.ledger:
            return
        try:
            rows = json.loads(self.ledger.read_text())
        except (OSError, ValueError):
            return
        for sid, r in (rows or {}).items():
            if isinstance(r, dict):
                s = self.sessions[sid] = Live(sid, r.get('cwd'), owned=True)
                s.model, s.agent, s.parent = r.get('model'), r.get('agent'), r.get('parent')

    def _save_ledger(self):
        if not self.ledger:
            return
        with self.lock:
            rows = {s.sid: {'cwd': s.cwd, 'model': s.model, 'agent': s.agent, 'parent': s.parent}
                    for s in self.sessions.values() if s.owned}
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.ledger.with_suffix('.tmp')
            tmp.write_text(json.dumps(rows, indent=1))
            os.replace(tmp, self.ledger)
        except OSError:
            pass          # kept in memory; the next save tries again

    # ---------- plumbing ----------
    def publish(self, msg):
        if self.publish_fn:
            try:
                self.publish_fn(msg)
            except Exception:
                pass

    @staticmethod
    def native(sid):
        return str(sid)[len(PREFIX):] if str(sid).startswith(PREFIX) else str(sid)

    def live(self, sid, create=True):
        n = self.native(sid)
        with self.lock:
            s = self.sessions.get(n)
            if s is None and create:
                s = self.sessions[n] = Live(n)
            return s

    def command(self):
        return list(self.binary) if self.binary else find_binary(self.env)

    def http(self):
        self.ensure()
        return self.server.http

    # ---------- observe ----------
    MISSING_RECHECK = 10.0          # s between looks for a command that was not there

    def probe(self):
        """`opencode --version` once (10 s bound [T3 versionProbe.ts]): (command, version, reason)."""
        if self._probe is not None and (self._probe[0] or time.time() - self._probe_at < self.MISSING_RECHECK):
            return self._probe
        cmd = self.command()
        self._probe_at = time.time()
        if not cmd:          # not cached for good: installed while the workspace runs, it shows up at the next look [ours]
            self._probe = (None, None, INSTALL)
            return self._probe
        try:
            r = subprocess.run(cmd + ['--version'], capture_output=True, text=True, timeout=10, env=self.env)
            m = re.search(r'(\d+\.\d+\.\d+[\w.+-]*)', r.stdout or '')
            version = m.group(1) if m else None
        except (OSError, subprocess.TimeoutExpired):
            version = None
        reason = None
        if generation(version) == 'v1':
            reason = UPGRADE % version
        elif version is None:
            reason = 'The OpenCode command did not report a version.'
        self._probe = (cmd, version, reason)
        return self._probe

    def store(self):
        """OpenCode's data folder: OPENCODE_DATA_DIR (first of a comma list), else $XDG_DATA_HOME/opencode, else
        ~/.local/share/opencode [T3 usage.ts resolveOpenCodeDataDirs]."""
        if self.data_dir:
            return Path(self.data_dir)
        own = (self.env.get('OPENCODE_DATA_DIR') or '').split(',')[0].strip()
        if own:
            return Path(os.path.expanduser(own))
        xdg = self.env.get('XDG_DATA_HOME', '').strip()
        return Path(xdg if xdg and os.path.isabs(xdg) else os.path.expanduser('~/.local/share')) / 'opencode'

    def available(self):
        """Never waits on `opencode --version` (speed, 2026-11-21): while the first probe runs in the background the row says it is
        being checked; a missing binary is known at once (no command runs)."""
        if self._probe is None and self.command():
            if not getattr(self, '_probing', False):
                self._probing = True

                def look():
                    try:
                        self.probe()
                    finally:
                        self._probing = False
                threading.Thread(target=look, name='agent-ide-opencode-probe', daemon=True).start()
            return {'name': self.name, 'label': self.label, 'found': True, 'store': None, 'binary': self.command()[0], 'version': None,
                    'reason': None, 'install': None, 'checking': True}
        cmd, version, reason = self.probe()
        store = self.store()
        return {'name': self.name, 'label': self.label, 'found': bool(cmd) and reason is None and not self.down,
                'store': str(store) if store.exists() else None, 'binary': cmd[0] if cmd else None, 'version': version,
                'reason': self.down or reason, 'install': INSTALL if not cmd else None}

    def running_server(self):
        return self.server is not None and self.server.alive() and self.connected.is_set()

    def list_sessions(self, project_root=None):
        rows = None
        if self.running_server():
            try:
                rows, query = [], {'limit': '200', 'order': 'desc'}
                for _ in range(5):          # bounded paging [ours]
                    page = self.server.http.call('GET', '/api/session', query=query)
                    rows += unwrap(page) or []
                    nxt = (page.get('cursor') or {}).get('next') if isinstance(page, dict) else None
                    if not nxt:
                        break
                    query = {'limit': '200', 'cursor': nxt}          # cursor pages omit order [T3 OpenCode2Client.ts paginate]
            except (HttpError, OSError):
                rows = None
        if rows is None:
            rows = self.stored_sessions()
        out = []
        for r in rows:
            cwd = (r.get('location') or {}).get('directory') or r.get('directory')
            if project_root and not under(cwd, project_root):
                continue
            live = self.live(r['id'], create=False)
            fork = r.get('fork') if isinstance(r.get('fork'), dict) else {}
            row = {'id': PREFIX + r['id'], 'provider': self.name, 'title': r.get('title') or 'OpenCode session', 'cwd': cwd,
                   'started': ms((r.get('time') or {}).get('created')), 'updated': ms((r.get('time') or {}).get('updated')),
                   'running': bool(live and live.phase != 'idle'), 'parent': PREFIX + r['parentID'] if r.get('parentID') else None,
                   'external': not (live and live.owned)}
            if fork.get('sessionID'):
                row['copyOf'] = PREFIX + fork['sessionID']
            out.append(row)
        return out

    def read_history(self, sid):
        n = self.native(sid)
        s = self.live(n, create=False)
        msgs, cwd, info = None, (s.cwd if s else None), None
        if self.running_server():
            try:
                msgs = self.api_messages(n)
                if not cwd or not (s and s.parent):
                    info = unwrap(self.server.http.call('GET', '/api/session/%s' % n)) or {}
            except (HttpError, OSError):
                msgs = None
        if msgs is None:
            msgs = self.stored_messages(n)
            if not cwd:
                info = next((r for r in self.stored_sessions() if r['id'] == n), None)
        if info:
            cwd = cwd or (info.get('location') or {}).get('directory')
        h = history_of(msgs, cwd)
        if info and info.get('parentID'):
            h['parent'] = PREFIX + info['parentID']
        h['window'] = self.window_of((s.model if s else None) or h.get('model'))
        if s is not None:
            with self.lock:
                # a block still streaming: OpenCode keeps its text only once the block ends, so the text that arrived
                # live is kept, also across a reconnect [T3 200a373fb]
                for (mid, kind, _), text in s.text.items():
                    if text and kind == 'text' and not any(m.get('id') == mid and m['text'].startswith(text) for m in h['messages']):
                        h['messages'].append({'role': 'assistant', 'text': text, 't': time.time(), 'id': mid, 'tokens': 0, 'streaming': True})
                h['turnActive'] = s.phase != 'idle'
                h['phase'] = s.phase
                h['retry'] = s.retry
                if s.errors:          # the live failure says more than the stored `idle` outcome's generic line
                    h['errors'] = [e for e in h['errors'] if e['text'] != 'The turn ended with an error.'] + s.errors
                h['errors'] = h['errors'][-10:]
                if s.context is not None and s.context > 0:
                    h['context'] = s.context
                h['parent'] = PREFIX + s.parent if s.parent else h['parent']
        return h

    def api_messages(self, n):
        """Newest HISTORY_LIMIT messages, oldest first [T3 75c439f4e]."""
        out, query = [], {'order': 'desc', 'limit': '200'}
        while len(out) < HISTORY_LIMIT:
            page = self.server.http.call('GET', '/api/session/%s/message' % n, query=query)
            items = unwrap(page) or []
            out += items
            nxt = (page.get('cursor') or {}).get('next') if isinstance(page, dict) else None
            if not items or not nxt:
                break
            query = {'limit': '200', 'cursor': nxt}
        return list(reversed(out[:HISTORY_LIMIT]))

    # ---------- storage, read-only (no server running) ----------
    def databases(self):
        root = self.store()
        try:
            names = sorted((p for p in root.iterdir() if re.match(r'^opencode(?:-[A-Za-z0-9_-]+)?\.db$', p.name) and not p.is_symlink()),
                           key=lambda p: (p.name != 'opencode.db', p.name))          # [T3 usage.ts]
        except OSError:
            return []
        return names

    def columns(self, con, table):
        try:
            return {r[1] for r in con.execute('PRAGMA table_info(%s)' % table)}
        except sqlite3.Error:
            return set()

    def stored_sessions(self):
        """Sessions from the database (`session_v2`, or `session` on older builds). Its columns are not documented: read by name
        where present, and from a JSON `data` column otherwise [ours: session_v2 read from OpenCode 2.2.6's own database]."""
        out = []
        for db in self.databases():
            try:
                con = sqlite3.connect('file:%s?mode=ro' % urllib.parse.quote(str(db)), uri=True, timeout=0.1)
                con.row_factory = sqlite3.Row
                # OpenCode 2.2.6 keeps sessions in `session_v2` (id, parent_id, fork_session_id, directory, title, time_created,
                # time_updated, ...; read 2026-11-09); older builds in `session`
                table = next((t for t in ('session_v2', 'session') if self.columns(con, t)), None)
                if not table:
                    con.close()
                    continue
                rows = [dict(r) for r in con.execute('SELECT * FROM %s ORDER BY time_created DESC LIMIT 500' % table
                                                     if 'time_created' in self.columns(con, table) else 'SELECT * FROM %s ORDER BY rowid DESC LIMIT 500' % table)]
                con.close()
            except sqlite3.Error:
                continue
            for r in rows:
                data = {}
                if isinstance(r.get('data'), str):
                    try:
                        data = json.loads(r['data'])
                    except ValueError:
                        data = {}
                sid = r.get('id') or data.get('id')
                if not sid:
                    continue
                directory = r.get('directory') or (data.get('location') or {}).get('directory') or data.get('directory')
                out.append({'id': sid, 'title': r.get('title') or data.get('title'), 'location': {'directory': directory},
                            'parentID': r.get('parent_id') or data.get('parentID'),
                            'time': {'created': r.get('time_created') or (data.get('time') or {}).get('created'),
                                     'updated': r.get('time_updated') or (data.get('time') or {}).get('updated')},
                            **({'fork': data['fork']} if isinstance(data.get('fork'), dict)
                               else {'fork': {'sessionID': r['fork_session_id']}} if r.get('fork_session_id') else {})})
        return out

    def stored_messages(self, n):
        """Messages from `session_message` (OpenCode 2: id, session_id, type, data, time_created [T3 usage.ts]); the
        projected shape is assumed to be `data` plus those columns [ours: inferred]."""
        out = []
        for db in self.databases():
            try:
                con = sqlite3.connect('file:%s?mode=ro' % urllib.parse.quote(str(db)), uri=True, timeout=0.1)
                cols = self.columns(con, 'session_message')
                if not cols:
                    con.close()
                    continue
                order = 'time_created' if 'time_created' in cols else 'rowid'
                rows = con.execute('SELECT * FROM (SELECT rowid AS rid, * FROM session_message WHERE session_id = ? ORDER BY %s DESC LIMIT ?) ORDER BY rid'
                                   % order, (n, HISTORY_LIMIT)).fetchall()
                names = [d[0] for d in con.execute('SELECT rowid AS rid, * FROM session_message LIMIT 0').description]
                con.close()
            except sqlite3.Error:
                continue
            for row in rows:
                r = dict(zip(names, row))
                try:
                    m = json.loads(r.get('data') or '{}')
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                m.setdefault('id', r.get('id'))
                m.setdefault('type', r.get('type'))
                m.setdefault('time', {'created': r.get('time_created')})
                out.append(m)
            if out:
                break
        return out

    # ---------- the server and its event stream ----------
    def ensure(self):
        """One server for every folder, started on first use and subscribed before any work (the stream is volatile:
        events while unsubscribed are lost [docs /api/event; T3 OpenCode2Client.ts])."""
        if self.server is not None and self.server.alive() and self.connected.is_set():
            return
        with self.start_lock:
            if self.server is not None and self.server.alive() and self.connected.wait(SUBSCRIBE_TIMEOUT):
                return
            if self.closing:
                raise RuntimeError('OpenCode provider is closed')
            now = time.time()
            self.deaths = [t for t in self.deaths if now - t < DEATH_WINDOW]
            if len(self.deaths) >= DEATHS:
                self.down = 'The OpenCode server stopped %d times in a minute; it stays down until reloaded.' % DEATHS
                raise RuntimeError(self.down)
            cmd, version, reason = self.probe()
            if not cmd:
                raise RuntimeError(INSTALL)
            if generation(version) == 'v1':
                raise RuntimeError(reason)
            if self.server is not None:
                self.server.kill()
            self.server = Server(cmd, self.env, startup_timeout=self.startup_timeout).start()
            self.gen += 1
            self.connected.clear()
            threading.Thread(target=self._follow, args=(self.gen, self.server), daemon=True, name='opencode-events').start()
            if not self.connected.wait(SUBSCRIBE_TIMEOUT):
                raise RuntimeError('The OpenCode server did not accept an event subscription.')

    def _follow(self, gen, server):
        """The SSE reader: reconnects up to RECONNECT_ATTEMPTS times, RECONNECT_DELAY apart; after a reconnect it
        reconciles what the gap may have hidden; when the server is gone it ends every turn and request it held."""
        attempts, ever = 0, False
        while not self.closing and gen == self.gen:
            try:
                conn, resp = server.http.subscribe(silence=self.silence)
            except (HttpError, OSError, http.client.HTTPException) as e:
                attempts += 1
                if attempts >= self.reconnect_attempts or not server.alive():
                    return self._lost(gen, 'The OpenCode server stopped sending events (%s).' % e)
                time.sleep(self.reconnect_delay)
                continue
            attempts = 0
            try:
                frames = sse_frames(resp)
                if ever:
                    self._reconcile(server)          # after subscribing, so nothing that happens next is missed
                ever = True
                self.connected.set()
                for data in frames:
                    if gen != self.gen or self.closing:
                        break
                    if data is not None:
                        self._handle(data)
            except (OSError, http.client.HTTPException, ValueError):
                pass          # silence past SILENCE (socket timeout), a reset, a cut stream: reconnect
            finally:
                self.connected.clear()
                try:
                    conn.close()
                except Exception:
                    pass
            if not server.alive():
                return self._lost(gen, 'The OpenCode server stopped.')

    def _lost(self, gen, reason):
        """The server or its stream is gone for good: turns end failed (transport_error), live requests can no longer be
        answered, and the next call starts a fresh server [orchestration.md §2; design 'Codex' death rule]."""
        if gen != self.gen or self.closing:
            return
        self.deaths.append(time.time())
        with self.lock:
            lives = [s for s in self.sessions.values() if s.turn_open or s.phase != 'idle']
            for rid in list(self.pending):
                self._settle(rid, 'cancelled', reason='not_resumable: ' + reason)
        for s in lives:
            self._finish(s, 'failed', {'class': 'transport_error', 'message': reason, 'code': None, 'resetsAt': None})
        server, self.server = self.server, None
        if server is not None:
            server.kill()

    def _reconcile(self, server):
        """After a reconnect: a session we think is running that the server no longer runs ended in the gap; its end comes
        from the newest `idle` message [T3 cb7d07393 and v2/adapter.ts reconcile]; pending asks are listed again, and those
        no longer listed close."""
        http_ = server.http
        try:
            active = unwrap(http_.call('GET', '/api/session/active', timeout=5.0)) or {}
        except (HttpError, OSError):
            return
        with self.lock:
            lives = [s for s in self.sessions.values() if s.owned or s.turn_open]
        for s in lives:
            if s.sid in active or not (s.turn_open or s.phase in ('running', 'stopping', 'compacting')):
                continue
            outcome = None
            try:
                newest = unwrap(http_.call('GET', '/api/session/%s/message' % s.sid, query={'order': 'desc', 'limit': '5'})) or []
                outcome = next((m.get('outcome') for m in newest if m.get('type') == 'idle'), None)
            except (HttpError, OSError):
                pass
            if outcome == 'succeeded':
                self._finish(s, 'interrupted' if s.stop_requested else 'completed')
            elif outcome == 'failed':
                self._finish(s, 'failed', {'class': 'provider_error', 'message': 'OpenCode ended the turn with an error while the event stream was reconnecting.',
                                           'code': None, 'resetsAt': None})
            else:
                self._finish(s, 'interrupted')
        for s in lives:
            listed = set()
            for kind, path in (('permission', '/api/session/%s/permission'), ('form', '/api/session/%s/form')):
                try:
                    items = unwrap(http_.call('GET', path % s.sid, timeout=5.0)) or []
                except (HttpError, OSError):
                    continue
                for item in items if isinstance(items, list) else []:
                    listed.add(item.get('id'))
                    if not self.pending.get(PREFIX + str(item.get('id'))):
                        self._ask_permission(item) if kind == 'permission' else self._ask_form(item)
            for rid, e in list(self.pending.items()):
                if e['sid'] == s.sid and e['native']['id'] not in listed:
                    self._settle(rid, 'cancelled')

    # ---------- events ----------
    def _handle(self, data):
        try:
            ev = json.loads(data)
        except ValueError:
            self.skipped.add('<unreadable>')          # one bad frame never ends the stream [T3 OpenCode2Client.ts undecodable]
            return
        if not isinstance(ev, dict):
            return
        t, d = ev.get('type'), ev.get('data') if isinstance(ev.get('data'), dict) else {}
        fn = self.HANDLERS.get(t)
        if fn is None:
            self.skipped.add(str(t)[:80])          # an event type this build does not know: skipped, not fatal
            return
        try:
            fn(self, ev, d)
        except (KeyError, TypeError, AttributeError, ValueError):
            self.skipped.add('bad:' + str(t)[:80])          # tolerant per payload

    def _activity(self, s, **extra):
        self.publish({'method': 'provider/activity', 'params': {'threadId': PREFIX + s.sid, 'provider': self.name, 'phase': s.phase, **extra}})

    def on_created(self, ev, d):
        s = self.live(d['sessionID'])
        loc = d.get('location') or ev.get('location') or {}
        s.cwd = s.cwd or loc.get('directory')
        s.title = d.get('title') or s.title
        s.parent = d.get('parentID') or s.parent
        m = d.get('model') or {}
        if m.get('id'):
            s.model = '%s/%s' % (m.get('providerID'), m['id'])

    def on_forked(self, ev, d):
        s = self.live(d['sessionID'])
        s.parent = s.parent or d.get('parentID')
        parent = self.live(d['parentID'], create=False) if d.get('parentID') else None
        if parent is not None:
            s.cwd = s.cwd or parent.cwd
        loc = ev.get('location') or {}
        s.cwd = s.cwd or loc.get('directory')
        self._activity(s, forkedFrom=PREFIX + d['parentID'] if d.get('parentID') else None)

    def on_exec_started(self, ev, d):
        s = self.live(d['sessionID'])
        with self.lock:
            s.exec_running = True
            s.held_end = None
            first = not s.turn_open
            s.turn_open = True
            if s.phase in ('idle', 'starting'):
                s.phase = 'running'
        if first:
            self.publish({'method': 'provider/turn', 'params': {'threadId': PREFIX + s.sid, 'provider': self.name, 'started': True}})

    def on_exec_end(self, ev, d):
        s = self.live(d['sessionID'])
        t = ev.get('type')
        with self.lock:
            s.exec_running = False
            if t == 'session.execution.failed':
                status, failure = 'failed', classify(d.get('error'))
            elif t == 'session.execution.interrupted':
                status, failure = 'interrupted', None
            else:
                status, failure = ('interrupted' if s.stop_requested else 'completed'), None
            if status == 'completed' and s.steers:
                s.held_end = (status, failure)          # undelivered steers start another execution of this turn
                return
        self._finish(s, status, failure)

    def on_inbox_done(self, ev, d):
        s = self.live(d['sessionID'])
        with self.lock:
            s.steers.discard(d.get('inboxID'))
            held = s.held_end if (s.held_end and not s.steers and not s.exec_running) else None
        if held:
            self._finish(s, *held)

    def on_step_started(self, ev, d):
        s = self.live(d['sessionID'])
        m = d.get('model') or {}
        if m.get('id'):
            s.model = '%s/%s' % (m.get('providerID'), m['id'])
        s.agent = d.get('agent') or s.agent

    def on_step_ended(self, ev, d):
        s = self.live(d['sessionID'])
        tok = d.get('tokens')
        if isinstance(tok, dict):
            cache = tok.get('cache') or {}
            size = (tok.get('input') or 0) + (cache.get('read') or 0) + (cache.get('write') or 0)          # the context this call read
            if size:
                s.context = size
                s.contexts.append({'t': time.time(), 'context': size})
        self._activity(s, step='ended', context=s.context)

    def _retry_over(self, s):
        """OpenCode sends nothing when a retry goes through; the provider's first output marks it [T3 aac28657c]."""
        if s.retry is not None:
            s.retry = None
            self._activity(s, retry=None)

    def on_text(self, ev, d):
        s = self.live(d['sessionID'])
        t = ev['type']
        kind = 'text' if t.startswith('session.text.') else 'reasoning'
        key = (d.get('assistantMessageID'), kind, d.get('ordinal', 0))
        if t.endswith('.started'):
            self._retry_over(s)
            s.text[key] = ''
        elif t.endswith('.delta'):
            s.text[key] = s.text.get(key, '') + (d.get('delta') or '')          # not published per token [T3 2f8de8d7b]
        elif t.endswith('.ended'):
            s.text.pop(key, None)
            if kind == 'text':
                self._activity(s, said=True)

    def on_tool(self, ev, d):
        s = self.live(d['sessionID'])
        t = ev['type']
        if t == 'session.tool.input.started':
            self._retry_over(s)
            s.tools[d.get('id')] = d.get('name')
            return
        name = s.tools.get(d.get('id'))
        if t == 'session.tool.called':
            inp = d.get('input') if isinstance(d.get('input'), dict) else {}
            self._activity(s, tool=name, kind=kind_of(name), path=relative(path_of(inp), s.cwd) if s.cwd and path_of(inp) else None,
                           text=summary(name, inp, s.cwd)[:200])
        elif t == 'session.tool.success':
            status = tool_status(name, {'status': 'completed', 'metadata': d.get('metadata')})
            self._activity(s, tool=name, kind=kind_of(name), status=status)
        elif t == 'session.tool.failed':
            status = tool_status(name, {'status': 'error', 'error': d.get('error'), 'metadata': d.get('metadata')})
            self._activity(s, tool=name, kind=kind_of(name), status=status)

    def on_retry(self, ev, d):
        """A provider retry is shown as progress, not a silent run [T3 aac28657c]. OpenCode numbers the request it retries
        with, so its first retry is attempt 2; `at` is when it fires, on the clock of the event's `created`."""
        s = self.live(d['sessionID'])
        err = d.get('error') if isinstance(d.get('error'), dict) else {}
        resp = err.get('response') if isinstance(err.get('response'), dict) else {}
        delay = (d.get('at') - ev['created']) if isinstance(d.get('at'), (int, float)) and isinstance(ev.get('created'), (int, float)) else None
        s.retry = {'attempt': max(1, int(d.get('attempt') or 2) - 1), 'delayMs': max(0, int(delay)) if delay is not None else None,
                   'message': readable_error(err.get('message'), err.get('status'), resp.get('body')), 'code': err.get('type'),
                   'class': 'provider_error'}
        self._activity(s, retry=s.retry)

    def on_compaction(self, ev, d):
        s = self.live(d['sessionID'])
        t = ev['type']
        if t.endswith('.started'):
            if s.phase in ('idle', 'running', 'starting'):
                s.phase = 'compacting'
            s.compacts.append({'t': time.time(), 'from': s.context, 'to': None})
        elif t.endswith('.ended') or t.endswith('.failed'):
            if s.phase == 'compacting':
                s.phase = 'running' if s.exec_running else 'idle'
            if s.compacts and t.endswith('.ended'):
                s.compacts[-1]['summary'] = d.get('text')
        self._activity(s, compaction=t.rsplit('.', 1)[1])

    def on_permission(self, ev, d):
        self._ask_permission(d)

    def on_form(self, ev, d):
        self._ask_form(d.get('form') or {})

    def on_replied(self, ev, d):
        nid = d.get('requestID') if ev['type'] == 'permission.replied' else d.get('id')
        rid = PREFIX + str(nid)
        if rid in self.pending:
            # answered here (already settled by reply) or in another client
            self._settle(rid, 'resolved' if ev['type'] != 'form.cancelled' and d.get('reply') != 'reject' else 'cancelled')

    HANDLERS = {
        'session.created': on_created, 'session.forked': on_forked,
        'session.execution.started': on_exec_started,
        'session.execution.succeeded': on_exec_end, 'session.execution.failed': on_exec_end, 'session.execution.interrupted': on_exec_end,
        'session.inbox.delivered': on_inbox_done, 'session.inbox.cancelled': on_inbox_done,
        'session.step.started': on_step_started, 'session.step.ended': on_step_ended, 'session.step.failed': on_step_ended,
        'session.text.started': on_text, 'session.text.delta': on_text, 'session.text.ended': on_text,
        'session.reasoning.started': on_text, 'session.reasoning.delta': on_text, 'session.reasoning.ended': on_text,
        'session.tool.input.started': on_tool, 'session.tool.called': on_tool, 'session.tool.success': on_tool, 'session.tool.failed': on_tool,
        'session.retry.scheduled': on_retry,
        'session.compaction.started': on_compaction, 'session.compaction.ended': on_compaction, 'session.compaction.failed': on_compaction,
        'permission.asked': on_permission, 'permission.replied': on_replied,
        'form.created': on_form, 'form.replied': on_replied, 'form.cancelled': on_replied,
        # known and deliberately not read: the volatile stream's hello, usage totals (the step ends carry the context),
        # shells (the tool events carry them), status mirrors of the execution events
        'server.connected': lambda self, ev, d: None, 'session.usage.updated': lambda self, ev, d: None,
        'session.inbox.enqueued': lambda self, ev, d: None, 'session.step.streamed': lambda self, ev, d: None,
        'session.tool.input.ended': lambda self, ev, d: None, 'session.tool.progress': lambda self, ev, d: None,
        'session.instructions.updated': lambda self, ev, d: None, 'session.status': lambda self, ev, d: None,
        'session.idle': lambda self, ev, d: None, 'session.compaction.delta': lambda self, ev, d: None,
        'shell.created': lambda self, ev, d: None, 'shell.exited': lambda self, ev, d: None, 'shell.deleted': lambda self, ev, d: None,
        'project.updated': lambda self, ev, d: None, 'plugin.updated': lambda self, ev, d: None,
    }

    def _finish(self, s, status, failure=None):
        """A turn ends once: phase idle, retry cleared, its open asks closed (no execution waits on them any more), the
        page told, and stop() woken."""
        with self.lock:
            if not s.turn_open and s.phase == 'idle':
                return
            s.turn_open = s.exec_running = s.stop_requested = False
            s.held_end = None
            s.phase = 'idle'
            s.retry = None
            s.text = {}
            s.tools = {}
            if failure:
                s.errors = (s.errors + [{'t': time.time(), 'text': failure['message'], 'code': failure.get('code'), 'class': failure['class'],
                                         'resetsAt': failure.get('resetsAt')}])[-10:]
            rids = [rid for rid, e in self.pending.items() if e['sid'] == s.sid]
        for rid in rids:
            self._settle(rid, 'cancelled', reason='not_resumable: the turn ended before an answer')
        self.refusals.turn_ended(failure['message'] if failure else None, (failure or {}).get('class'), (failure or {}).get('resetsAt'),
                                 session=PREFIX + s.sid, cancelled=status not in ('completed', 'failed'))
        self.publish({'method': 'provider/turn', 'params': {'threadId': PREFIX + s.sid, 'provider': self.name, 'ended': True, 'status': status,
                                                            'error': failure['message'] if failure else None, 'failure': failure}})
        with s.ended:
            s.turns_ended += 1
            s.ended.notify_all()

    # ---------- requests ----------
    def _ask_permission(self, d):
        """An approval in core.request_message shape; the options carry OpenCode's own decision ids (`once`, `reject`;
        `always` only when OpenCode offers patterns to save) [T3 v2/adapter.ts onPermissionAsked; adding-a-provider.md]."""
        nid, sid = d.get('id'), d.get('sessionID')
        if not nid or not sid:
            return
        rid = PREFIX + nid
        action = str(d.get('action') or 'permission')
        resources = [str(r) for r in (d.get('resources') or [])]
        save = [str(r) for r in (d.get('save') or [])]
        a = action.lower()
        kind = ('command' if a in ('shell', 'bash') else 'file-change' if a in ('edit', 'write', 'patch', 'apply_patch')
                else 'file-read' if a in ('read', 'glob', 'grep', 'lsp', 'list', 'external_directory') else 'permission')
        options = [{'id': 'once', 'label': 'Allow once', 'kind': 'allow_once'}]
        if save:
            options.append({'id': 'always', 'label': 'Always allow ' + ', '.join(save), 'kind': 'allow_always'})
        options.append({'id': 'reject', 'label': 'Decline', 'kind': 'reject_once'})
        s = self.live(sid)
        msg = core.request_message(self.name, PREFIX + sid, rid, kind, action + (': ' + ', '.join(resources) if resources else ''), options,
                                   command=resources[0] if kind == 'command' and resources else None,
                                   paths=resources if kind in ('file-change', 'file-read') else None)
        msg['params']['answerVia'] = 'live'
        with self.lock:
            if rid in self.pending:
                return
            self.pending[rid] = {'msg': msg, 'sid': sid, 'native': {'type': 'permission', 'id': nid, 'action': action, 'save': save},
                                 'status': 'pending', 'asked': time.time()}
        self._activity(s, asking=rid)
        self.publish(msg)

    def _ask_form(self, form):
        """A question form [T3 v2/adapter.ts formQuestions]; selection flags are kept [T3 04bd86951]. Fields we cannot draw
        (links, hidden or dependent fields) make it an 'other' request answered in OpenCode itself."""
        nid, sid = form.get('id'), form.get('sessionID')
        if not nid or not sid:
            return
        rid = PREFIX + nid
        questions, unsupported = [], None
        for i, f in enumerate(form.get('fields') or []):
            if f.get('type') not in ('string', 'multiselect') or f.get('hidden') or f.get('when'):
                unsupported = f.get('type')
                break
            header = f.get('title') or 'Question %d' % (i + 1)
            opts = [{'label': o.get('label') or o.get('value') or 'Option', 'description': o.get('description') or o.get('label') or '',
                     'value': o.get('value')} for o in (f.get('options') or [])]
            questions.append({'id': f.get('key'), 'header': header, 'question': f.get('description') or header, 'options': opts,
                              'multiSelect': f.get('type') == 'multiselect', 'allowCustom': bool(f.get('custom')) or not opts})
        kind = 'other' if unsupported else 'question'
        options = [{'id': 'answer', 'label': 'Answer', 'kind': 'answer'}, {'id': 'cancel', 'label': 'Dismiss', 'kind': 'reject_once'}]
        msg = core.request_message(self.name, PREFIX + sid, rid, kind, form.get('title') or 'Questions', options,
                                   questions=questions if not unsupported else None,
                                   reason=('OpenCode asks for %s; answer it in OpenCode.' % unsupported) if unsupported else None)
        msg['params']['answerVia'] = 'elsewhere' if unsupported else 'live'
        with self.lock:
            if rid in self.pending:
                return
            self.pending[rid] = {'msg': msg, 'sid': sid, 'native': {'type': 'form', 'id': nid, 'form': form}, 'status': 'pending', 'asked': time.time()}
        self._activity(self.live(sid), asking=rid)
        self.publish(msg)

    def _settle(self, rid, status, reason=None):
        with self.lock:
            e = self.pending.pop(rid, None)
        if e is None:
            return
        e['status'] = status
        self.publish({'method': 'serverRequest/resolved', 'params': {'threadId': PREFIX + e['sid'], 'requestId': rid, 'status': status,
                                                                     **({'reason': reason} if reason else {})}})

    def requests(self):
        with self.lock:
            return [e['msg'] for e in self.pending.values()]

    def reply(self, request_id, answer):
        """answer: {'optionId'} (an id from the request's options), {'decision': accept|acceptForSession|decline|cancel},
        or {'answers': {question id: [str]}} for a form. A request OpenCode no longer has (404) closes as cancelled."""
        rid = request_id if str(request_id).startswith(PREFIX) else PREFIX + str(request_id)
        with self.lock:
            e = self.pending.get(rid)
        if e is None:
            raise KeyError('No pending OpenCode request %s' % request_id)
        answer = answer or {}
        oid = answer.get('optionId') or {'accept': 'once', 'acceptForSession': 'always', 'acceptAlways': 'always',
                                         'decline': 'reject', 'cancel': 'reject'}.get(answer.get('decision'))
        sid, native = e['sid'], e['native']
        http_ = self.http()
        try:
            if native['type'] == 'permission':
                if oid not in ('once', 'always', 'reject'):
                    raise ValueError('OpenCode approval %s needs one of its own options: once, always, reject' % request_id)
                if oid == 'always':
                    self._grant(http_, sid, native)
                body = {'decision': 'reject' if oid == 'reject' else 'once'}
                if oid == 'reject' and answer.get('message'):
                    body['message'] = str(answer['message'])
                http_.call('POST', '/api/session/%s/permission/%s/reply' % (sid, native['id']), body)
            else:
                answers = answer.get('answers')
                if oid in ('cancel', 'reject') or not answers:
                    http_.call('DELETE', '/api/session/%s/form/%s' % (sid, native['id']))
                else:
                    http_.call('POST', '/api/session/%s/form/%s/reply' % (sid, native['id']), {'answer': form_answer(native['form'], answers)})
        except HttpError as err:
            if err.status == 404:          # already answered or withdrawn: the card closes
                self._settle(rid, 'cancelled', reason='OpenCode no longer has this request')
                return {'gone': True}
            raise
        self._settle(rid, 'resolved')
        return {}

    def _grant(self, http_, sid, native):
        """'Always' for OpenCode 2: the session's own permission rules gain an allow for each pattern OpenCode offered to
        save, then the request is answered `once` [T3 v2/adapter.ts: decision is once|reject, grants go to rules]."""
        sess = unwrap(http_.call('GET', '/api/session/%s' % sid)) or {}
        rules = list(sess.get('permissions') or [])
        for r in native['save']:
            rule = {'action': native['action'], 'resource': r, 'effect': 'allow'}
            if rule not in rules:
                rules.append(rule)
        http_.call('PATCH', '/api/session/%s' % sid, {'permissions': rules})

    # ---------- drive ----------
    def owns(self, sid):
        s = self.live(sid, create=False)
        return bool(s and s.owned)

    def phase(self, sid):
        s = self.live(sid, create=False)
        return s.phase if s else 'idle'

    def capabilities(self, sid=None):
        """Every key, set from what the OpenCode 2 API offers [docs /api routes; T3 v2/adapter.ts capabilities]. A session
        this provider did not start or fork is watch-only (another OpenCode client may hold it) [ours]."""
        ok = self.probe()[2] is None and not self.down
        drive = ok and (sid is None or self.owns(sid))
        s = self.live(sid, create=False) if sid else None
        return core.capabilities(
            start=ok, send=drive, stop=drive, reply=drive,
            steer=drive,                 # a prompt sent mid-execution joins it (inbox delivery "steer") [T3 supportsActiveSteering]
            native_queue=False,          # no separate queued delivery is documented [ours]
            restart_steer=False,         # never: interrupt-and-restart kills subagents (design "Delivery") [ours]
            fork=drive, fork_at_turn=drive,          # POST /api/session/:id/fork {before} [docs]
            rewind=drive,                # revert/stage + revert/commit [docs]
            compact=drive, rename=drive,
            archive=False,               # a session.archived event exists; no documented route to archive [docs]
            models=ok, effort=ok, modes=ok,          # /api/model variants, /api/agent primary agents [docs; T3 recordings]
            questions=True, subagents=True,
            plan_limits=False,
            context='window' if (s and self.window_of(s.model)) else 'tokens',
            release=drive)

    def start(self, cwd, model=None, effort=None, mode=None):
        """A new OpenCode session in `cwd` on the one server. model 'providerID/modelID'; effort = one of the model's
        variants; mode = a primary agent id (build, plan, ...)."""
        http_ = self.http()
        body = {'location': {'directory': str(cwd)}}
        if model:
            prov, _, mid = str(model).partition('/')
            body['model'] = {'providerID': prov, 'id': mid or prov, **({'variant': effort} if effort else {})}
        if mode:
            body['agent'] = mode
        sess = unwrap(http_.call('POST', '/api/session', body)) or {}
        s = self.live(sess['id'])
        with self.lock:
            s.owned, s.cwd = True, str(cwd)
            s.title = sess.get('title')
            if model:
                s.model = str(model)
            s.agent = mode
        self._save_ledger()
        return {'threadId': PREFIX + sess['id']}

    def send(self, sid, text, how='start'):
        """how comes from core.route(): 'start' (idle) or 'steer' (running; joins the execution at its next step)."""
        s = self.live(sid, create=False)
        if s is None or not s.owned:
            raise PermissionError('This OpenCode session is not driven here; continue it in OpenCode.')
        if how not in ('start', 'steer'):
            raise ValueError('OpenCode has no %s delivery' % how)
        http_ = self.http()
        body = {'text': str(text)}
        mid = 'msg_aide_' + uuid.uuid4().hex          # our own id, so the inbox events can be matched [T3 steerPromptId; format ours]
        body['id'] = mid
        with self.lock:
            prev = s.phase
            if how == 'steer':
                body['delivery'] = 'steer'
                s.steers.add(mid)
            elif s.phase == 'idle':
                s.phase = 'starting'          # set by what we send, cleared by the stream (core.PHASES)
        try:
            http_.call('POST', '/api/session/%s/prompt' % s.sid, body)
        except Exception:
            with self.lock:
                s.steers.discard(mid)
                if s.phase == 'starting' and prev == 'idle':
                    s.phase = 'idle'
            raise
        return {'messageId': mid}

    RESCANS = 3

    def stop(self, sid):
        """Stop the session and every running session under it: OpenCode's task subagents are child sessions of their
        own (parentID), and an interrupt of the parent is not relied on to reach them [T3 interruptTurn: the lineage is
        stopped, re-scanned for children started meanwhile]. The children's result is under 'children' when there were any."""
        out = self._stop_root(sid)
        kids = self._stop_descendants(self.native(sid))
        return dict(out, children=kids) if kids else out

    def _stop_descendants(self, root):
        """Interrupt the running descendants of root, read from OpenCode's own session list (not from our events), up to
        RESCANS more times while new running ones turn up. Returns the prefixed ids interrupted."""
        http_ = self.http()
        done = []
        tried = set()
        for _ in range(1 + self.RESCANS):
            try:
                rows = unwrap(http_.call('GET', '/api/session', query={'limit': '200', 'order': 'desc'}, timeout=5.0)) or []
                active = unwrap(http_.call('GET', '/api/session/active', timeout=5.0)) or {}
            except (HttpError, OSError, socket.timeout):
                break
            kids = {}
            for r in rows:
                if r.get('parentID'):
                    kids.setdefault(r['parentID'], []).append(r['id'])
            with self.lock:
                for x in self.sessions.values():          # children we heard of that the page of rows may not hold
                    if x.parent and x.sid not in kids.get(x.parent, []):
                        kids.setdefault(x.parent, []).append(x.sid)
            order, seen = [root], {root}
            for t in order:
                for c in kids.get(t, []):
                    if c not in seen:
                        seen.add(c)
                        order.append(c)
            fresh = [c for c in order[1:] if c in active and c not in tried]
            if not fresh:
                break
            for c in fresh:
                tried.add(c)
                try:
                    r = http_.call('POST', '/api/session/%s/interrupt' % c, timeout=self.stop_wait) or {}
                    if r.get('interrupted'):
                        done.append(PREFIX + c)
                except (HttpError, OSError, socket.timeout):
                    x = self.live(c, create=False)
                    if x is not None and x.phase != 'idle':
                        self._finish(x, 'interrupted', {'class': 'unknown', 'message': 'OpenCode did not confirm the stop.', 'code': None, 'resetsAt': None})
                    done.append(PREFIX + c)
        return done

    def _stop_root(self, sid):
        """Bounded: interrupt (10 s), then wait up to 10 s for OpenCode's own end; otherwise settle locally and say so.
        A Stop between steer executions retracts the waiting steers [T3 v2/adapter.ts interruptTurn]."""
        s = self.live(sid, create=False)
        if s is None or not s.owned:
            raise PermissionError('This OpenCode session is not driven here.')
        http_ = self.http()
        with self.lock:
            if s.phase == 'idle' and not s.turn_open:
                return {'settled': 'idle'}
            s.stop_requested = True
            s.phase = 'stopping'
            seen = s.turns_ended
        try:
            r = http_.call('POST', '/api/session/%s/interrupt' % s.sid, timeout=self.stop_wait) or {}
        except (OSError, socket.timeout, HttpError):
            self._finish(s, 'interrupted', {'class': 'unknown', 'message': 'OpenCode did not confirm the stop.', 'code': None, 'resetsAt': None})
            return {'settled': 'local'}
        if r.get('interrupted'):
            with s.ended:
                s.ended.wait_for(lambda: s.turns_ended > seen, timeout=self.stop_wait)
            if s.turns_ended > seen:
                return {'settled': 'confirmed'}
            self._finish(s, 'interrupted', {'class': 'unknown', 'message': 'OpenCode did not report the end of the stopped turn.', 'code': None, 'resetsAt': None})
            return {'settled': 'local'}
        with self.lock:
            stranded = list(s.steers)
            s.steers.clear()
        for inbox in stranded:
            try:
                http_.call('DELETE', '/api/session/%s/inbox/%s' % (s.sid, inbox))
            except (HttpError, OSError):
                pass
        self._finish(s, 'interrupted')
        return {'settled': 'idle'}

    def fork(self, sid, before=None):
        """A copy of the session, whole or up to (not including) message `before`; the copy is driven here."""
        s = self.live(sid)
        sess = unwrap(self.http().call('POST', '/api/session/%s/fork' % s.sid, {'before': before} if before else {})) or {}
        c = self.live(sess['id'])
        with self.lock:
            c.owned, c.cwd, c.parent, c.model = True, s.cwd or (sess.get('location') or {}).get('directory'), s.sid, s.model
        self._save_ledger()
        return {'threadId': PREFIX + sess['id']}

    def rewind(self, sid, message_id, files=False):
        """Take the conversation back to before `message_id` (and the files too when files=True)."""
        s = self.live(sid, create=False)
        if s is None or not s.owned:
            raise PermissionError('This OpenCode session is not driven here.')
        http_ = self.http()
        http_.call('POST', '/api/session/%s/revert/stage' % s.sid, {'messageID': message_id, 'files': bool(files)})
        http_.call('POST', '/api/session/%s/revert/commit' % s.sid)
        return {}

    def compact(self, sid):
        s = self.live(sid, create=False)
        if s is None or not s.owned:
            raise PermissionError('This OpenCode session is not driven here.')
        self.http().call('POST', '/api/session/%s/compact' % s.sid, {})
        return {}

    def rename(self, sid, title):
        self.http().call('PATCH', '/api/session/%s' % self.native(sid), {'title': str(title)})
        return {}

    def window_of(self, model):
        if model and self.catalog is None and self.running_server():
            try:
                self.options()          # the context window comes from the model catalogue
            except (HttpError, OSError, RuntimeError):
                pass          # not cached: tried again next time [T3 807319c27]
        if not model or not self.catalog:
            return None
        hit = next((m for m in self.catalog if m['id'] == model), None)
        return hit.get('window') if hit else None

    def options(self, sid=None):
        """Models (with their variants as efforts and their context window) and primary agents as modes. A failed
        catalogue is not cached, so the next call tries again [T3 807319c27]."""
        s = self.live(sid, create=False) if sid else None
        http_ = self.http()
        q = {'location[directory]': s.cwd} if s and s.cwd else None          # deepObject query [T3 recordings]
        models = unwrap(http_.call('GET', '/api/model', query=q)) or []
        agents = unwrap(http_.call('GET', '/api/agent', query=q)) or []
        catalog = [{'id': '%s/%s' % (m.get('providerID'), m.get('id')), 'label': m.get('name') or m.get('id'),
                    'efforts': [v.get('id') for v in (m.get('variants') or []) if v.get('id')],
                    'window': (m.get('limit') or {}).get('context')}
                   for m in models if isinstance(m, dict) and m.get('enabled', True) is not False]
        self.catalog = catalog
        modes = [{'id': a.get('id'), 'label': a.get('name') or a.get('id')} for a in agents
                 if isinstance(a, dict) and a.get('mode') in ('primary', 'all') and not a.get('hidden')]
        cur = next((m for m in catalog if s and m['id'] == s.model), None)
        return {'models': catalog, 'efforts': cur['efforts'] if cur else [], 'modes': modes,
                'current': {'model': s.model if s else None, 'mode': s.agent if s else None}}

    def release(self, sid):
        """Stop holding the session; OpenCode keeps it in its own database, so any OpenCode client can continue it.
        Refused while a turn runs (stop it first) [ours]."""
        s = self.live(sid, create=False)
        if s is None:
            return {}
        if s.phase != 'idle' or s.turn_open:
            raise RuntimeError('The OpenCode session is still running; stop it before releasing.')
        with self.lock:
            s.owned = False
            for rid in [r for r, e in self.pending.items() if e['sid'] == s.sid]:
                self._settle(rid, 'cancelled', reason='released')
        self._save_ledger()
        return {}

    def close(self):
        """Kill the server's whole process group; the reader thread ends with it."""
        self.closing = True
        self.gen += 1
        with self.lock:
            for rid in list(self.pending):
                self._settle(rid, 'cancelled', reason='not_resumable: the workspace closed OpenCode')
        if self.server is not None:
            self.server.kill()
            self.server = None
        self.connected.clear()


def form_answer(form, answers):
    """{field key: value} for OpenCode's form reply: an option's label is sent as its value; a multiselect field takes a
    list, any other a string [T3 v2/adapter.ts formAnswer]."""
    out = {}
    for f in form.get('fields') or []:
        raw = answers.get(f.get('key'))
        vals = [v for v in (raw if isinstance(raw, list) else [raw]) if isinstance(v, str) and v.strip()]
        if not vals:
            continue
        by_label = {o.get('label'): o.get('value') for o in (f.get('options') or []) if o.get('value') is not None}
        vals = [by_label.get(v, v) for v in vals]
        out[f.get('key')] = vals if f.get('type') == 'multiselect' else ', '.join(vals)
    return out


def history_of(messages, cwd=None, window=None):
    """OpenCode 2 projected messages (GET /api/session/:id/message) into the history shape base.py describes."""
    h = {'messages': [], 'events': [], 'contexts': [], 'compacts': [], 'context': 0, 'window': window, 'base': None,
         'turnActive': False, 'start': None, 'last': None, 'parent': None, 'truncated': len(messages or []) >= HISTORY_LIMIT,
         'reasoning': [], 'turns': [], 'errors': [], 'model': None}
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        tm = m.get('time') if isinstance(m.get('time'), dict) else {}
        t = ms(tm.get('created')) or h['last'] or time.time()
        h['start'] = h['start'] or t
        h['last'] = t
        typ = m.get('type')
        if typ == 'user':
            text = m.get('text') or (m.get('payload') or {}).get('text') or ''
            h['messages'].append({'role': 'user', 'text': text, 't': t, 'id': m.get('id'), 'tokens': 0})
            h['turns'].append({'t': t})
        elif typ == 'assistant':
            mod = m.get('model') or {}
            if mod.get('id'):
                h['model'] = '%s/%s' % (mod.get('providerID'), mod['id'])
            for i, p in enumerate(m.get('content') or []):
                if not isinstance(p, dict):
                    continue
                if p.get('type') == 'text' and p.get('text'):
                    h['messages'].append({'role': 'assistant', 'text': p['text'], 't': t, 'id': m.get('id'), 'tokens': 0})
                elif p.get('type') == 'reasoning' and p.get('text'):
                    h['reasoning'].append({'text': p['text'], 't': t, 'end': t, 'id': '%s:%d' % (m.get('id'), i)})
                elif p.get('type') == 'tool':
                    name, st = p.get('name') or 'tool', p.get('state') if isinstance(p.get('state'), dict) else {}
                    inp = st.get('input') if isinstance(st.get('input'), dict) else {}
                    kind, status = kind_of(name), tool_status(name, st)
                    out = clean_output(name, text_of(st.get('content'))) or ''
                    if status in ('failed', 'interrupted') and not out:
                        err = st.get('error')
                        out = (err.get('message') if isinstance(err, dict) else err) or ''
                    text = summary(name, inp, cwd)
                    msg = {'role': 'tool', 'text': text + ('\n\n' + (out if len(out) <= 4000 else out[:4000] + '…') if out else ''),
                           't': t, 'id': p.get('id'), 'tokens': 0, 'tool': name, 'kind': kind, 'status': status}
                    h['messages'].append(msg)
                    raw = path_of(inp)
                    path = relative(raw, cwd) if (raw and cwd) else None
                    if kind in ('read', 'write') and not path:
                        continue          # outside the session's folder, or no path given
                    e = {'at': t, 'kind': kind, 'path': path, 'text': text[:200], 'item': p.get('id'), 'tokens': 0, 'tool': name, 'status': status}
                    d = diff_of(name, inp)
                    if d:
                        e['diff'] = d
                    h['events'].append(e)
            if m.get('content') and any(isinstance(p, dict) and p.get('type') == 'text' and p.get('text') for p in m['content']):
                h['events'].append({'at': t, 'kind': 'say', 'path': None, 'text': next(p['text'] for p in m['content'] if isinstance(p, dict) and p.get('type') == 'text' and p.get('text'))[:200],
                                    'item': m.get('id'), 'tokens': 0})
            tok = m.get('tokens')
            if isinstance(tok, dict):
                cache = tok.get('cache') or {}
                size = (tok.get('input') or 0) + (cache.get('read') or 0) + (cache.get('write') or 0)
                if size:
                    h['context'] = size
                    h['contexts'].append({'t': ms(tm.get('completed')) or t, 'context': size})
            if isinstance(m.get('error'), dict):
                f = classify(m['error'])
                h['errors'].append({'t': t, 'text': f['message'], 'code': f['code'], 'class': f['class'], 'resetsAt': f['resetsAt']})
        elif typ == 'compaction':
            h['compacts'].append({'t': t, 'from': h['context'] or None, 'to': None, 'summary': m.get('text') or m.get('summary')})
        elif typ == 'shell':
            cmd = m.get('command') or (m.get('payload') or {}).get('command') or 'shell'
            h['events'].append({'at': t, 'kind': 'run', 'path': None, 'text': str(cmd)[:200], 'item': m.get('id'), 'tokens': 0, 'tool': 'shell'})
        elif typ == 'idle' and m.get('outcome') == 'failed':
            h['errors'].append({'t': t, 'text': 'The turn ended with an error.', 'code': None, 'class': 'provider_error', 'resetsAt': None})
    h['errors'] = h['errors'][-10:]
    return h
