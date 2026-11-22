"""Agents over the Agent Client Protocol (ACP): one generic client, one AcpProvider per agent flavor (acp_flavors.py).

Design: docs/providers-design.md "Agents over ACP"; every mechanism and its reason: docs/providers-study/acp.md (cited as §n).
Spec read: agentclientprotocol docs/protocol/v1 and v2/migration.mdx.

Pieces:
- Connection: one agent process (its own process group, binary pipes). A reader thread that is strict per line (a line that
  is not JSON ends the connection) and tolerant per payload (an update we cannot read is skipped and counted, §3); a writer
  thread fed by one FIFO queue, so a prompt can never overtake a cancel (§3); a redacted stderr tail (§15). initialize sends
  the v1 and v2 fields together and reads the generation from the reply's shape, never its number (§1).
- History: the mapper from raw frames to the page's history shape. It is fed every frame of a live session as it passes and,
  after a restart, the same frames read back from our record, so a live column and a replayed one are built by one code path
  (§18).
- Session: one agent session we drive: new / resume / load (§6), prompt, turn settlement on the v1 prompt response or the v2
  idle state (§2), stop (pending permissions answered `cancelled`, session/cancel, a second cancel when the first may have
  arrived before the agent registered the prompt, then the process group killed after the flavor's bound, §4), options
  (§8), permission requests (§5), our record file.
- AcpProvider(Provider): the provider interface for one flavor.

Our record: every raw frame of a session we drive is appended to <data dir>/acp/<flavor>/<agent session id>.ndjson as
{t, dir: 'in' | 'out' | 'note', msg}; 'note' frames are what we decided locally (a turn ended by killing the process, a
load counted as done by its idle rule, a stdout line that was not a frame). A sidecar <id>.json holds {cwd, title, started,
model, flavor}. Data dir: data_root() (AGENT_IDE_DATA, else the app data folder's providers/). Sessions started elsewhere (in the agent's own app or CLI) are
out of scope for now: list_sessions lists only these records.

Not done here (each is in acp.md): sign-in calls (`authenticate`; we only show the flavor's sign-in command, §7), client
file system and terminals (never advertised, §10), elicitation, subagent lineage (§14), Antigravity's quota log (§13),
registry installs (§17).

How the ACP provider maps to the page
-------------------------------------
Ids. A session on the page is '<flavor name>:<agent session id>' (e.g. 'antigravity:ab887975-...'). A request is
'<session id>#<connection number>:<JSON-RPC id>'; the connection number makes an answer to a request from a process that
has since died recognisable as stale (§5), and such an answer is refused.

Event stream (the `publish` callable):
  {'method': 'provider/turn', 'params': {threadId, provider, started: True}}             a prompt was sent / v2 'running'
  {'method': 'provider/turn', 'params': {threadId, provider, ended: True, error, stopReason}}
                                                     v1 prompt response, v2 idle state, our kill after Stop, process death
  {'method': 'provider/activity', 'params': {threadId, provider}}   new output; at most every 300 ms per session; tool-call
                                                     updates coalesced (status/title change, completion, +256 chars, every 10th)
  core.request_message(...)                          a permission request or (flavor.question_prefix) a question
  {'method': 'serverRequest/resolved', 'params': {threadId, requestId}}   answered here, cancelled by Stop, or withdrawn by the
                                                     turn's end or the process's death

Requests (core.request_message shape): kind 'command' (execute), 'file-change' (edit/delete/move), 'file-read'
(read/search/fetch), 'permission' (anything else), 'question'; options = the agent's own [{id: optionId, label: name, kind}];
command (rawInput.command), paths (relative to the session's folder where inside it). reply(request_id, answer) takes
{'optionId'} (must be one the agent offered), {'decision': allow_once | allow_always | reject_once | reject_always | cancel}
(allow_always falls back to allow_once, reject_always to reject_once, when the agent did not offer it), or for a question
{'answers': {'0': [label]}}.

History (read_history, the shape claude_code.py returns):
  messages  [{role: user | assistant | tool, text, t, id, tokens: 0, tool?, kind?, status?}]  (assistant text accumulated per
            messageId; a tool message carries its title and the last 4,000 characters of its output)
  events    [{at, kind, path, text, item, tokens: 0, tool, status, diff?}] with kinds
              read   ACP kind read / search / fetch (path relative to the session folder; fetch has none)
              write  ACP kind edit / delete / move, one event per path; diff = unified diff (difflib for v1 oldText/newText,
                     the agent's patch for v2)
              run    ACP kind execute (text = the command)
              say    one per assistant message (its first 200 characters)
              ask    one per permission request or question, item 'ask:<rpc id>', answer filled in when answered
              tool   any other kind ('think' is not drawn)
            delegate is not produced: ACP has no portable subagent contract (§14).
  reasoning [{text, t, end, id}] from agent_thought_chunk
  turns     [{t}] one per prompt; turnActive while a turn runs (always False for a session no process holds)
  context / window / contexts [{t, context}] from usage_update (used, size); cost the agent's cumulative cost
  plan      [{t, steps: [{text, status}]}], each state the whole list; an empty list clears it; cancelled steps count done
  errors    [{t, text, code}] failed turns; stopReason the last turn's
  plus model, mode, title, cwd, start, last, options (the agent's configOptions / modes / models as last advertised).
"""
import collections
import difflib
import itertools
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time

from . import core, planuse
from .base import Provider, under, relative
from .keep import cache_dir


def data_root():
    """Where the driven providers keep their records (packaging, 2026-11-21): AGENT_IDE_DATA when set (tests), else providers/
    beside the cache in the app's data folder (keep.cache_dir: the Electron app's userData when it runs as the app, else
    ~/Library/Application Support/agent-ide on macOS). The branch's ~/.agent-ide is neither read nor moved."""
    return Path(os.environ.get('AGENT_IDE_DATA') or cache_dir().parent / 'providers')
from .acp_flavors import AUTO, LAUNCH, FLAVORS, Flavor

PROTOCOL = 2                 # the newest generation we speak; v1 agents read the v1 fields of the same request (§1)
OUTPUT_TAIL = 8_000          # cumulative tool output kept per call (§9, T3 runtimeModel.ts:647)
MESSAGE_OUTPUT = 4_000       # of which the chat shows this much under the tool's line
DIFF_CAP = 200_000           # a diff larger than this is cut (judgement: keeps one huge write from bloating the history)
STDERR_TAIL = 4_096          # §15, T3 stderr.ts
STDERR_LINE = 32_768         # one stderr read; a Google sign-in URL can be 16 KiB (§7)
ACTIVITY_EVERY = 0.3         # seconds between provider/activity messages per session
GROW_EMIT = 256              # a tool update is passed on when its output grew this much (§9 decideToolCallUpdateEmission)
NTH_EMIT = 10                # ... or on every 10th update
MAX_MESSAGES, MAX_EVENTS, MAX_STATES = 500, 3_000, 200
NOT_FOUND = -32601


class AcpError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class ConnectionClosed(AcpError):
    pass


# ---------------------------------------------------------------- redaction (§15) ----------------------------------------------------------------
_HOME = str(Path.home())
_SECRETS = [
    (re.compile(r'https://accounts\.google\.com/\S+'), '<google sign-in url>'),
    (re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}'), r'\1 <redacted>'),
    (re.compile(r'\bya29\.[A-Za-z0-9._-]+'), '<redacted token>'),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{20,}'), '<redacted key>'),
    (re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'), '<redacted key>'),
    (re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{20,})'), '<redacted token>'),
    (re.compile(r'(?i)("?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password|authorization)"?\s*[:=]\s*"?)'
                r'([^"\s,}]{8,})'), r'\1<redacted>'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), '<redacted jwt>'),
]


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def redact(text):
    """Home folder as ~, sign-in URLs, bearer/basic credentials, API keys and token-like values hidden."""
    s = str(text)
    if _HOME and _HOME != '/':
        s = s.replace(_HOME, '~')
    for pat, rep in _SECRETS:
        s = pat.sub(rep, s)
    return s


def login_said(tail):
    """The sign-in's last words worth showing (its last non-empty line, redacted, at most 240 characters): on a refused pasted
    code, the agent's own reason (2026-11-22)."""
    for line in reversed(list(tail)):
        line = line.strip()
        if line:
            return redact(line)[:240]
    return ''


def redact_obj(o):
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, list):
        return [redact_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: redact_obj(v) for k, v in o.items()}
    return o


def export_fixture(src, dst):
    """Copy a record with every string redacted (home -> ~, tokens hidden): how recorded/ fixtures are made."""
    with open(src, encoding='utf-8') as f, open(dst, 'w', encoding='utf-8') as g:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            g.write(json.dumps(redact_obj(rec), ensure_ascii=False) + '\n')


# ---------------------------------------------------------------- paths (§5) ----------------------------------------------------------------
def canonical(path):
    """realpath of the deepest existing ancestor, the rest appended: symlinks resolved before '..' is read (T3
    acpCanonicalPathForContainment: lexical normalisation can hide traversal)."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    rest = []
    while not os.path.exists(p):
        p, tail = os.path.split(p)
        if not tail:
            break
        rest.append(tail)
    out = os.path.realpath(p)
    for part in reversed(rest):
        out = os.path.normpath(os.path.join(out, part))
    return out


def contained(path, roots):
    c = canonical(path)
    for r in roots:
        r = canonical(r)
        if c == r or c.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


# ---------------------------------------------------------------- the connection ----------------------------------------------------------------
class Connection:
    """One agent process speaking ACP on stdio. `sink` receives every frame: sink.frame(dir, msg) ('in' as read, 'out' just
    before it is written, 'note' for a stdout line that is not a frame) and sink.closed(error) once."""

    numbers = itertools.count(1)

    def __init__(self, argv, cwd, flavor, env=None, sink=None):
        self.argv, self.cwd, self.flavor, self.env, self.sink = list(argv), str(cwd), flavor, env, sink
        self.number = next(Connection.numbers)
        self.pending = {}
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.wq = queue.Queue()
        self.closed = None          # the reason, once closed
        self.done = threading.Event()
        self.stderr_tail = ''
        self.signin_url = None
        self.proto = 1
        self.caps = {}
        self.proc = None

    # ---- process ----
    def start(self):
        env = dict(os.environ if self.env is None else self.env)
        bindir = os.path.dirname(self.argv[0])
        if bindir:          # an nvm-installed agent starts with `#!/usr/bin/env node`: its own folder holds that node
            env['PATH'] = bindir + os.pathsep + env.get('PATH', '')
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
        for fn in (self._read, self._stderr, self._write):
            threading.Thread(target=fn, daemon=True, name='acp-' + fn.__name__.strip('_')).start()
        return self

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and not self.closed

    def kill(self, grace=1.0):
        """End the agent's whole process group: SIGTERM, then SIGKILL after `grace` (§15: children such as browsers die too)."""
        p = self.proc
        if p is None:
            return
        try:
            pgid = os.getpgid(p.pid)
        except OSError:
            pgid = None
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if p.poll() is not None and sig == signal.SIGKILL:
                break
            try:
                if pgid:
                    os.killpg(pgid, sig)
                else:
                    p.send_signal(sig)
            except OSError:
                pass
            try:
                p.wait(timeout=grace)
                if pgid:          # the leader is gone; the rest of the group gets the same signal
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass
                break
            except subprocess.TimeoutExpired:
                continue
        self.wq.put(None)

    # ---- framing ----
    def _read(self):
        err = None
        try:
            for raw in iter(self.proc.stdout.readline, b''):          # the unterminated last line comes at EOF (T3 6d80df55b)
                line = raw.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                if any(line.startswith(p) for p in self.flavor.stdout_noise_prefixes):
                    m = re.search(r'https?://\S+', line)
                    if m:
                        self.signin_url = m.group(0)
                    self._sink('note', {'stdout': redact(line)})
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    msg = None
                if not isinstance(msg, dict):
                    err = 'The agent wrote a line that is not a protocol message: ' + redact(line[:300])
                    break
                self._dispatch(msg)
        except Exception as e:          # a broken pipe or a bug of ours: the connection ends, never silently
            err = 'Reading from the agent failed: %s' % e
        if err:
            self.kill(grace=0.5)
        else:
            try:
                code = self.proc.wait(timeout=0.25)          # stdout can close before the process exits (T3 82503ccf2)
            except subprocess.TimeoutExpired:
                code = None
            err = 'The agent exited' + ('' if code is None else ' (code %s)' % code)
            if self.stderr_tail.strip():
                err += ': ' + self.stderr_tail.strip().splitlines()[-1][:500]
        self._close(err)

    def _close(self, err):
        with self.lock:
            if self.closed:
                return
            self.closed = err
            pending, self.pending = self.pending, {}
        for slot in pending.values():
            if slot is not None:
                slot['error'] = {'code': -32099, 'message': err}
                slot['event'].set()
        self.wq.put(None)
        self.done.set()
        if self.sink is not None:
            try:
                self.sink.closed(err)
            except Exception:
                pass

    def _dispatch(self, msg):
        if 'method' not in msg and 'id' in msg:          # a response to one of ours
            with self.lock:
                slot = self.pending.pop(msg['id'], None)
            self._sink('in', msg)          # the history sees it before the caller wakes
            if slot is not None:
                slot.update(result=msg.get('result'), error=msg.get('error'))
                slot['event'].set()
            return
        self._sink('in', msg)

    def _sink(self, d, msg):
        if self.sink is None:
            return
        try:
            self.sink.frame(d, msg)
        except Exception as e:          # tolerant per payload: our own mapping bug never ends the connection
            self.sink_errors = getattr(self, 'sink_errors', 0) + 1
            self.last_sink_error = repr(e)

    def _stderr(self):
        try:
            while True:
                chunk = self.proc.stderr.readline(STDERR_LINE)
                if not chunk:
                    break
                text = chunk.decode('utf-8', errors='replace')
                m = re.search(r'https://accounts\.google\.com/\S+', text)
                if m:
                    self.signin_url = m.group(0)
                self.stderr_tail = (self.stderr_tail + redact(text))[-STDERR_TAIL:]
        except (OSError, ValueError):
            pass

    def _write(self):
        while True:
            item = self.wq.get()
            if item is None:
                return
            msg, data = item
            self._sink('out', msg)          # recorded in the order written: one queue, so nothing overtakes a cancel
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                return

    def send(self, msg):
        if self.closed:
            raise ConnectionClosed(-32099, self.closed)
        self.wq.put((msg, (json.dumps(msg, separators=(',', ':'), ensure_ascii=False) + '\n').encode('utf-8')))

    # ---- JSON-RPC ----
    def request(self, method, params, wait=True):
        """Send a request; with wait, a slot whose 'event' is set by the response (or by the connection's end)."""
        rid = next(self.ids)          # small ids: within int32 for SDKs that decode ids as Int (T3 fbe670c66)
        slot = {'event': threading.Event()} if wait else None
        with self.lock:
            if self.closed:
                raise ConnectionClosed(-32099, self.closed)
            self.pending[rid] = slot
        self.send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
        return rid, slot

    def call(self, method, params, timeout=30.0):
        rid, slot = self.request(method, params)
        if not slot['event'].wait(timeout):
            with self.lock:
                self.pending.pop(rid, None)
            raise AcpError(-32001, '%s: no answer within %ss' % (method, timeout))
        e = slot.get('error')
        if e:
            raise (ConnectionClosed if e.get('code') == -32099 else AcpError)(e.get('code'), str(e.get('message') or e), e.get('data'))
        return slot.get('result') or {}

    def notify(self, method, params):
        self.send({'jsonrpc': '2.0', 'method': method, 'params': params})          # no id: agents drop notifications that carry one (§3)

    def respond(self, rid, result=None, error=None):
        msg = {'jsonrpc': '2.0', 'id': rid}
        if error is not None:
            msg['error'] = error
        else:
            msg['result'] = result if result is not None else {}
        self.send(msg)

    def initialize(self, timeout=30.0):
        """One superset request (v1 and v2 fields); the generation from the reply's shape, never its number (§1). No file
        system, no terminals (§10)."""          # Antigravity reported version 2 in the v1 shape (acp.md §1)
        info = {'name': self.flavor.client_name, 'title': 'Agent IDE', 'version': '1'}
        r = self.call('initialize', {'protocolVersion': PROTOCOL, 'info': info, 'capabilities': {},
                                     'clientInfo': info, 'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}},
                      timeout=timeout)
        self.proto = 2 if 'info' in r else 1
        self.caps = normalize_caps(r, self.proto)
        return self.caps


def _marker(v):
    """A capability marker: True/False as given, {} present = supported, None/absent = not."""
    if isinstance(v, bool):
        return v
    return v is not None


def normalize_caps(r, proto):
    if proto == 2:
        caps = r.get('capabilities') or {}
        sess = caps.get('session') or {}
        prompt = sess.get('prompt') or {}
        info = r.get('info') or {}
        auth = [m.get('methodId') or m.get('id') for m in r.get('authMethods') or [] if isinstance(m, dict) and m.get('type')]
        return {'load': _marker(sess.get('load')), 'resume': _marker(sess.get('resume')), 'close': _marker(sess.get('close')),
                'list': _marker(sess.get('list')), 'image': _marker(prompt.get('image')), 'auth_methods': auth,
                'agent_name': info.get('name'), 'agent_version': info.get('version'), 'protocolVersion': r.get('protocolVersion')}
    caps = r.get('agentCapabilities') or {}
    sess = caps.get('sessionCapabilities') or {}
    info = r.get('agentInfo') or {}
    return {'load': bool(caps.get('loadSession')), 'resume': _marker(sess.get('resume')), 'close': _marker(sess.get('close')),
            'list': _marker(sess.get('list')), 'image': bool((caps.get('promptCapabilities') or {}).get('image')),
            'auth_methods': [m.get('id') for m in r.get('authMethods') or [] if isinstance(m, dict)],
            'agent_name': info.get('name'), 'agent_version': info.get('version'), 'protocolVersion': r.get('protocolVersion')}


# ---------------------------------------------------------------- the mapper ----------------------------------------------------------------
TOOL_KINDS = {'read': 'read', 'search': 'read', 'fetch': 'read', 'edit': 'write', 'delete': 'write', 'move': 'write',
              'execute': 'run', 'think': None}
REQUEST_KINDS = {'execute': 'command', 'edit': 'file-change', 'delete': 'file-change', 'move': 'file-change',
                 'read': 'file-read', 'search': 'file-read', 'fetch': 'file-read'}
OPEN = ('pending', 'in_progress')
# Input field names read for a tool's command and path. ACP leaves rawInput free-form; the capitalised ones were seen in
# Antigravity 1.5.3 frames here (2026-11-09 live check: rawInput {CommandLine}, {Path, Content}).
COMMAND_KEYS = ('command', 'cmd', 'commandLine', 'CommandLine')
PATH_KEYS = ('path', 'filePath', 'file_path', 'Path', 'AbsolutePath')


def command_of(ri):
    for k in COMMAND_KEYS:
        v = ri.get(k)
        if isinstance(v, list):
            return ' '.join(str(x) for x in v)
        if isinstance(v, str) and v:
            return v
    return None


def empty_history(cwd=None):
    return {'messages': [], 'events': [], 'turnActive': False, 'parent': None, 'contexts': [], 'compacts': [], 'window': None,
            'context': 0, 'base': None, 'reasoning': [], 'turns': [], 'errors': [], 'plan': [], 'start': None, 'last': None,
            'truncated': False, 'model': None, 'mode': None, 'effort': None, 'title': None, 'cwd': cwd, 'stopReason': None,
            'cost': None, 'asking': {}, 'options': {}, 'unknown': 0, 'commands': []}


def text_of(content):
    blocks = content if isinstance(content, list) else [content]
    return ''.join(b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str))


def tail(s, n=OUTPUT_TAIL):
    return s if len(s) <= n else '[… earlier output cut]\n' + s[-n:]


def select_values(opt):
    """A select config option's values, groups flattened: [(value, name, description)]."""
    out = []
    for o in opt.get('options') or []:
        if not isinstance(o, dict):
            continue
        if 'options' in o and 'value' not in o:
            out += [(x.get('value'), x.get('name') or x.get('value'), x.get('description')) for x in o['options'] if isinstance(x, dict)]
        else:
            out.append((o.get('value'), o.get('name') or o.get('value'), o.get('description')))
    return [v for v in out if v[0] is not None]


class History:
    """Raw frames -> the page's history (module docstring). feed() returns effects for a live session to act on:
    ('turn_started',), ('turn_ended', stopReason, error), ('request', rpc id, params), ('unhandled', rpc id, method),
    ('resolved', rpc id), ('update',), ('activity',)."""

    def __init__(self, cwd=None):
        self.c = empty_history(cwd)
        self.gen = 1
        self.out = {}
        self.tools = {}
        self.msgs = {}
        self.last_item = None
        self.plans = {}
        self.replay = None          # 'keep' | 'drop' while a session/load replays the conversation
        self.prompt_id = None
        self.echo = None            # the text we sent, until its echo has been seen (v2 user_message; §2)
        self.echo_ids = set()
        self.warned = set()

    # ---- entry ----
    def feed(self, d, msg, t):
        c, fx = self.c, []
        if c['start'] is None:
            c['start'] = t
        c['last'] = t
        if d == 'note':
            self.note(msg or {}, t, fx)
            return fx
        if not isinstance(msg, dict):
            return fx
        method, mid, params = msg.get('method'), msg.get('id'), msg.get('params') if isinstance(msg.get('params'), dict) else {}
        if d == 'out':
            if method and mid is not None:
                self.out[mid] = (method, params)
                self.sent(mid, method, params, t, fx)
            elif method == 'session/cancel':
                c['cancelAt'] = t
            elif mid is not None:
                self.answered(mid, msg, t, fx)
        else:
            if method and mid is not None:
                self.asked(mid, method, params, t, fx)
            elif method == 'session/update':
                try:
                    self.update(params.get('update'), t, fx)
                except Exception as e:          # tolerant per payload (§3, T3 559c4b1f2): skipped, counted
                    c['unknown'] += 1
                    self.warned.add('%s: %s' % (type(e).__name__, e))
            elif method:
                c['unknown'] += 1
            elif mid is not None:
                self.response(mid, msg, t, fx)
        self.trim()
        return fx

    def trim(self):
        c = self.c
        for k, n in (('messages', MAX_MESSAGES), ('events', MAX_EVENTS), ('reasoning', MAX_MESSAGES), ('turns', MAX_MESSAGES), ('plan', MAX_STATES), ('contexts', MAX_EVENTS)):
            if len(c[k]) > n + n // 5:
                del c[k][:len(c[k]) - n]
                c['truncated'] = True

    # ---- what we sent ----
    def sent(self, mid, method, params, t, fx):
        c = self.c
        if method == 'initialize':          # a new process: a turn the old one left open is over
            if c['turnActive']:
                self.settle(t, 'error', 'The agent process ended before the turn finished.', [])
            c['asking'] = {}
        elif method == 'session/prompt':
            text = text_of(params.get('prompt'))
            m = {'role': 'user', 'text': text, 't': t, 'id': 'prompt:%s' % mid, 'tokens': 0}
            c['messages'].append(m)
            self.last_item = m
            c['turns'].append({'t': t})
            if not c['title']:
                c['title'] = ' '.join(text.split())[:80] or None
            self.prompt_id, self.echo = mid, text
            c['cancelAt'] = None
            self.msgs = {}          # message ids belong to a turn: an agent that reuses one never joins two turns' text
            if not c['turnActive']:
                c['turnActive'] = True
                c['stopReason'] = None
                fx.append(('turn_started',))
        elif method == 'session/load':
            self.replay = 'drop' if c['messages'] else 'keep'          # our record already has the conversation (§6)

    def answered(self, mid, msg, t, fx):
        a = self.c['asking'].pop(str(mid), None)
        if a is None:
            return
        out = ((msg.get('result') or {}).get('outcome') or {}) if isinstance(msg.get('result'), dict) else {}
        ans = out.get('optionId') if out.get('outcome') == 'selected' else (out.get('outcome') or 'error')
        for e in reversed(self.c['events']):
            if e.get('item') == 'ask:%s' % mid:
                e['answer'] = ans
                break
        fx.append(('resolved', mid))

    # ---- what the agent answered ----
    def response(self, mid, msg, t, fx):
        c = self.c
        method, params = self.out.pop(mid, (None, {}))
        result, error = msg.get('result'), msg.get('error')
        if method == 'initialize' and isinstance(result, dict):
            self.gen = 2 if 'info' in result else 1
            info = result.get('info') or result.get('agentInfo') or {}
            c['agent'] = {'name': info.get('name'), 'version': info.get('version'), 'protocol': self.gen}
        elif method in ('session/new', 'session/resume', 'session/load'):
            if isinstance(result, dict):
                self.setup(result)
                if result.get('sessionId'):
                    c['sessionId'] = result['sessionId']
            if method == 'session/load':
                self.replay = None
        elif method == 'session/prompt':
            if mid != self.prompt_id:
                return          # an earlier prompt's late answer
            if error:
                self.settle(t, 'error', str(error.get('message') or error), fx, code=error.get('code'))
            elif self.gen == 1:
                self.settle(t, (result or {}).get('stopReason') or 'end_turn', None, fx)
            elif isinstance(result, dict) and result.get('messageId'):
                self.echo_ids.add(result['messageId'])          # v2: an acknowledgement; the turn ends on idle (§2)
        elif method == 'session/set_config_option' and isinstance(result, dict) and isinstance(result.get('configOptions'), list):
            c['options']['config'] = result['configOptions']
            self.current()
        elif method == 'session/set_mode' and not error:
            c['mode'] = params.get('modeId')
        elif method == 'session/set_model' and not error:
            c['model'] = params.get('modelId')

    def setup(self, r):
        o = self.c['options']
        if isinstance(r.get('configOptions'), list):
            o['config'] = r['configOptions']
        if isinstance(r.get('modes'), dict):
            o['modes'] = r['modes']
        if isinstance(r.get('models'), dict):
            o['models'] = r['models']
        self.current()

    def current(self):
        c, o = self.c, self.c['options']
        cats = {x.get('category'): x for x in o.get('config') or [] if isinstance(x, dict) and x.get('category')}
        c['model'] = (cats.get('model') or {}).get('currentValue') or (o.get('models') or {}).get('currentModelId') or c['model']
        c['mode'] = (cats.get('mode') or {}).get('currentValue') or (o.get('modes') or {}).get('currentModeId') or c['mode']
        c['effort'] = (cats.get('thought_level') or {}).get('currentValue') or c['effort']

    def settle(self, t, reason, error, fx, code=None):
        c = self.c
        if not c['turnActive']:
            return
        c['turnActive'] = False
        c['stopReason'] = reason
        if error:
            c['errors'] = (c['errors'] + [{'t': t, 'text': error, 'code': code}])[-10:]
        for st in self.tools.values():          # a tool still open when the turn ends was interrupted (T3 076aacca1)
            if st['status'] in OPEN:
                st['status'] = 'interrupted' if reason in ('cancelled', 'error') else st['status']
                self.render(st, t)
        for k in list(c['asking']):
            c['asking'].pop(k)
            fx.append(('resolved', k))
        self.prompt_id, self.echo = None, None
        fx.append(('turn_ended', reason, error))

    # ---- what the agent asked ----
    def asked(self, mid, method, params, t, fx):
        c = self.c
        if method != 'session/request_permission':
            fx.append(('unhandled', mid, method))
            return
        tc = params.get('toolCall') if isinstance(params.get('toolCall'), dict) else {}
        st = self.tools.get(tc.get('toolCallId'))
        title = tc.get('title') or (st or {}).get('title') or 'Permission'
        c['asking'][str(mid)] = {'t': t, 'title': title, 'toolCallId': tc.get('toolCallId'), 'options': params.get('options') or []}
        c['events'].append({'at': t, 'kind': 'ask', 'path': None, 'text': str(title)[:200], 'item': 'ask:%s' % mid, 'tokens': 0, 'tool': title})
        fx.append(('request', mid, params))

    # ---- session/update ----
    def update(self, u, t, fx):
        c = self.c
        if not isinstance(u, dict):
            raise ValueError('update is not an object')
        if (u.get('_meta') or {}).get('isReplay') or self.replay == 'drop':
            return          # replayed history we already hold (§6, T3 runtimeModel.ts:1247)
        kind = u.get('sessionUpdate')
        fx.append(('update',))
        if kind in ('agent_message_chunk', 'agent_message'):
            self.message('assistant', u, t, fx, whole=kind == 'agent_message')
        elif kind in ('user_message_chunk', 'user_message'):
            self.message('user', u, t, fx, whole=kind == 'user_message')
        elif kind in ('agent_thought_chunk', 'agent_thought'):
            self.thought(u, t, whole=kind == 'agent_thought')
        elif kind in ('tool_call', 'tool_call_update'):
            self.tool(u, t, fx)
        elif kind == 'plan':
            self.plans = {None: u.get('entries') if isinstance(u.get('entries'), list) else []}
            self.push_plan(t)
        elif kind == 'plan_update':
            p = u.get('plan') or {}
            self.plans[p.get('planId')] = p.get('entries') if isinstance(p.get('entries'), list) else []
            self.push_plan(t)
        elif kind == 'plan_removed':
            self.plans.pop(u.get('planId'), None)
            self.push_plan(t)
        elif kind == 'usage_update':
            used, size = u.get('used'), u.get('size')
            if isinstance(used, (int, float)):
                c['context'] = int(used)
                c['contexts'].append({'t': t, 'context': int(used)})
            if isinstance(size, (int, float)) and size > 0:
                c['window'] = int(size)
            if isinstance(u.get('cost'), dict):
                c['cost'] = u['cost']
        elif kind == 'current_mode_update':
            c['mode'] = u.get('currentModeId') or u.get('modeId') or c['mode']
        elif kind == 'config_option_update':
            if isinstance(u.get('configOptions'), list):
                c['options']['config'] = u['configOptions']
                self.current()
        elif kind == 'session_info_update':
            if u.get('title'):
                c['title'] = str(u['title'])
        elif kind == 'available_commands_update':
            c['commands'] = [x.get('name') for x in u.get('availableCommands') or [] if isinstance(x, dict)]
        elif kind == 'state_update':          # v2 foreground state (§2)
            st = u.get('state')
            if st in ('running', 'requires_action') and not c['turnActive']:
                c['turnActive'] = True
                fx.append(('turn_started',))
            elif st == 'idle':
                err = u.get('error') if isinstance(u.get('error'), dict) else None
                reason = u.get('stopReason') or ('error' if err else None)
                self.settle(t, reason, (err or {}).get('message') if err else None, fx, code=(err or {}).get('code'))
        elif kind in ('compaction_update', 'compaction_summary_chunk', 'notice'):
            pass          # not advertised; ignored if sent anyway
        else:
            c['unknown'] += 1
            self.warned.add('unknown update %s' % kind)
            return
        if kind not in ('tool_call', 'tool_call_update'):
            fx.append(('activity',))

    def message(self, role, u, t, fx, whole=False):
        c = self.c
        text = text_of(u.get('content'))
        if not isinstance(u.get('content'), (dict, list)):
            raise ValueError('message content is not a content block')
        mid = u.get('messageId')
        if role == 'user':
            if mid is not None and mid in self.echo_ids:
                return
            if self.echo is not None and text and self.echo.startswith(text):          # the echo of what we sent (§2, T3 3f75a0e00)
                self.echo = self.echo[len(text):] or None
                if mid is not None:
                    self.echo_ids.add(mid)
                return
        key = (role, mid) if mid is not None else None
        m = self.msgs.get(key) if key else None
        if m is None and key is None and self.last_item is not None and self.last_item.get('role') == role and self.last_item.get('chunked'):
            m = self.last_item
        if m is None:
            m = {'role': role, 'text': '', 't': t, 'id': mid or '%s:%d' % (role, len(c['messages'])), 'tokens': 0, 'chunked': True}
            c['messages'].append(m)
            if key:
                self.msgs[key] = m
            if role == 'assistant':
                m['say'] = {'at': t, 'kind': 'say', 'path': None, 'text': '', 'item': m['id'], 'tokens': 0}
                c['events'].append(m['say'])
        m['text'] = text if whole else m['text'] + text
        if 'say' in m:
            m['say']['text'] = m['text'][:200]
        self.last_item = m

    def thought(self, u, t, whole=False):
        r = self.c['reasoning']
        text = text_of(u.get('content'))
        mid = u.get('messageId')
        if r and self.last_item is r[-1] and (mid is None or r[-1]['id'] == mid):
            r[-1]['text'] = text if whole else r[-1]['text'] + text
            r[-1]['end'] = t
        else:
            r.append({'text': text, 't': t, 'end': t, 'id': mid or 'thought:%d' % len(r)})
            self.last_item = r[-1]

    def tool(self, u, t, fx):
        tid = u.get('toolCallId')
        if not tid:
            raise ValueError('tool call without toolCallId')
        st = self.tools.get(tid)
        if st is None:
            st = self.tools[tid] = {'id': tid, 'title': '', 'kind': None, 'status': 'pending', 'locations': [], 'rawInput': None,
                                    'content': [], 'rawOutput': None, 't': t, 'msg': None, 'events': {}, 'n': 0, 'emitted': None}
        for k, empty in (('title', ''), ('kind', None), ('status', 'pending'), ('locations', []), ('rawInput', None), ('content', []), ('rawOutput', None)):
            if k in u:          # present overrides, absent keeps, null clears (§9, v2 patch rule)
                st[k] = u[k] if u[k] is not None else empty
        if st['status'] == 'cancelled':
            st['status'] = 'failed'
        if st['status'] == 'failed' and self.c.get('cancelAt') and self.c['turnActive']:
            st['status'] = 'interrupted'          # failed because we stopped it (T3 stoppedToolStatus, adapter.ts:1281)
        st['n'] += 1
        self.render(st, t)
        snap = (st['status'], st['title'], len(st.get('output') or ''))
        prev = st['emitted']
        if prev is None or snap[:2] != prev[:2] or st['status'] not in OPEN or snap[2] - prev[2] >= GROW_EMIT or st['n'] % NTH_EMIT == 0:
            st['emitted'] = snap
            fx.append(('activity',))

    def paths_of(self, st):
        ps = [x.get('path') for x in st['locations'] or [] if isinstance(x, dict) and x.get('path')]
        ps += [x.get('path') for x in st['content'] or [] if isinstance(x, dict) and x.get('type') == 'diff' and x.get('path')]
        ri = st['rawInput'] if isinstance(st['rawInput'], dict) else {}
        if not ps:
            ps = [ri.get(k) for k in PATH_KEYS if isinstance(ri.get(k), str)]
        seen, out = set(), []
        for p in ps:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def render(self, st, t):
        """A tool call's chat line and floor events from its merged state."""
        c = self.c
        cwd = c['cwd']
        acp_kind = st['kind'] or 'other'
        kind = TOOL_KINDS.get(acp_kind, 'tool')
        ri = st['rawInput'] if isinstance(st['rawInput'], dict) else {}
        cmd = command_of(ri)
        texts, diffs = [], {}
        for x in st['content'] or []:
            if not isinstance(x, dict):
                continue
            if x.get('type') == 'content':
                texts.append(text_of(x.get('content')))
            elif x.get('type') == 'diff':
                diffs[x.get('path') or ''] = diff_text(x, cwd)
        out = '\n'.join(s for s in texts if s)
        if not out and st['rawOutput'] is not None:
            ro = st['rawOutput']
            out = ro if isinstance(ro, str) else str(ro.get('output') or ro.get('stdout') or ro.get('text') or '') if isinstance(ro, dict) else ''
        st['output'] = tail(out)
        line = (str(cmd) if kind == 'run' and cmd else st['title']) or acp_kind
        if kind is None:          # 'think': not drawn
            return
        m = st['msg']
        if m is None:
            m = st['msg'] = {'role': 'tool', 'text': '', 't': st['t'], 'id': st['id'], 'tokens': 0, 'tool': st['title'], 'kind': kind}
            c['messages'].append(m)
            self.last_item = m
        m.update(tool=st['title'], kind=kind, status=st['status'],
                 text=line + ('\n\n' + st['output'][-MESSAGE_OUTPUT:] if st['output'] else ''))
        paths = self.paths_of(st)
        if kind in ('read', 'write'):
            targets = [(p, relative(p, cwd) if cwd else None) for p in paths]
            targets = [(p, r) for p, r in targets if r]
            if acp_kind == 'fetch':
                targets = [(None, None)]
        else:
            targets = [(None, None)]
        for i, (p, rel) in enumerate(targets):
            key = rel or ''
            e = st['events'].get(key)
            if e is None:
                e = st['events'][key] = {'at': st['t'], 'kind': kind, 'path': rel, 'text': '', 'item': st['id'] if not st['events'] else '%s#%d' % (st['id'], len(st['events'])),
                                         'tokens': 0, 'tool': st['title']}
                c['events'].append(e)
            e.update(text=line[:200], tool=st['title'], status=st['status'])
            if p is not None and p in diffs:
                e['diff'] = diffs[p]

    def push_plan(self, t):
        steps = []
        for entries in self.plans.values():
            for e in entries or []:
                if not isinstance(e, dict):
                    continue
                s = str(e.get('status') or 'pending')
                step = {'text': str(e.get('content') or ''), 'status': 'completed' if s in ('completed', 'cancelled') else 'in_progress' if s == 'in_progress' else 'pending'}
                if s == 'cancelled':
                    step['cancelled'] = True          # terminal, never "running" (T3 5ff148276)
                steps.append(step)
        states = self.c['plan']
        if states and states[-1]['steps'] == steps:
            return
        states.append({'t': t, 'steps': steps})          # an empty list is a state too: it clears the plan (T3 9522c7dd8)

    # ---- our local decisions ----
    def note(self, n, t, fx):
        if n.get('loaded'):
            self.replay = None
        if isinstance(n.get('turn_end'), dict):
            te = n['turn_end']
            self.settle(t, te.get('stopReason'), te.get('error'), fx)
        if n.get('closed'):
            self.settle(t, 'error', str(n['closed']), fx)
            self.c['asking'] = {}
        if isinstance(n.get('failure'), dict):          # a turn the agent ended normally that failed (Flavor.silent_failure)
            f = n['failure']
            self.c['stopReason'] = 'error'
            self.c['errors'] = (self.c['errors'] + [{'t': t, 'text': f.get('message') or 'The turn failed.', 'code': f.get('class'),
                                                     'resetsAt': f.get('resetsAt')}])[-10:]

    def turn_tail(self):
        """The last turn as the agent answered it: its assistant text and how many tools it ran."""
        text, tools = [], 0
        for m in reversed(self.c['messages']):
            if m.get('role') == 'user':
                break
            if m.get('role') == 'assistant':
                text.append(m.get('text') or '')
            elif m.get('role') == 'tool':
                tools += 1
        return {'text': ''.join(reversed(text)), 'tools': tools}

    def snapshot(self, live=True):
        c = self.c
        out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in c.items()}
        out['messages'] = [{k: v for k, v in m.items() if k not in ('say', 'chunked')} for m in c['messages']]
        if not live:
            out['turnActive'] = False
            out['asking'] = {}
        return out


def diff_text(x, cwd=None):
    """A diff content entry as a unified diff: v1 {path, oldText, newText} through difflib (no oldText: a new file), v2 the
    agent's own patch (§9)."""
    if isinstance(x.get('patch'), str):
        return x['patch'][:DIFF_CAP]
    path = x.get('path') or ''
    rel = (relative(path, cwd) if cwd else None) or path
    old, new = x.get('oldText'), x.get('newText')
    a = [] if old is None else str(old).splitlines(keepends=True)
    b = [] if new is None else str(new).splitlines(keepends=True)
    lines = difflib.unified_diff(a, b, fromfile='/dev/null' if old is None else 'a/' + rel, tofile='/dev/null' if new is None else 'b/' + rel)
    out = ''.join(ln if ln.endswith('\n') else ln + '\n' for ln in lines)
    return out[:DIFF_CAP]


LOOKS = {}          # agent name -> what its --version and status command last said (AcpProvider.sign_in_status)
CUT_TURN = ('This turn did not finish: the agent\'s process ended (the workspace server stopped or restarted) before the agent '
            'finished. Send a message to go on.')


def replay(path, cwd=None):
    """A record file through the mapper: the History of a session no process holds."""
    h, last = History(cwd), 0
    try:
        f = open(path, encoding='utf-8')
    except OSError:
        return h
    with f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                h.feed(rec.get('dir'), rec.get('msg'), rec.get('t') or 0)
                last = rec.get('t') or last
    if h.c['turnActive']:          # robust (port 2026-11-21): a record that ends mid-turn: its process went with the server, said plainly
        h.settle(last, 'error', CUT_TURN, [])
    return h


# ---------------------------------------------------------------- one session we drive ----------------------------------------------------------------
class Session:
    def __init__(self, provider, conn, cwd, key=None, history=None, launch_mode=None):
        self.p, self.conn, self.cwd = provider, conn, str(cwd)
        self.key = key          # the agent's session id
        self.h = history or History(self.cwd)
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.buffer = []
        self.record = None
        self.pending = {}          # request id -> {rpc, gen, msg, params, question}
        self.prompting = False
        self.stopping = False
        self.starting = True
        self.full_access = False
        self.page_mode = None
        self.launch_mode = launch_mode
        self.recancel = False
        self.updates = 0
        self.last_output = time.time()
        self.last_activity = 0.0
        self.turn_no = 0
        self.released = False
        if key:
            self.attach(key)

    @property
    def sid(self):
        return '%s:%s' % (self.p.name, self.key)

    # ---- record ----
    def attach(self, key):
        with self.lock:
            self.key = key
            path = self.p.record_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.record = open(path, 'a', encoding='utf-8')
            for line in self.buffer:
                self.record.write(line)
            self.record.flush()
            self.buffer = []

    def write(self, d, msg, t):
        line = json.dumps({'t': round(t, 3), 'dir': d, 'msg': msg}, ensure_ascii=False) + '\n'
        if self.record is None:
            self.buffer.append(line)
        else:
            try:
                self.record.write(line)
                self.record.flush()
            except (OSError, ValueError):
                pass

    # ---- frames (Connection sink) ----
    def frame(self, d, msg):
        t = time.time()
        with self.lock:
            self.write(d, msg, t)
            fx = self.h.feed(d, msg, t)
            if fx:
                self.cond.notify_all()
        for f in fx:
            self.effect(f)

    def closed(self, err):
        with self.lock:          # taken before the note settles the turn (which withdraws open requests as answered-by-the-turn's-end):
            stale = list(self.pending)          # a request open when the process went is lost, not withdrawn (port fix 2026-11-21)
            self.pending.clear()
        self.p.dropped(self)          # out of the live sessions before the note says the turn ended (race seen 2026-11-22)
        self.frame('note', {'closed': err})
        for rid in stale:
            self.p.resolved(self, rid, lost='the agent process stopped' + (' (%s)' % str(err)[:200] if err else ''))

    def note(self, **n):
        self.frame('note', n)

    def effect(self, f):
        kind = f[0]
        if kind == 'turn_started':
            self.prompting = False
            self.turn_no += 1
            self.last_output = time.time()
            self.p.emit({'method': 'provider/turn', 'params': {'threadId': self.sid, 'provider': self.p.name, 'started': True}})
            if self.p.flavor.inactivity_timeout:
                threading.Thread(target=self._watch, args=(self.turn_no,), daemon=True).start()
        elif kind == 'turn_ended':
            self.prompting = False
            with self.lock:
                stale = list(self.pending)
                self.pending.clear()
                self.recancel = False
            for rid in stale:          # withdrawn with the turn: the card closes
                self.p.resolved(self, rid)
            fail = None
            check = self.p.flavor.silent_failure
            if check and f[1] not in ('cancelled',) and not f[2] and self.key:
                try:
                    with self.lock:
                        tail = self.h.turn_tail()
                    fail = check(self.key, tail)
                except Exception:          # a store we cannot read never fails a turn by itself
                    fail = None
                if fail:
                    self.note(failure=fail)
            params = {'threadId': self.sid, 'provider': self.p.name, 'ended': True, 'error': f[2], 'stopReason': f[1]}
            if fail:
                params.update(error=fail.get('message'), stopReason='error', failure={'class': fail.get('class'), 'resetsAt': fail.get('resetsAt')})
            # plan use: a refusal (its plan, quota or usage limit) becomes the agent's state in the usage panel; a turn that went
            # through clears it (providers/planuse.py)
            err, cls = params.get('error'), (params.get('failure') or {}).get('class')
            refused = self.p.refusals.turn_ended(err, cls, (params.get('failure') or {}).get('resetsAt'), session=self.sid, cancelled=f[1] == 'cancelled')
            if refused:
                params['failure'] = dict(params.get('failure') or {}, refused=True, resetsAt=refused['resetsAt'])
                self.p.plan_cache.due()          # its figures are read again (within the once-per-5-minutes cap): they may now show the limit
            self.stopping = False          # settled before the end is said: whoever acts on it reads an idle session and its title
            self.p.touch(self)
            self.p.emit({'method': 'provider/turn', 'params': params})
        elif kind == 'request':
            self.permission(f[1], f[2])
        elif kind == 'unhandled':
            try:
                self.conn.respond(f[1], error={'code': NOT_FOUND, 'message': 'Not supported by this client: %s' % f[2]})
            except AcpError:
                pass
        elif kind == 'update':
            self.updates += 1
            self.last_output = time.time()
            if self.recancel:          # the first update after a cancel that may have come too early (§4)
                self.recancel = False
                self._cancel()
        elif kind == 'activity':
            now = time.time()
            if now - self.last_activity >= ACTIVITY_EVERY:
                self.last_activity = now
                self.p.emit({'method': 'provider/activity', 'params': {'threadId': self.sid, 'provider': self.p.name}})

    # ---- permissions (§5) ----
    def permission(self, rpc, params):
        fl = self.p.flavor
        tc = params.get('toolCall') if isinstance(params.get('toolCall'), dict) else {}
        st = self.h.tools.get(tc.get('toolCallId')) or {}
        acp_kind = tc.get('kind') or st.get('kind')          # Kiro leaves kind off the request (T3 adapter.ts:6537)
        options = [o for o in params.get('options') or [] if isinstance(o, dict) and o.get('optionId')]
        question = bool(fl.question_prefix and str(tc.get('toolCallId') or '').startswith(fl.question_prefix))
        merged = dict(st)
        for k in ('locations', 'rawInput', 'content'):
            if tc.get(k):
                merged[k] = tc[k]
        merged.setdefault('locations', [])
        merged.setdefault('content', [])
        merged.setdefault('rawInput', None)
        paths = self.h.paths_of(merged)
        auto = None if question else self.auto_option(acp_kind, paths, options)
        if auto:
            try:
                self.conn.respond(rpc, {'outcome': {'outcome': 'selected', 'optionId': auto}})
            except AcpError:
                pass
            return
        rid = '%s#%d:%s' % (self.sid, self.conn.number, rpc)
        title = tc.get('title') or st.get('title') or ('Question' if question else 'Permission')
        cmd = command_of(merged['rawInput'] if isinstance(merged['rawInput'], dict) else {})
        rels = [relative(p, self.cwd) or p for p in paths]
        if question:
            opts = [{'id': o['optionId'], 'label': o.get('name') or o['optionId'], 'kind': 'answer'} for o in options]
            qtext = text_of([x.get('content') for x in tc.get('content') or [] if isinstance(x, dict) and x.get('type') == 'content']) or title
            msg = core.request_message(self.p.name, self.sid, rid, 'question', title, opts,
                                       questions=[{'id': '0', 'question': qtext, 'multiSelect': False,
                                                   'options': [{'label': o['label'], 'description': ''} for o in opts]}])
        else:
            opts = []
            for o in options:
                x = {'id': o['optionId'], 'label': o.get('name') or o['optionId'], 'kind': o.get('kind') or 'allow_once'}
                warn = ((o.get('_meta') or {}).get(fl.option_warning_meta) or {}) if fl.option_warning_meta and isinstance(o.get('_meta'), dict) else {}
                if isinstance(warn, dict) and (warn.get('message') or warn.get('title')):
                    x['warning'] = str(warn.get('message') or warn.get('title'))[:512]          # shown under the button (§5)
                opts.append(x)
            msg = core.request_message(self.p.name, self.sid, rid, REQUEST_KINDS.get(acp_kind, 'permission'), title, opts,
                                       command=cmd or None, paths=rels or None)
        msg['params']['answerVia'] = 'live'
        with self.lock:
            self.pending[rid] = {'rpc': rpc, 'gen': self.conn.number, 'msg': msg, 'options': options, 'question': question}
        self.p.index(rid, self)
        self.p.emit(msg)

    def auto_option(self, acp_kind, paths, options):
        """Full access answered here: reads always; edits only when every path is inside the session's folder after symlinks are
        resolved (no path: ask); commands always (judgement: the person chose full access); anything else asks. allow_once
        only: allow_always can outlive the session (§5)."""
        if not self.full_access:
            return None
        once = next((o['optionId'] for o in options if o.get('kind') == 'allow_once'), None)
        if not once:
            return None
        if acp_kind in ('read', 'search', 'think', 'fetch', 'execute'):
            return once
        if acp_kind in ('edit', 'delete', 'move'):
            return once if paths and all(contained(p, [self.cwd]) for p in paths) else None
        return None

    def reply(self, rid, answer):
        with self.lock:
            req = self.pending.get(rid)
            if req is None:
                raise ValueError('This request has already been resolved.')
            if req['gen'] != self.conn.number or self.conn.closed:
                self.pending.pop(rid, None)
                stale = True
            else:
                stale = False
        if stale:
            self.p.resolved(self, rid)
            raise ValueError('The agent that asked this has stopped; the request has expired.')
        outcome = choose(req, answer or {})
        with self.lock:
            self.pending.pop(rid, None)
        self.conn.respond(req['rpc'], {'outcome': outcome})
        self.p.resolved(self, rid)
        return {}

    # ---- turns ----
    def prompt(self, text):
        with self.lock:
            if self.h.c['turnActive'] or self.prompting or self.stopping:
                raise ValueError('A turn is running; wait for it to end or stop it.')
            self.prompting = True
            self.recancel = False
            self.updates = 0
        try:
            self.conn.request('session/prompt', {'sessionId': self.key, 'prompt': [{'type': 'text', 'text': text}]}, wait=False)
        except AcpError:
            self.prompting = False
            raise

    def running(self):
        return bool(self.h.c['turnActive'] or self.prompting)

    def phase(self):
        if self.starting:
            return 'starting'
        if self.stopping:
            return 'stopping'
        return 'running' if self.running() else 'idle'

    def _cancel(self):
        try:
            self.conn.notify('session/cancel', {'sessionId': self.key})
        except AcpError:
            pass

    def stop(self, wait=False):
        """§4: pending permissions answered `cancelled` (the spec requires it), session/cancel, a second cancel if no update
        came since the prompt (on the first update or after recancel_after), then the turn may take cancel_timeout to settle
        before the process group is killed and the turn ended here."""
        with self.lock:
            if not self.running():
                raise ValueError('This agent is not running here.')
            self.stopping = True
            pend, self.pending = self.pending, {}
            self.recancel = self.updates == 0
        self.p.emit({'method': 'provider/activity', 'params': {'threadId': self.sid, 'provider': self.p.name}})
        for rid, req in pend.items():
            try:
                self.conn.respond(req['rpc'], {'outcome': {'outcome': 'cancelled'}})
            except AcpError:
                pass
            self.p.resolved(self, rid)
        self._cancel()
        th = threading.Thread(target=self._stop_wait, daemon=True)
        th.start()
        if wait:
            th.join()
        return {}

    def _stop_wait(self):
        fl = self.p.flavor
        start = time.time()
        deadline = start + fl.cancel_timeout
        with self.lock:
            while self.running() and time.time() < deadline:
                if self.recancel and time.time() - start >= fl.recancel_after:
                    self.recancel = False
                    self.lock.release()
                    try:
                        self._cancel()
                    finally:
                        self.lock.acquire()
                self.cond.wait(0.05)
            still = self.running()
        if still:
            self.note(turn_end={'stopReason': 'cancelled',
                                'error': 'The agent did not finish cancelling within %gs; its process was stopped.' % fl.cancel_timeout})
            self.conn.kill()
        self.stopping = False

    def _watch(self, turn):
        """§13: a prompt that writes nothing for inactivity_timeout fails and its process is replaced; open permission
        requests and tools pause the clock."""
        limit = self.p.flavor.inactivity_timeout
        step = max(0.05, min(5.0, limit / 10))
        while self.turn_no == turn and self.running() and not self.released:
            time.sleep(step)
            if self.pending or any(s['status'] in OPEN for s in self.h.tools.values()):
                self.last_output = time.time()
            if time.time() - self.last_output > limit and self.turn_no == turn and self.running():
                self.note(turn_end={'stopReason': 'error', 'error': 'The agent wrote nothing for %gs; its process was stopped.' % limit})
                self.conn.kill()
                return

    def wait_idle(self, timeout):
        end = time.time() + timeout
        with self.lock:
            while self.running() and time.time() < end:
                self.cond.wait(0.05)
            return not self.running()

    # ---- options (§8) ----
    def set_options(self, model=None, effort=None, mode=None):
        """Apply a choice only when the session advertises it; the rest come back as skipped [{what, value, reason}]."""
        skipped = []
        o = self.h.c['options']
        cfg = [x for x in o.get('config') or [] if isinstance(x, dict)]
        bycat = {}
        for x in cfg:
            bycat.setdefault(x.get('category'), x)
        for cat, value in (('model', model), ('thought_level', effort)):
            if value is None or value == '':
                continue
            what = 'model' if cat == 'model' else 'effort'
            if str(value).lower() == 'default':
                skipped.append({'what': what, 'value': value, 'reason': '"Default" is not a model; nothing was sent'})
                continue
            opt = bycat.get(cat)
            models = o.get('models') or {}
            try:
                if opt and value in [v for v, _, _ in select_values(opt)]:
                    if opt.get('currentValue') != value:
                        self.conn.call('session/set_config_option', {'sessionId': self.key, 'configId': opt['id'], 'value': value}, timeout=20)
                elif cat == 'model' and value in [m.get('modelId') for m in models.get('availableModels') or [] if isinstance(m, dict)]:
                    if models.get('currentModelId') != value:          # the removed unstable API older v1 agents still use (§8)
                        self.conn.call('session/set_model', {'sessionId': self.key, 'modelId': value}, timeout=20)
                else:
                    skipped.append({'what': what, 'value': value, 'reason': 'not offered by this session'})
            except AcpError as e:
                skipped.append({'what': what, 'value': value, 'reason': e.message})
        if mode:
            target = self.p.mode_target(mode, self.h.c['options'])
            if target is None:
                skipped.append({'what': 'mode', 'value': mode, 'reason': 'not offered by this agent'})
            elif target == AUTO:
                self.full_access, self.page_mode = True, mode
            elif target == LAUNCH:
                if mode == self.launch_mode or (self.launch_mode is None and not self.p.flavor.launch_args.get(mode)):
                    self.full_access, self.page_mode = False, mode
                else:
                    skipped.append({'what': 'mode', 'value': mode, 'reason': 'this agent takes its mode at launch; start a new session'})
            else:
                try:
                    self.set_agent_mode(target)
                    self.full_access, self.page_mode = False, mode
                except AcpError as e:
                    skipped.append({'what': 'mode', 'value': mode, 'reason': e.message})
        return skipped

    def set_agent_mode(self, target):
        o = self.h.c['options']
        opt = next((x for x in o.get('config') or [] if isinstance(x, dict) and x.get('category') == 'mode'), None)
        if target == self.h.c['mode']:
            return
        if opt:
            self.conn.call('session/set_config_option', {'sessionId': self.key, 'configId': opt['id'], 'value': target}, timeout=20)
        else:          # v1 agents that only advertise modes (gemini-cli) take session/set_mode (§8)
            self.conn.call('session/set_mode', {'sessionId': self.key, 'modeId': target}, timeout=20)

    # ---- release ----
    def release(self):
        self.released = True
        if self.conn.caps.get('close') and self.conn.alive():
            try:
                self.conn.call('session/close', {'sessionId': self.key}, timeout=5)
            except AcpError:
                pass
        self.conn.kill()
        self.conn.done.wait(3)
        with self.lock:
            if self.record is not None:
                try:
                    self.record.close()
                except OSError:
                    pass
                self.record = None


def choose(req, answer):
    """An answer as an ACP outcome; the option must be one the agent offered (its own id passed through, §5)."""
    options = req['options']
    ids = [o['optionId'] for o in options]
    if answer.get('optionId') is not None:
        if answer['optionId'] not in ids:
            raise ValueError('That option was not offered.')
        return {'outcome': 'selected', 'optionId': answer['optionId']}
    if isinstance(answer.get('answers'), dict):
        for vals in answer['answers'].values():
            for v in (vals if isinstance(vals, list) else [vals]):
                for o in options:
                    if v in (o['optionId'], o.get('name')):
                        return {'outcome': 'selected', 'optionId': o['optionId']}
        raise ValueError('That answer was not offered.')
    d = answer.get('decision')
    alias = {'accept': 'allow_once', 'approve': 'allow_once', 'acceptForSession': 'allow_always', 'decline': 'reject_once',
             'deny': 'reject_once'}
    d = alias.get(d, d)
    if d in ('cancel', 'cancelled'):
        return {'outcome': 'cancelled'}
    order = {'allow_once': ['allow_once'], 'allow_always': ['allow_always', 'allow_once'],          # T3 21e4f980c
             'reject_once': ['reject_once'], 'reject_always': ['reject_always', 'reject_once']}.get(d)
    if not order:
        raise ValueError('Unknown decision.')
    for k in order:
        for o in options:
            if o.get('kind') == k:
                return {'outcome': 'selected', 'optionId': o['optionId']}
    raise ValueError('The agent did not offer that choice.')


# ---------------------------------------------------------------- the provider ----------------------------------------------------------------
class AcpProvider(Provider):
    """One agent flavor behind the provider interface. publish(msg) receives the event-stream messages (module docstring)."""

    can_start = can_send = can_stop = can_reply = True

    def __init__(self, flavor, publish=None, data_dir=None, env=None):
        self.flavor = flavor
        self.name, self.label = flavor.name, flavor.label
        self.publish = publish
        self.data = Path(data_dir or data_root())
        self.env = env
        self.live = {}          # agent session id -> Session
        self.reqs = {}          # request id -> Session
        self.lock = threading.RLock()
        self.cache = {}         # record path -> ((size, mtime), History)
        self.version = None
        self.signed_in = None          # see available()
        self.refused = None            # {'reason', 'at'}: the agent's service refused this account outright (setup_error)
        self.refusals = planuse.Refusals()          # plan use: the agent refusing requests (its plan, quota or limit)
        self.plan_cache = planuse.Background(lambda: self.flavor.plan_reader(self))
        self.last_options = {}
        self.offer_at = 0.0             # when the lists in last_options were read from the agent (a session or a look)
        self.offer_try = 0.0            # when the last look ahead of time (look_ahead) started
        self.offer_busy = False
        self.children = {}      # subagent key -> {key, parent, cwd, role, type, ...} (Flavor.subagents)

    # ---- plumbing ----
    def emit(self, msg):
        if self.publish:
            try:
                self.publish(msg)
            except Exception:
                pass

    def record_path(self, key):
        return self.data / 'acp' / self.name / (str(key) + '.ndjson')

    def side_path(self, key):
        return self.data / 'acp' / self.name / (str(key) + '.json')

    def key_of(self, sid):
        s = str(sid)
        if not s.startswith(self.name + ':'):
            raise ValueError('Not a %s session.' % self.label)
        return s[len(self.name) + 1:]

    def index(self, rid, s):
        with self.lock:
            self.reqs[rid] = s

    def resolved(self, s, rid, lost=None):
        """lost: why nobody can answer it any more (its process went away); the request is then recorded as lost."""
        with self.lock:
            self.reqs.pop(rid, None)
        self.emit({'method': 'serverRequest/resolved', 'params': {'threadId': s.sid, 'requestId': rid,
                                                                  **({'lost': True, 'reason': 'not_resumable: ' + lost} if lost else {})}})

    def dropped(self, s):
        with self.lock:
            if self.live.get(s.key) is s:
                self.live.pop(s.key, None)

    def touch(self, s):
        """Sidecar refreshed (title, model) when a turn ends."""
        if s.key:
            self.write_side(s.key, s.cwd, s.h.c.get('title'), s.h.c.get('model'))

    def write_side(self, key, cwd, title=None, model=None, started=None):
        p = self.side_path(key)
        try:
            old = json.loads(p.read_text())
        except (OSError, ValueError):
            old = {}
        side = {**old, 'cwd': cwd, 'title': title or old.get('title'), 'started': old.get('started') or started or time.time(),
                'model': model or old.get('model'), 'flavor': self.name}
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(side))
        os.replace(tmp, p)

    def read_side(self, key):
        try:
            return json.loads(self.side_path(key).read_text())
        except (OSError, ValueError):
            return None

    def mode_target(self, mode, options):
        """A page mode (or an agent mode id) as what to set: the agent's mode id, AUTO, LAUNCH, or None when not offered."""
        fl = self.flavor
        agent = agent_modes(options)
        if mode in fl.refused_modes:
            return None
        if mode in agent:
            return mode
        target = fl.page_modes.get(mode)
        if target in (AUTO, LAUNCH):
            return target
        if target and target in agent and target not in fl.refused_modes:
            return target
        return None

    # ---- observe ----
    LOGIN_URL_WAIT = 30.0          # s the page waits for the sign-in link
    LOGIN_BOUND = 600.0            # s a sign-in may stay open before its process is ended
    CODE_WAIT = 15.0               # s a pasted code's answer is waited for before the page is told "code sent, waiting"

    def sign_in(self, wait=None):
        """Start the agent's own sign-in (Flavor.login_args) and return the link it prints, for the page to open: {url, running,
        exit, tail}. The person signs in in their browser; nothing here sees or enters a credential. One at a time per agent;
        a second call while one runs returns the same link. When it ends, publishes provider/signin {provider, ok, tail}.
        [T3 AcpSessionRuntime.ts:2165-2285 calls the agent's own method; ours runs its login command, acp.md §7]"""
        fl = self.flavor
        b = fl.find()
        if not b:
            raise ValueError('%s is not installed. %s' % (self.label, fl.install_hint))
        if not fl.login_args:
            raise ValueError(fl.auth_hint or '%s has no sign-in command; sign in with its own app.' % self.label)
        with self.lock:
            cur = getattr(self, '_login', None)
            if cur and cur['proc'].poll() is None:
                st = cur
            else:
                env = dict(os.environ, BROWSER='/bin/echo', NO_OPEN_BROWSER='1')
                # stdin is a pipe only for an agent that asks for a pasted code (sign_in_code); otherwise it reads end of input
                proc = subprocess.Popen([b] + list(fl.login_args), stdin=subprocess.PIPE if fl.login_code_prompt else subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True)
                st = self._login = {'proc': proc, 'url': None, 'tail': collections.deque(maxlen=20), 'got': threading.Event(),
                                    'needsCode': False, 'codeSent': False, 'asked': threading.Event()}
                threading.Thread(target=self._login_read, args=(st,), daemon=True).start()
        st['got'].wait(self.LOGIN_URL_WAIT if wait is None else wait)
        if fl.login_code_prompt and st['url'] and not st['needsCode']:
            st['asked'].wait(2.0)          # the code prompt follows the link by a line or two
        return self._login_state(st)

    def _login_state(self, st):
        return {'url': st['url'], 'running': st['proc'].poll() is None, 'exit': st['proc'].returncode,
                'opensBrowser': self.flavor.login_opens_browser, 'needsCode': st['needsCode'] and not st['codeSent'],
                'codeSent': bool(st['codeSent']), 'said': login_said(st['tail']), 'tail': redact('\n'.join(st['tail']))[-1500:]}

    def sign_in_code(self, code):
        """The code (or landing URL) the person pasted on the page, for a sign-in that asked for one: written to the sign-in's
        stdin, never stored or logged. Returns the sign-in's state after it has had a moment to answer."""
        st = getattr(self, '_login', None)
        code = str(code or '').strip()
        if not st or st['proc'].poll() is not None:
            raise ValueError('No %s sign-in is waiting; press sign in again.' % self.label)
        if not st['needsCode'] or st['codeSent']:
            raise ValueError('%s\'s sign-in is not asking for a code.' % self.label)
        if not code or '\n' in code or len(code) > 8192:
            raise ValueError('Paste the code (or the address of the page you landed on) as one line.')
        try:
            st['proc'].stdin.write((code + '\n').encode())
            st['proc'].stdin.flush()
            st['proc'].stdin.close()
        except (OSError, ValueError):
            raise ValueError('%s\'s sign-in stopped before the code arrived; press sign in again.' % self.label)
        st['codeSent'] = True
        try:
            st['proc'].wait(self.CODE_WAIT)
        except subprocess.TimeoutExpired:
            pass
        # 2026-11-22: a line that repeats the code (an agent that echoes what it was given) is hidden before anyone reads it
        for i, line in enumerate(list(st['tail'])):
            if code in line:
                st['tail'][i] = line.replace(code, '[the code]')
        return self._login_state(st)

    def _login_read(self, st):
        proc, end = st['proc'], time.time() + self.LOGIN_BOUND
        threading.Thread(target=lambda: (time.sleep(self.LOGIN_BOUND), proc.poll() is None and _kill_group(proc)), daemon=True).start()
        for raw in iter(proc.stdout.readline, b''):
            line = raw.decode('utf-8', 'replace').rstrip()
            st['tail'].append(line)
            if not st['url']:
                m = re.search(r'https://[^\s"\'<>]+', line)
                if m:
                    st['url'] = m.group(0).rstrip('.,);')
                    st['got'].set()
            if self.flavor.login_code_prompt and re.search(self.flavor.login_code_prompt, line, re.I):
                st['needsCode'] = True
                st['asked'].set()
            if time.time() > end:
                break
        code = proc.wait()
        st['got'].set(); st['asked'].set()
        if code == 0:
            self.signed_in = True
        LOOKS.pop(self.name, None)          # looked at again: a sign-in can change what the agent reports
        self.emit({'method': 'provider/signin', 'params': {'provider': self.name, 'ok': code == 0,
                                                            'tail': redact('\n'.join(st['tail']))[-600:]}})

    STATUS_EVERY = 60.0            # s between looks with the agent's status command

    def sign_in_status(self, b):
        """What is known of the agent's version and sign-in, without blocking (speed, 2026-11-21: a page answer never waits on a
        command): `--version` once, and the status command (Flavor.status_args) at most once a minute, both on one background
        thread with timeouts; their answers show from the next look. Kept per agent for the process (LOOKS), so every provider
        object made for the same agent (tests make many) shares one look."""
        fl = self.flavor
        lk = LOOKS.setdefault(self.name, {'at': 0.0, 'version': None, 'signed': None, 'busy': False, 'binary': None})
        if lk['binary'] != b:          # another binary (installed or moved since): looked at afresh
            lk.update(at=0.0, version=None, signed=None, binary=b)
        want_status = bool(fl.status_args) and time.time() - lk['at'] > self.STATUS_EVERY
        if b and not lk['busy'] and (want_status or lk['version'] is None):
            lk['busy'] = True
            if want_status:
                lk['at'] = time.time()
            env = dict(os.environ, BROWSER='/bin/echo', NO_OPEN_BROWSER='1', PATH=os.path.dirname(b) + os.pathsep + os.environ.get('PATH', ''))

            def look():
                try:
                    if lk['version'] is None:
                        try:
                            r = subprocess.run([b, '--version'], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL, env=env)
                            lk['version'] = (r.stdout.strip().splitlines() or [''])[0][:80]
                        except (OSError, subprocess.SubprocessError):
                            lk['version'] = ''
                    if want_status:
                        try:
                            r = subprocess.run([b] + list(fl.status_args), capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL, env=env)
                            lk['signed'] = bool(re.search(fl.status_ok, (r.stdout or '') + (r.stderr or ''), re.I))
                            self.signed_in = None          # the status command's answer is the newer word
                        except (OSError, subprocess.SubprocessError):
                            pass          # unknown stays as it was
                finally:
                    lk['busy'] = False
            threading.Thread(target=look, name='agent-ide-%s-look' % self.name, daemon=True).start()
        self.version = lk['version']
        return self.signed_in if self.signed_in is not None else lk['signed']

    def available(self):
        """Never runs a command itself (sign_in_status looks in the background)."""
        b = self.flavor.find()
        out = {'name': self.name, 'label': self.label, 'found': bool(b), 'store': str(self.data / 'acp' / self.name), 'binary': b, **self.flags(),
               'canSignIn': bool(b and self.flavor.login_args),
               # True once a session started here, a sign-in finished or the status command says so; False after a start refused
               # for sign-in or a status saying not; None unknown
               'signedIn': self.sign_in_status(b)}
        if not b:
            out.update(found=False, reason='not installed', hint=self.flavor.install_hint, canStart=False, canSend=False)
            return out
        out['version'] = self.version or None
        if self.refusal():
            out.update(canStart=False, reason='refused: ' + self.refused['reason'])
        return out

    plan_reads_on_look = True          # usage.collect passes look: the reader runs only while someone looks (planuse.Background)

    def plan_usage(self, look=False):
        """The agent's plan figures (Flavor.plan_reader, read in the background while someone looks) with its refusal state and how
        old the figures are; a note says why there are none (providers/planuse.py)."""
        if self.flavor.plan_reader:
            r = self.plan_cache.get(look)
            rd = self.plan_cache.state()
            if r:
                r = dict(r)
            elif rd['busy']:
                r = planuse.reading('reading %s\'s plan figures…' % self.label)
            else:
                r = planuse.reading('not read yet: read when Plan use is open')
            r['read'] = rd if rd['at'] else ({'at': None, 'busy': True} if rd['busy'] else None)
        else:
            r = planuse.reading("%s doesn't report its plan use to other apps" % self.label)
        if self.refusal() and not r['windows']:
            r['note'] = 'refused: ' + self.refused['reason']
        r['state'] = self.refusals.current()
        r.pop('retry', None)
        return r

    def refusal(self):
        r = self.refused
        return bool(r and time.time() - r['at'] < self.REFUSAL_HOLD)

    def owns(self, sid):
        s = str(sid)          # an agent's subagent is shown, never driven: its parent drives it (acp.md §14)
        return s.startswith(self.name + ':') and s[len(self.name) + 1:] not in self.children

    def list_sessions(self, project_root=None):
        """The sessions this server drove (our records). Sessions started in the agent's own app or CLI are not listed: out
        of scope for now (ACP's session/list exists on some agents and would be the way in)."""
        d = self.data / 'acp' / self.name
        out = []
        for side in sorted(d.glob('*.json')) if d.is_dir() else []:
            key = side.stem
            try:
                s = json.loads(side.read_text())
            except (OSError, ValueError):
                continue
            cwd = s.get('cwd')
            if project_root and not under(cwd, project_root):
                continue
            rec = self.record_path(key)
            try:
                updated = rec.stat().st_mtime
            except OSError:
                updated = s.get('started')
            live = self.live.get(key)
            out.append({'id': '%s:%s' % (self.name, key), 'provider': self.name, 'title': s.get('title') or self.label + ' session',
                        'cwd': cwd, 'started': s.get('started'), 'updated': updated, 'running': bool(live and live.running()),
                        'parent': None, 'external': False, 'model': s.get('model')})
            out += self.child_rows(key, cwd, s.get('started'))
        return out

    def child_rows(self, key, cwd, started):
        """The subagents a session of ours started (Flavor.subagents), as read-only rows under it."""
        find = self.flavor.subagents
        if not find:
            return []
        try:
            kids = find(key, since=started) or []
        except Exception:          # the agent's own files changed shape: no children rather than no sessions
            return []
        rows = []
        for k in kids:
            self.children[k['key']] = {**k, 'parent': key, 'cwd': cwd}
            title = ' · '.join(x for x in (k.get('role'), k.get('type')) if x) or 'subagent'
            rows.append({'id': '%s:%s' % (self.name, k['key']), 'provider': self.name, 'title': title, 'cwd': cwd,
                         'started': k.get('started'), 'updated': k.get('updated') or k.get('started'), 'running': bool(k.get('running')),
                         'parent': '%s:%s' % (self.name, key), 'external': True, 'model': None, 'subagent': True})
        return rows

    def history_of(self, key):
        s = self.live.get(key)
        if s is not None:
            return s.h, True
        path = self.record_path(key)
        try:
            st = path.stat()
        except OSError:
            return History(), False
        stamp = (st.st_size, st.st_mtime_ns)
        hit = self.cache.get(str(path))
        if hit and hit[0] == stamp:
            return hit[1], False
        side = self.read_side(key) or {}
        h = replay(path, side.get('cwd'))
        self.cache[str(path)] = (stamp, h)
        if len(self.cache) > 100:
            self.cache.pop(next(iter(self.cache)))
        return h, False

    def read_history(self, sid):
        key = self.key_of(sid)
        if key in self.children and self.flavor.subagent_history:          # a subagent: read from the agent's own files
            kid = self.children[key]
            got = self.flavor.subagent_history(key) or {'messages': [], 'reasoning': [], 'running': False}
            out = History(kid.get('cwd')).snapshot(live=False)
            out.update(messages=got['messages'], reasoning=got['reasoning'], turnActive=bool(got['running']),
                       parent='%s:%s' % (self.name, kid['parent']), title=' · '.join(x for x in (kid.get('role'), kid.get('type')) if x))
            return out
        h, live = self.history_of(key)
        s = self.live.get(key)
        if s is not None:
            with s.lock:
                return h.snapshot(live=True)
        return h.snapshot(live=False)

    def capabilities(self, sid=None):
        opts = self.options(sid) if sid else self.last_options
        ctx = 'none'
        if sid:
            c = self.history_of(self.key_of(sid))[0].c
            ctx = 'window' if c.get('window') else 'tokens' if c.get('context') else 'none'
        found = bool(self.flavor.find())
        return core.capabilities(start=found and not self.refusal(), send=found, stop=True, reply=True, models=bool(opts.get('models')), effort=bool(opts.get('efforts')),
                                 modes=bool(opts.get('modes')), questions=bool(self.flavor.question_prefix), context=ctx, release=True)

    def options(self, sid=None):
        if sid is None:          # a new agent's window: what this agent offers, kept from its last session or look (offer_path)
            if not self.last_options:
                self.read_offer()
            if not self.last_options:          # an older install: the newest record of a session [ours]
                d = self.data / 'acp' / self.name
                for side in sorted(d.glob('*.json'), key=lambda f: f.stat().st_mtime, reverse=True)[:5] if d.is_dir() else []:
                    try:
                        o = self.options(self.name + ':' + side.stem)
                    except Exception:          # an unreadable record: try the next
                        continue
                    if o.get('models') or o.get('efforts'):
                        break
            self.look_ahead()
            out = dict(self.last_options)
            if self.offer_busy and not out.get('models'):
                out['reading'] = True
            return out
        key = self.key_of(sid)
        h, _ = self.history_of(key)
        s = self.live.get(key)
        out = self.shape(h.c['options'], {'model': h.c.get('model') or (self.read_side(key) or {}).get('model'), 'effort': h.c.get('effort'),
                                          'mode': (s.page_mode if s and s.page_mode else h.c.get('mode'))})
        if out['models'] or out['efforts']:
            self.remember_offer(out)
        elif self.last_options or self.read_offer():          # a record without the agent's lists (it never said them, or they were
            # cut): the harness's own lists, with this session's choices; a choice it does not take is said as skipped when sent
            lo = self.last_options
            out = {**out, 'models': lo.get('models') or [], 'efforts': lo.get('efforts') or [],
                   'modes': out['modes'] if any(not m.get('ours') and 'maps' not in m for m in out['modes']) else lo.get('modes') or out['modes'],
                   'fromHarness': True}
        return out

    def shape(self, o, current):
        """The agent's advertised options (configOptions / models / modes) in the page's shape."""
        cfg = {x.get('category'): x for x in o.get('config') or [] if isinstance(x, dict) and x.get('category')}
        if cfg.get('model'):
            models = [{'id': v, 'label': n} for v, n, _ in select_values(cfg['model'])]
        else:
            models = [{'id': m.get('modelId'), 'label': m.get('name') or m.get('modelId')} for m in (o.get('models') or {}).get('availableModels') or [] if isinstance(m, dict)]
        efforts = [{'id': v, 'label': n} for v, n, _ in select_values(cfg['thought_level'])] if cfg.get('thought_level') else []
        modes = [{'id': i, 'label': n, 'description': d} for i, (n, d) in agent_modes(o).items() if i not in self.flavor.refused_modes]
        for page, label in (('read-only', 'Read Only'), ('default', 'Default'), ('full-access', 'Full Access')):
            target = self.mode_target(page, o)
            if target is None or any(m['id'] == page for m in modes):
                continue
            m = {'id': page, 'label': label}
            if target == AUTO:
                m.update(ours=True, description='Answered here: reads and commands allowed, edits allowed inside this folder; anything else asks.')
            elif target != LAUNCH:
                m['maps'] = target
            modes.append(m)
        return {'models': models, 'efforts': efforts, 'modes': modes, 'current': current}

    # ---- what the agent offers, ahead of a session (2026-11-22: a new agent's model list said only "default", greyed
    # out): ACP gives the lists only in session/new's answer, so a new agent's window had none until a session of this
    # harness had run here. The lists are kept per harness (offer_path) from every session and from a look ahead: initialize +
    # session/new in an empty folder of ours, no prompt, session/close when offered, the process ended ----
    OFFER_FRESH = 24 * 3600.0          # s the kept lists count as current before another look [judgement]
    OFFER_RETRY = 600.0                # s between looks that failed (not signed in, no network) [judgement]

    def offer_path(self):
        return self.data / 'acp-offers' / (self.name + '.json')

    def read_offer(self):
        try:
            got = json.loads(self.offer_path().read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(got, dict) or not isinstance(got.get('offer'), dict):
            return False
        self.last_options, self.offer_at = got['offer'], float(got.get('at') or 0)
        return bool(self.last_options.get('models') or self.last_options.get('efforts'))

    def remember_offer(self, out, current=None, at=None):
        """Lists the agent just said are kept for a new agent's window; its default (current) only from a look ahead, since a
        session's current choices may be the person's, not the agent's default."""
        keep = {'models': out['models'], 'efforts': out['efforts'], 'modes': out['modes'],
                'current': current if current is not None else (self.last_options.get('current') or out.get('current') or {})}
        changed = {k: keep[k] for k in ('models', 'efforts', 'modes')} != {k: self.last_options.get(k) for k in ('models', 'efforts', 'modes')}
        self.last_options = keep
        if at is not None or changed or time.time() - self.offer_at > 60:
            self.offer_at = at or time.time()
            try:
                p = self.offer_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix('.json.tmp')
                tmp.write_text(json.dumps({'at': self.offer_at, 'offer': keep, 'version': self.version}))
                os.replace(tmp, p)
            except OSError:
                pass
        return keep

    def look_ahead(self, wait=False):
        """Read the agent's lists in the background when none are kept or they are older than OFFER_FRESH. Never more than one
        look at a time, never sooner than OFFER_RETRY after the last one; not while the agent is missing or refused us."""
        now = time.time()
        known = self.last_options.get('models') or self.last_options.get('efforts')
        if self.offer_busy or (known and now - self.offer_at < self.OFFER_FRESH) or now - self.offer_try < self.OFFER_RETRY:
            return None
        if os.environ.get('AGENT_IDE_NO_LOOK_AHEAD') or not self.flavor.find() or self.refusal():
            return None
        self.offer_busy, self.offer_try = True, now
        t = threading.Thread(target=self._look, daemon=True, name='acp-look-' + self.name)
        t.start()
        if wait:
            t.join(120)
        return t

    def _look(self):
        conn = None
        try:
            b = self.flavor.find()
            folder = self.data / 'acp-look' / self.name          # an empty folder of ours: the agent is shown no project of the person's
            folder.mkdir(parents=True, exist_ok=True)
            conn = Connection(self.flavor.argv(b), folder, self.flavor, env=self.env)
            conn.start()
            conn.initialize()
            r = conn.call('session/new', {'cwd': str(folder), 'mcpServers': []}, timeout=90)
            key = r.get('sessionId')
            if key and conn.caps.get('close'):
                try:
                    conn.call('session/close', {'sessionId': key}, timeout=10)
                except AcpError:
                    pass
            if key:
                self.signed_in, self.refused = True, None
            h = History(str(folder))
            h.setup(r)
            out = self.shape(h.c['options'], {'model': h.c.get('model') or (self.read_side(key) or {}).get('model'), 'effort': h.c.get('effort'), 'mode': h.c.get('mode')})
            if out['models'] or out['efforts']:
                self.remember_offer(out, current=out['current'], at=time.time())
        except AcpError as e:
            if conn is not None:
                self.setup_error(conn, e)          # a sign-in or refusal says so on the page, as a start would
        except Exception:          # the agent missing or failing to start: the window keeps the harness's default
            pass
        finally:
            if conn is not None:
                conn.kill()
            self.offer_busy = False
            self.emit({'method': 'provider/options', 'params': {'provider': self.name}})

    # ---- drive ----
    def _connect(self, cwd, key=None, history=None, mode=None):
        b = self.flavor.find()
        if not b:
            raise ValueError('%s is not installed. %s' % (self.label, self.flavor.install_hint))
        launch = mode if mode in self.flavor.launch_args else None
        conn = Connection(self.flavor.argv(b, launch), cwd, self.flavor, env=self.env)
        s = Session(self, conn, cwd, key=key, history=history, launch_mode=launch)
        conn.sink = s
        try:
            conn.start()
        except OSError as e:
            raise ValueError('%s could not be started: %s' % (self.label, e))
        try:
            conn.initialize()
        except AcpError as e:
            conn.kill()
            raise ValueError('%s did not start: %s%s' % (self.label, e.message, self.stderr_hint(conn)))
        return s

    def stderr_hint(self, conn):
        tailtxt = conn.stderr_tail.strip().splitlines()
        return (' (' + tailtxt[-1][:300] + ')') if tailtxt else ''

    REFUSAL_HOLD = 3600.0          # s a refused account is not offered for a new agent (a retry after that may find it fixed)

    def setup_error(self, conn, e):
        msg = '%s: %s' % (self.label, e.message)
        text = str(e.message or '').lower()
        # an agent that parsed a web page as data (its service answered with HTML: a retired or blocked endpoint) says
        # "Unexpected token '<', "<!DOCTYPE "... is not valid JSON"; the page shows what that means instead [ours, 2026-11-09]
        if '<!doctype' in text or '<html' in text or re.search(r"unexpected token '?<", text):
            msg = ('%s: its service answered with a web page instead of data, so the agent could not read it (usually a '
                   'retired or blocked sign-in or endpoint). Run it in a terminal to see the full error.' % self.label)
        # Gemini CLI 0.66, seen 2026-11-09: "This client is no longer supported for Gemini Code Assist for individuals ...
        # migrate to the Antigravity suite": every later start fails the same way after ~18 s, so it is not offered for a while
        if re.search(r'no longer supported|please migrate|has been (retired|discontinued)', text):
            self.refused = {'reason': str(e.message or '')[:300], 'at': time.time()}
        elif planuse.is_refusal(text):          # plan use: a start refused for the plan or a limit is the agent's state too
            self.refusals.turn_ended(str(e.message or ''))
        # -32000 is the spec's "authentication required", but agents also use it to refuse an account outright (Gemini CLI
        # 0.66: "no longer supported ... migrate"): the sign-in hint only when the message is about signing in or says nothing
        if conn.signin_url or re.search(r'auth|sign.?in|log.?in|credential|api key', text) or (e.code == -32000 and len(text.strip()) < 12):
            msg += ' ' + self.flavor.auth_hint
            self.signed_in = False
        return ValueError(msg)

    def start(self, cwd, model=None, effort=None, mode=None):
        cwd = str(cwd)
        s = self._connect(cwd, mode=mode)
        try:
            r = s.conn.call('session/new', {'cwd': cwd, 'mcpServers': []}, timeout=90)
        except AcpError as e:
            s.conn.kill()
            raise self.setup_error(s.conn, e)
        key = r.get('sessionId')
        if key:
            self.signed_in = True          # the agent accepted a session: whatever account it holds works
            self.refused = None
        if not key:
            s.conn.kill()
            raise ValueError('%s returned no session id.' % self.label)
        s.attach(key)
        with self.lock:
            self.live[key] = s
        self.write_side(key, cwd, started=time.time(), model=s.h.c.get('model'))
        skipped = s.set_options(model, effort, mode)
        s.starting = False
        self.options(s.sid)
        return {'threadId': s.sid, 'thread': {'id': s.sid, 'cwd': cwd, 'harness': self.name}, 'model': s.h.c.get('model'), 'skipped': skipped}

    def resume(self, key):
        """A session from our record, reopened in a new process: session/resume when offered (no replay), else session/load
        with its replay dropped because our record holds the conversation (§6)."""
        side = self.read_side(key)
        if not side or not side.get('cwd'):
            raise ValueError('This session\'s folder is not known.')
        hist = replay(self.record_path(key), side['cwd'])
        s = self._connect(side['cwd'], key=key, history=hist, mode=None)
        caps, fl = s.conn.caps, self.flavor
        params = {'sessionId': key, 'cwd': side['cwd'], 'mcpServers': []}
        try:
            if caps.get('resume') and (fl.prefer_resume or not caps.get('load')):
                s.conn.call('session/resume', params, timeout=90)
            elif caps.get('load'):
                self._load(s, params)
            else:
                raise AcpError(NOT_FOUND, 'This agent cannot reopen a session (no session/resume or session/load).')
        except AcpError as e:
            s.conn.kill()
            raise self.setup_error(s.conn, e)
        with self.lock:
            self.live[key] = s
        s.starting = False
        return s

    def _load(self, s, params):
        """session/load: done on its response, or once replay traffic has been quiet for load_idle, at most load_bound (§6,
        T3 52b04b947)."""
        fl = self.flavor
        rid, slot = s.conn.request('session/load', params)
        start = time.time()
        while True:
            if slot['event'].wait(0.05):
                if slot.get('error'):
                    e = slot['error']
                    raise AcpError(e.get('code'), str(e.get('message')))
                return
            now = time.time()
            quiet = now - max(s.h.c['last'] or start, start)
            if quiet >= fl.load_idle or now - start >= fl.load_bound:
                s.note(loaded=True)
                return

    def session(self, sid):
        key = self.key_of(sid)
        with self.lock:
            s = self.live.get(key)
        return s, key

    def send(self, sid, text, how='start', model=None, effort=None, mode=None):
        if how in ('steer', 'native_queue'):
            raise ValueError('%s cannot take a message mid-turn; it waits in the queue.' % self.label)
        s, key = self.session(sid)
        result = 'sent'
        if s is None or not s.conn.alive():
            s = self.resume(key)
            result = 'resumed'
        skipped = s.set_options(model, effort, mode) if (model or effort or mode) else []
        s.prompt(text)
        return {'threadId': s.sid, 'how': result, 'skipped': skipped}

    def stop(self, sid, wait=False):
        s, _ = self.session(sid)
        if s is None:
            raise ValueError('This agent is not running here.')
        return s.stop(wait=wait)

    def reply(self, request_id, answer):
        with self.lock:
            s = self.reqs.get(str(request_id))
        if s is None:
            raise ValueError('This request has already been resolved.')
        return s.reply(str(request_id), answer)

    def requests(self):
        out = []
        for s in list(self.live.values()):
            with s.lock:
                out += [r['msg'] for r in s.pending.values()]
        return out

    def phase(self, sid):
        s, _ = self.session(sid)
        return s.phase() if s else 'idle'

    def release(self, sid):
        s, key = self.session(sid)
        if s is None:
            return {}
        s.release()
        with self.lock:
            self.live.pop(key, None)
        return {}

    def close(self):
        for key in list(self.live):
            try:
                self.release('%s:%s' % (self.name, key))
            except Exception:
                pass


def agent_modes(options):
    """The agent's own modes {id: (name, description)}: a mode config option, else the modes field."""
    out = {}
    cfg = next((x for x in options.get('config') or [] if isinstance(x, dict) and x.get('category') == 'mode'), None)
    if cfg:
        for v, n, d in select_values(cfg):
            out[v] = (n, d)
    else:
        for m in (options.get('modes') or {}).get('availableModes') or []:
            if isinstance(m, dict) and m.get('id'):
                out[m['id']] = (m.get('name') or m['id'], m.get('description'))
    return out


def providers(publish=None, data_dir=None):
    """One AcpProvider per known flavor."""
    return [AcpProvider(f, publish=publish, data_dir=data_dir) for f in FLAVORS]
