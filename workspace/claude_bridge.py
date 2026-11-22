"""Driving Claude Code sessions from the page (branch ide-claudedrive, 2026-11-06). Stdlib only, like server.py.

server.py talks to claude_worker.py (the Python Agent SDK, in the venv claude_bridge_setup.sh makes) over JSON lines on
stdio: the same shape as the Codex app-server bridge. One SDK client, so one official Claude Code process, per driven
session. What the page sees is shaped like Codex's: approval and question requests carry Codex's method names so the
page's existing cards answer them; the column itself grows from the transcript the Claude Code process writes, which
providers/claude_code.py already tails.

Credentials: nothing here reads, stores or forwards a credential or token. The official binary signs itself in (the
person's Claude subscription). Variables that would send it down the API-key route are dropped from the worker's
environment unless AGENT_IDE_CLAUDE_API_KEY=pass is set (off by default), and then they are passed through untouched.

Two writers corrupt a transcript, so a session is resumed here only when no other live process holds it (its
~/.claude/sessions/<pid>.json registration names a live pid that is not one of ours). When one does, a send forks it only
when the request says fork; without that it answers how "choice" and starts nothing, so the page can ask first (2026-11-06:
a silent fork made an unlisted copy of a desktop-app chat on Opus).
"""
import collections
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid as uuidlib

ROOT = Path(__file__).resolve().parent
VENV = ROOT / '.claude-bridge' / 'venv'
WORKER = ROOT / 'claude_worker.py'
SETUP = 'zsh ' + shlex.quote(str(ROOT / 'claude_bridge_setup.sh'))          # found beside this file, from any folder
PREFIX = 'claude:'
# variables that switch Claude Code to an API key or another account; dropped unless the API-key route is on
KEY_VARS = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX')
WRITE_TOOLS = {'Write', 'Edit', 'MultiEdit', 'NotebookEdit'}
# the permission modes offered on the page (Claude Code's own names); bypassPermissions and dontAsk are left out on purpose
PRESETS = [
    {'id': 'cc-default', 'label': 'Ask first', 'approvalPolicy': 'default', 'sandbox': None, 'default': True,
     'description': 'Claude Code asks before it edits files or runs commands (permission mode "default").'},
    {'id': 'cc-acceptEdits', 'label': 'Accept edits', 'approvalPolicy': 'acceptEdits', 'sandbox': None,
     'description': 'File edits in the working folder go ahead; commands still ask (permission mode "acceptEdits").'},
    {'id': 'cc-plan', 'label': 'Plan', 'approvalPolicy': 'plan', 'sandbox': None,
     'description': 'Claude Code reads and plans, and asks before leaving plan mode (permission mode "plan").'},
    {'id': 'cc-auto', 'label': 'Auto', 'approvalPolicy': 'auto', 'sandbox': None,
     'description': 'Claude Code decides with its own safety checks, as the desktop app\'s Auto mode (permission mode "auto").'},
]
MODES = {p['approvalPolicy'] for p in PRESETS}
COMMANDS = [
    {'name': 'status', 'description': 'show the session\'s model, permission mode and context', 'how': 'local', 'method': 'GET /api/status'},
    {'name': 'diff', 'description': 'show git diff (including untracked files)', 'how': 'local', 'method': 'GET /api/diff'},
    {'name': 'compact', 'description': 'summarise the conversation (sent to Claude Code as /compact)', 'how': 'text'},
    {'name': 'archive', 'description': 'archive this session', 'how': 'local', 'method': 'a mark kept by this page (a visual cue only); nothing is sent to Claude Code'},
    {'name': 'desktop', 'description': 'hand this session to the Claude desktop app (it appears in its Code list)', 'how': 'api', 'method': 'claude://resume?session=<id>'},
]


def desktop_cli(app_support=None):
    """The newest Claude Code binary the desktop app ships (the one its own sessions run), else None."""
    base = Path(app_support or Path.home() / 'Library/Application Support/Claude')
    found = sorted(base.glob('claude-code/*/*/claude.app/Contents/MacOS/claude'), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(found[0]) if found else None



def worker_python(venv=None, here=None):
    """The Python the worker runs in: the bridge venv claude_bridge_setup.sh makes when it exists, else this server's own
    Python when the Agent SDK is installed in it (the desktop app runs the server with `uv run` from pyproject.toml, which
    includes the SDK), else None (Claude Code sessions stay watch-only)."""
    v = Path(venv or VENV) / 'bin' / 'python'
    if v.is_file():
        return str(v)
    if here is None:
        import importlib.util
        here = importlib.util.find_spec('claude_agent_sdk') is not None
    return sys.executable if here and sys.executable else None

def worker_env(environ=None):
    """The worker's environment: this server's, minus the API-key route unless AGENT_IDE_CLAUDE_API_KEY=pass. Values are
    copied as they are and never looked at."""
    env = dict(os.environ if environ is None else environ)
    if env.get('AGENT_IDE_CLAUDE_API_KEY') != 'pass':
        for k in KEY_VARS:
            env.pop(k, None)
    env.pop('CLAUDECODE', None)          # a server started from inside Claude Code must not make the child think it is nested
    return env


def summary(name, inp, cwd):
    """A permission request in words, as the page's card shows it."""
    inp = inp if isinstance(inp, dict) else {}

    def rel(p):
        p = str(p or '')
        root = str(cwd or '').rstrip('/') + '/'
        return p[len(root):] if root != '/' and p.startswith(root) else p
    if name == 'Write':
        n = len(str(inp.get('content') or '').splitlines())
        return 'write ' + rel(inp.get('file_path')) + ' (%d line%s)' % (n, '' if n == 1 else 's')
    if name in ('Edit', 'MultiEdit'):
        edits = inp.get('edits') if name == 'MultiEdit' else [inp]
        plus = sum(len(str(e.get('new_string') or '').splitlines()) for e in edits or [] if isinstance(e, dict))
        minus = sum(len(str(e.get('old_string') or '').splitlines()) for e in edits or [] if isinstance(e, dict))
        return 'edit ' + rel(inp.get('file_path')) + ' (+%d −%d)' % (plus, minus)
    if name == 'NotebookEdit':
        return 'edit ' + rel(inp.get('notebook_path'))
    if name == 'Bash':
        return str(inp.get('command') or '')
    if name == 'ExitPlanMode':
        plan = str(inp.get('plan') or '').strip().splitlines()
        return 'leave plan mode' + (': ' + plan[0][:160] if plan else '')
    if name.startswith('mcp__'):
        server, _, tool = name[5:].partition('__')
        return 'use ' + server + '.' + tool
    for k in ('file_path', 'path', 'url', 'query', 'pattern', 'description'):
        if inp.get(k):
            return name + ' ' + rel(inp[k]) if k in ('file_path', 'path') else name + ' ' + str(inp[k])[:200]
    return name


def as_request(p, cwd):
    """A worker permission request as the page's request shape (Codex's method names, so its cards answer it)."""
    name, inp, key = p.get('toolName') or '', p.get('input') or {}, p.get('key')
    base = {'threadId': PREFIX + key, 'itemId': p.get('toolUseId'), 'cwd': cwd,
            'claude': {'tool': name, 'summary': summary(name, inp, cwd), 'agentId': p.get('agentId')}}
    if name == 'AskUserQuestion':
        qs = []
        for i, q in enumerate(inp.get('questions') or []):
            if isinstance(q, dict):
                qs.append({'id': 'q%d' % i, 'header': q.get('header'), 'question': q.get('question') or '', 'multiSelect': bool(q.get('multiSelect')), 'isOther': True,          # questions: Claude Code always takes an answer of your own
                           'options': [{'label': o.get('label', ''), 'description': o.get('description', '')} for o in q.get('options') or [] if isinstance(o, dict)]})
        return {'id': p['requestId'], 'method': 'item/tool/requestUserInput', 'params': {**base, 'questions': qs}}
    reason = p.get('reason') or p.get('description') or None
    # piece B: Claude Code's own suggestions for "don't ask again" (a rule for this project, a mode for the session), passed back
    # unchanged when the person takes them; "acceptForSession" is offered only when Claude Code made some (the page's words say
    # what they allow, from the rules themselves)
    sugs = [s for s in p.get('suggestions') or [] if isinstance(s, dict) and s.get('type')]
    base['claude']['suggestions'] = sugs
    avail = ['accept'] + (['acceptForSession'] if sugs else []) + ['decline', 'cancel']
    if name == 'Bash':
        return {'id': p['requestId'], 'method': 'item/commandExecution/requestApproval',
                'params': {**base, 'command': inp.get('command') or '', 'reason': inp.get('description') or reason, 'availableDecisions': avail}}
    if name in WRITE_TOOLS:
        return {'id': p['requestId'], 'method': 'item/fileChange/requestApproval',
                'params': {**base, 'reason': base['claude']['summary'], 'changes': changes_of(name, inp, cwd), 'availableDecisions': avail}}
    return {'id': p['requestId'], 'method': 'item/claude/requestApproval',      # any other tool: the page's generic approve card
            'params': {**base, 'command': base['claude']['summary'], 'reason': reason, 'availableDecisions': avail}}


# piece B: at most this many lines of an edit go to the page with its request; the rest are counted ("N more lines not sent").
# Naive (builder's number, 2026-11-18): enough for an ordinary edit, small enough that a whole-file write cannot flood the stream.
CHANGE_LINES_MAX = 400


def changes_of(name, inp, cwd):
    """piece B: the change an Edit / MultiEdit / Write would make, in Codex's fileChange shape ([{path, kind, diff}]), so the
    page shows the lines it takes out ("-") and puts in ("+"). MultiEdit's edits are separated by "@@" lines, as hunks are.
    A Write over a file that exists is all "+" with kind "replace" (the page says the whole file is written anew). Bounded at
    CHANGE_LINES_MAX lines; "more" counts the lines not sent. None for a tool without a file."""
    inp = inp if isinstance(inp, dict) else {}
    path = inp.get('file_path') or inp.get('notebook_path')
    if not path or name == 'NotebookEdit':
        return None
    root = str(cwd or '').rstrip('/') + '/'
    rel = str(path)[len(root):] if root != '/' and str(path).startswith(root) else str(path)
    if name == 'Write':
        lines = ['+' + x for x in str(inp.get('content') or '').split('\n')]
        kind = 'replace' if os.path.isabs(str(path)) and os.path.exists(str(path)) else 'add'
    else:
        edits = inp.get('edits') if name == 'MultiEdit' else [inp]
        lines = []
        for e in edits or []:
            if not isinstance(e, dict):
                continue
            if lines:
                lines.append('@@')
            lines += ['-' + x for x in str(e.get('old_string') or '').split('\n')] if e.get('old_string') else []
            lines += ['+' + x for x in str(e.get('new_string') or '').split('\n')] if e.get('new_string') else []
        kind = 'update'
    more = max(0, len(lines) - CHANGE_LINES_MAX)
    return [{'path': rel, 'kind': {'type': kind}, 'diff': '\n'.join(lines[:CHANGE_LINES_MAX]), 'more': more}]


def answer_of(req, result):
    """The page's reply ({decision} or {answers}) as the worker's permission answer."""
    if not isinstance(result, dict):
        raise ValueError('A reply needs a result object')
    p = req['params']
    if req['method'] == 'item/tool/requestUserInput':
        answers = result.get('answers')
        if not isinstance(answers, dict):
            raise ValueError('Answers are required')
        asked = {q['id']: q for q in p.get('questions') or []}
        for qid, a in answers.items():
            if qid not in asked or not isinstance(a, dict) or not isinstance(a.get('answers'), list) or not all(isinstance(x, str) for x in a['answers']):
                raise ValueError('Invalid answer for question ' + str(qid))
        if set(asked) - set(answers):
            raise ValueError('Every question needs an answer')
        # AskUserQuestion takes the answers back in its own input: {questions, answers: {<question text>: <answer>}}
        given = {asked[qid]['question']: ', '.join(a['answers']) for qid, a in answers.items()}
        return {'allow': True, 'updatedInput': {**(p.get('original') or {}), 'answers': given}}
    d = result.get('decision')
    if not isinstance(d, str) or d not in (p.get('availableDecisions') or ('accept', 'decline', 'cancel')):
        raise ValueError('Invalid approval decision')
    if d == 'accept':
        return {'allow': True}
    if d == 'acceptForSession':          # piece B: allow, and take Claude Code's own suggested rules or mode back, unchanged
        return {'allow': True, 'updatedPermissions': (p.get('claude') or {}).get('suggestions') or []}
    why = reason_of(result)          # piece B: the line written with a no travels in the deny itself (its message is what the agent reads)
    said = ' They said: ' + why if why else ''
    return {'allow': False, 'interrupt': d == 'cancel', 'message': ('The user declined this from the agent workspace.' if d == 'decline' else 'The user cancelled this turn from the agent workspace.') + said}


def reason_of(result):
    """piece B: the line a person wrote with a no (or None): one paragraph, at most 2000 characters."""
    r = result.get('message') if isinstance(result, dict) else None
    if not isinstance(r, str) or not r.strip():
        return None
    return ' '.join(r.split())[:2000]


def open_url(url):
    """Hands a session to the Claude app (`open claude://...`): a window on the person's screen, so never under a mock or
    AGENT_IDE_NO_WINDOWS=1 (drive runs and live checks)."""
    if os.environ.get('AGENT_IDE_MOCK') or os.environ.get('AGENT_IDE_NO_WINDOWS'):
        raise ValueError('Not opened: this server puts no windows on the screen (a mock or AGENT_IDE_NO_WINDOWS).')
    subprocess.run(['open', url], check=True, capture_output=True, timeout=20)


class ClaudeBridge:
    """The server's side of the worker. `publish` puts a message on the page's event stream (the Codex bridge's)."""

    def __init__(self, publish=None, provider=None, command=None, cli=None, environ=None, opener=None):
        self.publish = publish or (lambda m: None)
        self.provider = provider          # providers/claude_code.py: the live registry, to tell idle from held sessions
        self.command = command            # tests: a fake worker
        self.cli = cli if cli is not None else (os.environ.get('AGENT_IDE_CLAUDE_CLI') or desktop_cli())
        self.environ = environ
        self.opener = opener or open_url
        self.process = None
        self.lock = threading.RLock()
        self.pending, self.seq = {}, 0
        self.requests = collections.OrderedDict()          # request id -> the page's request (oldest first)
        self.originals = {}                                 # request id -> the tool input as Claude Code sent it
        self.held = {}                                      # session uuid -> what this server knows of a session it drives
        self.errors = collections.deque(maxlen=40)
        self.models_cache = None
        self.last_activity = {}
        self.closing = False          # robust (stopping): once set, no worker is started again

    # ---------- the worker ----------
    def available(self):
        if self.command:
            return {'ready': True}
        if not worker_python():
            return {'ready': False, 'reason': 'Claude Code driving is not set up: use Set up driving in the Providers panel, or run ' + SETUP + ' (watch only until then).'}
        if not self.cli:
            return {'ready': True, 'cli': 'the SDK\'s bundled Claude Code'}
        return {'ready': True, 'cli': self.cli}

    def ensure(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            if self.closing:
                raise RuntimeError('The workspace server is stopping; nothing was sent.')
            st = self.available()
            if not st['ready']:
                raise ValueError(st['reason'])
            cmd = self.command or [worker_python(), '-I', str(WORKER)]
            if self.process is not None:          # robust: never a second writer: what the last worker left must be gone first
                end_group(self.process, wait=True)
            self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                            env=worker_env(self.environ), cwd=str(ROOT), start_new_session=True)          # robust: its own process group
            threading.Thread(target=self.read, args=(self.process,), daemon=True).start()
            threading.Thread(target=self.read_errors, args=(self.process,), daemon=True).start()
            threading.Thread(target=watch_worker, args=(self.process,), daemon=True).start()          # robust: its end, even while a child holds its pipes

    def read_errors(self, proc):
        for line in proc.stderr:
            self.errors.append(line.rstrip()[:2000])

    def call(self, method, params=None, timeout=90):
        self.ensure()
        with self.lock:
            self.seq += 1
            key = self.seq
            fut = concurrent.futures.Future()
            self.pending[key] = fut
            try:
                self.process.stdin.write(json.dumps({'id': key, 'method': method, 'params': params or {}}) + '\n')
                self.process.stdin.flush()
            except (OSError, ValueError):
                self.pending.pop(key, None)
                raise RuntimeError('The Claude Code bridge stopped. Restart the workspace server.') from None
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            raise RuntimeError('Claude Code did not answer in time (' + method + ').') from None
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def read(self, proc):
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if 'id' in msg and 'method' not in msg:
                    with self.lock:
                        f = self.pending.get(msg['id'])
                    if f and not f.done():
                        if msg.get('error'):
                            f.set_exception(RuntimeError(str(msg['error'])))
                        else:
                            f.set_result(msg.get('result') or {})
                    continue
                self.take(msg.get('method'), msg.get('params') or {})
        finally:
            with self.lock:
                for f in self.pending.values():
                    if not f.done():
                        f.set_exception(RuntimeError('The Claude Code bridge stopped.'))
                for key in list(self.held):
                    self.closed(key)
            threading.Thread(target=end_group, args=(proc,), daemon=True).start()          # robust: a lost worker takes its whole group with it

    # ---------- what the worker reports ----------
    def take(self, method, p):
        key = p.get('key')
        h = self.held.get(key)
        if method == 'permission' and h is not None:
            req = as_request(p, h['cwd'])
            with self.lock:
                self.requests[req['id']] = req
                self.originals[req['id']] = p.get('input') or {}
            self.publish(req)          # carries an id: the page refreshes and shows the card
        elif method == 'message' and h is not None:
            m = p.get('message') or {}
            t, sub = m.get('_type'), m.get('subtype')
            # low latency (2026-11-14): text as it is written and tools as they start, ahead of the transcript. Only what the page
            # draws is passed on, and only the session's own: the SDK does not stream a subagent's tokens
            ev = m.get('event') or {}
            if t == 'StreamEvent' and not m.get('parent_tool_use_id') and (ev.get('type') in ('message_start', 'content_block_start', 'content_block_stop')
                                                                         or (ev.get('delta') or {}).get('type') == 'text_delta'):
                if ev.get('type') == 'message_start':
                    ev = {'type': 'message_start', 'message': {'id': (ev.get('message') or {}).get('id')}}
                self.publish({'method': 'claude/stream', 'params': {'threadId': PREFIX + key, 'event': ev}})
            if t == 'SystemMessage' and sub == 'init':
                d = m.get('data') or {}
                h['model'] = d.get('model') or h.get('model')
                h['permissionMode'] = d.get('permissionMode') or h.get('permissionMode')
            elif t == 'ResultMessage':
                h['active'] = False
                h['turns'] = h.get('turns', 0) + 1
                h['cost'] = (h.get('cost') or 0) + (m.get('total_cost_usd') or 0) if m.get('total_cost_usd') is not None else h.get('cost')
                h['lastResult'] = {'subtype': sub, 'error': bool(m.get('is_error')), 'usage': m.get('usage'), 'at': time.time()}
                self.publish({'method': 'claude/turn', 'params': {'threadId': PREFIX + key, 'ended': True, 'error': (m.get('result') if m.get('is_error') else None)}})
            elif t == 'RateLimitEvent':
                h['rate'] = m.get('rate_limit_info')
            elif t in ('AssistantMessage', 'UserMessage', 'StreamEvent') and not h.get('active') and h.get('queued') and m.get('model') != '<synthetic>':
                # a message queued mid-turn starts its own turn after the previous result: the session is running again
                # (not on the <synthetic> line Claude Code writes after a stop)
                h['active'] = True
                h['queued'] -= 1
                self.publish({'method': 'claude/turn', 'params': {'threadId': PREFIX + key, 'started': True}})
            now = time.time()
            if now - self.last_activity.get(key, 0) > 0.7 or t == 'ResultMessage':
                self.last_activity[key] = now
                self.publish({'method': 'claude/activity', 'params': {'threadId': PREFIX + key}})
        elif method == 'closed' and key:
            self.closed(key)
        elif method == 'log':
            self.errors.append((key or '') + ' ' + str(p.get('line', ''))[:500])

    def closed(self, key):
        h = self.held.get(key)
        if h:
            h['open'] = False
            h['active'] = False
            h['queued'] = 0
        with self.lock:
            gone = [rid for rid, r in self.requests.items() if r['params']['threadId'] == PREFIX + key]
            for rid in gone:
                self.requests.pop(rid, None)
                self.originals.pop(rid, None)
        for rid in gone:
            self.publish({'method': 'serverRequest/resolved', 'params': {'threadId': PREFIX + key, 'requestId': rid}})

    # ---------- what the page asks ----------
    def models(self, cwd):
        """The SDK's model list (the CLI's initialize answer), in the page's model shape."""
        if self.models_cache is None:
            r = self.call('models', {'cwd': cwd, 'cli': self.cli}, timeout=60)
            self.models_cache = r.get('models') or []
        out = []
        for m in self.models_cache:
            efforts = m.get('supportedEffortLevels') or []
            out.append({'id': m.get('value'), 'model': m.get('resolvedModel'), 'displayName': m.get('displayName'),
                        'description': ' · '.join(x for x in (m.get('displayName'), m.get('description')) if x), 'isDefault': m.get('value') == 'default',
                        'supportedReasoningEfforts': [{'reasoningEffort': e, 'description': ''} for e in efforts], 'defaultReasoningEffort': ''})
        return {'data': out}

    def alias(self, model):
        """A full model id as the page's model list names it (haiku for claude-haiku-5-5), when one alias matches."""
        for m in self.models_cache or []:
            if m.get('resolvedModel') == model and m.get('value') != 'default':
                return m.get('value')
        return model

    def model_name(self, model):
        """A model as a person reads it: the SDK list's display name (Haiku 5.5) for an alias or a full id; before that list
        is read, a full id spelled out (claude-opus-5-5 -> Opus 5.5); else as given."""
        for m in self.models_cache or []:
            if model and m.get('value') != 'default' and model in (m.get('value'), m.get('resolvedModel')):
                return m.get('displayName') or model
        g = re.fullmatch(r'claude-([a-z]+)-(\d+)-(\d+)(?:-\d{8})?', model or '')
        return g.group(1).capitalize() + ' ' + g.group(2) + '.' + g.group(3) if g else model

    @staticmethod
    def holder(reg):
        """Who holds a session, from its registration's entrypoint: app (the Claude desktop app), terminal (the claude CLI) or
        process (anything else, such as an SDK program or another of these servers)."""
        ep = (reg or {}).get('entrypoint')
        return 'app' if ep == 'claude-desktop' else 'terminal' if ep == 'cli' else 'process'

    def held_elsewhere(self, key):
        """The live registration of another process holding this session (busy or idle), else None."""
        live = (self.provider.live(fresh=True) if self.provider else {}) or {}
        reg = live.get(key)
        if not reg:
            return None
        mine = {h.get('pid') for h in self.held.values() if h.get('open')}
        return None if reg.get('pid') in mine else reg

    def open(self, key, cwd, how, source=None, model=None, effort=None, mode=None):
        params = {'key': key, 'cwd': cwd, 'cli': self.cli, 'permissionMode': mode or 'default', 'sessionId': key}
        if model:
            params['model'] = model
        if effort:
            params['effort'] = effort
        if how in ('resumed', 'forked'):
            params['resume'] = source
            params['fork'] = how == 'forked'
            if how == 'resumed':
                params.pop('sessionId')
        prev = self.held.get(key) or {}
        self.held[key] = {**prev, 'cwd': cwd, 'how': how, 'from': source, 'model': model or prev.get('model'), 'effort': effort, 'permissionMode': params['permissionMode'],
                          'open': False, 'active': False, 'created': prev.get('created') or time.time(), 'title': prev.get('title')}
        try:
            r = self.call('open', params, timeout=90)
        except Exception:
            if not prev:
                self.held.pop(key, None)
            else:
                self.held[key]['open'] = False
            raise
        self.held[key]['open'] = True
        if self.provider:
            reg = (self.provider.live(fresh=True) or {}).get(key)
            self.held[key]['pid'] = (reg or {}).get('pid')
        if r.get('models') and self.models_cache is None:
            self.models_cache = r['models']
        return self.held[key]

    def start(self, cwd, model=None, effort=None, mode=None, plan=False):
        """plan (the page's plan toggle, 2026-11-09): the session starts in permission mode "plan"; once its plan is approved it
        works under the mode chosen (`mode`, or "default" when none or "plan" itself was chosen)."""
        if mode and mode not in MODES:
            raise ValueError('Unsupported permission mode')
        key = str(uuidlib.uuid4())
        self.open(key, cwd, 'new', model=model, effort=effort, mode='plan' if plan else mode)
        self.held[key]['plan'] = bool(plan)
        if plan:
            self.held[key]['afterPlan'] = mode if mode and mode != 'plan' else 'default'
        return {'thread': {'id': PREFIX + key, 'cwd': cwd, 'harness': 'claude'}, 'model': model}

    def send(self, sid, text, cwd=None, model=None, effort=None, mode=None, fork=False, session_model=None):
        """Send to a session: through its open client here; else resume it when no other live process holds it. When one
        does: with fork, start a copy (a new session with the whole conversation, on the model chosen or else the session's
        own); without it, start nothing and answer how "choice" with who holds it and what the copy would run on.
        Returns {threadId, how: sent | queued | resumed | forked | choice}."""
        key = self.key_of(sid)
        h = self.held.get(key)
        if mode and mode not in MODES:
            raise ValueError('Unsupported permission mode')
        how = 'sent'
        if not (h and h.get('open')):
            other = self.held_elsewhere(key)
            cwd = cwd or (h or {}).get('cwd')
            if not cwd:
                raise ValueError('This session\'s folder is not known.')
            if other and not fork:
                by = self.holder(other)
                return {'threadId': PREFIX + key, 'how': 'choice', 'heldBy': by, 'model': self.model_name(model or session_model),
                        'desktopUrl': 'claude://resume?session=' + key if by == 'app' else None}
            if other:
                new = str(uuidlib.uuid4())
                self.open(new, cwd, 'forked', source=key, model=model or session_model, effort=effort, mode=mode)
                self.held[new]['original'] = (other or {}).get('name')          # a copy is named after its original (server.claude_sessions)
                key, how = new, 'forked'
            else:
                self.open(key, cwd, 'resumed', source=key, model=model, effort=effort, mode=mode)
                how = 'resumed'
            h = self.held[key]
        else:
            if model and model != h.get('model'):
                self.call('set_model', {'key': key, 'model': model})
                h['model'] = model
            if mode and mode != h.get('permissionMode'):
                self.call('set_mode', {'key': key, 'mode': mode})
                h['permissionMode'] = mode
            if h.get('active'):
                how = 'queued'          # Claude Code takes a message sent mid-turn as a queued message (its own queue-operation)
        h['queued'] = h.get('queued', 0) + 1 if how == 'queued' else 0          # an idle session has nothing queued
        was = h.get('active')
        h['active'] = True          # before the call: a short turn's result can arrive before the call's own answer
        h['sentAt'] = time.time()
        try:
            self.call('query', {'key': key, 'text': text})
        except Exception:
            h['active'] = was
            raise
        if not h.get('title'):
            h['title'] = ' '.join(text.split())[:80]
        return {'threadId': PREFIX + key, 'how': how, 'from': PREFIX + h['from'] if h.get('from') and how == 'forked' else None}

    def stop(self, sid):
        key = self.key_of(sid)
        h = self.held.get(key)
        if not (h and h.get('open') and h.get('active')):
            raise ValueError('This agent is not running here.')
        self.call('interrupt', {'key': key})
        return {}

    def reply(self, rid, result):
        with self.lock:
            req = self.requests.get(str(rid))
            if not req:
                raise ValueError('This request has already been resolved.')
            req = {**req, 'params': {**req['params'], 'original': self.originals.get(str(rid))}}
        ans = answer_of(req, result)
        key = self.key_of(req['params']['threadId'])
        h = self.held.get(key) or {}
        after = h.get('afterPlan') if (req['params'].get('claude') or {}).get('tool') == 'ExitPlanMode' and ans.get('allow') else None
        if after:          # plan toggle: an approved plan leaves plan mode for the mode chosen at start, as Claude Code's own dialog does
            ans['updatedPermissions'] = [{'type': 'setMode', 'mode': after, 'destination': 'session'}]
        self.call('permission', {'key': key, 'requestId': str(rid), **ans})
        if after:
            h['permissionMode'] = after
        with self.lock:
            self.requests.pop(str(rid), None)
            self.originals.pop(str(rid), None)
        self.publish({'method': 'serverRequest/resolved', 'params': {'threadId': req['params']['threadId'], 'requestId': str(rid)}})
        return {}

    def to_desktop(self, sid):
        """Hand a session to the Claude desktop app: close this server's client (one writer at a time), then open the app's
        own resume link, which imports the transcript into its Code session list (the app filters SDK sessions out of its
        list of terminal sessions, so this import is the way in)."""
        key = self.key_of(sid)
        h = self.held.get(key)
        if h and h.get('active'):
            raise ValueError('Wait for the turn to end (or stop it) before handing the session to the desktop app.')
        if h and h.get('open'):
            self.call('close', {'key': key})
            h['open'] = False
        other = self.held_elsewhere(key)
        if other and self.holder(other) != 'app':
            raise ValueError('Another Claude Code process holds this session; the desktop app refuses to import a running session.')
        self.opener('claude://resume?session=' + key)          # held by the app itself: the same link switches its window to it
        if h is not None:
            h['desktop'] = time.time()
        return {'threadId': PREFIX + key, 'url': 'claude://resume?session=' + key, 'heldBy': 'app' if other else None}

    def key_of(self, sid):
        sid = str(sid)
        if not sid.startswith(PREFIX):
            raise ValueError('Not a Claude Code session')
        rest = sid[len(PREFIX):]
        if '/' in rest:
            raise ValueError('A Claude Code subagent cannot be messaged from here: message its parent session.')
        try:
            return str(uuidlib.UUID(rest))
        except ValueError:
            raise ValueError('Not a Claude Code session id') from None

    def is_active(self, sid):
        try:
            h = self.held.get(self.key_of(sid))
        except ValueError:
            return False
        return bool(h and h.get('active'))

    def settings(self, sid):
        h = self.held.get(self.key_of(sid)) or {}
        return {k: v for k, v in {'model': h.get('model') and self.alias(h['model']), 'effort': h.get('effort'), 'permissionMode': h.get('permissionMode'),
                                   'how': h.get('how'), 'cost': h.get('cost'), 'rate': h.get('rate'), 'open': h.get('open'), 'plan': h.get('plan')}.items() if v is not None}

    def close(self, grace=None):
        """End the worker and its whole group. The worker is first asked to close its sessions (its stdin closes) and given 5 s,
        or `grace` s when the server is stopping (stop_all), as is the group after SIGTERM before SIGKILL. No worker starts after."""
        self.closing = True
        with self.lock:
            proc = self.process
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(5 if grace is None else grace / 2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass          # end_group sends the group SIGTERM, then SIGKILL
        if proc:
            end_group(proc, wait=True, grace=GROUP_GRACE if grace is None else grace)          # robust: and whatever it started


# robust (2026-11-21): the worker runs in a process group of its own (start_new_session), so stopping it, or finding it gone, ends
# everything it started: the SDK's CLI launchers (claude_wrap.py, which end each CLI's own group in turn) and any other child.
# The group is sent SIGTERM, then SIGKILL after GROUP_GRACE s if anything is left (longer than claude_wrap.GRACE, so a wrapper has
# ended its CLI by then). A group id is not handed out again while any process in it lives, so the signal reaches only ours.
GROUP_GRACE = 5.0


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def watch_worker(proc):
    try:
        proc.wait()
    except Exception:
        return
    end_group(proc)


def end_group(proc, wait=False, grace=GROUP_GRACE):
    """End the worker's process group (its pid is the group id): SIGTERM, then SIGKILL after `grace` s if anything is left;
    with wait, return only once nothing in it is left."""
    pgid = proc.pid
    try:
        proc.wait(0.2)          # reaped, so a dead worker does not hold its group open
    except subprocess.TimeoutExpired:
        pass
    if not group_alive(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.time() + grace
    while time.time() < end:
        try:
            proc.wait(0.05)
        except subprocess.TimeoutExpired:
            pass
        if not group_alive(pgid):
            return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    if wait:
        end = time.time() + 2
        while time.time() < end and group_alive(pgid):
            time.sleep(0.05)
