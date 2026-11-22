"""Claude Code behind the provider interface: read-only observation of ~/.claude (see ../harnesses.md).

Store: ~/.claude/projects/<cwd with every non-alphanumeric character replaced by '-'>/<session uuid>.jsonl, one JSON
record per line; subagents in <session uuid>/subagents/agent-<id>.jsonl with agent-<id>.meta.json (description,
toolUseId of the parent's Agent call). Running sessions register in ~/.claude/sessions/<pid>.json (sessionId, cwd,
status busy/idle, name). The folder name is lossy (my_project and my-project encode alike), so the cwd is taken
from the records themselves, never decoded from the name.

Session ids on this server are 'claude:<uuid>' and 'claude:<uuid>/<agent id>' so they never collide with Codex ids.
Nothing here writes to ~/.claude.
"""
from providers.base import edit_lines          # map engine: lines an edit touched
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import datetime

import glob
import mmap

from .base import FileCache, Provider, fmt_k, under, relative
from . import spend
from . import plans
from .spend import WEEK, Ledger, Tail, ts

PREFIX = 'claude:'
READ_TOOLS = {'Read': 'file_path', 'NotebookRead': 'notebook_path', 'Grep': 'path', 'Glob': 'path', 'LS': 'path'}
WRITE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path', 'NotebookEdit': 'notebook_path'}
RUN_TOOLS = {'Bash', 'BashOutput', 'KillShell', 'PowerShell', 'Monitor'}
DELEGATE_TOOLS = {'Agent', 'Task', 'SendMessage'}
ASK_TOOLS = {'AskUserQuestion', 'ExitPlanMode'}
TAIL = 4_000_000          # like server.Observer: a large transcript is read from its last 4 MB
RECENT = 60               # main sessions listed per scan, newest first, plus every registered live one
SUBS = 12                 # subagents listed per session: the newest twelve plus any still writing
SUB_DAYS = 2              # subagents are listed only for sessions active in the last two days
QUIET = 30                # seconds: past the newest SUBS, a subagent quiet this long and not working is left off the list
# speed (2026-11-21): the parsed histories kept. The cache held 300, dropping the oldest, while one listing on a developer machine
# read 357 transcripts (sessions, subagents, the files continuations join): every entry was dropped before its next read and
# every poll parsed every transcript again (~240k records, 8-10 s, CPU busy). Now an entry read since the listing before last
# (the working set of a poll, however large) is never dropped; beyond it, the least recently used are dropped while the cache's
# estimated size passes CACHE_BYTES. Measured 2026-11-21 on one real history: 357 entries, ~133 MB of Python objects
# (messages ~80% of it: the chat's last 500 per transcript). CACHE_BYTES is judgement (the builder's), not measured.
CACHE_BYTES = 160_000_000


def weight(c):
    """speed: about how many bytes of Python objects one cached history holds (each item ~1 kB of dict, plus its text)."""
    n = sum(len(c.get(k) or ()) for k in ('messages', 'events', 'contexts', 'compacts', 'reasoning', 'turns', 'units'))
    return 1000 * n + sum(len(m.get('text') or '') for m in c.get('messages') or ()) + sum(len(e.get('text') or '') for e in c.get('events') or ())
# running (2026-11-09; the mini-map used to mark working sessions inactive for a while, then bring them back): a session works while its
# process is live (its registration) and either says busy or its transcript says work is still going: a tool call without its
# result (a long Bash, a sleep, a permission it waits on), or a turn not yet ended (no final answer, no error, no interrupt). A
# subagent is done once its parent has its result or its own final turn ended. GRACE is the fallback where neither says so (a turn
# begun, nothing open, the file quiet; Claude Code writes nothing while it compacts): how long after its last write it still counts
# as working. A guess (proposed by the builder), not measured.
GRACE = 120

# Where Claude Code compacts by itself: about 33k tokens below its auto-compact window. The window is
# `modelSettings.<model>.autoCompactWindow`, else `autoCompactWindow`, in settings.json (what /autocompact writes), never more
# than the model's own window. Docs (code.claude.com/docs/en/model-config, read 2026-11-07): a 1M model compacts "at about
# 967K"; for other windows the 33k (min(max output, 20k) + 13k) is users' reading of the code (gist for v2.1.68, issue
# #31806). Measured here 2026-11-07: 395 of 407 automatic compactions fell between 3k below and 15k above window - 33k.
# Not visible from outside the process, so not read: CLAUDE_CODE_AUTO_COMPACT_WINDOW and the other environment overrides, the
# --autocompact flag, a subagent's own autoCompactWindow.
RESERVE = 33_000
WIDE = ('claude-opus-5', 'claude-opus-4-7', 'claude-opus-4-6', 'claude-sonnet-5', 'claude-sonnet-4-6', 'claude-fable', 'claude-mythos',
        'claude-haiku-5-5')          # 1M windows (docs); every other model named claude-* is taken as 200k
NARROW = ('claude-opus-4', 'claude-sonnet-4', 'claude-haiku-4', 'claude-3')          # known 200k families
_settings = FileCache(lambda t: {k: v for k, v in json.loads(t).items() if k in ('autoCompactWindow', 'modelSettings')}, {})


def encode(cwd):
    return re.sub(r'[^A-Za-z0-9]', '-', str(cwd))


def stamp(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def mcp_of(name):
    """connectors: a tool_use name mcp__<server>__<tool> as (server, tool), else None."""
    if not str(name).startswith('mcp__'):
        return None
    server, _, tool = str(name)[5:].partition('__')
    return (server, tool) if server and tool else None


def kind_of(name):
    if mcp_of(name):
        return 'mcp'      # connectors
    if name in READ_TOOLS:
        return 'read'
    if name in WRITE_TOOLS:
        return 'write'
    if name in RUN_TOOLS:
        return 'run'
    if name in DELEGATE_TOOLS:
        return 'delegate'
    if name in ASK_TOOLS:
        return 'ask'
    return 'tool'


def summary(name, inp, cwd=None):
    """A tool call's one-line text, as the chat shows it."""
    if name in READ_TOOLS or name in WRITE_TOOLS:
        p = inp.get(READ_TOOLS.get(name) or WRITE_TOOLS.get(name)) or ''
        p = (relative(p, cwd) or p) if cwd and p else p
        extra = inp.get('pattern') or ''
        return ' '.join(x for x in (name, p, extra) if x)
    if name in RUN_TOOLS:
        return str(inp.get('command') or inp.get('description') or name)
    if name in DELEGATE_TOOLS:
        return name + ': ' + str(inp.get('description') or inp.get('to') or str(inp.get('message', ''))[:80] or '')
    if name == 'AskUserQuestion':
        qs = inp.get('questions') or []
        return 'asks: ' + (qs[0].get('question', '') if qs and isinstance(qs[0], dict) else '')
    if mcp_of(name):
        return '.'.join(mcp_of(name))      # connectors: server.tool, as Codex's mcpToolCall reads
    return name


SYNTHETIC = '<synthetic>'          # the model Claude Code names on lines it writes itself (a stopped turn, a usage limit)
NO_RESPONSE = 'No response requested.'          # its line after a stopped turn ("[Request interrupted by user]")


def texts_of(content):
    return [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text'] if isinstance(content, list) else []


def result_text(block):
    c = block.get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return '\n'.join(x.get('text', '') for x in c if isinstance(x, dict) and x.get('type') == 'text')
    return ''


# chat tags (2026-11-21: a basic, readable rendering, nothing fancy): the blocks Claude Code
# writes into its own transcript as the person's message (a background task's notification, a slash command's echo and output, a
# shell command's output, a hook's output). A slash command (or a "!" shell line) is the person's and reads as they typed it; every
# other block is not theirs: role 'note', with rows the chat draws as plain lines ({'h'} a head, {'k', 'v'} a field, {'v'} a line)
# and text, one readable line for anywhere it is quoted. Ids and file paths (task-id, tool-use-id, output-file) are left out.
TAG = re.compile(r'<([A-Za-z][\w-]*)(?:\s[^<>]*)?>([\s\S]*?)</\1>')
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
REMINDER = re.compile(r'<system-reminder>[\s\S]*?</system-reminder>')
TAG_HEADS = {'task-notification': 'background task', 'local-command-stdout': 'command output', 'local-command-stderr': 'command error output',
             'bash-stdout': 'shell output', 'bash-stderr': 'shell error output', 'user-prompt-submit-hook': 'hook output',
             'local-command-caveat': 'note'}
TAG_SLASH = {'command-name', 'command-message', 'command-args'}
TAG_DROP = {'task-id', 'tool-use-id', 'tool_use_id', 'output-file', 'status'}


def tag_blocks(text):
    """The top-level blocks of a text in order: (tag, inner), and (None, text) for words between them."""
    out, at = [], 0
    for m in TAG.finditer(text):
        if text[at:m.start()].strip():
            out.append((None, text[at:m.start()].strip()))
        out.append((m.group(1), m.group(2)))
        at = m.end()
    if text[at:].strip():
        out.append((None, text[at:].strip()))
    return out


def tag_words(n):
    return re.sub(r'[-_]+', ' ', n).strip()


def took_words(ms):
    s = int(ms // 1000)
    return f'{s} s' if s < 60 else f'{s // 60} min' if s < 3600 else f'{s // 3600} h {s % 3600 // 60} min'


def usage_line(inner):
    f = {k: v.strip() for k, v in TAG.findall(inner)}
    out = []
    for k, say in (('subagent_tokens', 'tokens'), ('total_tokens', 'tokens'), ('tool_uses', 'tool uses'), ('duration_ms', None)):
        v = f.get(k, '')
        if not v.isdigit() or (say == 'tokens' and any(w.endswith('tokens') for w in out)):
            continue
        n = int(v)
        out.append(took_words(n) if say is None else fmt_k(n) + ' tokens' if say == 'tokens' else f"{n} {'tool use' if n == 1 else say}")
    return ' · '.join(out)


def chat_tags(text):
    """None for the person's own words; {'role': 'user', 'text'} for a slash command or shell line they ran; else {'role': 'note',
    'text', 'rows'}. A message counts as Claude Code's own when it starts with a block it is known to write, or is wholly blocks."""
    s = text.strip()
    if not s.startswith('<'):
        return None
    bs = tag_blocks(s)
    if not bs or bs[0][0] is None:
        return None
    names = [n for n, _ in bs if n]
    if bs[0][0] not in TAG_HEADS and bs[0][0] not in TAG_SLASH and bs[0][0] != 'bash-input' and any(n is None for n, _ in bs):
        return None          # the person's own text that happens to open with a tag
    if TAG_SLASH & set(names):
        d = {n: ANSI.sub('', v).strip() for n, v in bs if n}
        name = d.get('command-name') or (d.get('command-message') or '').split(' ')[0]
        if name:
            return {'role': 'user', 'text': ('/' + name.lstrip('/') + ' ' + d.get('command-args', '')).strip()}
    if names == ['bash-input']:
        return {'role': 'user', 'text': '!' + bs[0][1].strip()}
    rows = []
    for n, inner in bs:
        inner = ANSI.sub('', inner)
        if n is None:
            rows.append({'v': inner.strip()})
            continue
        if not inner.strip() and len(bs) > 1:
            continue          # an empty block beside others (a shell's empty stderr) says nothing
        kids = tag_blocks(inner.strip())
        head = TAG_HEADS.get(n, tag_words(n))
        if n == 'task-notification':
            st = next((v.strip() for k, v in kids if k == 'status'), '')
            rows.append({'h': head + (' · ' + st if st else '')})
        else:
            rows.append({'h': head})
        if not any(k for k, _ in kids):
            if inner.strip():
                rows.append({'v': inner.strip('\r\n').rstrip()})          # a command's output keeps its leading spaces
            continue
        for k, v in kids:
            if k in TAG_DROP:
                continue
            if k == 'usage':
                u = usage_line(v)
                if u:
                    rows.append({'v': u})
            elif k is None or k == 'summary':
                rows.append({'v': v.strip()})
            elif v.strip():
                rows.append({'k': tag_words(k), 'v': v.strip()})          # as written: the page escapes it
    first = next((r['v'] for r in rows if r.get('v')), '')
    head = rows[0].get('h', '') if rows else ''
    line = (head + ': ' + first.split('\n')[0].strip()) if head and first else head or first
    return {'role': 'note', 'text': line, 'rows': rows}


class Transcript(Tail):
    """A transcript's calls (message.usage, once per response) and the plan readings in it (the desktop app's usage tool, quotaLimits).
    before: a continued transcript's copied calls (timestamped at or before its join) are left out."""

    def __init__(self, path, before=None):
        super().__init__(path)
        self.before = before

    def reset(self):
        self.led, self.seen, self.plan, self.quota = Ledger(), {}, [], []

    def read(self):
        if self.inode is None:
            self.reset()
        for d in self.lines((b'"usage"', b'percentUsed', b'quotaLimits')):
            m = d.get('message') if isinstance(d.get('message'), dict) else {}
            if self.before is not None and (ts(d.get('timestamp')) or 0) <= self.before:
                continue          # continuation: a record copied from the session it continues
            if d.get('type') == 'assistant':
                q = d.get('quotaLimits')
                if q and q.get('status') == 'rejected':
                    self.quota.append({'t': ts(d.get('timestamp')), 'window': q.get('rateLimitType'), 'resetsAt': q.get('resetsAt'), 'status': 'rejected'})
                u, mid, model = m.get('usage'), m.get('id'), m.get('model')
                if not u or not mid or model == '<synthetic>' or mid in self.seen:
                    continue          # every content block of one response repeats its usage: count the response once
                self.seen[mid] = 1
                cc = u.get('cache_creation') or {}
                w5, w1 = (cc.get('ephemeral_5m_input_tokens') or 0), (cc.get('ephemeral_1h_input_tokens') or 0)
                if not cc:
                    w5 = u.get('cache_creation_input_tokens') or 0
                self.led.add(ts(d.get('timestamp')) or 0, model, u.get('input_tokens') or 0, u.get('cache_read_input_tokens') or 0, w5, w1,
                             u.get('output_tokens') or 0, (u.get('output_tokens_details') or {}).get('thinking_tokens') or 0, u.get('speed') == 'fast')
            elif d.get('type') == 'user' and isinstance(m.get('content'), list):
                for b in m['content']:
                    if not isinstance(b, dict) or b.get('type') != 'tool_result':
                        continue
                    c = b.get('content')
                    txt = c[0].get('text') if isinstance(c, list) and c and isinstance(c[0], dict) else c
                    if not isinstance(txt, str) or '"windows"' not in txt:
                        continue
                    try:
                        j = json.loads(txt)['plan']
                    except (ValueError, KeyError, TypeError):
                        continue
                    self.plan.append({'t': ts(d.get('timestamp')), 'plan': j.get('plan'),
                                      'windows': [{'label': w.get('label'), 'percent': w.get('percentUsed'), 'resetsAt': ts(w.get('resetsAt'))} for w in j.get('windows') or []]})
        return self


class ClaudeCodeProvider(Provider):
    name = 'claude'
    label = 'Claude Code'
    # read-only here; claude drive: server.py's Claude bridge (claude_bridge.py) sets `drive` when its venv is set up, and
    # then the can-flags say what it can do
    can_start = can_send = can_stop = can_reply = False
    drive = None

    def flags(self):
        on = bool(self.drive and self.drive.available().get('ready'))
        return {'canStart': on, 'canSend': on, 'canStop': on, 'canReply': on}

    def __init__(self, tokens, home=None, app_support=None):
        """tokens: the context accounting server.py uses for Codex (unit, settle, reading, compaction, estimate)."""
        self.t = tokens
        self.home = Path(home or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        self.app = Path(app_support or Path.home() / 'Library/Application Support/Claude')
        self.lock = threading.RLock()
        self.cache, self.heads, self.paths, self.meta = {}, {}, {}, {}
        self.links, self.branches, self.chains, self._folders, self.merged = {}, {}, {}, {}, {}          # continuation
        self.copies = {}          # a copy made from this page (made_here): its transcript -> the transcript it copies
        self.joined = {}          # running: an older file's uuid -> the newest file's uuid of its chain (whose process holds them now)
        self._scan = (0, None, None)
        self._live = (0, {})
        self._touched, self._before = set(), set()          # speed: transcripts read since the last listing began, and in the poll before
        self._weights = {}          # speed: weight() of each cached history
        self.parses = 0          # speed: transcript reads that parsed records (tests count them)
        self.transcripts = spend.Files(Transcript)          # usage panel
        self.own_calls = spend.Files(lambda path: Transcript(path, before=self.copied_until(Path(path))))          # continuation: without copied calls

    # ---------- where things are ----------
    def binary(self):
        found = shutil.which('claude')
        if found:
            return found
        bundles = sorted(self.app.glob('claude-code/*/*/claude.app/Contents/MacOS/claude'), key=lambda p: p.stat().st_mtime, reverse=True)
        return str(bundles[0]) if bundles else None

    def available(self):
        store = self.home / 'projects'
        return {'name': self.name, 'label': self.label, 'found': store.is_dir(), 'store': str(store), 'binary': self.binary(), **self.flags()}

    def owns(self, sid):
        return str(sid).startswith(PREFIX)

    def window(self, model):
        """The auto-compact window the user set for the model in ~/.claude/settings.json (not the model's own limit)."""
        try:
            s = json.loads((self.home / 'settings.json').read_text())
        except (OSError, ValueError):
            return None
        per = (s.get('modelSettings') or {}).get(model or '', {})
        return per.get('autoCompactWindow') or s.get('autoCompactWindow')

    def compaction_point(self, sid, model=None, window=None):
        if not model:
            try:
                model = (self.read_history(sid) or {}).get('model')
            except Exception:
                model = None
        return claude_point(model, _settings.get(self.home / 'settings.json'))

    def live(self, fresh=False):
        """sessionId -> the registration a running Claude Code process keeps in ~/.claude/sessions/<pid>.json."""
        at, data = self._live
        if time.time() - at < 1.0 and not fresh:
            return data
        data = {}
        for f in (self.home / 'sessions').glob('*.json'):
            try:
                d = json.loads(f.read_text())
                os.kill(int(d['pid']), 0)
            except (OSError, ValueError, KeyError, TypeError):
                continue      # unreadable, or its process has exited: not live
            data[d.get('sessionId')] = {'status': d.get('status'), 'name': d.get('name'), 'cwd': d.get('cwd'), 'pid': d.get('pid'), 'entrypoint': d.get('entrypoint')}
        self._live = (time.time(), data)
        return data

    def files(self, project_root=None):
        """Main transcripts as (path, mtime, size), newest first; for a project, only folders its name could encode."""
        key = encode(project_root) if project_root else None
        at, k, data = self._scan
        if time.time() - at < 2.0 and k == key:
            return data
        out = []
        root = self.home / 'projects'
        for d in root.iterdir() if root.is_dir() else []:
            if key and not (d.name == key or d.name.startswith(key + '-')):
                continue
            for f in d.glob('*.jsonl'):
                try:
                    st = f.stat()
                except OSError:
                    continue
                out.append((f, st.st_mtime, st.st_size))
        out.sort(key=lambda x: x[1], reverse=True)
        self._scan = (time.time(), key, out)
        return out

    def head(self, path):
        """cwd, first time and first prompt from the start of a transcript (cached: they never change)."""
        h = self.heads.get(path)
        if h:
            return h
        h = {'cwd': None, 'start': None, 'prompt': None, 'title': None, 'first': None, 'firstT': None, 'forked': False}
        try:
            with open(path, 'rb') as f:
                for i, line in enumerate(f):
                    if i > 400 or f.tell() > 2_000_000:
                        break
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get('type') == 'custom-title':
                        h['title'] = r.get('customTitle') or h['title']
                    h['cwd'] = h['cwd'] or r.get('cwd')
                    h['start'] = h['start'] or stamp(r.get('timestamp'))
                    if r.get('uuid') and not h['first']:          # continuation: the first conversation record (a copy, in a continued session)
                        h['first'], h['firstT'], h['forked'] = r['uuid'], stamp(r.get('timestamp')), isinstance(r.get('forkedFrom'), dict)
                    m = r.get('message')
                    if not h['prompt'] and r.get('type') == 'user' and not r.get('isMeta') and isinstance(m, dict):
                        c = m.get('content')
                        text = c if isinstance(c, str) else ' '.join(b.get('text', '') for b in c if isinstance(b, dict) and b.get('type') == 'text') if isinstance(c, list) else ''
                        if text.strip() and not text.lstrip().startswith('<'):
                            h['prompt'] = ' '.join(text.split())[:80]
                    if h['cwd'] and h['start'] and h['prompt'] and h['first']:
                        break
        except OSError:
            pass
        if h['cwd']:
            self.heads[path] = h
        return h

    # ---------- the interface ----------
    def list_sessions(self, project_root=None):
        live, now, out = self.live(), time.time(), []
        files = self.files(project_root)
        live_ids = set(live)
        chosen = [x for i, x in enumerate(files) if i < RECENT or x[0].stem in live_ids]
        with self.lock:          # continuation: its caches advance offsets, one reader at a time
            self._before, self._touched = self._touched, set()          # speed: the working set the cache keeps
            chains, hidden = self.continuations([x[0] for x in chosen])
        for path, mtime, size in chosen:
            uuid = path.stem
            if path in hidden:
                continue          # continuation: a session another one continues is drawn in that one's column
            h = self.head(path)
            cwd = h['cwd'] or (live.get(uuid) or {}).get('cwd')
            if not cwd or (project_root and not under(cwd, project_root)):
                continue
            reg = live.get(uuid)
            sid = PREFIX + uuid
            self.paths[sid] = path
            chain = chains.get(path) or []          # continuation: the sessions this one continues, oldest first, with each join's time
            if chain:
                self.chains[sid] = chain
                self.joined.update((q.stem, uuid) for q, _ in chain)
            else:
                self.chains.pop(sid, None)
            title = self.named(path, reg) or next((self.named(q) for q, _ in reversed(chain) if self.named(q)), None) \
                or (self.head(chain[0][0])['prompt'] if chain else h['prompt']) or 'Claude Code session'
            running = self.working(sid) if reg else False          # running: busy, or its transcript (the newest file) says work is still going (running_of)
            out.append({'id': sid, 'provider': self.name, 'title': title, 'cwd': cwd, 'started': (self.head(chain[0][0])['start'] if chain else None) or h['start'] or mtime,
                        'updated': mtime, 'running': running, 'parent': None, 'external': bool(reg),
                        'continues': [PREFIX + q.stem for q, _ in chain], **({'copyOf': PREFIX + self.copies[path].stem} if path in self.copies else {})})
            subs = []
            for q in [path] + [q for q, _ in chain]:          # continuation: the subagents of the sessions it continues sit under it too
                try:
                    qm = q.stat().st_mtime
                except OSError:
                    continue
                if now - qm < SUB_DAYS * 86400:
                    for sub in (q.parent / q.stem / 'subagents').glob('agent-*.jsonl'):
                        try:
                            subs.append((sub.stat().st_mtime, sub))
                        except OSError:
                            pass
            subs.sort(reverse=True)
            for i, (smt, sub) in enumerate(subs):
                ssid = PREFIX + sub.parent.parent.name + '/' + sub.stem.removeprefix('agent-')          # its own session's id (handback reads that one's transcript)
                self.paths[ssid] = sub
                if i >= SUBS and now - smt >= QUIET and not (reg and not self.handback(ssid) and self.working(ssid)):
                    continue          # older subagents of a long session stay off the list (their parent's chat still names them), unless still working
                sh = self.head(sub)
                meta = self.meta_of(sub)
                srun = self.working(ssid) if reg else False          # running: what its transcript says, not how long it has been quiet
                out.append({'id': ssid, 'provider': self.name, 'title': meta.get('description') or sh['prompt'] or 'subagent', 'cwd': sh['cwd'] or cwd,
                            'started': sh['start'] or smt, 'updated': smt, 'running': srun, 'parent': sid, 'external': srun,
                            'agentType': meta.get('agentType')})
        return out

    def named(self, path, reg=None):
        """A name the session was given (the app's registration, the custom title), or None; a first prompt is not a name."""
        h = self.head(path)
        return (reg or {}).get('name') or self.custom_title(path) or (self.cache.get(str(path)) or {}).get('title') or h['title']

    # ---------- continuation: one conversation that goes on in a new transcript file ----------
    # Claude Code starts a new session file for a conversation that goes on: the desktop app's rewind (and an edited message)
    # resumes at an earlier message with forkSession, `claude --resume <id> --fork-session` does the same from the command
    # line, and /branch copies the conversation with each record marked forkedFrom {sessionId, messageUuid}. The new file
    # begins with a copy of the old one's records from its last compaction (or its start) up to the message it resumed at,
    # each with its original uuid and timestamp, written after a record stamped with the new file's own start; its own
    # records follow. (Read 2026-11-09 in Claude Code 2.1.317's bundled code and in this machine's transcripts.) So:
    #   a copy:   its first record's time is earlier than the file's first time (or the record carries forkedFrom);
    #   the join: the last copied record (J), the point the conversation went on from;
    #   the predecessor: the one other transcript in the same folder that holds J and had started by J's time (copies of J
    #                    made later started later); found by a byte search from the end, once, and kept.
    # Plain --resume and --continue go on in the same file, as does every ordinary compaction: nothing to join.
    def copy_mode(self, h):
        if h.get('forked'):
            return 'forked'
        if h.get('first') and h.get('firstT') is not None and h.get('start') is not None and h['firstT'] < h['start'] - 1:
            return 'time'
        return None

    def link_of(self, path):
        """The session `path` continues, as {'pred': Path, 'join': uuid, 'tj': time of the join, 'off': the join's byte offset
        in the predecessor, 'born': when this file's own records begin}, or None. Cached: a join never moves once the file's own
        records have begun, and a predecessor once found is kept; a search that found nothing is repeated only when the
        folder's list of transcripts changes."""
        h = self.head(path)
        mode = self.copy_mode(h)
        if not mode:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        c = self.links.get(path)
        if not c or c['ino'] != st.st_ino or (not c['final'] and c['size'] != st.st_size):
            c = dict(self.join_of(path, h, mode), ino=st.st_ino, size=st.st_size, pred=None, off=None, tried=None)
            self.links[path] = c
        if not c.get('join'):
            return None
        if c['pred'] is None:
            names = self.folder(path.parent)
            if c['tried'] != names:
                new = set(names) - set(c['tried'] or ())          # a transcript searched once need not be again: what is added later is newer than the join
                c['tried'] = names
                c['pred'], c['off'] = self.find_pred(path, c, new)
        return c if c['pred'] is not None else None

    def copied_until(self, path):
        """The time of a continued file's join (its copied records are at or before it), or None for any other file."""
        h = self.head(path)
        mode = self.copy_mode(h)
        if not mode:
            return None
        try:
            ino = path.stat().st_ino
        except OSError:
            return None
        c = self.links.get(path)
        if not c or c['ino'] != ino:
            c = self.join_of(path, h, mode)
        return c.get('tj')

    def join_of(self, path, h, mode):
        """The last copied record of a continued file (its uuid and time), where its own records begin, and the sessionId a
        /branch copy names. Reads the file from its start only up to its first own record."""
        out = {'join': None, 'tj': None, 'born': h['start'], 'final': False, 'from': None, 'entry': None}
        try:
            with open(path, 'rb') as f:
                for line in f:
                    if b'"uuid"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict) or not r.get('uuid'):
                        continue
                    t = stamp(r.get('timestamp'))
                    ff = r.get('forkedFrom') if isinstance(r.get('forkedFrom'), dict) else None
                    copy = bool(ff) if mode == 'forked' else (t is not None and t < h['start'] - 1)
                    if not copy:
                        out['final'], out['entry'] = True, r.get('entrypoint')          # who wrote its own records (copies keep the original's)
                        if mode == 'forked':
                            out['born'] = t
                        break
                    out['join'], out['tj'] = (ff or {}).get('messageUuid') or r['uuid'], t
                    out['from'] = (ff or {}).get('sessionId') or out['from']
        except OSError:
            pass
        if mode == 'forked' and not out['final']:
            out['born'] = None
        return out

    def folder(self, d):
        """The transcripts in one project folder as a tuple of names (cached for two seconds)."""
        at, names = self._folders.get(d, (0, None))
        if time.time() - at > 2.0 or names is None:
            names = tuple(sorted(f.name for f in d.glob('*.jsonl')))
            self._folders[d] = (time.time(), names)
        return names

    def find_pred(self, path, c, names):
        tj = c['tj'] or 0
        if c.get('from'):
            cands = [path.parent / (c['from'] + '.jsonl')]
        else:
            cands = []
            for name in sorted(names):
                q = path.parent / name
                if q == path:
                    continue
                try:
                    qm = q.stat().st_mtime
                except OSError:
                    continue
                if qm < tj - 1:
                    continue          # last written before the join was: cannot hold it
                qs = self.head(q)['start']
                if qs is not None and qs > tj + 1:
                    continue          # started after the join was written: a later copy of it, not where it was written
                cands.append((qm, q))
            # the one last written soonest after the join first: a conversation that went on elsewhere stopped there
            cands = [q for _, q in sorted(cands, key=lambda x: x[0])]
        for q in cands:
            off = find_record(q, c['join'])
            if off is not None:
                return q, off
        return None, None

    def branched(self, pred, link):
        """True when the predecessor kept going after this file began (a message of the person's or a model answer written
        after it): a real branch, kept as its own column. Reads the predecessor after the join only, and only what was added."""
        key = (pred, link['born'])
        b = self.branches.get(key) or {'off': link['off'], 'yes': False}
        self.branches[key] = b
        if b['yes'] or link['born'] is None:
            return b['yes'] or link['born'] is None
        try:
            with open(pred, 'rb') as f:
                f.seek(b['off'])
                data = f.read()
        except OSError:
            return False
        done = data.rfind(b'\n') + 1
        b['off'] += done
        for line in data[:done].splitlines():
            if b'"timestamp"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = stamp(r.get('timestamp')) if isinstance(r, dict) else None
            if t is None or t <= link['born'] + 1 or r.get('isMeta') or r.get('isSidechain'):
                continue
            m = r.get('message') if isinstance(r.get('message'), dict) else {}
            if r.get('type') == 'assistant' and m.get('model') not in (None, SYNTHETIC):
                b['yes'] = True
            elif r.get('type') == 'user' and not r.get('isCompactSummary'):
                c = m.get('content')
                text = c if isinstance(c, str) else '\n'.join(texts_of(c))
                if text.strip() and not text.lstrip().startswith(('<', '[Request interrupted')):
                    b['yes'] = True
            if b['yes']:
                break
        return b['yes']

    def made_here(self, path, link):
        """A copy started from this page ("Start a copy here": the bridge's send with fork): the person made it on purpose, so it
        never joins its original. Known from the bridge (how 'forked') while this server runs, and after a restart from its own
        records' entrypoint: 'sdk-py' is the Python Agent SDK this server's bridge drives (the desktop app writes
        'claude-desktop', its SDK sessions 'sdk-cli'); another Python SDK client's copy is kept apart too."""
        h = (getattr(getattr(self, 'drive', None), 'held', None) or {}).get(path.stem)
        return bool(h and h.get('how') == 'forked') or link.get('entry') == 'sdk-py'

    def continuations(self, paths):
        """For the listed transcripts: {tip: [(predecessor, join time), ...] oldest first} and the set of predecessors drawn in
        their continuation's column. A predecessor joins a continuation when it did not go on after that one began. One it
        went on after is a real branch and keeps its own column (as does every continuation of a predecessor that stopped after
        two or more of them began: which one carries on is not known)."""
        links = {}
        for q in paths:
            try:
                lk = self.link_of(q)
            except Exception:
                lk = None
            if lk and not self.made_here(q, lk):          # a copy made from this page keeps its own column, and is no rival continuation
                links[q] = lk
            elif lk:
                self.copies[q] = lk['pred']          # ... drawn as a branch of what it copies (the server names it "copy of ...")
        kids = {}
        for q, lk in links.items():
            kids.setdefault(lk['pred'], []).append(q)
        def joins(q):          # the predecessor stopped after this one began, and after no other of its continuations
            lk = links.get(q)
            if not lk or self.branched(lk['pred'], lk):
                return False
            return not any(k != q and not self.branched(lk['pred'], links[k]) for k in kids[lk['pred']])
        def chain(q, seen):
            if not joins(q) or q in seen:
                return []
            lk = links[q]
            if lk['pred'] not in links and lk['pred'] not in seen:          # a predecessor older than the listed ones may continue another
                pl = self.link_of(lk['pred'])
                pl = pl if pl and not self.made_here(lk['pred'], pl) else None
                if pl and lk['pred'] not in kids.get(pl['pred'], []):
                    kids.setdefault(pl['pred'], []).append(lk['pred'])
                if pl:
                    links[lk['pred']] = pl
            return chain(lk['pred'], seen | {q}) + [(lk['pred'], lk['tj'])]
        chains, hidden = {}, set()
        for q in paths:
            ch = chain(q, set())
            if ch:
                chains[q] = ch
                hidden.update(x for x, _ in ch)
        return {q: ch for q, ch in chains.items() if q not in hidden}, hidden

    def custom_title(self, path):
        try:
            return json.loads((path.parent / path.stem / 'custom-title.json').read_text()).get('customTitle')
        except (OSError, ValueError, AttributeError):
            return None

    def meta_of(self, sub):
        m = self.meta.get(sub)
        if m is None:
            try:
                m = json.loads(sub.with_name(sub.stem + '.meta.json').read_text())
            except (OSError, ValueError):
                m = {}
            self.meta[sub] = m
        return m

    def path_of(self, sid):
        p = self.paths.get(sid)
        if p:
            return p
        rest = str(sid).removeprefix(PREFIX)
        uuid, _, agent = rest.partition('/')
        for d in (self.home / 'projects').glob('*'):
            f = d / (uuid + '.jsonl')
            if f.exists():
                p = (d / uuid / 'subagents' / ('agent-' + agent + '.jsonl')) if agent else f
                self.paths[sid] = p
                return p
        return None

    def empty(self):
        return {'messages': [], 'events': [], 'turnActive': False, 'parent': None, 'contexts': [], 'compacts': [], 'window': None, 'context': 0, 'base': None,
                'reasoning': [], 'turns': [], 'errors': [], 'handbacks': {}, 'asking': {}, 'plan': []}

    def read_history(self, sid):
        """One session's history; for a continuation, the sessions it continues joined with its own (see merge)."""
        chain = self.chains.get(sid) if '/' not in str(sid) else None
        own = self.own_history(sid)
        if not chain:
            return own
        with self.lock:
            parts = [(self.own_history(PREFIX + q.stem, q), tj) for q, tj in chain]
            key = tuple((id(h), h.get('size'), h.get('mtime')) for h, _ in parts) + ((id(own), own.get('size'), own.get('mtime')),)
            m = self.merged.get(sid)
            if m and m[0] == key:
                m[1]['turnActive'] = own['turnActive']          # running: the newest file's, recomputed on every read (the clock moves)
                return m[1]
            out = merge(parts, own)
            self.merged[sid] = (key, out)
            return out

    def own_history(self, sid, path=None):
        """One transcript file's history, read from where it was left."""
        path = path or self.path_of(sid)
        if not path:
            return self.empty()
        with self.lock:
            key = str(path)
            c = self.cache.get(key)
            try:
                st = path.stat()
            except OSError:
                return self.empty()
            self._touched.add(key)
            if c and c['size'] == st.st_size and c['mtime'] == st.st_mtime_ns:
                self.cache[key] = self.cache.pop(key)          # speed: most recently used last (the order eviction drops from)
                c['turnActive'] = self.running_of(sid, c, self.reg_of(sid), st.st_mtime)          # running: the registration and the clock move without the file
                return c
            if not c or st.st_size < c['offset'] or c.get('inode') != st.st_ino:
                h = self.head(path)
                c = {'offset': 0, 'messages': [], 'events': [], 'turnActive': False, 'parent': sid.rsplit('/', 1)[0] if '/' in sid else None,
                     'start': h['start'], 'last': h['start'], 'title': None, 'model': None, 'cwd': h['cwd'], 'mid': None, 'pending': {},
                     'context': 0, 'window': None, 'contexts': [], 'compacts': [], 'base': None, 'units': [], 'carry': 0, 'pendingCompact': None,
                     'reasoning': [], 'turns': [],
                     'errors': [], 'handbacks': {},          # attention: API errors that ended a turn; subagent results this session got back
                     'asking': {},          # attention: tool calls that wait on the person (ASK_TOOLS) with no tool result yet, by tool_use id
                     'open': {}, 'ended': False}          # running: this turn's tool calls without their result (id -> time); its final answer written
                if st.st_size > TAIL:
                    with path.open('rb') as f:
                        f.seek(st.st_size - TAIL)
                        f.readline()
                        c['offset'] = f.tell()
                    c['truncated'] = True
            with path.open('rb') as f:
                f.seek(c['offset'])
                data = f.read()
            done = data.rfind(b'\n') + 1
            c['offset'] += done
            for line in data[:done].splitlines():
                try:
                    rec = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(rec, dict):
                    self.take(c, rec)
            self.t.settle(c['units'], self.t.estimate(c['units']))
            c['messages'] = c['messages'][-500:]
            c['events'] = c['events'][-3000:]
            c['reasoning'] = c['reasoning'][-500:]
            c['turns'] = c['turns'][-500:]
            c['window'] = self.window(c['model'])
            c['turnActive'] = self.running_of(sid, c, self.reg_of(sid), st.st_mtime)
            c.update(size=st.st_size, mtime=st.st_mtime_ns, inode=st.st_ino)
            self.cache.pop(key, None)
            self.cache[key] = c
            self.parses += 1
            self._weights[key] = weight(c)
            self.trim()
            return c

    def trim(self):
        """speed: drop the least recently used histories outside the working set while the cache weighs more than CACHE_BYTES."""
        total = sum(self._weights.get(k, 0) for k in self.cache)
        if total <= CACHE_BYTES:
            return
        for k in list(self.cache):
            if total <= CACHE_BYTES:
                break
            if k in self._touched or k in self._before:
                continue
            self.cache.pop(k)
            total -= self._weights.pop(k, 0)

    def running_of(self, sid, c, reg, mtime, now=None):
        """running: is this session (or subagent) working now, by the rule above GRACE. reg: its main session's live registration."""
        if not reg:
            return False          # no live process holds it
        now = now or time.time()
        if '/' in str(sid) and self.handback(sid):
            return False          # its parent has its result
        if '/' not in str(sid) and reg.get('status') == 'busy':
            return True
        if any(k not in c.get('asking', {}) for k in c.get('open', {})):
            return True          # a tool call without its result, however long it runs
        return not c.get('ended') and now - mtime < GRACE

    def reg_of(self, sid):
        """running: the live registration of the process that holds this session. A subagent of an older file of a continuation
        is held by the process of the chain's newest file (the older file's own id is registered no more)."""
        u = str(sid).removeprefix(PREFIX).split('/')[0]
        live = self.live()
        return live.get(u) or (live.get(self.joined.get(u)) if '/' in str(sid) and self.joined.get(u) else None)

    def working(self, sid):
        """running: running_of from the transcript (read incrementally, as the chat reads it)."""
        return bool(self.read_history(sid).get('turnActive'))

    def handback(self, sid):
        """attention: when a subagent's result reached its parent, {t, text}, or None. Read from the parent's transcript: the tool
        result of the Agent call that started it (meta.json's toolUseId), or, for one run in the background, the message that
        brought its report (origin.handback) or the task notification naming it."""
        if '/' not in str(sid):
            return None
        parent, agent = str(sid).rsplit('/', 1)
        path = self.path_of(sid)
        meta = self.meta_of(path) if path else {}
        hbs = self.read_history(parent).get('handbacks') or {}
        got = [h for h in (hbs.get('agent:' + agent), hbs.get('tool:' + str(meta.get('toolUseId')))) if h]
        return max(got, key=lambda h: h['t']) if got else None

    # ---- attention: a question waiting on the person ----
    def waiting(self, sid, log=None):
        """The tool calls of a main session that wait on the person (AskUserQuestion, ExitPlanMode) with no tool result yet,
        oldest first. Where the store registers its running sessions (~/.claude/sessions/), only a session whose process is
        alive counts: a transcript left with an unanswered call by a process that exited waits on nobody. A tool call held
        for a permission prompt is not here: the transcript records the decision only on the call's result
        (permissionDecision), and the registration says busy or idle, so a pending permission looks like a running call."""
        if '/' in str(sid):
            return []          # subagents do not ask the person
        log = log if log is not None else self.read_history(sid)
        asking = log.get('asking') or {}
        if not asking:
            return []
        if (self.home / 'sessions').is_dir() and str(sid).removeprefix(PREFIX) not in self.live():
            return []
        return sorted(({'id': k, **v} for k, v in asking.items()), key=lambda a: a['t'])

    def ask_requests(self, sid, log=None):
        """The waiting tool calls as the page's question requests (Codex's item/tool/requestUserInput shape), marked
        watchOnly: this server cannot answer them; the person answers in Claude Code and the call's result clears them."""
        out = []
        for a in self.waiting(sid, log):
            inp = a.get('input') or {}
            if a['tool'] == 'AskUserQuestion':
                qs = [{'id': str(i), 'header': q.get('header') or '', 'question': str(q.get('question') or ''), 'multiSelect': bool(q.get('multiSelect')),
                       'options': [{'label': str(o.get('label') or ''), 'description': str(o.get('description') or '')} for o in q.get('options') or [] if isinstance(o, dict)]}
                      for i, q in enumerate(inp.get('questions') or []) if isinstance(q, dict)]
            else:          # ExitPlanMode: the plan, to approve or not
                first = next((ln.strip().lstrip('#*->` ').strip() for ln in str(inp.get('plan') or '').splitlines() if ln.strip().lstrip('#*->` ').strip()), '')
                qs = [{'id': 'plan', 'header': 'Plan', 'question': 'Approve its plan' + (': ' + first[:160] if first else '') + '?', 'options': []}]
            out.append({'id': 'claude-ask:' + a['id'], 'method': 'item/tool/requestUserInput',
                        'params': {'threadId': sid, 'itemId': a['id'], 'startedAtMs': int(a['t'] * 1000), 'questions': qs or [{'id': '0', 'question': 'It asks you something', 'options': []}],
                                   'watchOnly': True, 'harness': self.label, 'tool': a['tool']}})
        return out

    def take(self, c, rec):
        """One transcript record into the history (messages, file events, token readings, compactions)."""
        typ = rec.get('type')
        t = stamp(rec.get('timestamp')) or c['last'] or time.time()
        if rec.get('timestamp'):
            c['last'] = t
        origin = rec.get('origin') if isinstance(rec.get('origin'), dict) else {}
        if origin.get('handback') and origin.get('from'):          # attention: a background subagent's report arriving in its parent
            c['handbacks']['agent:' + str(origin['from'])] = {'t': t, 'text': origin.get('handbackReportText') or origin.get('body')}
        att = rec.get('attachment') if isinstance(rec.get('attachment'), dict) else {}
        note = att.get('prompt') if att.get('commandMode') == 'task-notification' else None
        if isinstance(note, str) and '<task-notification>' in note:          # attention: the task notification for a finished background agent
            tid, tu = re.search(r'<task-id>([^<]+)</task-id>', note), re.search(r'<tool-use-id>([^<]+)</tool-use-id>', note)
            st = re.search(r'<status>([^<]+)</status>', note)
            if not st or st.group(1) in ('completed', 'failed', 'killed', 'stopped'):
                h = {'t': t, 'text': None}
                if tid and 'agent:' + tid.group(1) not in c['handbacks']:
                    c['handbacks']['agent:' + tid.group(1)] = h
                if tu:
                    c['handbacks'].setdefault('tool:' + tu.group(1), h)
        if typ == 'custom-title':
            c['title'] = rec.get('customTitle') or c['title']
            return
        if typ == 'system' and rec.get('subtype') == 'compact_boundary':
            self.t.compaction(c, t)
            meta = rec.get('compactMetadata') or {}
            if c['pendingCompact'] is not None and meta.get('preTokens'):
                c['pendingCompact']['from'] = meta['preTokens']      # Claude Code records the size before; the next reading gives the size after
            return
        if rec.get('permissionMode'):
            c['permissionMode'] = rec['permissionMode']      # Claude Code records the mode on the person's messages
        m = rec.get('message')
        if not isinstance(m, dict) or typ not in ('user', 'assistant'):
            return
        content = m.get('content')
        cwd = rec.get('cwd') or c['cwd']
        if typ == 'user':
            if isinstance(content, str):
                content = [{'type': 'text', 'text': content}]
            if not isinstance(content, list):
                return
            hidden = rec.get('isMeta') or rec.get('isCompactSummary') or rec.get('isVisibleInTranscriptOnly')
            texts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_result':
                    out = result_text(b)
                    plans.claude_result(c, b.get('tool_use_id'), out, bool(b.get('is_error')))          # plan progress: a TaskCreate's id; a refused plan call undone
                    c['asking'].pop(b.get('tool_use_id'), None)          # attention: answered (or refused, or interrupted)
                    c['open'].pop(b.get('tool_use_id'), None)          # running
                    u = c['pending'].pop(b.get('tool_use_id'), None)
                    tur = rec.get('toolUseResult')
                    if u is not None and u['msg'] is not None and u['msg'].get('tool') in ('Agent', 'Task') and not (isinstance(tur, dict) and tur.get('status') == 'async_launched'):
                        c['handbacks']['tool:' + str(b.get('tool_use_id'))] = {'t': t, 'text': out}          # attention: a subagent's result, back in its parent
                    if u is not None and u in c['units']:
                        u['w'] += len(out)
                    else:
                        self.t.unit(c, None, len(out))
                    if u is not None and u['msg'] is not None and out:
                        u['msg']['text'] += '\n\n' + (out if len(out) <= 4000 else out[:4000] + '…')
                    if b.get('is_error'):          # the count tells the truth: an edit refused or failed (old_string not found) changed nothing,
                        for ev in (u or {}).get('evs', []):          # so it leaves "N files changed, M lines" (and the floor's writes)
                            if ev.get('kind') == 'write' and ev in c['events']:
                                c['events'].remove(ev)
                    for ev in (u or {}).get('evs', []):
                        if ev.get('kind') == 'mcp':
                            ev['end'] = t      # connectors: an MCP call ends when its result arrives
                elif b.get('type') == 'text':
                    texts.append(b.get('text', ''))
            text = '\n'.join(x for x in texts if x)
            if text and rec.get('isCompactSummary'):      # memory: the summary the compaction left, on the compaction it belongs to
                pc = c['pendingCompact'] or (c['compacts'][-1] if c['compacts'] else None)
                if pc is not None and not pc.get('summary'):
                    pc['summary'] = text
            if text:
                own = REMINDER.sub('', text).strip() if '<system-reminder>' in text else text          # chat tags: a reminder is context, not words
                if hidden or not own:
                    self.t.unit(c, None, len(text))      # in the context, not in the chat
                else:
                    if text.startswith('[Request interrupted'):
                        c['ended'] = True; c['open'].clear()          # running: stopped by the person
                    elif not own.lstrip().startswith(('<command-', '<local-command')):
                        c['ended'] = False; c['open'].clear()          # running: a turn begins (a slash command's echo is no turn)
                    tags = chat_tags(own)          # chat tags: Claude Code's own blocks are not the person's message
                    msg = {'role': 'user', 'text': own, 't': t, 'id': rec.get('uuid'), 'tokens': 0}
                    if tags:
                        msg.update(tags)
                    if msg['role'] == 'user':
                        c['asking'].clear()          # attention: the person wrote instead: nothing still waits on them
                    c['messages'].append(msg)
                    c['turns'].append({'t': t})      # a Claude Code turn starts at the person's message (the chat's one pill per turn)
                    self.t.unit(c, msg, len(text))
            return
        # an assistant record: one content block of one model response; the response's usage is repeated on each
        if m.get('stop_reason') in ('end_turn', 'stop_sequence', 'max_tokens') or rec.get('isApiErrorMessage') or (m.get('model') == SYNTHETIC and texts_of(content) == [NO_RESPONSE]):
            c['ended'] = True          # running: the final answer, an error or the line after a stop ends the turn
        elif m.get('stop_reason') == 'tool_use':
            c['ended'] = False
        if rec.get('isApiErrorMessage'):          # attention: the turn ended on an API error (retries are system api_error records, not kept)
            c['errors'] = (c['errors'] + [{'t': t, 'text': '\n'.join(texts_of(content)) or str(rec.get('error') or 'API error'), 'code': rec.get('error'), 'turn': rec.get('uuid')}])[-10:]
            return
        if m.get('model') == SYNTHETIC and texts_of(content) == [NO_RESPONSE]:
            return      # claude drive: the line Claude Code writes itself after a stopped turn, not the model's; not in the chat
        mid = m.get('id')
        if mid and mid != c['mid']:
            c['mid'] = mid
            c['model'] = m.get('model') if m.get('model') and m.get('model') != SYNTHETIC else c['model']      # a line Claude Code wrote itself names no model
            us = m.get('usage') or {}
            size = (us.get('input_tokens') or 0) + (us.get('cache_creation_input_tokens') or 0) + (us.get('cache_read_input_tokens') or 0)
            if size:
                # the context this call read: everything since the previous call is settled against it, as Codex's token_count
                self.t.reading(c, t, {'last_token_usage': {'input_tokens': size}})
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'thinking' and b.get('thinking'):
                # thinking the transcript kept in plain text (most blocks carry only a signature): the chat's reasoning items
                c['reasoning'].append({'text': b['thinking'], 't': t, 'end': t, 'id': (mid or '') + ':' + str(len(c['reasoning']))})
            elif b.get('type') == 'text' and b.get('text'):
                msg = {'role': 'assistant', 'text': b['text'], 't': t, 'id': mid, 'tokens': 0}
                c['messages'].append(msg)
                self.t.unit(c, msg, len(b['text']))
            elif b.get('type') == 'tool_use':
                name, inp = b.get('name', ''), b.get('input') if isinstance(b.get('input'), dict) else {}
                plans.claude_use(c, t, name, inp, b.get('id'))          # plan progress: TodoWrite, the task tools, the desktop app's plan
                kind = kind_of(name)
                msg = {'role': 'tool', 'text': summary(name, inp, cwd), 't': t, 'id': b.get('id'), 'tokens': 0, 'tool': name, 'kind': kind}
                if kind == 'mcp':
                    msg.update(connector=mcp_of(name)[0], tool=mcp_of(name)[1])      # connectors
                c['messages'].append(msg)
                if b.get('id'):
                    c['open'][b['id']] = t          # running: open until its result arrives
                if name in ASK_TOOLS and b.get('id'):          # attention: it waits on the person until its tool result arrives
                    c['asking'][b['id']] = {'t': t, 'tool': name, 'input': inp}
                u = self.t.unit(c, msg, len(json.dumps(inp)))
                c['pending'][b.get('id')] = u
                field = READ_TOOLS.get(name) or WRITE_TOOLS.get(name)
                path = relative(inp.get(field), cwd) if field else None
                if kind in ('read', 'write') and not path:
                    continue      # outside the session's folder, or no path given (a Grep of the whole folder)
                e = {'at': t, 'kind': kind, 'path': path, 'text': msg['text'][:200], 'item': b.get('id'), 'tokens': 0, 'tool': msg['tool']}
                if kind == 'write':
                    e['lines'] = edit_lines(name, inp)          # map engine: lines the edit touched (standing)
                if kind == 'mcp':
                    e['connector'] = msg['connector']      # connectors
                c['events'].append(e)
                u['evs'].append(e)

    # ---- use and spend (usage panel) ----
    def session_usage(self, sid):
        path = self.paths.get(sid)
        if not path:
            return None
        chain = self.chains.get(sid) if '/' not in str(sid) else None
        if not chain:
            return self.transcripts.read(path).led
        # continuation: every file's own calls once: the oldest whole, each later one without the calls it copied
        return spend.merge([self.transcripts.read(chain[0][0]).led] + [self.own_calls.read(q).led for q, _ in chain[1:]] + [self.own_calls.read(path).led])

    def recent_transcripts(self):
        projects, cut = self.home / 'projects', time.time() - WEEK
        out = []
        for p in glob.glob(str(projects / '*' / '*.jsonl')) + glob.glob(str(projects / '*' / '*' / 'subagents' / '*.jsonl')):
            try:
                if os.path.getmtime(p) < cut:
                    continue
            except OSError:
                continue
            out.append(self.transcripts.read(p))
        return out

    plan_note = 'no reading yet: Claude Code reports it when an agent checks its usage, or in a session this page drives'

    def plan_usage(self):
        """The newest reading of the desktop app's usage tool in any transcript ('5-hour', 'weekly', then the app's other windows),
        or, for a session this server drives, the Agent SDK's own figure for its window. The bridge is never started from here."""
        d = self.claude_plan(self.recent_transcripts(), getattr(self, 'drive', None))
        now, wins, at, plan = time.time(), None, None, None
        r = (d['readings'] or [None])[-1]
        if r:
            five = next((w for w in r['windows'] if re.search('5-hour', w['label'] or '', re.I)), None)
            wk = next((w for w in r['windows'] if re.search('all models', w['label'] or '', re.I)), None)
            wins = [{'label': '5-hour', 'percent': five['percent'] if five and (five['resetsAt'] or 0) > now else None, 'resetsAt': five and five['resetsAt']},
                    {'label': 'weekly', 'percent': wk['percent'] if wk and (not wk['resetsAt'] or wk['resetsAt'] > now) else None, 'resetsAt': wk and wk['resetsAt']}]
            wins += [{'label': re.sub('^Weekly · ', 'weekly, ', w['label'] or ''), 'percent': w['percent'] if not w['resetsAt'] or w['resetsAt'] > now else None,
                      'resetsAt': w['resetsAt']} for w in r['windows'] if w is not five and w is not wk]          # a window past its reset: its old figure no longer holds
            at, plan = r['t'], r['plan']
        sdk = (d['sdk'] or [None])[0]
        if sdk and sdk.get('utilization') is not None:
            wins = wins or [{'label': '5-hour', 'percent': None, 'resetsAt': None}, {'label': 'weekly', 'percent': None, 'resetsAt': None}]
            name = {'five_hour': '5-hour', 'seven_day': 'weekly'}.get(sdk.get('window'))
            for w in wins:
                if w['label'] == name:
                    w['percent'], w['resetsAt'], at = round(100 * sdk['utilization']), sdk.get('resetsAt'), now
        if not wins:
            return None
        return {'plan': plan, 'at': at, 'windows': wins, 'detail': d}

    def claude_plan(self, files, drive):
        readings = sorted((x for f in files for x in f.plan if x['t']), key=lambda x: x['t'])
        quota = sorted((x for f in files for x in f.quota if x['t']), key=lambda x: x['t'])
        out = {'readings': readings[-40:], 'overLimit': quota[-10:], 'sdk': [],
               'source': "the desktop app's usage tool, when an agent called it (its result is in that agent's transcript)"}
        for key, h in (getattr(drive, 'held', None) or {}).items():          # sessions this server drives: the SDK's rate_limit_event
            r = h.get('rate') if isinstance(h, dict) else None
            if r:
                out['sdk'].append({'session': 'claude:' + key, 'window': r.get('rate_limit_type'), 'utilization': r.get('utilization'), 'status': r.get('status'), 'resetsAt': r.get('resets_at')})
        return out


def claude_point(model, settings):
    """Claude Code's auto-compact point for a model under settings.json's auto-compact keys: see RESERVE. A model the record does
    not name, or that is none of the known families, gets a stand-in point marked as an estimate."""
    base = (model or '').split('[')[0]
    known = base.startswith(WIDE) or base.startswith(NARROW) or '[1m]' in (model or '')
    own = 1_000_000 if (base.startswith(WIDE) or '[1m]' in (model or '')) else 200_000
    per = ((settings.get('modelSettings') or {}).get(base) or {}).get('autoCompactWindow') if base else None
    if isinstance(per, int):
        w, src = per, "this model's auto-compact window in Claude Code's settings (what /autocompact sets)"
    elif isinstance(settings.get('autoCompactWindow'), int):
        w, src = settings['autoCompactWindow'], "the auto-compact window in Claude Code's settings"
    else:
        w, src = own, "the model's own window (no auto-compact window set)"
    w = min(w, own)
    if not known:
        src = ('no model in the record' if not base else base + ' is not a model this page knows') + ': ' + fmt_k(own) + ' assumed, ' + src
    return {'threshold': w - RESERVE, 'window': w, 'model': model, 'rule': fmt_k(w) + ' window less 33k kept back', 'source': src, 'estimate': not known}


def find_record(path, uuid):
    """The byte offset of the line whose record has this uuid, searching from the end of the file (a join sits near the end of
    the transcript it was copied from), or None. Only the lines holding the uuid's text are parsed."""
    needle = ('"' + uuid + '"').encode()
    try:
        with open(path, 'rb') as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                end = len(mm)
                while True:
                    i = mm.rfind(needle, 0, end)
                    if i < 0:
                        return None
                    s = mm.rfind(b'\n', 0, i) + 1
                    e = mm.find(b'\n', i)
                    try:
                        r = json.loads(mm[s:e if e >= 0 else len(mm)])
                    except ValueError:
                        r = None
                    if isinstance(r, dict) and r.get('uuid') == uuid:
                        return s
                    end = s          # the uuid named as another record's parent, or in a list: look further back
    except (OSError, ValueError):
        return None          # unreadable, or empty (mmap of an empty file)


LISTS = ('messages', 'events', 'contexts', 'compacts', 'reasoning', 'turns', 'errors', 'plan')


def merge(parts, own):
    """continuation: one history from the transcripts of a conversation that went on in new files. parts: [(history, join
    time)] oldest first; own: the newest file's history. Each earlier file gives what it held up to its join (what came after
    the join there was rewound away); each later one what it wrote after the previous join (before that it holds copies)."""
    out = dict(own)
    lo = None
    lists = {k: [] for k in LISTS}
    for h, tj in parts + [(own, None)]:
        for k in LISTS:
            tk = 'at' if k == 'events' else 't'
            lists[k] += [x for x in h.get(k) or [] if (lo is None or (x.get(tk) or 0) > lo) and (tj is None or (x.get(tk) or 0) <= tj)]
        lo = tj
    out.update(lists)
    out['messages'], out['events'], out['reasoning'], out['turns'] = out['messages'][-500:], out['events'][-3000:], out['reasoning'][-500:], out['turns'][-500:]
    root = parts[0][0]
    out['handbacks'] = {k: v for h, _ in parts for k, v in (h.get('handbacks') or {}).items()}
    out['handbacks'].update(own.get('handbacks') or {})
    out['start'], out['base'], out['first'] = root.get('start'), root.get('base') or own.get('base'), root.get('first') or own.get('first')
    out['model'] = own.get('model') or next((h.get('model') for h, _ in reversed(parts) if h.get('model')), None)
    out['truncated'] = any(h.get('truncated') for h, _ in parts) or bool(own.get('truncated'))
    out['continues'] = len(parts)
    return out
