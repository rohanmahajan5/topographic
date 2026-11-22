#!/usr/bin/env python3
"""Local agent workspace. No Python dependencies; Codex owns agent execution."""
import argparse
import atexit
import collections
import concurrent.futures
import datetime
import fcntl
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from types import SimpleNamespace

from providers.codex import CodexProvider          # harness providers (harnesses.md)
from providers.claude_code import ClaudeCodeProvider
from providers import plans          # plan progress
from providers.base import patch_lines, change_lines          # map engine: lines a change touched (standing)
import claude_bridge          # claude drive: Claude Code sessions driven through the Agent SDK worker (claude_bridge.py)
import compact_points          # the columns' scale: where each session compacts, from its provider (compact_points.py)
from log_watch import LogWatch          # low latency: the providers' own stores watched (log_watch.py)
import provider_setup          # providers panel: which tools are here, signed in, ready to drive, and the step that fixes each
import usage          # usage panel: tokens, API-equivalent dollars and plan readings from the record files, read only (usage.py)

ROOT = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
TITLE_MAX = 200
FIRST_WAIT = 0.2          # s the first answer waits for the other providers' first listing before it goes without it (judgement)
FIRST_LISTING_LOCK = threading.Lock()
STARTED = time.time()
DEFAULT_PROJECT = Path(os.environ.get('AGENT_IDE_PROJECT', str(ROOT.parent.parent)))


def binary():
    candidates = [os.environ.get('AGENT_IDE_CODEX'), '/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex', shutil.which('codex')]
    for p in candidates:
        if p and Path(p).is_file():
            return p
    raise RuntimeError('Install a current Codex CLI or set AGENT_IDE_CODEX.')


# piece B: what a Codex approval may be answered with (read from the other version's asks.py, which read Codex CLI 0.167's
# app-server schema): accept, acceptForSession, {acceptWithExecpolicyAmendment: {execpolicy_amendment: [prefix...]}}, decline,
# cancel; only what the request's availableDecisions lists (where it lists them), and a rule only as Codex proposed it
APPROVALS = ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval')
SIMPLE_DECISIONS = ('accept', 'acceptForSession', 'decline', 'cancel')


def codex_decision(req, result):
    """The page's answer to a Codex approval as the one decision Codex takes; ValueError if Codex does not offer it here."""
    if not isinstance(result, dict):
        raise ValueError('A reply needs a result object')
    d, p = result.get('decision'), req.get('params') or {}
    offered = p.get('availableDecisions')
    if isinstance(d, dict):
        amend = (d.get('acceptWithExecpolicyAmendment') or {}).get('execpolicy_amendment') if req['method'] == APPROVALS[0] and set(d) == {'acceptWithExecpolicyAmendment'} else None
        if not (isinstance(amend, list) and amend and all(isinstance(x, str) for x in amend)):
            raise ValueError('Invalid approval decision')
        proposed = [(x.get('acceptWithExecpolicyAmendment') or {}).get('execpolicy_amendment') for x in offered if isinstance(x, dict)] if isinstance(offered, list) else [p.get('proposedExecpolicyAmendment')]
        if amend not in proposed:
            raise ValueError('Codex did not propose that rule for this request')
        return {'decision': {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': amend}}}
    if d not in SIMPLE_DECISIONS:
        raise ValueError('Invalid approval decision')
    if isinstance(offered, list) and d not in offered:          # no list: the schema's four apply (a file change came with none, live)
        raise ValueError('Codex does not offer "%s" for this request' % d)
    return {'decision': d}


def reason_text(result):
    """piece B: the line a person wrote with a no (or None): one paragraph, at most 2000 characters."""
    r = result.get('message') if isinstance(result, dict) else None
    if not isinstance(r, str) or not r.strip():
        return None
    return ' '.join(r.split())[:2000]


def what_of(req):
    """piece B: a request in a few words, for the message that carries a no's line ("the command `pytest -q`", "the change to a.py")."""
    p = req.get('params') or {}
    if req.get('method') == 'item/permissions/requestApproval':          # robust: an extra-access request declined with a line
        return 'the extra access you asked for'
    if req.get('method') == APPROVALS[1]:
        paths = [c.get('path') for c in p.get('changes') or [] if isinstance(c, dict) and c.get('path')]
        return 'the change to ' + ', '.join(paths) if paths else 'the file change'
    cmd = p.get('command')
    cmd = ' '.join(cmd) if isinstance(cmd, list) else str(cmd or '').strip()
    m = re.match(r'''^(?:\S*/)?(?:ba|z)?sh\s+-l?c\s+(['"])([\s\S]*)\1\s*$''', cmd)          # Codex sends the shell's wrapper: the command it runs
    if m:
        cmd = m.group(2).replace("'\\''", "'") if m.group(1) == "'" else m.group(2)
    return 'the command `' + cmd[:300] + '`' if cmd else 'that'


class FileItems:
    """piece B (the other version's asks.FileItems): Codex's fileChange items as the stream delivered them, by thread and item id
    (the last 200), so a file change approval listed later still carries its change (Codex's request has no diff)."""

    def __init__(self, keep=200):
        self.keep, self.items = keep, {}

    def note(self, method, p):
        if method not in ('item/started', 'item/completed') or not isinstance(p, dict):
            return
        it = p.get('item')
        if isinstance(it, dict) and it.get('type') == 'fileChange' and it.get('id') and isinstance(it.get('changes'), list):
            self.items[(p.get('threadId'), it['id'])] = it['changes']
            while len(self.items) > self.keep:
                self.items.pop(next(iter(self.items)))

    def attach(self, req):
        """The request with `changes` in its params when it is a file change whose item was seen (else as it was)."""
        if req.get('method') != APPROVALS[1]:
            return req
        p = req.get('params') or {}
        ch = p.get('changes') or self.items.get((p.get('threadId'), p.get('itemId')))
        return {**req, 'params': {**p, 'changes': ch}} if ch and ch is not p.get('changes') else req


# robust (2026-11-21): the requests Codex's app-server may send (its schema, `codex app-server generate-json-schema`, CLI 0.169:
# ServerRequest). These five reach the person; any other is refused at once with a JSON-RPC error (an unanswered request would hold
# the turn until Stop: T3 13c7de296) and the refusal is written in the agent's chat.
PERMISSIONS = 'item/permissions/requestApproval'
ELICITATION = 'mcpServer/elicitation/request'
QUESTION = 'item/tool/requestUserInput'
HANDLED_REQUESTS = APPROVALS + (QUESTION, PERMISSIONS, ELICITATION)
# robust: the app-server restarts by itself after it ends (ported from the other version's Bridge.revive, made automatic): after
# RESTART_BACKOFF[i] seconds for the i-th end within RESTART_WINDOW; more than RESTART_LIMIT ends in the window and it stays down
# and says why (a person's next action tries once more). Judgement, not measured: three tries in two minutes, 1 s, 4 s, 15 s apart.
RESTART_LIMIT = 3
RESTART_WINDOW = 120.0
RESTART_BACKOFF = (1.0, 4.0, 15.0)
STARTED_CODEX = []          # robust (stopping): every app-server process this server started (stop_all ends any still running)
RESTART_WAIT = 25.0          # s an action taken while Codex restarts waits for it before it is refused with the reason (judgement)
# robust: what a permissions request may be answered with (PermissionsRequestApprovalResponse: the permissions granted, for the
# turn or the session; nothing granted is the no), and what an MCP elicitation may be (McpServerElicitationRequestResponse.action)
PERMISSION_DECISIONS = ('accept', 'acceptForSession', 'decline')
FORM_MODES = ('form', 'openai/form', 'openaiForm')
# questions (2026-11-21): Codex's non-blocking questions. Its request_user_input_async tool answers the model {"accepted":true} at
# once and the turn goes on; the questions reach the client as an ordinary agentMessage item with delivery "async" and
# questions [{title, options: [str] | null}] (app-server schema, CLI 0.169: AgentMessageThreadItem, AsyncUserInputQuestion; the
# tool's own description: one option or a free-text answer, never an "Other" option of its own). The protocol has no method that
# answers one: the answer is the person's next message (a steer into the running turn, else a new turn). This server keeps each
# such item as a question request of its own (ASYNC_PREFIX + thread + item id) so the page shows it as a question card; it is
# never the app-server's request, so a restart of Codex does not end it.
ASYNC_PREFIX = 'codex-async:'


def async_request(tid, item, t=None):
    """questions: an agentMessage item carrying async questions as the page's question request (Codex's requestUserInput shape,
    isBlocking false, delivery 'async'), or None when the item asks nothing."""
    if not (isinstance(item, dict) and item.get('type') == 'agentMessage' and item.get('delivery') == 'async' and item.get('id') and tid):
        return None
    qs = [q for q in item.get('questions') or [] if isinstance(q, dict) and str(q.get('title') or '').strip()]
    if not qs:
        return None
    return {'id': ASYNC_PREFIX + str(tid) + ':' + str(item['id']), 'method': QUESTION,
            'params': {'threadId': tid, 'itemId': item['id'], 'isBlocking': False, 'delivery': 'async', 'startedAtMs': int((t or time.time()) * 1000),
                       'text': str(item.get('text') or ''),
                       'questions': [{'id': 'q%d' % i, 'header': '', 'question': str(q['title']), 'isOther': True, 'isSecret': False,
                                      'options': [{'label': str(o), 'description': ''} for o in q.get('options') or [] if str(o).strip()] or None}
                                     for i, q in enumerate(qs)]}}


def checked_answers(req, result):
    """A question request's answers as the page sent them ({answers: {<question id>: {answers: [str, ...]}}}, one entry per
    question; a multi-select question's several picks are several strings), checked; ValueError otherwise."""
    answers = result.get('answers') if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        raise ValueError('Answers are required')
    asked = {q.get('id') for q in (req.get('params') or {}).get('questions') or []}
    for qid, a in answers.items():
        if qid not in asked or not isinstance(a, dict) or not isinstance(a.get('answers'), list) or not all(isinstance(x, str) for x in a['answers']):
            raise ValueError('Invalid answer for question ' + str(qid))
    if asked - set(answers):
        raise ValueError('Every question needs an answer')
    return answers


def async_answer_text(req, answers):
    """questions: the message that answers async questions (the person's words; the format is this app's own: Codex's own
    client keeps its reply format to itself). Each question by its title, then what was picked or typed."""
    qs = (req.get('params') or {}).get('questions') or []
    lines = ['Answer to your question "%s": %s' % (q.get('question'), ', '.join(a for a in answers[q['id']]['answers'] if a.strip()) or '(no answer)') for q in qs if q.get('id') in answers]
    return '\n'.join(lines)


def granted(asked):
    """The access a permissions request asked for, as the grant that gives exactly that (GrantedPermissionProfile)."""
    asked = asked if isinstance(asked, dict) else {}
    return {k: asked[k] for k in ('fileSystem', 'network') if asked.get(k) is not None}


def permissions_reply(req, result):
    """robust: the page's answer to an extra-access request as Codex takes it; ValueError for anything else."""
    d = result.get('decision') if isinstance(result, dict) else None
    if d not in PERMISSION_DECISIONS:
        raise ValueError('Allow once, allow for the session or decline')
    if d == 'decline':
        return {'permissions': {}, 'scope': 'turn'}
    return {'permissions': granted((req.get('params') or {}).get('permissions')), 'scope': 'session' if d == 'acceptForSession' else 'turn'}


def form_fields(schema):
    """robust: an MCP form's fields as the page's questions (one per field, in order), or None when a field is of a kind the
    page cannot ask (the request can then only be declined). Each: {id, header, question, options, isOther, isSecret, kind,
    required, values}; values maps an option's label to the value sent back."""
    if not isinstance(schema, dict) or not isinstance(schema.get('properties'), dict):
        return None
    need = set(schema.get('required') or [])
    out = []
    for name, s in schema['properties'].items():
        if not isinstance(s, dict):
            return None
        t, title = s.get('type'), s.get('title') or name
        values = None
        if t == 'string' and isinstance(s.get('oneOf'), list):
            values = {str(o.get('title') or o.get('const')): o.get('const') for o in s['oneOf'] if isinstance(o, dict) and 'const' in o}
        elif t == 'string' and isinstance(s.get('enum'), list):
            names = s.get('enumNames') if isinstance(s.get('enumNames'), list) and len(s['enumNames']) == len(s['enum']) else s['enum']
            values = {str(n): v for n, v in zip(names, s['enum'])}
        elif t == 'boolean':
            values = {'yes': True, 'no': False}
        elif t == 'array':
            items = s.get('items') or {}
            opts = items.get('anyOf') if isinstance(items.get('anyOf'), list) else [{'const': v, 'title': v} for v in items.get('enum') or []]
            values = {str(o.get('title') or o.get('const')): o.get('const') for o in opts if isinstance(o, dict) and 'const' in o}
            if not values:
                return None
        elif t not in ('string', 'number', 'integer'):
            return None
        words = title + (': ' + s['description'] if s.get('description') else '')
        if t == 'array':
            words += ' (one or more of: ' + ', '.join(values) + '; separate them with commas)'
        if name not in need:
            words += ' (optional: a lone - leaves it empty)'
        out.append({'id': name, 'header': title, 'question': words, 'isOther': t != 'boolean' and not (t == 'string' and values),
                    'isSecret': False, 'kind': t, 'required': name in need,
                    'options': [{'label': k, 'description': ''} for k in values] if values and t != 'array' else None, 'values': values})
    return out


def elicitation_card(p):
    """robust: what the card offers for an MCP elicitation: 'form' (its fields as questions; accept with them, decline, cancel),
    'url' (open the link it names, then accept; decline, cancel), 'confirm' (a form with no fields: accept, decline, cancel), or
    'decline' (a kind the page cannot answer, e.g. a device-verified approval: decline or cancel only)."""
    mode = p.get('mode')
    if mode == 'url' and isinstance(p.get('url'), str):
        return 'url', None
    if mode in FORM_MODES:
        f = form_fields(p.get('requestedSchema'))
        if f == []:
            return 'confirm', None
        if f:
            return 'form', f
    return 'decline', None


def elicitation_reply(req, result):
    """robust: the page's answer to an MCP elicitation as Codex takes it ({action, content, _meta}); ValueError for anything the
    request's kind does not offer, or a field answered with something its type does not take."""
    if not isinstance(result, dict):
        raise ValueError('A reply needs a result object')
    kind, fields = elicitation_card(req.get('params') or {})
    if 'answers' in result:
        if kind != 'form':
            raise ValueError('This request has no form to fill')
        answers, content = result.get('answers'), {}
        if not isinstance(answers, dict) or set(answers) - {f['id'] for f in fields}:
            raise ValueError('Answers are required')
        for f in fields:
            got = (answers.get(f['id']) or {}).get('answers') if isinstance(answers.get(f['id']), dict) else None
            v = got[0].strip() if isinstance(got, list) and got and isinstance(got[0], str) else None
            if v in (None, '', '-'):
                if f['required']:
                    raise ValueError(f['header'] + ' needs an answer')
                continue
            if f['kind'] in ('number', 'integer'):
                try:
                    n = float(v)
                except ValueError:
                    raise ValueError(f['header'] + ' takes a number') from None
                if f['kind'] == 'integer' and n != int(n):
                    raise ValueError(f['header'] + ' takes a whole number')
                content[f['id']] = int(n) if f['kind'] == 'integer' else n
            elif f['kind'] == 'array':
                parts = [x.strip() for x in v.split(',') if x.strip()]
                if not parts or any(x not in f['values'] for x in parts):
                    raise ValueError(f['header'] + ' takes one or more of: ' + ', '.join(f['values']))
                content[f['id']] = [f['values'][x] for x in parts]
            elif f['values'] is not None:
                if v not in f['values']:
                    raise ValueError(f['header'] + ' takes one of: ' + ', '.join(f['values']))
                content[f['id']] = f['values'][v]
            else:
                content[f['id']] = v
        return {'action': 'accept', 'content': content, '_meta': None}
    a = result.get('action')
    if a not in (('decline', 'cancel') if kind == 'decline' else ('accept', 'decline', 'cancel')):
        raise ValueError('Codex does not offer "%s" for this request' % a)
    if a == 'accept' and kind == 'form':
        raise ValueError('Fill in the form to accept')
    return {'action': a, 'content': ({} if kind == 'confirm' else None) if a == 'accept' else None, '_meta': None}


def card_of(req):
    """robust: a request as the page's card reads it: an extra-access request with the decisions it may get, an elicitation with
    its kind ('elicit') and, for a form, its fields as questions (the request itself is unchanged)."""
    m, p = req.get('method'), req.get('params') or {}
    if m == PERMISSIONS:
        return {**req, 'params': {**p, 'availableDecisions': list(PERMISSION_DECISIONS)}}
    if m == ELICITATION:
        kind, fields = elicitation_card(p)
        extra = {'elicit': kind}
        if fields:
            extra['questions'] = [{k: v for k, v in f.items() if k not in ('kind', 'required', 'values')} for f in fields]
        return {**req, 'params': {**p, **extra}}
    return req


DRIVEN_LABELS = {'antigravity': 'Antigravity', 'cursor': 'Cursor Agent', 'opencode': 'OpenCode'}


def answer_shape(result):
    """more providers: the page's answer as a driven provider's reply() takes it: {'optionId'}, {'decision'} (Codex's names; the
    provider maps them to its own options), {'answers': {qid: [str]}}, with a decline's line as 'message'."""
    if isinstance(result.get('answers'), dict):
        return {'answers': {k: (v.get('answers') if isinstance(v, dict) else v if isinstance(v, list) else [str(v)]) for k, v in result['answers'].items()}}
    out = {k: result[k] for k in ('optionId', 'message') if result.get(k)}
    d = result.get('decision')
    if isinstance(d, str):
        out['decision'] = d
    elif result.get('action') in ('decline', 'cancel'):
        out['decision'] = result['action']
    return out


def generic_card(req):
    """more providers: a driven provider's request (providers/core.py request_message) as the page's card reads it: its own
    options kept (agentOptions: the buttons carry the agent's own words), Codex's decisions it can be answered with, the command
    or paths as the card's lines, and an itemId (its own request id) so its answer is kept with its chat."""
    p = dict(req.get('params') or {})
    opts = [o for o in p.get('options') or [] if isinstance(o, dict) and o.get('id')]
    kinds = {o.get('kind') for o in opts}
    avail = [d for d, k in (('accept', 'allow_once'), ('acceptForSession', 'allow_always'), ('decline', 'reject_once')) if k in kinds]
    if 'reject_once' not in kinds:
        avail.append('cancel')
    p.update(agentOptions=opts, availableDecisions=avail, itemId=p.get('itemId') or str(req.get('id')))
    if not p.get('command') and p.get('paths'):
        p['reason'] = p.get('reason') or (p.get('title') or 'change') + ': ' + ', '.join(map(str, p['paths'][:6]))
    p.setdefault('reason', p.get('title'))
    if p.get('answerVia') == 'elsewhere':
        p['watchOnly'] = True          # a request the page cannot answer (OpenCode form fields it cannot draw): answered in the harness
        p['harness'] = DRIVEN_LABELS.get(p.get('provider'), p.get('provider'))
    return {**req, 'params': p}


def generic_words(req):
    """more providers: a driven provider's request in the kept entries' words (request_words), for one that ends unanswered."""
    p = req.get('params') or {}
    w = {'key': 'item:' + str(req.get('id')), 'threadId': p.get('threadId'), 't': time.time(), 'kind': 'approve', 'verb': 'do', 'cmd': '',
         'short': None, 'question': None, 'method': req.get('method')}
    if p.get('kind') == 'question':
        w.update(kind='ask', verb=None, question=' · '.join(str(q.get('question') or '') for q in p.get('questions') or [] if isinstance(q, dict)) or p.get('title') or 'The agent needs your input')
    elif p.get('kind') == 'command':
        w.update(verb='run', cmd=str(p.get('command') or p.get('title') or 'a command'))
    elif p.get('kind') == 'file-change':
        w.update(verb='change', cmd='edit ' + ', '.join(map(str, p.get('paths') or [])) if p.get('paths') else str(p.get('title') or 'a file change'))
    else:
        w['cmd'] = str(p.get('title') or 'something')
    w['cmd'] = w['cmd'][:2000]
    return w


def secret_asked(req):
    """robust: a question request with any question marked secret (its answers are never kept or logged)."""
    return req.get('method') == QUESTION and any(isinstance(q, dict) and q.get('isSecret') for q in (req.get('params') or {}).get('questions') or [])


def request_words(req, seen=None):
    """robust: a request in the words the page's kept entries use (keptAsks in live.html), so a request that ended unanswered can be
    shown in its chat after the process that held it is gone: its key in the chat (the page's reqKey), time, kind, verb, what."""
    m, p = req.get('method'), req.get('params') or {}
    cmd = p.get('command')
    cmd = ' '.join(cmd) if isinstance(cmd, list) else str(cmd or '')
    if p.get('itemId'):
        key = 'item:' + str(p['itemId'])
    elif p.get('startedAtMs'):
        key = 'at:%s:%s:%s' % (p['startedAtMs'], m, cmd or p.get('reason') or p.get('message') or '')
    else:
        key = 'req:%s:%s:%s' % (m, req.get('id'), int(seen or 0))
    w = {'key': key[:300], 'threadId': p.get('threadId'), 't': p['startedAtMs'] / 1000 if isinstance(p.get('startedAtMs'), (int, float)) else (seen or time.time()),
         'kind': 'approve', 'verb': 'run', 'cmd': '', 'short': None, 'question': None, 'method': m}
    if m == APPROVALS[0]:
        x = re.match(r'''^(?:\S*/)?(?:ba|z)?sh\s+-l?c\s+(['"])([\s\S]*)\1\s*$''', cmd)
        w['cmd'] = (x.group(2).replace("'\\''", "'") if x.group(1) == "'" else x.group(2)) if x else cmd or p.get('reason') or 'an action'
    elif m == APPROVALS[1]:
        paths = [c.get('path') for c in p.get('changes') or [] if isinstance(c, dict) and c.get('path')]
        w['verb'], w['cmd'] = 'change', 'edit ' + ', '.join(paths) if paths else 'a file change'
    elif m == QUESTION:
        w['kind'], w['verb'] = 'ask', None
        w['question'] = ' · '.join(str(q.get('question') or '') for q in p.get('questions') or [] if isinstance(q, dict)) or 'The agent needs your input'
    elif m == PERMISSIONS:
        pm = p.get('permissions') or {}
        fs = pm.get('fileSystem') or {}
        parts = (['network'] if (pm.get('network') or {}).get('enabled') else []) + [k + ' ' + ', '.join(fs[k]) for k in ('read', 'write') if fs.get(k)]
        w['verb'], w['cmd'] = 'get', 'more access' + (': ' + '; '.join(parts) if parts else '')
    elif m == ELICITATION:
        w['verb'], w['cmd'] = 'answer', (p.get('serverName') or 'a tool server') + ': ' + str(p.get('message') or p.get('title') or p.get('url') or 'input')
    else:
        w['verb'], w['cmd'] = 'do', m or 'something'
    w['cmd'] = w['cmd'][:2000]
    return w


class Bridge:
    """One `codex app-server` process serving every thread this server drives.

    robust (2026-11-21, ported by hand from the other version's Bridge, ide-providers-2026-11-09 5f5b8903f, after T3 Code):
    - the reader never dies from one message: each frame is handled in its own try; one that cannot be handled is noted once;
    - the reader never calls Codex (a call from it would wait on the reader itself);
    - when the process ends, everything waiting on it fails at once with the reason, a turn that was running is settled as
      failed, the requests it held end (recorded in `ended`: their ids belonged to that process, so they can no longer be
      answered), and a fresh app-server starts by itself (RESTART_*); threads are resumed again on their next action;
    - a request Codex sends that this page cannot answer is refused at once and the refusal recorded in `ended`."""

    def __init__(self, command=None):
        self.init_state(command)
        self.start_process()

    def init_state(self, command=None):
        """Everything a Bridge holds, without starting Codex (CodexAbsent shares it, so the two never drift apart)."""
        self.command = command
        self.process = None
        self.pending = {}
        self.requests = {}
        self.seen = {}            # robust: request id -> when it came (the page's time for a request without one)
        self.loaded = set()
        self.threads = {}
        self.active = {}
        self.lock = threading.RLock()
        self.restart_lock = threading.Lock()
        self.seq = 0
        self.events = collections.deque(maxlen=2500)
        self.event_seq = 0
        self.condition = threading.Condition()
        self.errors = collections.deque(maxlen=20)
        self.warned = set()
        self.ready = False
        self.up = threading.Event()          # set while the app-server answers (or when restarting stopped): a waiting action goes on
        self.restarting = False
        self.closing = False
        self.dead = None          # robust: why the last app-server ended ({'code', 'text', 'at'}), until a new one is up
        self.gave_up = None       # robust: why it is not restarted again (too many ends in RESTART_WINDOW, or a start that failed)
        self.ends = collections.deque(maxlen=RESTART_LIMIT + 1)
        self.ended = []           # robust: requests that ended unanswered ({words..., 'why'}), taken by the workspace (requests_sync)
        self.hooks = {}           # robust: 'requests' () when the open requests changed; run inline, must not call Codex
        self.settings, self.mcp, self.usage, self.rate = {}, {}, {}, None      # chat controls: what Codex last reported per thread
        self.failures, self.mock_ids, self.mock_threads = {}, set(), set()      # attention: per thread, the errors the stream reported (its last ten); the mock's requests and running threads
        self.files, self.mock_sent = FileItems(), []      # piece B: fileChange items by thread and item (a file change approval's diff); the lines the mock was sent
        self.async_ids = set()          # questions: the async questions held in `requests` (never the app-server's own requests)

    # ---------- the app-server process ----------
    def start_process(self):
        proc = self.process = subprocess.Popen(self.command or [binary(), 'app-server', '--stdio'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        STARTED_CODEX.append(proc)          # robust (stopping): stop_all ends every app-server this server started, even one a stop interrupted
        if self.closing:          # robust (stopping): a restart that raced the stop never outlives it
            self.kill_quietly(proc)
            raise RuntimeError('The workspace server is stopping.')
        threading.Thread(target=self.read, args=(proc,), daemon=True).start()
        threading.Thread(target=self.read_errors, args=(proc,), daemon=True).start()
        self._call('initialize', {'clientInfo': {'name': 'agent_ide', 'title': 'Agent workspace', 'version': '0.1.0'}, 'capabilities': {'experimentalApi': True}}, timeout=45)
        self.send({'method': 'initialized'})
        self.dead = None
        self.ready = True
        self.up.set()

    def read_errors(self, proc):
        try:
            for line in proc.stderr:
                self.errors.append(line.strip())
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stderr.close()
            except (OSError, ValueError):
                pass

    def send(self, msg):
        with self.lock:
            proc = self.process
            if proc is None or proc.poll() is not None:
                raise RuntimeError('Codex is not running' + (' (' + self.dead['text'] + ')' if self.dead and self.dead.get('text') else '') + '.')
            try:
                proc.stdin.write(json.dumps(msg) + '\n')
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:          # a failed write: the connection is gone (T3 34cfd2484)
                raise RuntimeError('The connection to Codex dropped (%s).' % e) from None

    def wait_up(self):
        """robust: an action taken while Codex is down waits for its restart (at most RESTART_WAIT s), or, when it stopped restarting,
        tries one start now (a person asked for this); RuntimeError with the plain reason otherwise, so nothing is lost silently."""
        if self.ready:
            return
        if self.closing:
            raise RuntimeError('The workspace server is stopping; nothing was sent.')
        if self.restarting:
            self.up.wait(RESTART_WAIT)
            if self.ready:
                return
            if self.restarting:
                raise RuntimeError('Codex stopped and is still restarting; nothing was sent. Try again in a moment.')
        self.revive_now()

    def revive_now(self):
        with self.restart_lock:
            if self.ready:
                return
            if self.closing:
                raise RuntimeError('The workspace server is stopping; nothing was sent.')
            try:
                self.start_process()
            except Exception as e:
                why = self.gave_up or str(e)
                self.kill_quietly()
                raise RuntimeError('Codex could not be started again (%s); nothing was sent. Restart the workspace server.' % why) from None
            self.gave_up = None
            self.ends.clear()
        self.publish({'method': 'bridge/reconnected', 'params': {}})

    def call(self, method, params=None, timeout=45):
        self.wait_up()
        return self._call(method, params, timeout)

    def _call(self, method, params=None, timeout=45):
        with self.lock:
            self.seq += 1
            key = self.seq
            future = concurrent.futures.Future()
            self.pending[key] = future
            try:
                self.send({'id': key, 'method': method, 'params': params or {}})
            except RuntimeError:
                self.pending.pop(key, None)
                raise
        try:
            try:
                return future.result(timeout)
            except concurrent.futures.TimeoutError:
                raise RuntimeError('Codex did not respond in time. It may still be initializing; check the agent before retrying.') from None
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def publish(self, msg):
        with self.condition:
            self.event_seq += 1
            self.events.append((self.event_seq, msg))
            self.condition.notify_all()

    # ---------- reading ----------
    def warn_once(self, what, err):
        key = (what, type(err).__name__, str(err)[:200])
        if key not in self.warned:
            self.warned.add(key)
            self.errors.append('agent_ide could not handle %s: %s: %s' % (what or 'a message', type(err).__name__, str(err)[:300]))

    def read(self, proc):
        """robust: one bad line or one frame that fails is noted and skipped; the reader goes on until the process's output ends."""
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.warn_once('a line that is not JSON', ValueError(line[:200]))
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self.route(msg)
                except Exception as e:          # one frame, never the reader
                    self.warn_once(msg.get('method'), e)
        except (OSError, ValueError) as e:          # the pipe itself failed
            self.warn_once('the app-server output', e)
        finally:
            if proc is self.process:
                self.terminated(proc)
            for f in (proc.stdin, proc.stdout):          # robust: an ended process's pipes go with it
                try:
                    f.close()
                except (OSError, ValueError):
                    pass

    def route(self, msg):
        if 'method' not in msg:
            with self.lock:
                f = self.pending.get(msg.get('id'))
            if f and not f.done():
                if 'error' in msg:
                    err = msg['error'] if isinstance(msg['error'], dict) else {'message': str(msg['error'])}
                    f.set_exception(RuntimeError(err.get('message', str(err))))
                else:
                    f.set_result(msg.get('result', {}))
            return
        method, p = msg['method'], msg.get('params') or {}
        if not isinstance(p, dict):
            p = {}
        tid = p.get('threadId')
        self.files.note(method, p)      # piece B: a file change's diff, for its approval
        if method == 'item/completed':          # questions: an agentMessage that asks without blocking becomes a question card
            q = async_request(tid, p.get('item'))
            if q:
                with self.lock:
                    if q['id'] not in self.requests:
                        self.requests[q['id']] = q
                        self.seen[q['id']] = time.time()
                        self.async_ids.add(q['id'])
                self.publish({'method': 'agent_ide/asyncQuestion', 'params': {'threadId': tid, 'requestId': q['id']}})
                self.hook('requests')
        if 'id' in msg:
            if method not in HANDLED_REQUESTS:          # robust: refused at once, and the refusal shown in the agent's chat
                self.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'agent_ide does not handle ' + method}})
                with self.lock:
                    self.ended.append(dict(request_words(msg, time.time()), why='unsupported'))
                self.hook('requests')
                self.publish({'method': 'agent_ide/requestEnded', 'params': {'threadId': tid, 'why': 'unsupported', 'method': method}})
                return
            with self.lock:
                self.requests[str(msg['id'])] = msg
                self.seen[str(msg['id'])] = time.time()
        if method == 'serverRequest/resolved':
            with self.lock:
                self.requests.pop(str(p.get('requestId')), None)
                self.seen.pop(str(p.get('requestId')), None)
        if method == 'thread/started' and isinstance(p.get('thread'), dict) and p['thread'].get('id'):
            self.loaded.add(p['thread']['id'])
            self.threads[p['thread']['id']] = p['thread']
        if method == 'turn/started' and tid and isinstance(p.get('turn'), dict):
            self.active[tid] = p['turn'].get('id')
        if method == 'turn/completed' and tid:
            self.active.pop(tid, None)
        failed(self, method, p)      # attention
        track(self, method, p)      # chat controls
        self.publish(msg)
        if 'id' in msg or method == 'serverRequest/resolved':
            self.hook('requests')

    def hook(self, name, *args):
        fn = self.hooks.get(name)
        if fn:
            try:
                fn(*args)
            except Exception as e:          # a hook never stops the reader
                self.warn_once('hook ' + name, e)

    def terminated(self, proc):
        """robust: the app-server's output closed: fail what waits on it, settle the turns that ran, end the requests it held (they
        cannot be answered any more: their ids belonged to it), say why, and start a fresh one unless stopping or given up."""
        try:
            code = proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:          # the output can close while the process keeps running (T3 7769d709c)
            code = None
            self.kill_quietly(proc)
        tail = [e for e in list(self.errors)[-3:] if e and not e.startswith('agent_ide could not')]
        text = ('it exited with code %s' % code if code is not None else 'its output closed') + ('; ' + tail[-1][:300] if tail else '')
        with self.lock:
            self.ready = False
            self.up.clear()
            self.dead = {'code': code, 'text': text, 'at': time.time()}
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(RuntimeError('The connection to Codex dropped (%s).' % text))
            interrupted = [(t, v) for t, v in self.active.items() if t not in self.mock_threads]          # the mock's turns were never the app-server's
            for tid, turn in interrupted:
                self.failures.setdefault(tid, []).append({'t': time.time(), 'text': 'Codex stopped while this turn ran (%s)' % text, 'code': 'appServerExited', 'turn': turn})
                del self.failures[tid][:-10]
                self.active.pop(tid, None)
            gone = [(k, r) for k, r in self.requests.items() if k not in self.mock_ids and k not in self.async_ids]          # questions: an async question is the thread's, not the process's
            for k, r in gone:
                self.ended.append(dict(request_words(r, self.seen.get(k)), why='dropped' if not self.closing else 'restart'))
                self.requests.pop(k, None)
                self.seen.pop(k, None)
            self.loaded.clear()
        if self.closing:
            self.hook('requests')
            self.publish({'method': 'bridge/disconnected', 'params': {'text': text, 'restarting': False, 'stopping': True}})
            return
        self.hook('requests')
        for tid, turn in interrupted:          # the page settles each column that was running
            self.publish({'method': 'turn/completed', 'params': {'threadId': tid, 'turn': {'id': turn, 'status': 'failed', 'error': {'message': 'Codex stopped while this turn ran'}}, 'source': 'agent_ide'}})
        restart = self.next_restart()
        self.publish({'method': 'bridge/disconnected', 'params': {'text': text, 'restarting': restart is not None, 'interrupted': len(interrupted), 'ended': len(gone)}})
        if restart is None:
            self.publish({'method': 'bridge/gaveup', 'params': {'text': self.gave_up}})
        else:
            threading.Thread(target=self.restart_after, args=(restart,), daemon=True).start()

    def next_restart(self):
        """robust: seconds to wait before the next automatic start, or None when it ended too often within RESTART_WINDOW."""
        now = time.time()
        self.ends.append(now)
        recent = [t for t in self.ends if now - t < RESTART_WINDOW]
        if len(recent) > RESTART_LIMIT:
            self.restarting = False
            self.gave_up = 'Codex stopped %d times in %d s; last: %s' % (len(recent), RESTART_WINDOW, (self.dead or {}).get('text') or 'no reason given')
            self.up.set()          # whoever waits learns at once that no restart is coming
            return None
        self.restarting = True
        return RESTART_BACKOFF[min(len(recent), len(RESTART_BACKOFF)) - 1]

    def restart_after(self, delay):
        end = time.time() + delay
        while time.time() < end and not self.closing:
            time.sleep(min(0.05, max(0.0, end - time.time())))
        with self.restart_lock:
            if self.closing or self.ready:
                self.restarting = False
                self.up.set()
                return
            old = self.process
            try:
                self.start_process()
            except Exception as e:
                self.errors.append('Codex did not start again: %s' % e)
                if self.process is not old:          # a process started but did not answer: its reader settles it (terminated), and the next try or giving up comes from there
                    self.kill_quietly()
                    return
                self.dead = {'code': None, 'text': str(e), 'at': time.time()}
                d = self.next_restart()
                if d is None:
                    self.publish({'method': 'bridge/gaveup', 'params': {'text': self.gave_up}})
                else:
                    threading.Thread(target=self.restart_after, args=(d,), daemon=True).start()
                return
            self.restarting = False
        self.publish({'method': 'bridge/reconnected', 'params': {}})

    def kill_quietly(self, proc=None):
        proc = proc or self.process
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
        except OSError:
            pass

    def status(self):
        """robust: where the connection to Codex stands, for the page: up, restarting, or down with why."""
        if self.ready:
            return {'state': 'up'}
        if self.restarting:
            return {'state': 'restarting', 'text': (self.dead or {}).get('text')}
        return {'state': 'down', 'text': self.gave_up or (self.dead or {}).get('text')}

    def open_words(self):
        """robust: the requests open now (the app-server's, not the mock's), in kept words: what a restart would end."""
        with self.lock:
            return [request_words(r, self.seen.get(k)) for k, r in self.requests.items() if k not in self.mock_ids and k not in self.async_ids]

    def take_ended(self):
        with self.lock:
            out, self.ended = self.ended, []
        return out

    # ---------- answering ----------
    def reply(self, key, result):
        with self.lock:
            req = self.requests.get(str(key))
            if not req:
                raise ValueError('This request has already been resolved.')
            method = req['method']
            if not isinstance(result, dict):
                raise ValueError('A reply needs a result object')
            if method in APPROVALS:
                out = codex_decision(req, result)  # approvals (ide-approvals), piece B: one decision Codex offers here; nothing else reaches Codex
            elif str(key) in self.async_ids:
                raise ValueError('An async question is answered with a message, not a reply.')
            elif method == QUESTION:
                # approvals (ide-approvals): {answers: {<question id>: {answers: [str, ...]}}}, one entry per question asked
                out = {'answers': checked_answers(req, result)}
            elif method == PERMISSIONS:
                out = permissions_reply(req, result)          # robust: the person's answer, only what the response type offers
            elif method == ELICITATION:
                out = elicitation_reply(req, result)          # robust: the person's answer: the form's values, decline or cancel
            else:
                raise ValueError('Unsupported request type: ' + method)
            if str(key) in self.mock_ids:          # attention mock: its requests were never the app-server's; answered here, as Codex would resolve them
                self.requests.pop(str(key), None)
                self.mock_sent.append({'id': req['id'], 'result': {'answers': 'hidden'} if secret_asked(req) else result})          # robust: a secret answer is never kept
                self.publish({'method': 'serverRequest/resolved', 'params': {'threadId': req['params'].get('threadId'), 'requestId': req['id']}})
                return req
            self.send({'id': req['id'], 'result': out})
            self.requests.pop(str(key), None)
            self.seen.pop(str(key), None)
        self.hook('requests')
        return req

    def async_drop(self, key):
        """questions: an async question answered or dismissed: gone from the open requests, and the page told so."""
        with self.lock:
            req = self.requests.pop(str(key), None) if str(key) in self.async_ids else None
            self.seen.pop(str(key), None)
            self.async_ids.discard(str(key))
        if req:
            self.publish({'method': 'serverRequest/resolved', 'params': {'threadId': req['params'].get('threadId'), 'requestId': req['id']}})
            self.hook('requests')
        return req

    def mock_live(self, live):
        """attention mock (mock.py): the scenario's running turns and the requests waiting on you, as if this server drove those threads."""
        with self.lock:
            self.active.update(live.get('active') or {})
            self.mock_threads.update(live.get('active') or {})
            for q in live.get('requests') or []:
                self.requests[str(q['id'])] = q
                self.mock_ids.add(str(q['id']))

    def close(self, wait=None):
        """Stop the app-server for good: `closing` first, so its end starts no restart (terminated, restart_after, start_process
        all check it); then SIGTERM, and with `wait`, SIGKILL if it has not ended that many seconds later."""
        self.closing = True
        self.up.set()
        proc = self.process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            if wait is not None:
                try:
                    proc.wait(wait)
                except subprocess.TimeoutExpired:
                    self.kill_quietly(proc)


CODEX_MISSING = 'Codex is not installed here: install it from the Providers panel, then restart the workspace server.'


class CodexAbsent(Bridge):
    """providers panel (2026-11-21): no Codex service, because no Codex CLI was found or it did not start. The page's event
    stream, request lists and mock requests work as a Bridge's; anything that needs Codex raises one plain sentence."""

    def __init__(self, reason=CODEX_MISSING):
        self.init_state()
        self.reason = reason

    def status(self):
        return {'state': 'absent', 'text': self.reason}

    def send(self, msg):
        raise RuntimeError(self.reason)

    def call(self, method, params=None, timeout=45):
        raise RuntimeError(self.reason)

    def close(self, wait=None):
        self.closing = True


def start_codex(shared=False):
    """providers panel: the Codex bridge when a Codex CLI is found and starts, else a CodexAbsent saying why in one sentence."""
    try:
        exe = binary()
    except RuntimeError:
        return CodexAbsent()
    try:
        return Bridge([exe, 'app-server', 'proxy'] if shared else [exe, 'app-server', '--stdio'])
    except Exception as e:
        return CodexAbsent('Codex did not start (' + (str(e).strip().splitlines() or [type(e).__name__])[0][:160] + '); restart the workspace server to try again.')


def codex_path():
    try:
        return binary()
    except RuntimeError:
        return None


def failed(b, method, p):
    """attention: a stream error that ends the turn (error with willRetry false, or turn/completed carrying turn.error), kept per
    thread with its turn, so a page that opens later still sees it."""
    tid = p.get('threadId') if isinstance(p, dict) else None
    if not tid:
        return
    if method == 'error' and p.get('willRetry') is False:
        e, turn = p.get('error') or {}, p.get('turnId')
    elif method == 'turn/completed' and isinstance(p.get('turn'), dict) and p['turn'].get('error'):
        e, turn = p['turn']['error'], p['turn'].get('id')
    else:
        return
    lst = b.failures.setdefault(tid, [])
    if turn and any(x['turn'] == turn for x in lst):
        return          # the turn's error came twice (error, then turn/completed): one
    lst.append({'t': time.time(), 'text': (e.get('message') if isinstance(e, dict) else str(e)) or 'the agent hit an error', 'code': e.get('codexErrorInfo') if isinstance(e, dict) else None, 'turn': turn})
    del lst[:-10]


def err_view(e, last_act):
    """attention: one failed turn for the page. text: its first line (the floor's marks); full: the harness's whole words, for the
    chat (plan use, 2026-11-22: a refusal is shown as the provider wrote it, never cut); refused: the harness refused the request for
    its plan, quota or a usage limit (providers/planuse.py), with resetsAt when its words or its record say when that lifts."""
    from providers import planuse
    whole = str(e.get('text') or '')
    out = {'at': e['t'], 'text': first_line(whole), 'turn': e.get('turn'), 'later': last_act > e['t']}
    if whole.strip() and whole.strip() != out['text']:
        out['full'] = whole[:4000]
    if planuse.is_refusal(whole, e.get('class') or e.get('code')):
        r = e.get('resetsAt')
        r = planuse.iso_epoch(r) if isinstance(r, str) else r if isinstance(r, (int, float)) else planuse.resets_in_text(whole, e['t'])
        out.update(refused=True, resetsAt=r)
    return out


def first_line(text, n=200):
    """attention: a message's first line with text, without Markdown's marks, at most n characters."""
    for line in str(text or '').splitlines():
        line = re.sub(r'\*\*|`|^#+\s*|^[-*]\s+', '', line).strip()
        if line:
            return line[:n - 1] + '…' if len(line) > n else line
    return ''


def thread_spawn(row):
    """The spawn record Codex keeps on a subagent's thread row (source {"subagent": {"thread_spawn": {...}}}), else {}."""
    try:
        src = json.loads(row.get('source') or '')
    except (ValueError, TypeError):
        return {}
    sub = src.get('subagent') if isinstance(src, dict) else None
    spawn = sub.get('thread_spawn') if isinstance(sub, dict) else None
    return spawn if isinstance(spawn, dict) else {}


def subagent_name(meta):
    """A Codex subagent's name, from its session_meta: the task name it was spawned under (agent_path "/root/page_ui" ->
    "page ui"), else the nickname Codex gave it ("Harvey"); None for a top-level session."""
    path = str(meta.get('agent_path') or '').rstrip('/')
    task = path.rsplit('/', 1)[-1].replace('_', ' ').strip() if path else ''
    return (task if task and task != 'root' else '') or meta.get('agent_nickname') or None


def timestamp(s):
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except (ValueError, AttributeError):
        return time.time()


def writer_locked(tid):
    """Probe the native writer lock, never infer ownership from a stale lock file."""
    p = CODEX_HOME / 'thread-writer-locks' / (tid + '.lock')
    try:
        with p.open('r') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # don't attempt to steal an unknown owner
    return False


def unit(cached, msg, weight):
    """An item that entered the agent's context since the last token reading; its share is set at the next reading."""
    u = {'msg': msg, 'evs': [], 'w': max(1, weight)}
    cached['units'].append(u)
    return u


def settle(units, total):
    """Split `total` tokens over the units by text length; a unit's read/write events share its tokens."""
    weight = sum(u['w'] for u in units) or 1
    for u in units:
        tok = round(total * u['w'] / weight)
        if u['msg'] is not None:
            u['msg']['tokens'] = tok
        for e in u['evs']:
            e['tokens'] = round(tok / len(u['evs']))


def estimate(units):
    return sum(u['w'] for u in units) // 4


def compaction(cached, t):
    """A compaction marker (the rollout's `compacted` record or a ContextCompaction item; both appear for one compaction)."""
    if cached['pendingCompact'] or (not cached['context'] and not cached['units']):
        return
    settle(cached['units'], estimate(cached['units']))
    cached['pendingCompact'] = {'t': t, 'from': cached['context'] + estimate(cached['units']), 'to': None}
    cached['units'] = []


def reading(cached, t, info):
    """One token_count, written after every model call: the context that call read is its input (last_token_usage.input_tokens)."""
    if info.get('model_context_window'):
        cached['window'] = info['model_context_window']
    last = info.get('last_token_usage') or {}
    size = last.get('input_tokens') or 0
    if not size:
        return  # a zero reading follows a compaction before the next call; it says nothing about the context
    if cached.get('first') is None:
        cached['first'] = {'t': t, 'tokens': size}      # memory: the first reading: everything the first call read (base + first items)
    units, comp = cached['units'], cached['pendingCompact']
    if comp is None and size < 0.5 * cached['context']:
        comp = {'t': t, 'from': cached['context'], 'to': None}  # the context halved with no marker: a compaction all the same
    if comp is not None:
        est = min(estimate(units), size - 1)
        settle(units, est)
        comp['to'] = size - est
        cached['compacts'].append(comp)
        cached['pendingCompact'] = None
        cached['carry'] = 0
    elif not cached['context'] and cached['base'] is None:
        est = min(estimate(units), size)
        settle(units, est)
        cached['base'] = {'t': t, 'tokens': size - est}  # instructions, tools and project notes the first call already carried
    else:
        grow = size - cached['context'] + cached['carry']
        if units and grow > 0:
            settle(units, grow)
            cached['carry'] = 0
        else:
            settle(units, 0)
            cached['carry'] = grow  # a call that added nothing visible, or a small drop (reasoning not carried over): settled at the next items
    cached['context'] = size
    cached['units'] = []
    cached['contexts'].append({'t': t, 'context': size})
    cached['contexts'] = cached['contexts'][-2000:]


# chat-log: reasoning summaries ------------------------------------------------------------------------------------------
# A rollout stores each reasoning item twice: event_msg/item_completed {item: {type: Reasoning, summary_text: [..], raw_content: [..]},
# started_at_ms, completed_at_ms} and response_item {type: reasoning, summary: [{type: summary_text, text}], content, encrypted_content}.
# Both are folded into one entry per item id; items whose summary (and raw text) is empty are not kept (most are: encrypted only).
def thought(cached, rid, summary, raw, start_ms, end_ms, t, turn=None):
    parts = [s.strip() for s in summary if isinstance(s, str) and s.strip()] or [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    if not parts:
        return None
    start = start_ms / 1000 if isinstance(start_ms, (int, float)) else None
    end = end_ms / 1000 if isinstance(end_ms, (int, float)) else None
    for r in cached['reasoning'][-6:]:
        if rid and r['id'] == rid:
            if start is not None:
                r['t'] = start
            if end is not None:
                r['end'] = end
            r['turn'] = r.get('turn') or turn
            return r
    r = {'kind': 'reasoning', 'text': '\n\n'.join(parts), 't': start if start is not None else t, 'end': end if end is not None else t, 'id': rid, 'turn': turn}
    cached['reasoning'].append(r)
    return r
# end chat-log -------------------------------------------------------------------------------------------------------------


def pair_form(messages, role, text, other):
    """A rollout writes each message twice, as response_item and as event_msg/item_completed, in either order. The second
    form pairs with one recent unpaired message of the other form (True: it is that message, add nothing); a message
    said twice on purpose is two pairs, so it stays two messages."""
    for m in reversed(messages[-3:]):
        if m.get('form') == other and m['role'] == role and m['text'] == text:
            m['form'] = 'paired'
            return True
    return False


def _stat_key(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size, st.st_ino
    except OSError:
        return None


class Observer:
    """Read all local Codex sources, including external CLI/desktop/subagent runs."""
    def __init__(self):
        self.cache = {}
        self.lock = threading.RLock()

    def rows(self, limit=300):
        dbs = sorted(CODEX_HOME.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
        if not dbs:
            return []
        active_ids = [p.stem for p in (CODEX_HOME / 'thread-writer-locks').glob('*.lock') if not p.name.startswith('.')]
        # speed (3rd pass): the query is most of a state poll's Codex share; the same database files and locks give the same rows
        key = (str(dbs[0]), limit, tuple(active_ids)) + tuple(_stat_key(Path(str(dbs[0]) + s)) for s in ('', '-wal'))
        hit = getattr(self, '_rows', None)
        if hit and hit[0] == key:
            return [dict(r) for r in hit[1]]
        with sqlite3.connect(dbs[0].as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            slots = ','.join('?' for _ in active_ids) or 'NULL'
            rows = db.execute(f'SELECT * FROM threads WHERE archived=0 AND (id IN ({slots}) OR id IN (SELECT id FROM threads WHERE archived=0 ORDER BY updated_at DESC LIMIT ?)) ORDER BY updated_at DESC', [*active_ids, limit]).fetchall()
            # archive stack (2026-11-09): the threads Codex itself archived join the floor, flagged, so the page puts them in its
            # archive stack from the moment they were archived; only those whose rollout is still on disk (one whose file is gone has
            # nothing to draw). Codex keeps few (5 in one store measured on 2026-11-09). A store without `archived_at` (the mocks'
            # fixtures) is ordered by its last update
            arch = db.execute('SELECT * FROM threads WHERE archived=1 ORDER BY updated_at DESC LIMIT 50').fetchall()
        rows = [dict(r) for r in rows] + [dict(r) for r in arch if r['rollout_path'] and Path(r['rollout_path']).exists()]
        self._rows = (key, rows)
        return [dict(r) for r in rows]

    def row(self, tid):
        """One saved thread by id (speed, 2026-11-06: a direct query instead of filtering the 300-row listing)."""
        dbs = sorted(CODEX_HOME.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
        if not dbs:
            return None
        with sqlite3.connect(dbs[0].as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            r = db.execute('SELECT * FROM threads WHERE id=?', [tid]).fetchone()          # archive stack: an archived thread is still read by id
        return dict(r) if r else None

    def read_log(self, row):
        path = Path(row['rollout_path'])
        with self.lock:
            cached = self.cache.get(row['id'])
            try:
                st = path.stat()
            except OSError:
                return {'messages': [], 'events': [], 'turnActive': False, 'parent': None, 'contexts': [], 'compacts': [], 'window': None, 'context': 0, 'base': None, 'reasoning': [], 'turns': [], 'errors': [], 'subdone': {}}
            if cached and cached['size'] == st.st_size and cached['mtime'] == st.st_mtime_ns:
                return cached
            # Incremental JSONL tail: retain partial trailing lines until their writer finishes.
            if not cached or st.st_size < cached['offset'] or cached.get('inode') != st.st_ino:
                cached = {'offset': 0, 'messages': [], 'events': [], 'turnActive': False, 'parent': None, 'start': row['created_at'], 'last': row['updated_at'], 'turnId': None, 'rate': None,
                          # context sizes: one reading per model call (token_count), the items since the last reading, compactions
                          'context': 0, 'window': None, 'contexts': [], 'compacts': [], 'base': None, 'units': [], 'carry': 0, 'pendingCompact': None,
                          'reasoning': [], 'turns': [],  # chat-log: reasoning summaries (kept apart from messages: no tokens, no preview) and turn starts
                          'errors': [], 'subdone': {}}  # attention: failed turns (task_complete with error); subagent id -> when it last completed (SubAgentActivity)
                if st.st_size > 4_000_000:
                    with path.open('rb') as f:
                        try:
                            meta = json.loads(f.readline()).get('payload', {})
                            cached['parent'] = meta.get('parent_thread_id')
                            cached['agent'] = subagent_name(meta)
                            cached['meta'] = True
                        except ValueError:
                            pass
                        f.seek(st.st_size - 4_000_000)
                        f.readline()
                        cached['offset'] = f.tell()
                    cached['truncated'] = True
            with path.open('rb') as f:
                f.seek(cached['offset'])
                data = f.read()
            complete = data.rfind(b'\n') + 1
            cached['offset'] += complete
            for line in data[:complete].splitlines():
                try:
                    rec = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                p = rec.get('payload', {})
                if not isinstance(p, dict):
                    continue
                t = timestamp(rec.get('timestamp'))
                cached['last'] = t
                if rec.get('type') == 'compacted':
                    compaction(cached, t)
                    pc = cached['pendingCompact'] or (cached['compacts'][-1] if cached['compacts'] else None)      # memory: what the compaction left
                    if pc is not None:
                        if isinstance(p.get('message'), str) and p['message'].strip():
                            pc['summary'] = p['message']
                        elif any(isinstance(x, dict) and x.get('type') == 'compaction' and x.get('encrypted_content') for x in p.get('replacement_history') or []):
                            pc['encrypted'] = True      # the summary is an encrypted compaction item: not readable here
                if rec.get('type') == 'session_meta':
                    if not cached.get('meta'):      # a forked subagent's log replays its parent's history, its session_meta included:
                        cached['meta'] = True       # the first record is the session's own
                        cached['parent'] = p.get('parent_thread_id')
                        cached['start'] = timestamp(p.get('timestamp'))
                        cached['agent'] = subagent_name(p)
                if rec.get('type') == 'event_msg':
                    typ = p.get('type')
                    if typ == 'task_started':
                        cached['turnActive'] = True
                        cached['turnId'] = p.get('turn_id')
                        cached['turns'].append({'id': p.get('turn_id'), 't': t})  # chat-log
                    elif typ in ('task_complete', 'turn_aborted'):
                        cached['turnActive'] = False
                        cached['ended'] = 'stopped' if typ == 'turn_aborted' else 'finished'          # walk 2026-11-19: how its last turn ended
                        if typ == 'turn_aborted':          # a turn stopped (Codex writes it when the turn is interrupted: the person's stop)
                            cached.setdefault('stops', []).append({'t': t, 'turn': p.get('turn_id'), 'reason': p.get('reason')})
                        if typ == 'task_complete' and isinstance(p.get('error'), dict):          # attention: a failed turn
                            cached['errors'] = (cached['errors'] + [{'t': t, 'text': p['error'].get('message') or 'the turn failed', 'code': p['error'].get('codex_error_info'), 'turn': p.get('turn_id')}])[-10:]
                    elif typ == 'token_count':
                        if p.get('rate_limits'):
                            cached['rate'] = p['rate_limits']
                        if isinstance(p.get('info'), dict):
                            reading(cached, t, p['info'])
                    elif typ == 'item_completed':
                        item = p.get('item', {})
                        if item.get('type') in ('AgentMessage', 'UserMessage'):
                            content = item.get('content', [])
                            text = '\n'.join(x.get('text', '') for x in content if isinstance(x, dict)) if isinstance(content, list) else str(content)
                            role = 'user' if item['type'] == 'UserMessage' else 'assistant'
                            if text and not pair_form(cached['messages'], role, text, 'response_item'):
                                m = {'role': role, 'text': text, 't': t, 'id': item.get('id'), 'tokens': 0, 'form': 'item_completed'}
                                cached['messages'].append(m)
                                unit(cached, m, len(text))
                        elif item.get('type') == 'CommandExecution':
                            command = item.get('command', '')
                            command = command[-1] if isinstance(command, list) else command
                            output = item.get('aggregated_output', '')
                            m = {'role': 'tool', 'text': command + ('\n\n' + output if output else ''), 't': t, 'id': item.get('id'), 'tokens': 0}
                            cached['messages'].append(m)
                            u = unit(cached, m, len(m['text']))
                            for action in item.get('parsed_cmd', []):
                                if action.get('type') == 'read' and action.get('path'):
                                    fp = Path(action['path'])
                                    if not fp.is_absolute():
                                        item_cwd = item.get('cwd', row['cwd']).removeprefix('file://')
                                        fp = Path(item_cwd) / fp
                                    try:
                                        rel = str(fp.resolve().relative_to(Path(row['cwd']).resolve()))
                                        e = {'at': t, 'kind': 'read', 'path': rel, 'text': command[:200], 'item': item.get('id'), 'tokens': 0}
                                        cached['events'].append(e)
                                        u['evs'].append(e)
                                    except ValueError:
                                        pass
                        elif item.get('type') == 'FileChange':
                            changes = item.get('changes', {})
                            paths = list(changes) if isinstance(changes, dict) else [c['path'] for c in changes]
                            u = unit(cached, None, len(json.dumps(changes)))
                            for fp in paths:
                                try:
                                    rel = str(Path(fp).resolve().relative_to(Path(row['cwd']).resolve())) if Path(fp).is_absolute() else fp
                                    e = {'at': t, 'kind': 'write', 'path': rel, 'text': 'File change', 'item': item.get('id'), 'tokens': 0,
                                         'lines': change_lines(changes.get(fp)) if isinstance(changes, dict) else change_lines(next((c for c in changes if c.get('path') == fp), None))}
                                    cached['events'].append(e)
                                    u['evs'].append(e)
                                except ValueError:
                                    pass
                        elif item.get('type') == 'ContextCompaction':
                            compaction(cached, t)
                        elif item.get('type') == 'SubAgentActivity' and item.get('kind') == 'completed' and item.get('agent_thread_id'):
                            cached['subdone'][item['agent_thread_id']] = t          # attention: a subagent handed its result back to this thread
                        elif item.get('type') == 'McpToolCall':      # connectors: the call's start and end times close the event its function_call opened
                            mcp_done(cached, item, p, t)
                        elif item.get('type') == 'Reasoning':  # chat-log
                            thought(cached, item.get('id'), item.get('summary_text') or [], item.get('raw_content') or [], p.get('started_at_ms'), p.get('completed_at_ms'), t, p.get('turn_id'))
                if rec.get('type') == 'response_item':
                    typ = p.get('type')
                    if typ == 'message':
                        text = '\n'.join(x.get('text', '') for x in p.get('content', []) if isinstance(x, dict))
                        if text and p.get('role') in ('user', 'assistant') and not pair_form(cached['messages'], p['role'], text, 'item_completed'):
                            m = {'role': p['role'], 'text': text, 't': t, 'id': p.get('id'), 'tokens': 0, 'form': 'response_item'}
                            cached['messages'].append(m)
                            unit(cached, m, len(text))
                    elif typ == 'reasoning':  # chat-log
                        thought(cached, p.get('id'), [x.get('text', '') for x in p.get('summary') or [] if isinstance(x, dict)],
                                [x.get('text', '') for x in p.get('content') or [] if isinstance(x, dict)], None, None, t)
                    elif typ in ('function_call_output', 'custom_tool_call_output'):
                        plans.codex_output(cached, t, p.get('call_id'))          # plan progress: a script's plan calls made after its other tool calls
                    elif typ in ('function_call', 'custom_tool_call'):
                        name = p.get('name', '')
                        raw = p.get('arguments', p.get('input', ''))
                        try:
                            args = json.loads(raw) if isinstance(raw, str) else raw
                        except ValueError:
                            args = {}
                        plans.codex_call(cached, t, name, raw if isinstance(raw, str) else args, p.get('call_id'))          # plan progress: the model's update_plan, or code mode's script that calls it
                        if not isinstance(args, dict):
                            args = {}
                        command = args.get('cmd', args.get('command', ''))
                        patch = raw if 'patch' in name else ''
                        paths = []
                        kind = 'read'
                        evs = []
                        if patch:
                            import re
                            paths = re.findall(r'\*\*\* (?:Update|Add|Delete) File: (.+)', patch)
                            kind = 'write'
                        elif args.get('path'):
                            paths = [args['path']]
                        # Only show file associations explicitly provided by tools, never guess from prose.
                        for fp in paths:
                            try:
                                rel = str(Path(fp).resolve().relative_to(Path(row['cwd']).resolve())) if Path(fp).is_absolute() else fp
                                if rel.startswith('../'):
                                    continue
                            except ValueError:
                                continue
                            e = {'at': t, 'kind': kind, 'path': rel, 'text': name, 'item': p.get('call_id'), 'tokens': 0}
                            if patch:
                                e['lines'] = patch_lines(patch).get(fp)          # map engine: lines this patch touched in this file
                            cached['events'].append(e)
                            evs.append(e)
                        m = None
                        if name not in ('exec', 'exec_command', 'write_stdin'):
                            m = {'role': 'tool', 'text': command or name, 't': t, 'id': p.get('call_id'), 'tokens': 0}
                            cached['messages'].append(m)
                        if str(p.get('namespace') or '').startswith('mcp__'):      # connectors: an MCP call (namespace mcp__<server>, name = the tool)
                            evs.append(mcp_open(cached, m, p['namespace'][5:], name, p.get('call_id'), t))
                        if m or evs:
                            unit(cached, m, len(raw) if isinstance(raw, str) else len(json.dumps(raw)))['evs'].extend(evs)
            settle(cached['units'], estimate(cached['units']))  # items after the last reading: their text length / 4 until the next one
            cached['messages'] = cached['messages'][-500:]
            cached['events'] = cached['events'][-3000:]
            cached['reasoning'] = cached['reasoning'][-1000:]  # chat-log
            cached['turns'] = cached['turns'][-1000:]
            cached.update(size=st.st_size, mtime=st.st_mtime_ns, inode=st.st_ino)
            self.cache[row['id']] = cached
            if len(self.cache) > 500:
                self.cache.pop(next(iter(self.cache)))
            return cached

    def sessions(self, bridge):
        result = []
        for r in self.rows():
            if r.get('thread_source') == 'guardian_review' or 'guardian' in r.get('source', ''):
                continue
            log = self.read_log(r)
            managed = r['id'] in bridge.loaded
            locked = writer_locked(r['id'])
            running = r['id'] in bridge.active or (locked and log['turnActive'])
            spawn = thread_spawn(r)
            result.append({'id': r['id'], 'title': r.get('name') or r['title'] or log.get('agent') or subagent_name(spawn) or 'Untitled agent', 'cwd': r['cwd'], 'projectId': r.get('project_id'), 'source': r.get('originator') or r['source'], 'model': r.get('model'), 'updatedAt': log.get('last', r['updated_at']), 'createdAt': log.get('start', r['created_at']), 'parent': log.get('parent') or spawn.get('parent_thread_id'), 'status': 'active' if running else 'idle', 'managed': managed, 'external': locked and not managed, 'turnId': bridge.active.get(r['id']) or log.get('turnId'), 'preview': (log['messages'][-1]['text'][:180] if log['messages'] else r.get('preview', '')),
                           **({'archivedAt': r.get('archived_at') or r['updated_at']} if r.get('archived') else {})})          # archive stack: archived by Codex, when
        return result


def run_git(cwd, *args):
    r = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, timeout=15)
    if r.returncode:
        raise ValueError(r.stderr.decode(errors='replace').strip() or 'This folder is not a Git repository.')
    return r.stdout.decode(errors='replace')


def project_path(value):
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise ValueError('Choose an absolute folder path.')
    p = p.resolve()
    if not p.is_dir():
        raise ValueError('Folder does not exist.')
    return p


def file_path(cwd, relative):
    root = project_path(cwd)
    p = (root / relative).resolve()
    if not p.is_relative_to(root) or '.git' in p.relative_to(root).parts:
        raise ValueError('Choose a file inside the project.')
    return p


SCAN_TTL, UNTRACKED_TTL = 3.0, 15.0
_scans, _untracked, _scan_locks, _scans_lock = {}, {}, {}, threading.Lock()


def dir_marks(root, paths, ignores=()):
    """speed (2nd pass): what a new or removed untracked file would change: the modification time of every folder that holds a
    listed file (and of the folders between it and the root), of the folders directly inside those (so a new file in an empty
    folder, or in one holding only ignored files, counts too), and of the ignore files. `.git` is left out (git itself writes
    there on every status). Returns {path: mtime_ns}; a folder that cannot be read counts as changed."""
    dirs = {''}
    for p in paths:
        i = p.rfind('/')
        while i > 0:
            d = p[:i]
            if d in dirs:
                break
            dirs.add(d)
            i = d.rfind('/')
    for d in list(dirs):
        try:
            with os.scandir(os.path.join(root, d)) as it:
                for e in it:
                    if e.name != '.git' and e.is_dir(follow_symlinks=False):
                        dirs.add(d + '/' + e.name if d else e.name)
        except OSError:
            pass
    return {d: mtime_ns(os.path.join(root, d)) for d in dirs} | {f: mtime_ns(os.path.join(root, f)) for f in ignores}


def mtime_ns(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def marks_moved(root, marks):
    return any(mtime_ns(os.path.join(root, p)) != m for p, m in marks.items())


def git_scan(root, writes=()):
    """speed (2026-11-06): the working tree is walked once for both the floor's file list and the change tints, and the result is
    shared for SCAN_TTL seconds, so the canvas and the status poll (sent together) cost one walk instead of three. Tracked files
    come from the index (no walk), changes to them from `git status` without untracked files, limited to the folder. Raises
    ValueError outside a Git repository.
    speed (2nd pass): the search for untracked files is most of git's time (0.4-0.7 s of CPU on this repository, against 0.3 s for
    the tracked changes), so it runs again only when something it could find has moved: a folder that holds a listed file, a
    folder directly inside one, or an ignore file has a new modification time (a file made, removed or renamed in it), or an
    agent wrote, after the last search, a file the list does not have yet (writes: (time, path) pairs, paths relative to the
    folder). Otherwise at the latest every UNTRACKED_TTL seconds (a new file two levels down inside a folder holding only
    ignored files is the case only that catches). A new file still shows on the next poll, as before."""
    key = str(root)
    with _scans_lock:
        lock = _scan_locks.setdefault(key, threading.Lock())
    with lock:
        now = time.monotonic()
        hit, un = _scans.get(key), _untracked.get(key)
        if hit and hit[2] is not None and now - hit[0] < SCAN_TTL:
            return hit[2]          # within SCAN_TTL of the last poll: the same answer, as before
        fresh_u = bool(un and hit and now - un[0] < UNTRACKED_TTL and not any(at > un[1] and p not in un[3] and p not in hit[1]['known'] for at, p in writes)
                       and not marks_moved(root, un[4]))
        # the tracked part: every SCAN_TTL seconds
        prefix = run_git(root, 'rev-parse', '--show-prefix').strip()
        tracked = [p for p in run_git(root, 'ls-files', '-c', '-z').split('\0') if p]
        fields = run_git(root, 'status', '--porcelain', '-z', '--untracked-files=no', '--', '.').split('\0')
        lines, i = [], 0
        while i < len(fields):
            f = fields[i]
            i += 1
            if len(f) < 4:
                continue
            xy, path = f[:2], f[3:]
            rel = path[len(prefix):] if prefix and path.startswith(prefix) else path      # porcelain paths are from the repository root
            if xy[0] in 'RC':                      # a rename or copy: the next field is where it came from
                orig = fields[i] if i < len(fields) else ''
                i += 1
                o = orig[len(prefix):] if prefix and orig.startswith(prefix) else orig
                lines.append(xy + ' ' + o + ' -> ' + rel)
            else:
                lines.append(xy + ' ' + rel)
        hit = (None, {'tracked': tracked, 'lines': lines, 'known': set(tracked)}, None)
        if not fresh_u:
            wall = time.time()
            ignores = [p for p in hit[1]['tracked'] if p == '.gitignore' or p.endswith('/.gitignore')]
            try:
                ignores.append(os.path.join(root, run_git(root, 'rev-parse', '--git-path', 'info/exclude').strip()))      # relative to the folder, or absolute
            except ValueError:
                pass
            marks = dir_marks(root, hit[1]['tracked'] + (un[2] if un else []), ignores)          # before the search: a file made during it moves a mark
            untracked = [p for p in run_git(root, 'ls-files', '-o', '--exclude-standard', '-z').split('\0') if p]      # relative to the folder
            marks |= {d: m for d, m in dir_marks(root, untracked).items() if d not in marks}          # folders that only now hold a file
            un = (time.monotonic(), wall, untracked, set(untracked), marks)          # counted from the end, as the scan's time is
            _untracked[key] = un
        lines = hit[1]['lines'] + ['?? ' + p for p in un[2]]
        result = {'tracked': hit[1]['tracked'], 'untracked': un[2], 'status': '\n'.join(lines) + ('\n' if lines else '')}
        _scans[key] = (time.monotonic(), hit[1], result)          # from the end: a scan slower than SCAN_TTL is still shared
        return result


HISTORY_KEEP = 4
_hist_sent, _hist_lock = {}, threading.Lock()


def history_since(sid, result, since=None):
    """speed (2nd pass): a session's history was sent whole on every change (100-170 KB for an active one). Each reply now
    carries a version; the server keeps what it sent under the last few versions (a short hash per message), and a request that
    names one of them gets only the messages from the first one that differs (`from` says where they start: the page keeps its
    own up to there). Any message that changed, wherever it is, is sent again; an unknown version gets everything."""
    hs = [hashlib.blake2b(json.dumps(m, sort_keys=True, separators=(',', ':')).encode(), digest_size=8).digest() for m in result['messages']]
    ver = hashlib.blake2b(b''.join(hs) + str(len(hs)).encode(), digest_size=8).hexdigest()
    with _hist_lock:
        sent = _hist_sent.setdefault(sid, collections.OrderedDict())
        old = sent.get(since) if since else None
        sent[ver] = hs
        sent.move_to_end(ver)
        while len(sent) > HISTORY_KEEP:
            sent.popitem(last=False)
    result['version'] = ver
    if old is not None:
        i, n = 0, min(len(old), len(hs))
        while i < n and old[i] == hs[i]:
            i += 1
        result['from'] = i
        result['messages'] = result['messages'][i:]
    return result


def files_in(cwd, writes=()):
    root = project_path(cwd)
    try:
        scan = git_scan(root, writes)
        paths = sorted(set(scan['tracked']) | set(scan['untracked']))
    except ValueError:
        return folder_files(root)
    return {'files': paths[:FILES_CAP], 'truncated': len(paths) > FILES_CAP}


# ---------- floor (2026-11-21): a folder outside Git that holds repositories and worktrees ----------
# A folder was opened that is not itself a repository: two repositories in it and a folder of 135 linked worktrees (each a
# full checkout). The walk used to take its files in name order and stop at the first 2000, so the first repository took them all,
# the worktrees never came, and every agent working in one stood in the floor's own lane. Now:
# - a linked worktree (a folder whose `.git` is a file naming `<repository>/.git/worktrees/<name>`) is left off the floor: the Git
#   tree beside the floor shows it as its repository's lane (git_route, nested_repos);
# - a repository inside the folder (a `.git` folder) is listed by Git, so what it ignores stays off the floor, as in a repository
#   project;
# - the cap is shared out between the folder's own files and each of its top-level entries (each gets an equal share, and what a
#   small one leaves goes to the others), so one large entry no longer crowds out the rest.
FILES_CAP = 2000
FAMILY_CAP = 120          # sessions on one floor at most once the 30 newest bring their families along (judgement: the made-up floor's own top, 120)
WALK_SKIP = ('node_modules', '__pycache__', 'venv', 'dist')
NEST_DEPTH, NEST_MAX = 3, 12          # repositories are looked for this many levels down, at most this many (the writer's judgement)


def git_kind(folder):
    """'repo' (a `.git` folder), 'worktree' (a `.git` file: a linked worktree or a submodule), or None."""
    g = os.path.join(folder, '.git')
    return 'repo' if os.path.isdir(g) else 'worktree' if os.path.isfile(g) else None


def walk_files(root, rel, limit):
    """The files under root/rel (rel '' : the folder's own files only), relative to root, in name order, at most a little over limit."""
    out = []
    if not rel:
        with os.scandir(root) as it:
            return sorted(e.name for e in it if e.is_file() and not e.name.startswith('.'))
    top = os.path.join(root, rel)
    for base, dirs, files in os.walk(top):
        kind = git_kind(base)
        if kind == 'worktree':
            dirs[:] = []
            continue
        if kind == 'repo':
            dirs[:] = []
            r = os.path.relpath(base, root)
            try:
                scan = git_scan(Path(base))
                out.extend(r + '/' + p for p in sorted(set(scan['tracked']) | set(scan['untracked']))[:limit])
            except ValueError:
                pass
        else:
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in WALK_SKIP)
            out.extend(os.path.relpath(os.path.join(base, f), root) for f in sorted(files) if not f.startswith('.'))
        if len(out) > limit:
            break
    return out


def share_cap(groups, cap):
    """{name: [paths]} -> how many of each to keep: equal shares, what a small group leaves going to the larger ones."""
    keep, left, todo = {}, cap, sorted(groups, key=lambda k: (len(groups[k]), k))
    while todo:
        share = left // len(todo)
        if len(groups[todo[0]]) <= share:
            k = todo.pop(0)
            keep[k] = len(groups[k])
            left -= keep[k]
            continue
        for i, k in enumerate(sorted(todo)):
            keep[k] = share + (1 if i < left - share * len(todo) else 0)
        break
    return keep


def folder_files(root):
    """A folder outside Git: its files (see above), at most FILES_CAP shared out between its top-level entries."""
    root = str(root)
    with os.scandir(root) as it:
        tops = sorted(e.name for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith('.') and e.name not in WALK_SKIP)
    groups = {'': walk_files(root, '', FILES_CAP)}
    for name in tops:
        groups[name] = walk_files(root, name, FILES_CAP)
    total = sum(len(v) for v in groups.values())
    if total <= FILES_CAP:
        return {'files': sorted(p for v in groups.values() for p in v), 'truncated': False}
    keep = share_cap(groups, FILES_CAP)
    return {'files': sorted(p for k, v in groups.items() for p in v[:keep[k]]), 'truncated': True}


def nested_repos(root):
    """A folder that is not a repository: the repositories it holds (a `.git` folder) and those whose linked worktrees it holds,
    by their top folders: those inside the folder first, in name order, then any outside it. Dot folders and WALK_SKIP are not
    searched, nor a repository's or worktree's own folders; NEST_DEPTH levels down, at most NEST_MAX."""
    root = str(root)
    inside, outside = [], []

    def look(folder, depth):
        try:
            with os.scandir(folder) as it:
                subs = sorted(e.path for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith('.') and e.name not in WALK_SKIP)
        except OSError:
            return
        for sub in subs:
            kind = git_kind(sub)
            if kind == 'repo':
                if sub not in inside:
                    inside.append(sub)
            elif kind == 'worktree':
                try:
                    with open(os.path.join(sub, '.git')) as f:
                        line = f.read(4096).strip()
                except OSError:
                    continue
                gd = line[len('gitdir:'):].strip() if line.startswith('gitdir:') else ''
                if not os.path.isabs(gd):
                    gd = os.path.normpath(os.path.join(sub, gd))
                if '/.git/worktrees/' in gd:          # a linked worktree (a submodule's `.git` file names .git/modules/)
                    top = gd.split('/.git/worktrees/')[0]
                    (inside if top == root or top.startswith(root + '/') else outside).append(top)
            elif depth > 1:
                look(sub, depth - 1)
    look(root, NEST_DEPTH)
    out = []
    for top in sorted(set(inside)) + sorted(set(outside) - set(inside)):
        if os.path.isdir(os.path.join(top, '.git')) and top not in out:
            out.append(top)
    return out[:NEST_MAX]


# ---------- floor grows (branch ide-floor-grows, 2026-11-07): when each file the agents made came onto the floor ----------
# The one rule, kept here (the page only compares a page's birth with its moment). A file is NEW during the floor's history when
#   - in a Git folder: it is untracked, or not in the tree of the last commit made before the floor's origin (added to Git since,
#     or staged and not committed yet); in a folder outside Git: any file (only the disk can tell, below);
#   - and the disk does not say it was there before the origin (its creation time, where the system records one: macOS's
#     st_birthtime; elsewhere this check is skipped).
# A new file's birth is the earliest of its first write event (a file change or an apply_patch Add/Update) and its creation time
# on disk, in seconds from the origin. A file created by a shell command (cat > file) has no write event; the creation time
# catches it. A new file with neither known (outside macOS, written only by a shell command) keeps no birth: always shown.
# Outside Git a file needs a creation time to count as new (an edit is all a write event could mean there).
_before_trees, _before_lock = {}, threading.Lock()


def tree_before(root, origin):
    """The paths (relative to the folder) in the last commit made before the origin, or an empty set when there is none (a
    repository whose first commit came later, or none yet). Cached by that commit: one ls-tree per commit, not per poll."""
    bkey = (str(root), int(origin))
    with _before_lock:
        b = _before_trees.get(bkey)
    if b and time.monotonic() - b[0] < SCAN_TTL:
        base = b[1]          # within SCAN_TTL of the last poll, as the file list
    else:
        try:
            base = run_git(root, 'rev-list', '-1', '--before=%d' % (int(origin) - 1), 'HEAD').strip()          # Git's times are whole seconds: a commit in the origin's own second counts as the run's
        except ValueError:
            base = ''          # no commit yet
        with _before_lock:
            _before_trees[bkey] = (time.monotonic(), base)
    if not base:
        return set()
    key = (str(root), base)
    with _before_lock:
        hit = _before_trees.get(key)
    if hit is None:
        hit = {p for p in run_git(root, 'ls-tree', '-r', '--name-only', '-z', base).split('\0') if p}
        with _before_lock:
            if len(_before_trees) > 32:
                _before_trees.clear()
            _before_trees[key] = hit
    return hit


def births(cwd, origin, writes, paths):
    """{path: seconds from the origin} for the files the floor's history created (the rule above); writes: (seconds from the
    origin, path) pairs, paths relative to the folder."""
    root = project_path(cwd)
    first = {}
    for t, p in writes:
        if p and (p not in first or t < first[p]):
            first[p] = t
    try:
        scan = git_scan(root)
        tracked, cand = set(scan['tracked']), None
        before = tree_before(root, origin)
        cand = [p for p in paths if p not in tracked or p not in before]
        in_git = True
    except ValueError:
        cand, in_git = list(paths), False
    out = {}
    for p in cand:
        try:
            made = getattr(os.stat(root / p), 'st_birthtime', None)
        except OSError:
            made = None
        made = None if made is None else made - origin
        if made is not None and made < 0:
            continue          # on disk before the floor's first moment
        if not in_git and made is None:
            continue
        known = [x for x in (first.get(p), made) if x is not None]
        if known:
            out[p] = max(0.0, round(min(known), 3))
    return out


# ---------- git: branches, worktrees and commits for the trunk beside the floor (read-only git commands; see gaps.md) ----------
# Exact: the current branch, the branch list, worktrees (path -> branch), ahead/behind and changed-file counts against the
# default branch, each commit's hash, subject, author, time and parents. Derived from subjects: which branch a merge commit
# closed (its parents say it is a merge; only its subject names the branch).
GIT_TTL = 10.0
GIT_CACHE = {}          # cwd -> (time, status signature, data); dropped when /api/diff sees the status change
GIT_LANES = 3           # git labels (2026-11-06): lanes drawn: the three most recently modified branches, plus this floor's
MERGE_RX = [re.compile(r"^Merge branch '([^']+)'"), re.compile(r"\(branch ([^)\s]+)\)\s*$"), re.compile(r"^Merge ([\w./-]+):"),
            re.compile(r"^Merge (?:remote-tracking )?branch ([\w./-]+)")]
LOG_FMT = '%h%x1f%H%x1f%s%x1f%an%x1f%at%x1f%P%x1e'


def merged_name(subject):
    """The branch a merge commit's subject names, or None (the parents say it is a merge; the name is only in the subject)."""
    for rx in MERGE_RX:
        m = rx.search(subject or '')
        if m:
            return m.group(1)
    return None


def git_log(cwd, *args):
    out = []
    for rec in run_git(cwd, 'log', '--format=' + LOG_FMT, *args).split('\x1e'):
        f = rec.strip('\n').split('\x1f')
        if len(f) < 6:
            continue
        parents = f[5].split()
        c = {'hash': f[0], 'full': f[1], 'subject': f[2], 'author': f[3], 'time': int(f[4] or 0), 'parents': len(parents)}
        if len(parents) > 1:
            c['merged'] = merged_name(f[2])
            c['mergedFull'] = parents[1]
        out.append(c)
    return out


def default_branch(cwd, names):
    try:
        ref = run_git(cwd, 'symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD').strip()
        if ref.split('/', 1)[-1] in names:
            return ref.split('/', 1)[-1]
    except ValueError:
        pass
    return next((n for n in ('main', 'master', 'trunk') if n in names), names[0] if names else None)


def worktrees(cwd):
    out, cur = [], {}
    for line in run_git(cwd, 'worktree', 'list', '--porcelain').splitlines() + ['']:
        if not line.strip():
            if cur.get('path'):
                out.append(cur)
            cur = {}
        elif line.startswith('worktree '):
            cur = {'path': line[9:], 'branch': None, 'head': None}
        elif line.startswith('branch '):
            cur['branch'] = line[7:].removeprefix('refs/heads/')
        elif line.startswith('HEAD '):
            cur['head'] = line[5:12]
        elif line.strip() == 'detached':
            cur['detached'] = True
    return out


def git_status_sig(cwd):
    """What /api/git's answer depends on: HEAD, every branch tip and the worktree list (uncommitted edits are /api/diff's)."""
    try:
        return hashlib.sha1((run_git(cwd, 'rev-parse', 'HEAD') + run_git(cwd, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads')
                             + run_git(cwd, 'worktree', 'list', '--porcelain')).encode()).hexdigest()
    except ValueError:
        return None


def git_repo(cwd):
    """Whether the folder is inside a Git work tree."""
    try:
        return run_git(str(project_path(cwd)), 'rev-parse', '--is-inside-work-tree').strip() == 'true'
    except ValueError:
        return False


def git_invalidate(cwd, sig):
    """The Git poll (/api/diff): a moved HEAD, branch tip or worktree drops the cached /api/git answer for that folder."""
    c = GIT_CACHE.get(str(cwd))
    if c and c[1] != sig:
        GIT_CACHE.pop(str(cwd), None)


def git_data(cwd, lanes=GIT_LANES):
    cwd = str(project_path(cwd))
    c = GIT_CACHE.get(cwd)
    if c and time.time() - c[0] < GIT_TTL:
        return c[2]
    top = run_git(cwd, 'rev-parse', '--show-toplevel').strip()
    try:
        current = run_git(cwd, 'symbolic-ref', '--quiet', '--short', 'HEAD').strip()
    except ValueError:
        current = None          # detached HEAD
    refs = []
    for line in run_git(cwd, 'for-each-ref', '--sort=-committerdate', '--format=%(refname:short)%1f%(committerdate:unix)%1f%(objectname:short)', 'refs/heads').splitlines():
        f = line.split('\x1f')
        if len(f) == 3:
            refs.append({'name': f[0], 'time': int(f[1] or 0), 'head': f[2]})
    names = [r['name'] for r in refs]
    base = default_branch(cwd, names)
    wts = worktrees(cwd)
    wt_of = {w['branch']: w['path'] for w in wts if w.get('branch')}
    # git labels (2026-11-06): the three most recently modified branches, newest first (it stands closest to the floor); the
    # branch this floor is on always shows, in addition when it is not among them, still in its place by time; the rest fold into "+N more"
    others = [n for n in names if n != base]          # refs are newest first (for-each-ref --sort=-committerdate)
    shown = others[:lanes]
    if current and current != base and current not in shown:
        shown = [n for n in others if n in shown or n == current]
    def detail(b):          # one branch's counts and commits (run side by side: each is a handful of git calls)
        r_name = b['name']
        if r_name == base:
            b['count'] = int(run_git(cwd, 'rev-list', '--count', '--first-parent', base).strip() or 0)
            b['commits'] = git_log(cwd, '-8', '--first-parent', base)
        else:
            behind, ahead = (int(x) for x in run_git(cwd, 'rev-list', '--left-right', '--count', base + '...' + r_name).split())
            b.update(ahead=ahead, behind=behind)
            b['files'] = len([p for p in run_git(cwd, 'diff', '--no-renames', '--name-only', base + '...' + r_name).splitlines() if p])
            b['commits'] = git_log(cwd, '-' + str(8 if b['current'] else 5), '--first-parent', base + '..' + r_name) if ahead else []
        for m in b['commits']:          # a merge: what it brought in (exact), up to five of those commits for an unfolded bead
            if m.get('mergedFull'):
                rng = m['full'] + '^1..' + m['mergedFull']
                m['mergedCount'] = int(run_git(cwd, 'rev-list', '--count', rng).strip() or 0)
                m['mergedCommits'] = git_log(cwd, '-5', rng)
                for x in m['mergedCommits']:
                    x.pop('mergedFull', None)
            m.pop('mergedFull', None)
        return b
    branches = [{'name': r['name'], 'time': r['time'], 'head': r['head'], 'worktree': wt_of.get(r['name']), 'current': r['name'] == current,
                 'base': r['name'] == base, 'shown': r['name'] in shown or r['name'] == base} for r in refs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(detail, [b for b in branches if b['shown'] and base]))
    data = {'root': top, 'cwd': cwd, 'current': current, 'base': base, 'branches': branches, 'worktrees': wts,
            'shown': [base] + shown if base else shown, 'more': max(0, len(names) - len(shown) - (1 if base else 0)), 'at': time.time(),
            'derived': 'merged: the branch a merge commit closed, read from its subject'}
    GIT_CACHE[cwd] = (time.time(), git_status_sig(cwd), data)
    return data


def git_tree(cwd, branch):
    """A branch with no worktree: its files from `git ls-tree` (names only; nothing is read from it)."""
    cwd = str(project_path(cwd))
    if branch not in run_git(cwd, 'for-each-ref', '--format=%(refname:short)', 'refs/heads').split():
        raise ValueError('Unknown branch')
    paths = [p for p in run_git(cwd, 'ls-tree', '-r', '--name-only', '-z', 'refs/heads/' + branch).split('\0') if p]
    return {'branch': branch, 'files': paths[:2000], 'truncated': len(paths) > 2000}
# ---------- end git ----------


# ---------- memory: what a session keeps besides its context (the memory-c concept; see gaps.md) ----------
# base: the context before the first call (instructions, tools, prompt) and the first token reading; compactions with the
# summary they left where the harness keeps it as text; the instruction files the harness loads for the session's folder
# (its own discovery rules, harnesses.md); notes: .md/.txt files the session wrote and later read back itself.
NOTE_RX = re.compile(r'\.(md|txt)$', re.I)
INSTR_RX = re.compile(r'(^|/)(AGENTS(\.override)?\.md|CLAUDE(\.local)?\.md)$')
SUMMARY_PREFACE = re.compile(r'^(This session is being continued|The summary below|The summarized conversation included|<tools-used-before-summary>|Summary:\s*$|Analysis:\s*$)')


def summary_first(text):
    """A summary's first line worth reading: Claude Code's preface lines and list numbers are skipped."""
    for line in str(text or '').splitlines():
        line = line.strip()
        if not line or SUMMARY_PREFACE.match(line) or line.startswith('<'):
            continue
        heading = bool(re.match(r'^(\d+\.|#+)\s', line)) or bool(len(line) < 60 and re.match(r'^(\*\*|__)[^*_]+(\*\*|__):?$', line))          # a short all-bold line ("**Summary**") is a heading too
        line = re.sub(r'^(\d+\.|[-*#]+)\s*', '', line).strip('* ').strip()
        if (line.endswith(':') or heading) and len(line) < 60:
            continue          # a section heading ("Primary Request and Intent:"): its first line of content is the one to show
        if line:
            return line[:160]
    return None


def instr_entry(path, how, scope=None):
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        return None
    return {'path': str(p), 'how': how, 'scope': scope, 'bytes': len(data), 'lines': data.decode(errors='replace').count('\n') + (0 if data.endswith(b'\n') or not data else 1)}


def codex_instructions(cwd, home):
    """Codex: the global AGENTS.override.md or AGENTS.md in the Codex home, then from the Git root down to the cwd one file per
    folder (AGENTS.override.md wins over AGENTS.md), joined root first and cut at project_doc_max_bytes (32 KiB) in total."""
    out = []
    g = next((home / n for n in ('AGENTS.override.md', 'AGENTS.md') if (home / n).is_file()), None)
    if g:
        out.append(instr_entry(g, 'global'))
    c = Path(cwd)
    try:
        root = Path(run_git(c, 'rev-parse', '--show-toplevel').strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        root = c
    chain = [c]          # the cwd and its parents up to the Git root (outside a repository, the cwd alone)
    if root in c.parents:
        chain += [q for q in c.parents if q == root or root in q.parents]
    budget = 32768
    for d in reversed(chain):
        f = next((d / n for n in ('AGENTS.override.md', 'AGENTS.md') if (d / n).is_file()), None)
        if f:
            e = instr_entry(f, 'project', str(d))
            if e:
                e['cut'] = e['bytes'] > budget
                budget = max(0, budget - e['bytes'])
                out.append(e)
    return [e for e in out if e]


def claude_instructions(cwd, home):
    """Claude Code (harnesses.md; partly from memory of its docs): ~/.claude/CLAUDE.md, CLAUDE.md and CLAUDE.local.md in the cwd
    and every parent, .claude/rules/*.md, the project's auto-memory MEMORY.md (seen loaded in this repository's own sessions);
    .claude/settings*.json are settings, not prompt text."""
    out = [instr_entry(home / 'CLAUDE.md', 'global')]
    c = Path(cwd)
    for d in reversed([c] + list(c.parents)):
        for n in ('CLAUDE.md', 'CLAUDE.local.md'):
            out.append(instr_entry(d / n, 'project', str(d)))
    rules = c / '.claude' / 'rules'
    for f in sorted(rules.glob('*.md')) if rules.is_dir() else []:
        out.append(instr_entry(f, 'rules', str(c)))
    from providers.claude_code import encode
    out.append(instr_entry(home / 'projects' / encode(cwd) / 'memory' / 'MEMORY.md', 'memory', str(c)))
    for n in ('settings.json', 'settings.local.json'):
        out.append(instr_entry(c / '.claude' / n, 'settings', str(c)))
    return [e for e in out if e]


def notes_of(log):
    """Files the session wrote and later read back itself (.md or .txt, not an instruction file), known from the first read-back.
    The rule also catches deliverables; files the session's last message names are kept apart as outputs."""
    by = {}
    for e in log.get('events', []):
        path = e.get('path')
        if not path or e.get('kind') not in ('read', 'write') or not NOTE_RX.search(path) or INSTR_RX.search(path):
            continue
        n = by.setdefault(path, {'path': path, 'writes': [], 'reads': []})
        if e['kind'] == 'write':
            n['writes'].append(e['at'])
        elif n['writes'] and e['at'] > n['writes'][0]:
            n['reads'].append(e['at'])
    last = next((m['text'] for m in reversed(log.get('messages', [])) if m.get('role') == 'assistant'), '')
    notes, outputs = [], []
    for n in by.values():
        if not n['reads']:
            continue
        (outputs if last and (n['path'] in last or ('/' in n['path'] and Path(n['path']).name in last)) else notes).append(n)
    return notes, outputs


def history_compacts(log):
    """/api/history's compactions: each with the summary's first line (and the summary where the harness kept its text)."""
    return [dict(c, first=summary_first(c.get('summary'))) for c in log.get('compacts', [])]


def memory_of(ws, sid):
    """GET /api/memory?id=: the memory sheet's data for one session of any provider."""
    p = ws.provider_of(sid)
    if p is ws.providers[0]:
        row = ws.session(sid)
        if not row:
            raise ValueError('Unknown agent')
        log = ws.observer.read_log(row) if row.get('rollout_path') else ws.observer.cache.get(sid, {})
        cwd, home, find = row.get('cwd'), CODEX_HOME, codex_instructions
    else:
        log = p.read_history(sid)
        cwd, home, find = log.get('cwd'), p.home, claude_instructions
    notes, outputs = notes_of(log)
    comps = [{'t': c['t'], 'from': c.get('from'), 'to': c.get('to'), 'summary': c.get('summary'), 'first': summary_first(c.get('summary')),
              'encrypted': bool(c.get('encrypted'))} for c in log.get('compacts', [])]
    return {'id': sid, 'provider': p.name, 'cwd': cwd, 'base': log.get('base'), 'first': log.get('first'), 'truncated': bool(log.get('truncated')),
            'compacts': comps, 'instructions': find(cwd, home) if cwd else [], 'notes': notes, 'outputs': outputs}
# ---------- end memory ----------


def no_windows():
    """True when this server must put nothing on the screen (no folder chooser, no app opened): a mock scenario
    (AGENT_IDE_MOCK) or AGENT_IDE_NO_WINDOWS=1, which every server started for a drive run or a live check sets."""
    return bool(MOCK_ON or os.environ.get('AGENT_IDE_MOCK') or os.environ.get('AGENT_IDE_NO_WINDOWS'))


def pick_folder():
    """The Mac's own folder chooser, run on the server's machine (a browser page cannot learn a picked folder's absolute path).
    Under no_windows() no process starts: a cancelled choice, or the path in AGENT_IDE_PICK_FOLDER when a run needs one."""
    if no_windows():
        given = os.environ.get('AGENT_IDE_PICK_FOLDER')
        return {'path': given.rstrip('/') or '/'} if given else {'cancelled': True, 'noWindows': True}
    try:
        r = subprocess.run(['osascript', '-e', 'POSIX path of (choose folder with prompt "Open a project")'], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {'cancelled': True, 'timedOut': True}
    except FileNotFoundError:
        raise ValueError('The folder chooser needs macOS (osascript).')
    if r.returncode:
        return {'cancelled': True}  # Cancel pressed: osascript exits non-zero ("User canceled")
    return {'path': r.stdout.strip().rstrip('/') or '/'}


# ---------------- chat controls (branch ide-chat-controls): model / effort / approval mode, commands, status, MCP, web search ----------------
# Approval modes as Codex's own /permissions picker names them (labels and descriptions read from the Codex CLI 0.166 binary's
# strings): each is an approval policy plus a sandbox mode.
PRESETS = [
    {'id': 'read-only', 'label': 'Read Only', 'approvalPolicy': 'on-request', 'sandbox': 'read-only',
     'description': 'Codex can read files in the current workspace. Approval is required to edit files or access the internet.'},
    {'id': 'default', 'label': 'Default', 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write', 'default': True,
     'description': 'Codex can read and edit files in the current workspace, and run commands. Approval is required to access the internet or edit other files.'},
    {'id': 'full-access', 'label': 'Full Access', 'approvalPolicy': 'never', 'sandbox': 'danger-full-access',
     'description': 'Codex can edit files outside this workspace and access the internet without asking for approval. Exercise caution when using.'},
]
APPROVAL_POLICIES = ('untrusted', 'on-request', 'never')
SANDBOX_MODES = ('read-only', 'workspace-write', 'danger-full-access')
# plan toggle (2026-11-09): "plan" on or off, chosen when a new agent is started and kept for the whole session.
# Codex: the plan checklist tool, config tools.update_plan.enabled for this thread only (a thread/start config override, passed
# again on thread/resume); gpt-6-luna on codex-cli 0.167 keeps no checklist without it. Claude Code: the session starts in
# permission mode "plan" (reads and proposes a plan, edits nothing until the plan is approved). Default on for Codex, so plans
# show on the floor; off for Claude Code, as before. A start request without `plan` gets its harness's default.
PLAN_DEFAULT = {'codex': True, 'claude': False}
PLAN_CONFIG = 'tools.update_plan.enabled'


def plan_of(p, harness):
    v = p.get('plan')
    if v is None:
        return PLAN_DEFAULT[harness]
    if not isinstance(v, bool):
        raise ValueError('Plan is either on or off')
    return v
# Codex's slash commands are a TUI feature: the app-server protocol has no method that lists them. This table is the subset that
# has a meaning here, with the TUI's own one-line descriptions (Codex CLI 0.166 binary strings). `how` says what this client does:
#   api: a protocol method · text: sent to the agent as an ordinary message · local: handled on the page with server data
COMMANDS = [
    {'name': 'compact', 'description': 'summarize conversation to prevent hitting the context limit', 'how': 'api', 'method': 'thread/compact/start'},
    {'name': 'status', 'description': 'show current session configuration and token usage', 'how': 'local', 'method': 'GET /api/status'},
    {'name': 'review', 'description': 'review my current changes and find issues', 'how': 'api', 'method': 'review/start', 'param': 'what to review (empty: uncommitted changes)'},
    {'name': 'diff', 'description': 'show git diff (including untracked files)', 'how': 'local', 'method': 'GET /api/diff'},
    {'name': 'init', 'description': 'create an AGENTS.md file with instructions for Codex', 'how': 'text'},
    {'name': 'mention', 'description': 'mention a file', 'how': 'text', 'param': 'file path, then your message'},
    {'name': 'new', 'description': 'start a new chat during a conversation', 'how': 'api', 'method': 'thread/start', 'param': 'first message (optional)'},
    {'name': 'model', 'description': 'choose what model and reasoning effort to use', 'how': 'local', 'method': 'the model and effort selects (turn/start overrides)'},
    {'name': 'permissions', 'description': 'choose what Codex is allowed to do', 'how': 'local', 'method': 'the approval-mode select (turn/start overrides)'},
    {'name': 'mcp', 'description': 'list MCP tools', 'how': 'local', 'method': 'mcpServerStatus/list'},
    {'name': 'rename', 'description': 'rename the current thread', 'how': 'api', 'method': 'thread/name/set', 'param': 'new name'},
    {'name': 'fork', 'description': 'fork the current chat', 'how': 'api', 'method': 'thread/fork'},
    {'name': 'archive', 'description': 'archive this session', 'how': 'local', 'method': 'a mark kept by this page (a visual cue only); nothing is sent to Codex'},
]
INIT_FALLBACK = 'Create an AGENTS.md file with instructions for Codex for this repository (a contributor guide). If AGENTS.md already exists, do not overwrite or modify it.'
_init_text = None


def init_prompt():
    """The text Codex's own /init sends, read from the installed CLI (the TUI has it built in; no protocol method sends it)."""
    global _init_text
    if _init_text is None:
        _init_text = INIT_FALLBACK
        try:
            exe = Path(binary()).resolve()
            real = exe.parent.parent / 'CodexCLI.app' / 'Contents' / 'MacOS' / 'codex'      # Codex.app's bin/codex is a shell launcher
            data = (real if real.is_file() else exe).read_bytes()
            i = data.find(b'Generate a file named AGENTS.md')
            j = data.find(b'Agent-Specific Instructions.', i)
            if i >= 0 and 0 < j - i < 6000:
                _init_text = data[i:j + len(b'Agent-Specific Instructions.')].decode('utf-8')
        except (OSError, RuntimeError, UnicodeError):
            pass
    return _init_text


def sandbox_mode(policy):
    """A SandboxPolicy (or the state database's JSON of one) as Codex's sandbox mode name."""
    if isinstance(policy, str):
        try:
            policy = json.loads(policy)
        except ValueError:
            return policy
    t = (policy or {}).get('type') if isinstance(policy, dict) else None
    if t == 'managed':      # the state database's permission-profile form: a project path with write access is the workspace-write shape
        fs = policy.get('file_system') or {}
        if fs.get('type') != 'restricted':
            return 'danger-full-access'
        return 'workspace-write' if any(e.get('access') == 'write' and (e.get('path') or {}).get('type') == 'path' for e in fs.get('entries', [])) else 'read-only'
    return {'readOnly': 'read-only', 'read-only': 'read-only', 'workspaceWrite': 'workspace-write', 'workspace-write': 'workspace-write',
            'dangerFullAccess': 'danger-full-access', 'danger-full-access': 'danger-full-access', 'disabled': 'danger-full-access',
            'externalSandbox': 'external-sandbox', 'managed': 'managed profile'}.get(t, t)


def sandbox_policy(mode):
    """Codex's sandbox mode name as the SandboxPolicy that turn/start and thread/settings/update take."""
    if mode == 'read-only':
        return {'type': 'readOnly', 'networkAccess': False}
    if mode == 'workspace-write':
        return {'type': 'workspaceWrite', 'writableRoots': [], 'networkAccess': False, 'excludeTmpdirEnvVar': False, 'excludeSlashTmp': False}
    if mode == 'danger-full-access':
        return {'type': 'dangerFullAccess'}
    raise ValueError('Unsupported sandbox mode')


def overrides(p):
    """The per-turn settings a message carries (only those the user changed), checked against Codex's names."""
    out = {}
    if p.get('model'):
        out['model'] = str(p['model'])
    if p.get('effort'):
        out['effort'] = str(p['effort'])
    if p.get('approvalPolicy'):
        if p['approvalPolicy'] not in APPROVAL_POLICIES:
            raise ValueError('Unsupported approval policy')
        out['approvalPolicy'] = p['approvalPolicy']
    if p.get('sandbox'):
        out['sandboxPolicy'] = sandbox_policy(p['sandbox'])
    return out


def remember(b, tid, r):
    """Settings from a thread/start or thread/resume response, or a thread/settings/updated notification."""
    if not isinstance(r, dict) or not hasattr(b, 'settings'):
        return
    s = b.settings.setdefault(tid, {})
    for k, v in (('model', r.get('model')), ('effort', r.get('reasoningEffort', r.get('effort'))), ('approvalPolicy', r.get('approvalPolicy')), ('cwd', r.get('cwd'))):
        if v is not None:
            s[k] = v
    pol = r.get('sandbox', r.get('sandboxPolicy'))
    if pol is not None:
        s['sandbox'] = sandbox_mode(pol)
        s['sandboxPolicy'] = pol
    if r.get('activePermissionProfile'):
        s['profile'] = r['activePermissionProfile'].get('id')
    if r.get('instructionSources') is not None:
        s['instructionSources'] = r['instructionSources']


def track(b, method, p):
    """Notifications the chat controls read: settings changes, MCP startup, token usage, rate limits."""
    if not hasattr(b, 'settings'):
        return
    tid = p.get('threadId')
    if method == 'thread/settings/updated' and tid:
        remember(b, tid, p.get('threadSettings'))
    elif method == 'mcpServer/startupStatus/updated':
        b.mcp.setdefault(tid or '*', {})[p.get('name')] = {'status': p.get('status'), 'error': p.get('error')}
    elif method == 'thread/tokenUsage/updated' and tid:
        b.usage[tid] = p.get('tokenUsage')
    elif method == 'account/rateLimits/updated':
        b.rate = {**(b.rate or {}), **{k: v for k, v in (p.get('rateLimits') or {}).items() if v is not None}}


def web_search_of(config):
    """Web search as configured: the `web_search` mode (disabled / cached / indexed / live) or the tools.web_search table."""
    mode = config.get('web_search')
    tools = (config.get('tools') or {}).get('web_search') if isinstance(config.get('tools'), dict) else None
    if mode:
        return {'mode': mode, 'on': mode != 'disabled', 'source': 'web_search in config'}
    if tools is not None:
        return {'mode': None, 'on': bool(tools), 'source': 'tools.web_search in config'}
    return {'mode': None, 'on': None, 'source': 'not set in config: Codex applies its built-in default, which the protocol does not report'}
# ---------------- end chat controls ----------------


# ---------------- connectors: MCP servers each harness has configured, and the calls its sessions made ----------------
def mcp_open(cached, m, server, tool, call_id, t):
    """A Codex MCP call seen as its function_call: the message names server.tool, an event of kind 'mcp' carries it to the floor."""
    if m is not None:
        m.update(text=server + '.' + tool, connector=server, tool=tool)
    e = {'at': t, 'kind': 'mcp', 'path': None, 'text': server + '.' + tool, 'item': call_id, 'tokens': 0, 'connector': server, 'tool': tool}
    cached['events'].append(e)
    cached.setdefault('mcpOpen', {})[call_id] = e
    return e


def mcp_done(cached, item, p, t):
    """item_completed of an McpToolCall: its started/completed times on the event; a call with no function_call gets its event here."""
    e = cached.setdefault('mcpOpen', {}).pop(item.get('id'), None)
    start, end = p.get('started_at_ms'), p.get('completed_at_ms')
    if e is None:
        server, tool = item.get('server') or '?', item.get('tool') or '?'
        m = {'role': 'tool', 'text': server + '.' + tool, 't': start / 1000 if start else t, 'id': item.get('id'), 'tokens': 0, 'connector': server, 'tool': tool}
        cached['messages'].append(m)
        e = mcp_open(cached, m, server, tool, item.get('id'), m['t'])
        cached['mcpOpen'].pop(item.get('id'), None)
        unit(cached, m, len(json.dumps(item.get('arguments') or {})))['evs'].append(e)
    e['end'] = end / 1000 if end else t


def connector_id(name):
    """The id a connector is known by across harnesses and in logos/: its server name, lower case, other characters as '-'."""
    import re
    return re.sub(r'[^a-z0-9_-]+', '-', str(name).lower()).strip('-') or 'connector'


def connector_label(name):
    """A display name: the server's own name with underscores as spaces; a claude.ai connector recorded only by its UUID says so."""
    import re
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', str(name)):
        return 'connector ' + str(name)[:8]
    return str(name).replace('_', ' ')


def codex_config_servers(home=None):
    """[mcp_servers.*] in the Codex config.toml: name, command or url."""
    import tomllib
    try:
        cfg = tomllib.loads((Path(home or CODEX_HOME) / 'config.toml').read_text())
    except (OSError, ValueError):
        return []
    out = []
    for name, v in (cfg.get('mcp_servers') or {}).items():
        if isinstance(v, dict):
            out.append({'name': name, 'command': v.get('command'), 'url': v.get('url'), 'enabled': v.get('enabled', True), 'where': str(Path(home or CODEX_HOME) / 'config.toml')})
    return out


CLAUDE_JSON = Path.home() / '.claude.json'


def claude_config_servers(cwd, claude_json=None):
    """Claude Code's MCP servers: ~/.claude.json mcpServers (user scope) and its entry for this project, and the project's .mcp.json."""
    out, path = [], Path(claude_json or CLAUDE_JSON)
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        d = {}
    scopes = [('user', d.get('mcpServers') or {})]
    for proj, v in (d.get('projects') or {}).items():
        if isinstance(v, dict) and v.get('mcpServers') and (cwd == proj or str(cwd).startswith(proj.rstrip('/') + '/')):
            scopes.append(('project ' + proj, v['mcpServers']))
    try:
        scopes.append(('.mcp.json', json.loads((Path(cwd) / '.mcp.json').read_text()).get('mcpServers') or {}))
    except (OSError, ValueError, AttributeError):
        pass
    for scope, servers in scopes:
        for name, v in servers.items() if isinstance(servers, dict) else []:
            v = v if isinstance(v, dict) else {}
            out.append({'name': name, 'command': v.get('command'), 'url': v.get('url'), 'scope': 'user' if scope == 'user' else 'mcp.json' if scope == '.mcp.json' else 'project',
                        'where': (str(path) + ' (' + scope + ')') if scope != '.mcp.json' else str(Path(cwd) / '.mcp.json')})
    return out


# ---- connectors list (branch ide-roomy, 2026-11-06): what each harness reports as connected, for the read-only connectors sheet ----
# Account-level connectors (claude.ai's, such as Google Drive) are in no config file: `claude mcp list` reports them, with a health
# check per server, so it runs in a background thread with a timeout and its answer is kept a few minutes; a request never waits on
# it. Codex: `codex mcp list --json` (no health check), else config.toml. Only names, kinds and status are kept: the URL or command
# part of a line, args, env and headers are dropped as they are read, so no secret can reach the page.
MCP_LIST_TTL = 300          # s an answer is kept (judgement: the health check costs a few seconds per server)
MCP_LIST_TIMEOUT = 60       # s before a list run is given up
BUILTIN_SERVERS = re.compile(r'^(node_repl|codex_apps?|cua|cua_repl|computer-use|computer_use|visualize|terminal|claude-artifacts|mcp-registry|scheduled-tasks|ccd_.*|Claude_.*)$')


def builtin_server(name):
    """A tool server the app itself ships (the Codex app's node_repl, codex_app, cua_repl; Claude desktop's ccd_*, Claude_*, computer-use,
    browser, terminal and the like): shown apart from connectors."""
    return bool(BUILTIN_SERVERS.match(str(name)))


def claude_binary():
    """The Claude Code CLI: AGENT_IDE_CLAUDE, the PATH's, ~/.claude/local, ~/.local/bin, else the newest one Claude desktop bundles."""
    for p in (os.environ.get('AGENT_IDE_CLAUDE'), shutil.which('claude'), str(Path.home() / '.claude/local/claude'), str(Path.home() / '.local/bin/claude')):
        if p and Path(p).is_file():
            return p
    def ver(p):
        try:
            return tuple(int(x) for x in p.parts[-6].split('.'))
        except (ValueError, IndexError):
            return ()
    found = sorted(Path.home().glob('Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude'), key=ver)
    return str(found[-1]) if found else None


def mcp_status(text):
    """A status word from a list line's tail: connected, needs auth, failed, or unknown."""
    t = str(text).lower()
    if 'connected' in t and 'not' not in t and 'fail' not in t:
        return 'connected'
    if 'auth' in t:
        return 'needs auth'
    if 'fail' in t or 'error' in t or '✗' in t:
        return 'failed'
    return 'unknown'


def parse_claude_mcp_list(out):
    """`claude mcp list` lines "<name>: <url or command> - <status>" -> [{name, account, kind, status}]; the middle part is dropped."""
    rows = []
    for line in str(out).splitlines():
        line = line.strip()
        if ': ' not in line or ' - ' not in line or line.lower().startswith('checking'):
            continue
        head, status = line.rsplit(' - ', 1)
        name, target = head.split(': ', 1)
        name = name.strip()
        if not name:
            continue
        account = name.startswith('claude.ai ')
        kind = 'http' if re.match(r'^https?://', target.strip()) else 'stdio'
        rows.append({'name': name[len('claude.ai '):] if account else name, 'account': account, 'kind': kind, 'status': mcp_status(status)})
    return rows


def parse_codex_mcp_list(out):
    """`codex mcp list --json` -> [{name, kind, status}]: enabled or not and its auth state only; command, args, env are never kept."""
    try:
        data = json.loads(out)
    except (TypeError, ValueError):
        return None
    rows = []
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict) or not e.get('name'):
            continue
        auth = str(e.get('auth_status') or '').lower()
        status = 'disabled' if e.get('enabled') is False else 'needs auth' if auth in ('not_logged_in', 'notloggedin', 'logged_out') else 'unknown'
        rows.append({'name': str(e['name']), 'kind': str((e.get('transport') or {}).get('type') or ''), 'status': status})
    return rows


class McpLists:
    """The harnesses' own MCP lists, per folder, run in background threads; get() answers at once from what is cached."""
    def __init__(self, runner=None, ttl=MCP_LIST_TTL, timeout=MCP_LIST_TIMEOUT):
        self.cache, self.busy, self.lock = {}, set(), threading.Lock()
        self.runner, self.ttl, self.timeout = runner or self.run, ttl, timeout

    def run(self, harness, cwd, timeout):
        """One list run: (rows, state); state is ok, absent (no CLI), timeout or failed."""
        if harness == 'claude':
            exe, args, parse = claude_binary(), ['mcp', 'list'], parse_claude_mcp_list
        else:
            try:
                exe = binary()
            except RuntimeError:
                exe = None
            args, parse = ['mcp', 'list', '--json'], parse_codex_mcp_list
        if not exe:
            return None, 'absent'
        env = dict(os.environ, **({'CODEX_HOME': str(CODEX_HOME)} if harness == 'codex' else {}))
        try:
            p = subprocess.run([exe, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return None, 'timeout'
        except OSError:
            return None, 'failed'
        rows = parse(p.stdout)
        if rows == [] and p.returncode == 0:          # nothing listed: a dropped network makes claude.ai's connectors vanish from the list, so the last answer is kept
            return None, 'empty'
        return (rows, 'ok') if rows is not None and (p.returncode == 0 or rows) else (None, 'failed')

    def get(self, harness, cwd):
        """{rows, state, at}: rows None until the first run finished; a stale or missing answer starts a run in the background."""
        key = (harness, str(cwd))
        with self.lock:
            c = self.cache.get(key)
            due = (c is None or time.time() - c['at'] > self.ttl) and key not in self.busy
            if due:
                self.busy.add(key)
        if due:
            threading.Thread(target=self._refresh, args=(key,), daemon=True).start()
        return dict(c) if c else {'rows': None, 'state': 'pending', 'at': None}

    def _refresh(self, key):
        try:
            rows, state = self.runner(key[0], key[1], self.timeout)
        except Exception:
            rows, state = None, 'failed'
        with self.lock:
            old = self.cache.get(key)
            self.cache[key] = {'rows': rows if rows is not None else (old['rows'] if old else None), 'state': state, 'at': time.time()}
            self.busy.discard(key)


MCP_LISTS = McpLists()
# ---------------- end connectors ----------------


# ---------------- mock: connector names and the mock scenario switch (README.md "Mock scenario") ----------------
CONNECTOR_NAMES = ROOT / 'connectors.json'
OWN_CONNECTOR_NAMES = None          # the person's own names; None: connectors.json in the app data folder (tests point it elsewhere)
MOCK_ON = None
MOCK = None          # attention mock: what mock.setup laid out (mock.act appends to it)


def connector_names(path=None, own=None):
    """connectors.json: server id (UUID or name) -> {name, logo}; keys starting with '_' are notes. Without a path: the shipped
    file, then the person's own connectors.json in the app data folder (beside the cache folder) over it, since a claude.ai
    connector's id is the person's own and the shipped folder may be read-only (inside the app)."""
    def read(f):
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in d.items() if not k.startswith('_') and isinstance(v, dict)} if isinstance(d, dict) else {}
    if path:
        return read(path)
    if own is None:
        from providers.keep import cache_dir
        own = OWN_CONNECTOR_NAMES or cache_dir().parent / 'connectors.json'
    return {**read(CONNECTOR_NAMES), **read(own)}


def mock_late():
    """AGENT_IDE_MOCK_LATE=<seconds>, in a mock only: the model list and the full chat controls answer that late, as Codex's do
    when it is busy (walk 2026-11-19: the box showed "loading…" pickers for about 1.5 s); nothing outside a mock waits."""
    try:
        late = float(os.environ.get('AGENT_IDE_MOCK_LATE') or 0)
    except ValueError:
        late = 0
    if MOCK_ON and late > 0:
        time.sleep(min(late, 10))


def mock_switch(name=None):
    """AGENT_IDE_MOCK=<scenario>: the providers read the scenario's fixture sessions instead of ~/.codex and ~/.claude, and
    the project is its fixture repository. Only paths change: Codex's state and rollouts (CODEX_HOME), Claude Code's store
    (CLAUDE_CONFIG_DIR) and ~/.claude.json, the default project; the app-server this server starts gets an empty home of its
    own, so the person's Codex threads and servers stay out of the mock (it cannot start agents: no sign-in there)."""
    global CODEX_HOME, CLAUDE_JSON, DEFAULT_PROJECT, MOCK_ON, MOCK
    name = name or os.environ.get('AGENT_IDE_MOCK')
    if not name:
        return None
    MOCK_ON = name          # connectors list: the harnesses' own lists are the person's, not the scenario's: not run
    import mock
    m = MOCK = mock.setup(name)
    CODEX_HOME, CLAUDE_JSON, DEFAULT_PROJECT = m['codex_home'], m['claude_json'], m['project']
    os.environ['CLAUDE_CONFIG_DIR'] = str(m['claude_home'])
    os.environ['CODEX_HOME'] = str(m['codex_runtime'])
    print(f'Mock scenario {name}: project {m["project"]}', flush=True)
    return m
# ---------------- end mock ----------------


def waiting_on_agents(rows):
    """running (2026-11-09): a session whose own subagents still work has not returned, whatever its harness says of its turn: it is
    active and waits on them (waitingOn: how many). Up the chain too (a parent of one that waits). A copy (fork) is not a subagent."""
    by = {s['id']: s for s in rows}
    for _ in range(6):
        kids = {}
        for s in rows:
            if s.get('parent') in by and s['status'] == 'active' and not s.get('copy'):
                kids[s['parent']] = kids.get(s['parent'], 0) + 1
        moved = False
        for pid, n in kids.items():
            par = by[pid]
            if par['status'] != 'active' or par.get('waitingOn'):
                if par['status'] != 'active':
                    moved = True
                par['status'] = 'active'
                par['waitingOn'] = n
        if not moved:
            return rows
    return rows


class Workspace:
    def __init__(self, bridge):
        self.bridge = bridge
        self.observer = Observer()
        self.token = secrets.token_urlsafe(32)
        if isinstance(getattr(bridge, 'hooks', None), dict):          # robust: open requests kept as they change (requests_sync)
            bridge.hooks['requests'] = self.requests_sync

    def logs_changed(self, changed):
        """Invalidate discovery caches before notifying clients; parsers keep their offsets."""
        providers = set(changed.values())
        ids = set()
        for path, provider in changed.items():
            if provider == 'codex' and path.suffix == '.jsonl':
                match = re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$', path.stem)
                if match:
                    ids.add(match[1])
            elif provider == 'claude' and path.suffix == '.jsonl':
                ids.add('claude:' + (path.parent.parent.name + '/' + path.stem.removeprefix('agent-')
                                     if path.parent.name == 'subagents' else path.stem))
        for p in self.providers:
            if p.name == 'claude' and p.name in providers:
                # Not under p.lock: a long first read of a transcript holds it, and the watch thread must not wait on it.
                # Each is one assignment or clear, whole under the GIL; files() and live() never held the lock anyway.
                p._scan = (0, None, None)
                p._live = (0, {})
                p._folders.clear()
                # Older files in continuation chains still feed the newest column.
                for sid, chain in list(p.chains.items()):
                    if any(path in changed for path, _ in chain):
                        ids.add(sid)
        self.bridge.publish({'method': 'provider/changed', 'params': {
            'providers': sorted(providers), 'threadIds': sorted(ids)}})

    def watch_logs(self):
        """low latency (2026-11-14, from the programming branch): only what the adapters read. Codex: the state database the
        session list comes from, the rollouts, the writer locks that say a session is running; never its logging and other
        databases beside them, which change many times a second. Claude Code: every transcript (subagents' included) and the
        running sessions' registrations."""
        def codex(parts, is_dir):
            if not parts:
                return True
            if parts[0] in ('sessions', 'archived_sessions'):
                return is_dir or parts[-1].endswith('.jsonl')
            if parts[0] == 'thread-writer-locks':
                return is_dir if len(parts) == 1 else parts[-1].endswith('.lock')
            return len(parts) == 1 and not is_dir and parts[0].startswith('state_') and parts[0].endswith(('.sqlite', '.sqlite-wal'))
        roots = {CODEX_HOME: ('codex', codex)}
        for p in self.providers:
            if p.name == 'claude':
                roots[p.home / 'projects'] = ('claude', lambda parts, is_dir: is_dir or parts[-1].endswith('.jsonl'))
                roots[p.home / 'sessions'] = ('claude', lambda parts, is_dir: not parts or (not is_dir and parts[-1].endswith('.json')))
        self.log_watch = LogWatch(roots, self.logs_changed).start(wait=False)          # the page's polls cover the first moment
        return self.log_watch

    # ---------- speed (2026-11-21): the first answers do not wait on the other providers' first listing ----------
    # On one developer machine (2026-11-21) Claude Code's first list_sessions took 9-10 s of a 13-15 s /api/bootstrap (it reads the
    # transcripts of every registered session and their subagents), and the page draws nothing before bootstrap and the canvas
    # answer. So the first listing of every provider after Codex runs on a thread of its own; the first answer waits for it at
    # most FIRST_WAIT s (the ones after it, not at all) and otherwise goes without those providers' sessions and questions,
    # naming them in `loading`. When it is done the page hears provider/changed (and its next state poll no longer says
    # `loading`) and reads the sessions and the floor again. From then on every answer lists as before. main() reads Codex's
    # rollouts before the port opens (0.8 s cold, 0.01 s once read, on a store of 286 Codex sessions) and starts this listing
    # as soon as the server is up.
    def listings_ready(self, wait=FIRST_WAIT):
        w = self.__dict__.get('_warm')
        if w is None:
            with FIRST_LISTING_LOCK:
                w = self.__dict__.get('_warm')
                if w is None:
                    w = self._warm = {'done': threading.Event(), 'missed': False}
                    threading.Thread(target=self.first_listing, args=(w,), name='agent-ide-first-listing', daemon=True).start()
        if w['done'].is_set() or (wait and not w['missed'] and w['done'].wait(wait)):          # only the first answer waits
            return True
        if wait:
            w['missed'] = True
        return False

    def first_listing(self, w):
        names = []
        for p in self.providers[1:]:
            try:
                p.list_sessions()
            except Exception:
                pass          # a store that cannot be read lists nothing; the answers after this one say so as before
            names.append(p.name)
        w['done'].set()
        if w['missed'] and names and hasattr(self.bridge, 'publish'):
            self.bridge.publish({'method': 'provider/changed', 'params': {'providers': names, 'threadIds': []}})

    def snapshot(self):
        absent = isinstance(self.bridge, CodexAbsent)          # providers panel: no Codex is not "disconnected"; Claude Code can still start
        ready = self.listings_ready()          # speed (2026-11-21): one answer for the whole snapshot
        return {'connected': self.bridge.ready or absent, 'codexAbsent': self.bridge.reason if absent else None, 'mock': MOCK_ON, 'sessions': self.all_sessions(ready), 'providers': self.provider_state(), 'requests': [card_of(self.bridge.files.attach(r) if hasattr(self.bridge, 'files') else r) for r in list(self.bridge.requests.values())] + self.claude_requests() + (self.watch_requests() if ready else []) + self.provider_requests(), 'defaultProject': str(DEFAULT_PROJECT), 'eventCursor': self.bridge.event_seq,
                'loading': [] if ready else [p.name for p in self.providers[1:]],          # speed (2026-11-21): whose sessions are still being read
                'answered': self.answered_now(),          # answers kept: what was answered through the page, per chat (robust: and requests that ended unanswered)
                'codex': self.bridge.status() if hasattr(self.bridge, 'status') else {'state': 'up' if self.bridge.ready else 'down'},          # robust: the connection to Codex (providers: 'absent' when there is none)
                'logos': sorted(f.stem for f in (ROOT / 'logos').glob('*.svg'))}

    def sessions(self):
        result = self.observer.sessions(self.bridge)
        known = {r['id'] for r in result}
        for tid, t in list(self.bridge.threads.items()):
            if tid in known:
                continue
            result.append({'id': tid, 'title': t.get('name') or t.get('preview') or 'New agent', 'cwd': t['cwd'], 'projectId': t.get('projectId'), 'source': 'agent_ide', 'model': t.get('model'), 'updatedAt': t.get('updatedAt', time.time()), 'createdAt': t.get('createdAt', time.time()), 'parent': t.get('forkedFromId'), 'status': 'active' if tid in self.bridge.active else 'idle', 'managed': True, 'external': False, 'turnId': self.bridge.active.get(tid), 'preview': ''})
        return result

    # ---------- providers (harness slice): Codex plus the read-only harnesses beside it ----------
    @property
    def providers(self):
        if not hasattr(self, '_providers'):
            tokens = SimpleNamespace(unit=unit, settle=settle, reading=reading, compaction=compaction, estimate=estimate)
            self._providers = [CodexProvider(self, CODEX_HOME, binary), ClaudeCodeProvider(tokens)] + self.driven_providers()
            test = os.environ.get('AGENT_IDE_TEST_PROVIDERS')          # tests only: which providers, in order ('fake': providers/fake.py)
            if test:
                from providers.fake import FakeProvider
                have = {p.name: p for p in self._providers}
                have['fake'] = FakeProvider(os.environ.get('AGENT_IDE_PROJECT') or DEFAULT_PROJECT)
                self._providers = [have[n] for n in test.split(',') if n in have]
        return self._providers

    # ---------- providers panel: each tool's state and the step that fixes it (provider_setup.py), made on first use ----------
    @property
    def provider_setup(self):
        if not hasattr(self, '_provider_setup'):
            b = lambda: {'present': not isinstance(self.bridge, CodexAbsent), 'ready': bool(getattr(self.bridge, 'ready', False)), 'reason': getattr(self.bridge, 'reason', None),
                         'call': self.bridge.call, 'seq': lambda: self.bridge.event_seq, 'events': lambda: list(self.bridge.events)}          # account/read, account/login/start; the login's end on the event list
            self._provider_setup = provider_setup.ProviderSetup(codex_path, claude_binary, b, lambda: self.claude.available(),
                                                                claude_env=lambda: claude_bridge.worker_env(),
                                                                driven=lambda: [p for p in self.providers[1:] if self.driven(p)])          # more providers
        return self._provider_setup

    # ---------- claude drive: the Claude Code bridge (claude_bridge.py), made on first use; it starts no process until asked ----------
    @property
    def claude(self):
        if not hasattr(self, '_claude'):
            cp = next((p for p in self.providers if getattr(p, 'name', None) == 'claude'), None)
            self._claude = claude_bridge.ClaudeBridge(publish=getattr(self.bridge, 'publish', None), provider=cp,
                                                      command=MOCK.get('claude_command') if MOCK else None)          # mock: a stand-in that starts no Claude Code
            if cp:
                cp.drive = self._claude
        return self._claude

    def claude_requests(self):
        return list(self.claude.requests.values()) if hasattr(self, '_claude') else []

    def watch_requests(self):
        """attention: the questions waiting on you in sessions this server only watches (a Claude Code session stopped on
        AskUserQuestion or ExitPlanMode), as requests marked watchOnly: from the sessions whose history was read already (the
        canvas reads its floor's) and every registered running one, each brought up to date (a tool result clears its question
        at the next poll); never answered from here."""
        out = []
        for p in self.providers[1:]:
            if not hasattr(p, 'ask_requests'):
                continue
            try:
                live = p.live()
                for s in p.list_sessions():
                    if s.get('parent'):
                        continue
                    if str(p.paths.get(s['id'])) in p.cache or s['id'].removeprefix(p.name + ':') in live:
                        hist = p.read_history(s['id'])          # read again only when the transcript changed (its size and time are compared)
                    else:
                        continue
                    if hist and hist.get('asking'):
                        out += p.ask_requests(s['id'], hist)
            except Exception:
                continue          # a store that cannot be read asks nothing
        held = getattr(self, '_claude', None) and self._claude.held          # a session this server drives asks through its own requests (and
        return [r for r in out if not (held and (held.get(str(r['params'].get('threadId')).removeprefix(claude_bridge.PREFIX)) or {}).get('open'))]          # its transcript trails an answer): never twice

    def claude_sessions(self, listed):
        """The Claude Code rows as this server drives them: status from the bridge, `managed` for its own, and the sessions it
        started whose transcript does not exist yet."""
        cb = self.claude
        ready = cb.available().get('ready')
        seen = set()
        for s in listed:
            key = s['id'][len(claude_bridge.PREFIX):]
            h = cb.held.get(key)
            seen.add(key)
            if h:
                s['status'] = 'active' if h.get('active') else 'idle'
                s['managed'] = bool(h.get('open'))
                s['how'] = h.get('how')
                if h.get('how') == 'forked' and h.get('from'):          # a copy keeps its original as parent once its transcript exists
                    s['parent'] = claude_bridge.PREFIX + h['from']
            if ready and '/' not in key:
                s['heldElsewhere'] = bool(s.get('external')) and not (h and h.get('open'))
                if s['heldElsewhere']:
                    s['heldBy'] = cb.holder((cb.provider.live() if cb.provider else {}).get(key))          # app, terminal or process: the page's quiet note
                s['external'] = False          # a send resumes it; while another process holds it, a send asks before forking
                s['drivable'] = True
        for key, h in list(cb.held.items()):
            if key not in seen:
                listed.append({'id': claude_bridge.PREFIX + key, 'title': h.get('title') or 'New Claude Code agent', 'cwd': h['cwd'], 'projectId': None, 'source': 'claude',
                               'model': h.get('model'), 'updatedAt': h.get('sentAt') or h['created'], 'createdAt': h['created'], 'parent': claude_bridge.PREFIX + h['from'] if h.get('how') == 'forked' else None,
                               'status': 'active' if h.get('active') else 'idle', 'managed': bool(h.get('open')), 'external': False, 'turnId': None, 'preview': '', 'provider': 'claude',
                               'how': h.get('how'), 'drivable': bool(ready)})
        # a copy (2026-11-09; it must be clear that sending a message started a copy of the chat): marked, and named as a copy
        # of its original (its transcript starts with the original's words, so its own first prompt would name it like the original)
        # A copy is known from the bridge (how 'forked') while this server runs, and after a restart from its own records (the provider's
        # copyOf: written by the SDK this bridge drives). Its original may since have gone on in a new file (continuation): the parent
        # is the column that draws it now, under its newest id
        titles = {s['id']: s.get('title') for s in listed}
        newest = {o: s['id'] for s in listed for o in s.get('continues') or []}
        for s in listed:
            h = cb.held.get(s['id'][len(claude_bridge.PREFIX):])
            orig = claude_bridge.PREFIX + h['from'] if h and h.get('how') == 'forked' and h.get('from') else s.get('copyOf')
            if orig:
                orig = newest.get(orig, orig)
                s['copy'], s['parent'] = True, orig
                s['title'] = 'copy of ' + (titles.get(orig) or (h or {}).get('original') or 'a Claude Code session')
        return listed

    def claude_action(self, op, p):
        cb = self.claude
        tid = p.get('threadId')
        if op == 'start':
            plan = plan_of(p, 'claude')
            r = cb.start(str(project_path(p['cwd'])), model=p.get('model') or None, effort=p.get('effort') or None, mode=p.get('approvalPolicy') or None, plan=plan)
            self.plan_keep(r['thread']['id'], plan)
            return r
        if op == 'send':
            text = (p.get('text') or '').strip()
            if not text:
                raise ValueError('Write a message first.')
            row = next((s for s in self.all_sessions() if s['id'] == tid), None)
            sm = None
            if not (cb.held.get(cb.key_of(tid)) or {}).get('open'):          # a copy runs on the session's own model unless one is chosen
                try:
                    sm = (self.provider_of(tid).read_history(tid) or {}).get('model')
                except Exception:
                    sm = None
            return cb.send(tid, text, cwd=row and row['cwd'], model=p.get('model') or None, effort=p.get('effort') or None, mode=p.get('approvalPolicy') or None,
                           fork=p.get('fork') is True, session_model=sm)          # held elsewhere: no fork without fork: true
        if op == 'stop':
            return cb.stop(tid)
        if op == 'desktop':
            return cb.to_desktop(tid)
        if op == 'resume':
            return {}
        raise ValueError('/' + op + ' is not available for Claude Code sessions here yet.')
    # ---------- end claude drive ----------

    # ---------- more providers (port 2026-11-21 from branch ide-providers-2026-11-09, 5f5b8903f): Antigravity and Cursor
    # over ACP (providers/acp.py, acp_flavors.py) and OpenCode through its own server (providers/opencode.py). These drive their own
    # processes; this server starts, sends, stops and answers through them, and passes what they report onto the event stream.
    # Not ported: the branch's send queue (store.py, delivery.py), checkpoints, handoff and arena (docs: harnesses.md).
    def driven_providers(self):
        """The harnesses this server drives through providers/*. A module that fails to load leaves its providers out and says
        why once. Not under a mock (its walks use the fixture harnesses only)."""
        if os.environ.get('AGENT_IDE_MOCK'):
            return []
        out = []
        try:
            from providers import acp
            out += acp.providers(publish=self.provider_publish)
        except Exception as e:
            getattr(self.bridge, 'warn_once', lambda *a: None)('ACP providers', e)
        try:
            from providers import acp, opencode
            out.append(opencode.OpenCodeProvider(publish=self.provider_publish, ledger=acp.data_root() / 'opencode' / 'driven.json'))
        except Exception as e:
            getattr(self.bridge, 'warn_once', lambda *a: None)('OpenCode provider', e)
        return out

    def driven(self, p):
        """True for a provider this server drives itself (it has phase, send and start)."""
        return p is not None and all(callable(getattr(p, k, None)) for k in ('phase', 'send', 'start')) and getattr(p, 'name', None) != 'claude'

    def provider_named(self, name):
        return next((p for p in self.providers if getattr(p, 'name', None) == name), None)

    def provider_publish(self, msg):
        """What a driven provider reports goes onto the event stream as it is. Its requests are remembered in words so one that
        ends unanswered because its process went away is said in its chat (ended_keep), as Codex's are."""
        m, prm = msg.get('method'), msg.get('params') or {}
        words = self.__dict__.setdefault('_prov_words', {})
        try:
            if m in ('item/agent/requestApproval', 'item/agent/requestUserInput') and 'id' in msg:
                words[str(msg['id'])] = generic_words(msg)
                while len(words) > 500:
                    words.pop(next(iter(words)))
            elif m == 'serverRequest/resolved':
                w = words.pop(str(prm.get('requestId')), None)
                if w and prm.get('lost'):
                    self.ended_keep(dict(w, why='dropped'))
        except Exception as e:
            getattr(self.bridge, 'warn_once', lambda *a: None)('provider event ' + str(m), e)
        publish = getattr(self.bridge, 'publish', None)
        if publish:
            publish(msg)

    def provider_requests(self):
        """Approvals and questions waiting in sessions the driven providers hold, as the page's cards read them."""
        out = []
        for p in self.providers[1:]:
            if self.driven(p) and callable(getattr(p, 'requests', None)):
                try:
                    out += [generic_card(r) for r in p.requests()]
                except Exception:
                    continue
        return out

    def request_owner(self, rid):
        """The driven provider holding request rid, or None."""
        for p in self.providers[1:]:
            if not self.driven(p) or not callable(getattr(p, 'requests', None)):
                continue
            try:
                if any(str(r.get('id')) == str(rid) for r in p.requests()):
                    return p
            except Exception:
                continue
        return None

    def start_on(self, name, p):
        """A new session on a driven provider. The answer is shaped like Codex's ({thread: {id}}) so the page's new-agent path
        takes it unchanged. The page's approval mode arrives as approvalPolicy (the harness's own mode id, from its controls)."""
        prov = self.provider_named(name)
        if not self.driven(prov):
            raise ValueError('Unknown harness')
        av = prov.available() or {}
        if not av.get('found'):
            raise ValueError('%s cannot start here: %s' % (prov.label, av.get('reason') or 'it is not installed'))
        if av.get('canStart') is False:
            raise ValueError('%s cannot start here: %s' % (prov.label, str(av.get('reason') or 'not available').removeprefix('refused: ')))
        r = prov.start(str(project_path(p['cwd'])), model=p.get('model') or None, effort=p.get('effort') or None,
                       mode=p.get('mode') or p.get('approvalPolicy') or None) or {}
        sid = r.get('threadId') or (r.get('thread') or {}).get('id')
        return {**r, 'threadId': sid, 'thread': {**(r.get('thread') or {}), 'id': sid, 'harness': name}}

    def provider_send(self, prov, tid, p):
        """A message to a driven session. Mid-turn: OpenCode takes it into the running turn (its own steering); the ACP agents
        cannot, so it is refused with what to do (the page keeps the text in its box). Nothing is queued here."""
        text = (p.get('text') or '').strip()
        if not text:
            raise ValueError('Write a message first.')
        if not prov.owns(tid):
            raise ValueError('This %s session is a subagent or was started outside this page: it can be watched here, not written to.' % prov.label)
        phase = prov.phase(tid)
        how = 'start'
        if phase != 'idle':
            if phase == 'running' and prov.capabilities(tid).get('steer'):
                how = 'steer'
            else:
                raise ValueError('%s is still %s this turn and cannot take a message until it ends: wait for it to finish, '
                                 'or press Stop, then send again (your message is still in the box).'
                                 % (prov.label, {'stopping': 'stopping', 'starting': 'starting'}.get(phase, 'working on')))
        import inspect
        try:
            takes = set(inspect.signature(prov.send).parameters)
        except (TypeError, ValueError):
            takes = set()
        kw = {k: v for k, v in (('model', p.get('model')), ('effort', p.get('effort')), ('mode', p.get('mode') or p.get('approvalPolicy'))) if v and k in takes}
        out = prov.send(tid, text, how, **kw) or {}
        return {**out, 'how': 'steered' if how == 'steer' else out.get('how') or 'sent'}

    def provider_action(self, prov, op, tid, p):
        """What a page action means for a driven session, by capability."""
        if op == 'send':
            return self.provider_send(prov, tid, p)
        caps = prov.capabilities(tid)
        if op == 'stop':
            if not caps.get('stop') or not prov.owns(tid):
                raise ValueError('%s cannot stop this session from here.' % prov.label)
            if prov.phase(tid) == 'idle':
                raise ValueError('This agent is not running.')
            return prov.stop(tid) or {}
        if op == 'resume':
            return {}
        if op == 'rename' and caps.get('rename') and hasattr(prov, 'rename'):
            return prov.rename(tid, p['name']) or {}
        if op == 'compact' and caps.get('compact') and hasattr(prov, 'compact'):
            return prov.compact(tid) or {}
        if op == 'fork' and caps.get('fork') and hasattr(prov, 'fork'):
            return prov.fork(tid) or {}
        raise ValueError('/%s is not available for %s sessions here.' % (op, prov.label))

    def provider_options(self, harness=None, sid=None):
        """What a driven harness offers: {models [{id, label, efforts?}], efforts, modes, current, perMessage}. Empty lists when
        it cannot say yet: never an error and never a wait (the session-3 fix for "loading…" for good on the branch)."""
        prov = self.provider_of(sid) if sid else self.provider_named(harness)
        empty = {'models': [], 'efforts': [], 'modes': [], 'current': {}, 'perMessage': [], 'reading': False}
        if not self.driven(prov) or not callable(getattr(prov, 'options', None)):
            return empty
        try:
            o = prov.options(sid) or {}
        except Exception as e:          # not running yet, or refused: the page shows the harness's defaults
            return {**empty, 'error': str(e)[:200]}
        import inspect
        try:
            takes = set(inspect.signature(prov.send).parameters)
        except (TypeError, ValueError):
            takes = set()
        norm = lambda xs: [x if isinstance(x, dict) else {'id': x, 'label': x} for x in xs or []]
        models = [{**m, 'efforts': norm(m.get('efforts'))} if m.get('efforts') else m for m in o.get('models') or []]
        return {'models': models, 'efforts': norm(o.get('efforts')), 'modes': norm(o.get('modes')), 'current': o.get('current') or {},
                'perMessage': sorted(takes & {'model', 'effort', 'mode'}), 'reading': bool(o.get('reading'))}

    def provider_models(self, harness=None, sid=None):
        """provider_options' models in the page's model shape (as /api/models and /api/claude/models give them): each model
        carries its own efforts, else the harness's."""
        o = self.provider_options(harness, sid)
        eff = o['efforts']
        out = []
        for m in o['models']:
            if not m.get('id'):
                continue
            es = m.get('efforts') or eff
            out.append({'id': m['id'], 'displayName': m.get('label') or m['id'], 'description': m.get('description') or m.get('label') or '',
                        'isDefault': m['id'] == o['current'].get('model'),
                        'supportedReasoningEfforts': [{'reasoningEffort': e['id'], 'description': e.get('label') or ''} for e in es if e.get('id')],
                        'defaultReasoningEffort': o['current'].get('effort') or ''})
        return {'data': out, 'error': o.get('error'), 'known': bool(o['models'] or o['modes']), 'reading': bool(o.get('reading'))}

    def provider_controls(self, harness=None, sid=None):
        """provider_options' modes as the page's approval presets (approvalPolicy carries the harness's own mode id)."""
        o = self.provider_options(harness, sid)
        cur = o['current'].get('mode')
        presets = [{'id': 'pv-' + str(m['id']), 'label': m.get('label') or m['id'], 'approvalPolicy': m['id'], 'sandbox': None,
                    'description': m.get('description') or (m.get('label') or str(m['id'])) + ' (this harness\'s own mode)',
                    **({'default': True} if m['id'] == cur else {})} for m in o['modes'] if m.get('id')]
        if presets and not any(x.get('default') for x in presets):
            presets[0]['default'] = True
        cmds = [c for c in COMMANDS if c.get('name') in ('status', 'diff')]
        return {'presets': presets, 'commands': cmds, 'skills': [], 'config': {'model': o['current'].get('model'), 'effort': o['current'].get('effort')}}
    # ---------- end more providers ----------

    def provider_of(self, sid):
        """The provider a session belongs to: by its id prefix ('<provider name>:'), else a provider that claims it (a driven
        provider's owns() means "we drive it", false for its subagents: never the routing rule alone)."""
        sid = str(sid)
        return next((p for p in self.providers[1:] if sid.startswith(getattr(p, 'name', '\0') + ':')), None) \
            or next((p for p in self.providers[1:] if p.owns(sid)), self.providers[0])

    def provider_state(self):
        out = []
        for p in self.providers:
            try:
                out.append({**p.available(), 'driven': True} if self.driven(p) else p.available())          # more providers: the page lists these as harnesses
            except Exception as e:
                out.append({'name': p.name, 'label': p.label, 'found': False, 'error': str(e)})
        return out

    def all_sessions(self, ready=True):
        """Codex sessions (unchanged) plus every other provider's, in the same shape, each carrying `provider`. Titles are cut at
        TITLE_MAX characters (speed, 2026-11-06: long pasted first prompts were over half of /api/state). ready=False (the first
        answers, listings_ready) leaves the other providers' sessions out."""
        result = [dict(s, provider='codex') for s in self.sessions()]
        for p in self.providers[1:] if ready else []:
            try:
                listed = p.list_sessions()
            except Exception:
                continue          # a provider whose store cannot be read shows nothing; Codex is unaffected
            for s in listed:
                hist = p.cache.get(str(p.paths.get(s['id']))) if hasattr(p, 'cache') and hasattr(p, 'paths') else None
                last = (hist or {}).get('messages') or []
                asking = bool(hist and hist.get('asking') and hasattr(p, 'waiting') and p.waiting(s['id'], hist))          # attention: stopped on a question to you is a turn not ended
                result.append({'id': s['id'], 'title': s['title'], 'cwd': s['cwd'], 'projectId': None, 'source': p.name, 'model': (hist or {}).get('model'),
                               'updatedAt': s['updated'], 'createdAt': s['started'], 'parent': s.get('parent'), 'status': 'active' if s['running'] or asking else 'idle',
                               'managed': False, 'external': bool(s.get('external')), 'turnId': None, 'preview': last[-1]['text'][:180] if last else '', 'provider': p.name,
                               **({'continues': s['continues']} if s.get('continues') else {}), **({'copyOf': s['copyOf']} if s.get('copyOf') else {}),          # continuation: the sessions drawn in this one's column; a copy made here
                               **(self.driven_row(p, s) if self.driven(p) else {})})          # more providers: driven here
        if any(getattr(p, 'name', None) == 'claude' for p in self.providers):      # claude drive
            result = [s for s in result if s.get('provider') != 'claude'] + self.claude_sessions([s for s in result if s.get('provider') == 'claude'])
        waiting_on_agents(result)
        for s in result:
            if isinstance(s.get('title'), str) and len(s['title']) > TITLE_MAX:
                s['title'] = s['title'][:TITLE_MAX - 1] + '…'
        return result

    def driven_row(self, p, s):
        """A driven provider's row: drivable (ours to send to), managed (its process is open here) and its model."""
        try:
            phase = p.phase(s['id'])
        except Exception:
            phase = 'idle'
        own = bool(p.owns(s['id']))
        return {'drivable': own and not s.get('subagent'), 'managed': own and phase != 'idle', 'phase': phase, 'model': s.get('model'),
                **({'status': 'active'} if phase != 'idle' else {}), **({'external': False} if own else {})}

    def watch_settings(self, sid, status=False):
        """A watch-only session's settings as its own store records them (model, permission mode), for the chat's bottom bar
        and /status card; nothing here can change them."""
        p = self.provider_of(sid)
        log = p.read_history(sid)
        st = {'model': log.get('model'), 'permissionMode': log.get('permissionMode'), 'provider': p.name, 'label': p.label, **p.flags(),
              'source': 'the ' + p.label + ' transcript'}
        if p.name == 'claude' and getattr(p, 'drive', None):      # claude drive: what this server set or was told, over the transcript's
            st['model'] = self.claude.alias(st.get('model'))
            try:
                st.update(self.claude.settings(sid))
            except ValueError:
                pass
            st['approvalPolicy'], st['sandbox'] = st.get('permissionMode'), None      # the page's approval select matches presets on these
            if self.plan_known(sid) is not None:
                st['plan'] = self.plan_known(sid)      # plan toggle: what this server started it with
        if self.driven(p):          # more providers: a driven session's current choices, in the page's names
            cur = self.provider_options(sid=sid).get('current') or {}
            st.update(model=cur.get('model') or st.get('model'), effort=cur.get('effort'), approvalPolicy=cur.get('mode'), sandbox=None,
                      permissionMode=cur.get('mode') or st.get('permissionMode'), source='what ' + p.label + ' reported to this server')
        st = {k: v for k, v in st.items() if v is not None or k == 'sandbox'}
        if not status:
            return st
        return {'threadId': sid, 'settings': st, 'provider': p.name, 'running': bool(log.get('turnActive')),
                'context': {'tokens': log.get('context') or 0, 'window': log.get('window'), 'source': 'the ' + p.label + ' transcript (usage of the last model call)'}}

    def log_of(self, s):
        p = self.provider_of(s['id'])
        if p is self.providers[0]:
            return self.observer.cache.get(s['id'], {})
        return p.read_history(s['id'])
    # ---------- end providers ----------

    # ---------- attention: what a column holds for the person, from what the stores record (the page keeps what was read) ----------
    def asking(self, s, log):
        """A watched session stopped on a question to you (providers' waiting(): Claude Code's AskUserQuestion, ExitPlanMode)."""
        p = self.provider_of(s['id'])
        return bool(log.get('asking') and hasattr(p, 'waiting') and p.waiting(s['id'], log))

    def attention_of(self, s, log):
        """say: the session's last answer, when nothing of yours came after it; errors: its failed turns (the transcript's, and for
        Codex the stream's), the last five, each with later (the session did something after it); handback: for a subagent, when its parent got its result and the result's first line.
        Times are epoch seconds."""
        msgs = log.get('messages') or []
        last = next((m for m in reversed(msgs) if m.get('role') in ('assistant', 'user')), None)
        say = last if last and last['role'] == 'assistant' and last.get('text') else None
        if self.asking(s, log):
            say = None          # stopped on a question to you: what it said before the question is not its last word (the question is the item)
        errs = list(log.get('errors') or [])
        for f in getattr(self.bridge, 'failures', {}).get(s['id'], []):
            if not any(f.get('turn') and e.get('turn') == f['turn'] for e in errs):
                errs.append(f)
        errs = sorted(errs, key=lambda e: e['t'])[-5:]
        # later: the session did something of its own after the error (a message, yours or its own; a tool call; a turn begun), so
        # the failure is no longer where it stands: resumed, retried by hand, or a queued message took the turn on (2026-11-09)
        acts = [m.get('t') or 0 for m in msgs] + [e.get('at') or 0 for e in log.get('events') or []] + [x.get('t') or 0 for x in log.get('turns') or []]
        last_act = max(acts, default=0)
        hb = None
        if s.get('parent'):
            p = self.provider_of(s['id'])
            if p is self.providers[0]:
                plog = self.observer.cache.get(s['parent'])
                if plog is None:          # the parent's rollout not read yet (the child came first): read it now (then cached)
                    row = self.session(s['parent'])
                    plog = self.observer.read_log(row) if row and row.get('rollout_path') else {}
                done = (plog or {}).get('subdone', {}).get(s['id'])
                hb = {'t': done, 'text': None} if done else None
            elif hasattr(p, 'handback'):
                try:
                    hb = p.handback(s['id'])
                except Exception:
                    hb = None
        if hb:
            final = next((m for m in reversed(msgs) if m.get('role') == 'assistant' and m.get('text')), None)
            whole = hb.get('text') or (final or {}).get('text')          # the report as the parent received it (Claude Code); a Codex parent's record keeps only when (SubAgentActivity): the child's last answer
            hb = {'at': hb['t'], 'text': first_line(whole) or 'finished', 'full': whole or None}          # text: the first line, for the floor; full: whole, for the chat
        stops, ended = self.stops_of(s['id'], log)          # walk 2026-11-19: the turns you stopped (Codex's record, and the page's for Claude Code)
        return {'stops': [{'at': t} for t in stops], 'ended': ended, 'say': say and {'at': say['t'], 'text': first_line(say['text'])}, 'errors': [err_view(e, last_act) for e in errs], 'handback': hb}
    # ---------- end attention ----------

    # ---------- connectors: /api/connectors ----------
    def connectors(self, cwd):
        """The MCP servers each harness has configured and used in this folder: Codex from config.toml [mcp_servers.*] and
        mcpServerStatus/list when connected; Claude Code from ~/.claude.json and .mcp.json; both from the tool calls their
        sessions here recorded (which is the only record of the desktop app's own and claude.ai connectors)."""
        cwd = str(project_path(cwd))
        rows = [s for s in self.all_sessions() if s['cwd'] and (s['cwd'] == cwd or s['cwd'].startswith(cwd.rstrip('/') + '/'))]
        here = sorted({s['provider'] for s in rows})
        by = {}

        def entry(name, harness):
            cid = connector_id(name)
            c = by.setdefault(cid, {'id': cid, 'name': connector_label(name), 'server': name, 'harnesses': [], 'sources': [], 'kind': None, 'status': None, 'builtin': builtin_server(name),
                                    'logo': cid if (ROOT / 'logos' / (cid + '.svg')).is_file() else None, 'calls': 0, 'tools': {}, 'per': {}})
            if harness not in c['harnesses']:
                c['harnesses'].append(harness)
            return c
        kind = lambda v: 'http' if v.get('url') else 'stdio' if v.get('command') else None          # connectors list: a kind, never the command or URL
        def per_h(c, harness, frm=None, status=None):          # connectors list: where it comes from and its status, per harness
            q = c['per'].setdefault(harness, {'from': None, 'status': 'unknown'})
            if frm and (q['from'] is None or q['from'] == 'seen in sessions only'):
                q['from'] = frm
            if status and (q['status'] == 'unknown' or status != 'unknown'):
                q['status'] = status
        for v in codex_config_servers():
            c = entry(v['name'], 'codex')
            c['sources'].append({'harness': 'codex', 'source': 'file', 'where': v['where']})
            c['kind'] = c['kind'] or kind(v)
            per_h(c, 'codex', 'built into the Codex app' if c['builtin'] else 'Codex config.toml', 'disabled' if v.get('enabled') is False else None)
        if self.bridge.ready:
            try:
                for d in self.bridge.call('mcpServerStatus/list', {'detail': 'toolsAndAuthOnly'}, timeout=8).get('data', []):
                    c = entry(d['name'], 'codex')
                    seen = (getattr(self.bridge, 'mcp', {}).get('*', {}) or {}).get(d['name']) or {}
                    c['status'] = seen.get('status') or d.get('runtimeStatus') or 'not started'
                    c['sources'].append({'harness': 'codex', 'source': 'live', 'where': 'mcpServerStatus/list'})
                    per_h(c, 'codex', 'built into the Codex app' if c['builtin'] else 'Codex config.toml', 'connected' if c['status'] == 'ready' else 'failed' if 'fail' in str(c['status']).lower() else None)
            except (RuntimeError, ValueError):
                pass
        for v in claude_config_servers(cwd):
            c = entry(v['name'], 'claude')
            c['sources'].append({'harness': 'claude', 'source': 'file', 'where': v['where']})
            c['kind'] = c['kind'] or kind(v)
            per_h(c, 'claude', {'user': 'user config', 'mcp.json': 'this project\'s .mcp.json', 'project': 'user config (this project)'}.get(v.get('scope'), 'user config'))
        lists = {}
        if not MOCK_ON:          # connectors list: the harnesses' own lists, from the cache (a stale one refreshes in the background)
            for h in ('claude', 'codex'):
                got = MCP_LISTS.get(h, cwd)
                lists[h] = {'state': got['state'], 'at': got['at']}
                for v in got['rows'] or []:
                    c = entry(('claude_ai_' + v['name'].replace(' ', '_')) if v.get('account') else v['name'], h)
                    if v.get('account'):
                        c['name'], c['account'] = v['name'], True
                    if not any(x['source'] == 'list' and x['harness'] == h for x in c['sources']):
                        c['sources'].append({'harness': h, 'source': 'list', 'where': 'claude mcp list' if h == 'claude' else 'codex mcp list'})
                    c['kind'] = c['kind'] or v.get('kind') or None
                    per_h(c, h, 'claude.ai account' if v.get('account') else ('built into the Codex app' if c['builtin'] else 'Codex config.toml') if h == 'codex' else 'Claude Code config', v['status'])
        for s in rows:
            try:
                log = self.log_of(s)
            except Exception:
                continue
            for e in log.get('events', []):
                if e.get('kind') != 'mcp':
                    continue
                c = entry(e['connector'], s['provider'])
                if not any(x['source'] == 'seen-in-session' and x['harness'] == s['provider'] for x in c['sources']):
                    c['sources'].append({'harness': s['provider'], 'source': 'seen-in-session', 'where': 'tool calls in this folder\'s ' + s['provider'] + ' sessions'})
                    per_h(c, s['provider'], ('built into Claude desktop' if s['provider'] == 'claude' else 'built into the Codex app') if c['builtin'] else 'seen in sessions only')
                c['calls'] += 1
                per = c['tools'].setdefault(e['tool'], {})
                per[s['id']] = per.get(s['id'], 0) + 1
        # ---- mock: connector names (connectors.json): a display name and a logo for a server id; an id with no entry keeps its own ----
        named = connector_names()
        for c in by.values():
            n = named.get(c['server']) or named.get(c['id']) or {}
            c['name'] = n.get('name') or c['name']
            if n.get('logo') and (ROOT / 'logos' / (n['logo'] + '.svg')).is_file():
                c['logo'] = n['logo']
        # ---- end mock: connector names ----
        # connectors list: a claude.ai connector the list names ("Google Drive") and one the sessions recorded by its UUID, named the
        # same by connectors.json, are one connector: the session's entry (its id is what the floor's calls carry) takes the list's word
        for cid, c in list(by.items()):
            if not c.get('account'):
                continue
            twin = next((o for o in by.values() if o is not c and not o.get('account') and o['name'] == c['name'] and 'claude' in o['harnesses']), None)
            if twin:
                twin['sources'] += [x for x in c['sources'] if x not in twin['sources']]
                twin['per']['claude'] = dict(c['per'].get('claude') or {})
                twin['account'] = True
                twin['logo'] = twin['logo'] or c['logo']
                del by[cid]
            elif not c['logo']:          # no session entry: its mark from connectors.json by display name
                n = next((v for v in named.values() if v.get('name') == c['name'] and v.get('logo')), None)
                if n and (ROOT / 'logos' / (n['logo'] + '.svg')).is_file():
                    c['logo'] = n['logo']
        for c in by.values():
            c['shown'] = c['calls'] > 0 or any(h in here for h in c['harnesses'] if any(x['harness'] == h and x['source'] != 'seen-in-session' for x in c['sources']))
        return {'cwd': cwd, 'harnesses': here, 'lists': lists, 'connectors': sorted(by.values(), key=lambda c: (-c['calls'], c['id']))}
    # ---------- end connectors ----------

    def session(self, tid):
        return self.observer.row(tid) or self.bridge.threads.get(tid)

    def ensure_control(self, tid):
        if tid in self.bridge.loaded:
            return
        if writer_locked(tid):
            raise ValueError('This agent is open in another Codex client. You can watch it here. Close it there before continuing here, or fork a new agent.')
        params = {'threadId': tid, 'excludeTurns': True}
        if self.plan_known(tid) is not None:      # plan toggle: a config override lasts as long as the loaded thread, so it goes again
            params['config'] = {PLAN_CONFIG: self.plan_known(tid)}
        r = self.bridge.call('thread/resume', params)
        self.bridge.loaded.add(tid)
        if hasattr(self.bridge, 'settings'):
            remember(self.bridge, tid, r)      # chat controls

    # ---- chat controls: what the status card, the bottom bar and the MCP dropdown read ----
    def controls(self, cwd, quick=False):
        if quick:      # the fixed part at once (approval modes, commands), without asking Codex: the box shows its words before the rest
            return {'presets': PRESETS, 'commands': COMMANDS, 'skills': [], 'config': {}, 'partial': True}
        skills = []
        try:
            for entry in self.bridge.call('skills/list', {'cwds': [cwd]} if cwd else {}).get('data', []):
                skills += [{'name': k['name'], 'description': (k.get('interface') or {}).get('shortDescription') or k.get('shortDescription') or k.get('description', ''), 'path': k['path']} for k in entry.get('skills', []) if k.get('enabled', True)]
        except (RuntimeError, ValueError):
            pass
        try:      # what Codex uses when a new agent is started without a choice: the effective config for this folder
            c = self.bridge.call('config/read', {'cwd': cwd} if cwd else {}).get('config', {})
            config = {'model': c.get('model'), 'effort': c.get('model_reasoning_effort'), 'approvalPolicy': c.get('approval_policy'), 'sandbox': c.get('sandbox_mode')}
        except (RuntimeError, ValueError):
            config = {}
        return {'presets': PRESETS, 'commands': COMMANDS, 'skills': skills, 'config': config}

    def settings_of(self, tid):
        """The thread's settings: as Codex last reported them to this server, else as its state database records them."""
        row = next((r for r in self.observer.rows() if r['id'] == tid), None) or {}
        live = dict(getattr(self.bridge, 'settings', {}).get(tid) or {})
        saved = {'model': row.get('model'), 'effort': row.get('reasoning_effort'), 'approvalPolicy': row.get('approval_mode'), 'sandbox': sandbox_mode(row.get('sandbox_policy')) if row.get('sandbox_policy') else None, 'cwd': row.get('cwd')}
        out = {k: v for k, v in saved.items() if v is not None}
        out.update(live)
        if isinstance(out.get('approvalPolicy'), str) and out['approvalPolicy'].startswith('{'):
            try:
                out['approvalPolicy'] = json.loads(out['approvalPolicy'])
            except ValueError:
                pass
        out['source'] = 'reported by Codex to this server' if live else 'the state database (saved session)'
        out.pop('sandboxPolicy', None)
        if self.plan_known(tid) is not None:
            out['plan'] = self.plan_known(tid)
        return out

    # ---- plan toggle: what each session was started with, for sessions this server started (kept in plan_path between runs) ----
    plan_path = None          # main() sets it under the cache folder; tests set their own or keep it in memory

    def plans(self):
        if not hasattr(self, '_plans'):
            try:
                self._plans = json.loads(Path(self.plan_path).read_text()) if self.plan_path else {}
            except (OSError, ValueError):
                self._plans = {}
        return self._plans

    def plan_known(self, sid):
        v = self.plans().get(sid)
        return v if isinstance(v, bool) else None

    def plan_keep(self, sid, on):
        self.plans()[sid] = bool(on)
        if self.plan_path:
            try:
                path = Path(self.plan_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(self._plans))
                tmp.replace(path)
            except OSError:
                pass          # not written: the page shows the setting until this server stops
    # ---- end plan toggle ----

    # ---- answers kept (walk 2026-11-19): what the person answered through the page, per chat, kept in answers_path between runs ----
    # Neither the Codex rollout (in the shapes the fixtures hold) nor the stream keeps an approval's answer, so an "allowed once" went
    # with the page; the page sends its entry's words with each reply (kept), the server keeps them, and /api/state hands them back.
    # Keyed by the request's own identity in its chat (kept.key: the tool call's item id, else its own time and words), never by the
    # request id, which is a number per connection and recurs in a later run.
    answers_path = None          # main() sets it under the cache folder; tests set their own or keep it in memory

    answers_lock = threading.RLock()          # a provider's own thread keeps an answer while the page reads them (race seen 2026-11-22)

    def answers(self):
        with self.answers_lock:
            if not hasattr(self, '_answers'):
                try:
                    self._answers = json.loads(Path(self.answers_path).read_text()) if self.answers_path else {}
                except (OSError, ValueError):
                    self._answers = {}
            return self._answers

    def answer_keep(self, p, out, req=None):
        k = p.get('kept')
        if isinstance(k, dict) and req is not None and secret_asked(req):          # robust: a secret answer is never kept (the page masks it too)
            k = dict(k, answerText=['(hidden)'])
        if isinstance(k, dict) and isinstance(k.get('threadId'), str) and k.get('said') in ('y', 'always', 'n', 'a'):
            r = p.get('result') if isinstance(p.get('result'), dict) else {}
            words = lambda v: str(v)[:2000] if isinstance(v, (str, int, float)) else None
            key = k.get('key')
            if not isinstance(key, str) or not key.strip():
                return out          # no identity of its own: not kept (it could attach to another request)
            rec = {'key': key[:300], 'said': k['said'], 'at': time.time(), 't': k['t'] if isinstance(k.get('t'), (int, float)) else time.time(),
                   **{f: words(k.get(f)) for f in ('kind', 'verb', 'cmd', 'short', 'label', 'question')},
                   'answerText': [words(x) for x in k['answerText'][:20]] if isinstance(k.get('answerText'), list) else None,
                   'why': words(r.get('message')), 'whySent': (out or {}).get('reason') == 'sent'}
            self.answer_put(k['threadId'], rec)
        return out

    def answer_put(self, tid, rec):
        with self.answers_lock:
            rows = [x for x in self.answers().get(tid, []) if x.get('key') and x['key'] != rec['key']] + [rec]          # a record of the older shape (by id) is dropped
            self._answers[tid] = rows[-200:]
            self.answers_write()

    def answers_write(self):
        if self.answers_path:
            try:
                path = Path(self.answers_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix('.tmp')
                with self.answers_lock:
                    text = json.dumps(self._answers)
                tmp.write_text(text)
                tmp.replace(path)
            except OSError:
                pass          # not written: the answer is kept until this server stops

    # ---- robust (2026-11-21): requests that ended unanswered. A request's id belongs to the app-server connection it came on (JSON-RPC
    # over its stdio): when that process ends, or this server stops (it ends the app-server it started), the turn that asked ended
    # with it and nothing can answer the request any more. So each request open now is kept in requests_path; one still there when the
    # next server starts, or one open when the app-server ended, becomes an entry in its chat (with the answers kept: said 'ended')
    # saying it ended unanswered and why; a message to the agent starts a new turn. A request Codex sends again (same item) shows live.
    def requests_path(self):
        return Path(self.answers_path).with_name(Path(self.answers_path).name.replace('answers', 'open-requests')) if self.answers_path else None

    def requests_sync(self):
        lock = self.__dict__.setdefault('_req_lock', threading.RLock())
        with lock:
            path = self.requests_path()
            if path is None:
                return          # main() has not set where kept state goes yet (or a test keeps none): the bridge holds what ended until then
            if not getattr(self, '_req_loaded', False):
                self._req_loaded = True
                try:
                    left = json.loads(path.read_text()) if path else []
                except (OSError, ValueError):
                    left = []
                for w in left if isinstance(left, list) else []:
                    if isinstance(w, dict):
                        self.ended_keep(dict(w, why='restart'))
                self._req_open = None
            for w in self.bridge.take_ended() if hasattr(self.bridge, 'take_ended') else []:
                self.ended_keep(w)
            now = self.bridge.open_words() if hasattr(self.bridge, 'open_words') else []
            if now != getattr(self, '_req_open', None):
                self._req_open = now
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix('.tmp')
                    tmp.write_text(json.dumps(now))
                    tmp.replace(path)
                except OSError:
                    pass          # not written: a restart would lose sight of these requests

    def ended_keep(self, w):
        tid, key = w.get('threadId'), w.get('key')
        if not isinstance(tid, str) or not isinstance(key, str) or not key:
            return
        words = lambda v: str(v)[:2000] if isinstance(v, (str, int, float)) else None
        rec = {'key': key[:300], 'said': 'ended', 'ended': w.get('why') if w.get('why') in ('restart', 'dropped', 'unsupported') else 'dropped', 'at': time.time(),
               't': w['t'] if isinstance(w.get('t'), (int, float)) else time.time(), **{f: words(w.get(f)) for f in ('kind', 'verb', 'cmd', 'short', 'question', 'method')},
               'label': None, 'answerText': None, 'why': None, 'whySent': False}
        self.answer_put(tid, rec)

    def answered_now(self):
        self.requests_sync()
        return self.answered()

    def answered(self):
        with self.answers_lock:
            return [dict(x, threadId=tid) for tid, rows in self.answers().items() for x in rows if x.get('key')]
    # ---- end answers kept ----

    # ---- stops kept (walk 2026-11-19): when a turn was stopped through the page, per chat, kept in stops_path between runs ----
    # Codex writes turn_aborted on a stop (read_log reads it); a Claude Code transcript (in the shapes the fixtures hold) marks no
    # interrupted turn, so the server keeps its own record of the stops it passed on. A stop made outside the page is then not shown.
    stops_path = None          # main() sets it under the cache folder; tests set their own or keep it in memory

    def kept_stops(self):
        if not hasattr(self, '_stops'):
            try:
                self._stops = json.loads(Path(self.stops_path).read_text()) if self.stops_path else {}
            except (OSError, ValueError):
                self._stops = {}
        return self._stops

    def stop_keep(self, tid, at=None, turn=None):
        """A stop passed on: when it was asked for, and the turn it stopped where the server knows it (Codex's running turn)."""
        if not isinstance(tid, str):
            return
        st = self.kept_stops()
        st[tid] = (st.get(tid, []) + [{'at': at or time.time(), 'turn': turn if isinstance(turn, str) else None}])[-50:]
        if self.stops_path:
            try:
                path = Path(self.stops_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(self._stops))
                tmp.replace(path)
            except OSError:
                pass          # not written: the stop is kept until this server stops

    def stops_of(self, sid, log):
        """The turns stopped in this chat: what its record says (Codex's turn_aborted), and the stops passed on through the page that
        the record does not show; and whether its last turn ended stopped. A page stop and an aborted record are one stop when they
        name the same turn, or else when the record is the first aborted one after the stop with no turn started between them,
        however long the interrupt took."""
        rec = [x for x in (log.get('stops') or []) if x.get('reason') in (None, 'interrupted')]
        starts = [x.get('t') or 0 for x in log.get('turns') or []]
        used, mine = set(), []
        for k in sorted((k if isinstance(k, dict) else {'at': k, 'turn': None} for k in self.kept_stops().get(sid, [])), key=lambda k: k['at']):
            same = next((i for i, r in enumerate(rec) if i not in used and k['turn'] and r.get('turn') == k['turn']), None)
            if same is None:
                nxt = next((i for i, r in sorted(enumerate(rec), key=lambda x: x[1]['t']) if r['t'] >= k['at'] and i not in used), None)
                if nxt is not None and not any(k['at'] < t0 < rec[nxt]['t'] for t0 in starts):
                    same = nxt
            if same is None:
                mine.append(k['at'])
            else:
                used.add(same)
        ts = sorted([r['t'] for r in rec] + mine)[-20:]
        acts = [m.get('t') or 0 for m in log.get('messages') or []] + [x.get('t') or 0 for x in log.get('turns') or []]
        ended = 'stopped' if mine and max(mine) >= max(acts, default=0) else log.get('ended')          # stopped through the page, nothing since
        return ts, ended
    # ---- end stops kept ----

    def mcp_of(self, tid):
        b, loaded = self.bridge, tid in self.bridge.loaded
        try:
            r = b.call('mcpServerStatus/list', {'detail': 'toolsAndAuthOnly', **({'threadId': tid} if loaded else {})})
            data = r.get('data', [])
        except (RuntimeError, ValueError) as e:
            return {'servers': [], 'error': str(e), 'threadScoped': loaded}
        seen = {**getattr(b, 'mcp', {}).get('*', {}), **getattr(b, 'mcp', {}).get(tid, {})}
        servers = [{'name': d['name'], 'status': (seen.get(d['name']) or {}).get('status') or d.get('runtimeStatus') or 'not started', 'error': (seen.get(d['name']) or {}).get('error') or d.get('toolsError'),
                    'tools': len(d.get('tools') or {}), 'auth': d.get('authStatus'), 'plugin': d.get('pluginId')} for d in data]
        return {'servers': servers, 'threadScoped': loaded}

    def status(self, tid):
        b, st = self.bridge, self.settings_of(tid)
        cwd = st.get('cwd') or (self.session(tid) or {}).get('cwd')
        try:
            config = b.call('config/read', {'cwd': cwd} if cwd else {}).get('config', {})
        except (RuntimeError, ValueError):
            config = {}
        rate = getattr(b, 'rate', None)
        if rate is None:
            try:
                rate = b.call('account/rateLimits/read', {}).get('rateLimits')
                b.rate = rate
            except (RuntimeError, ValueError):
                rate = None
        usage = getattr(b, 'usage', {}).get(tid)
        log = self.observer.cache.get(tid, {})
        context = {'tokens': ((usage or {}).get('last') or {}).get('inputTokens') or log.get('context') or 0, 'window': (usage or {}).get('modelContextWindow') or log.get('window'),
                   'total': ((usage or {}).get('total') or {}).get('totalTokens'), 'source': 'thread/tokenUsage/updated' if usage else 'the rollout log (token_count)'}
        return {'threadId': tid, 'settings': st, 'context': context, 'rateLimits': rate, 'mcp': self.mcp_of(tid), 'webSearch': web_search_of(config),
                'instructionSources': st.get('instructionSources'), 'running': tid in b.active}
    # ---- end chat controls ----

    # ---- plan mode switch (2026-11-21): Codex's own Plan mode, per message, as its desktop app sets it ----
    def collab_mode(self, tid, p):
        """The collaborationMode a send carries when it names a mode ('plan' or 'default'), else None. Codex needs a model in it:
        the message's, else the one Codex last reported for the thread, else the one the session list records; with none known
        it says so instead of sending (it never guesses one)."""
        m = p.get('mode')
        if m is None:
            return None
        if m not in ('plan', 'default'):
            raise ValueError('Plan mode is on or off')
        known = dict(getattr(self.bridge, 'settings', {}).get(tid) or {})
        model = p.get('model') or known.get('model') or (self.session(tid) or {}).get('model')
        if not model:
            raise ValueError('Plan mode needs a model, and this session has none known yet: choose one in the model picker, then send again.')
        effort = p.get('effort') or known.get('effort') or 'medium'
        return {'mode': m, 'settings': {'model': str(model), 'reasoning_effort': str(effort), 'developer_instructions': None}}

    def mode_keep(self, tid, mode):
        """plan mode switch: the mode last sent to the thread, shown back by settings_of ('plan' / 'default')."""
        if mode and hasattr(self.bridge, 'settings'):
            self.bridge.settings.setdefault(tid, {})['mode'] = mode['mode']

    # ---- questions: an async question answered (a message to the agent) or dismissed (nothing sent) ----
    def async_reply(self, p):
        b, key = self.bridge, str(p.get('id'))
        req = b.requests.get(key) if key in getattr(b, 'async_ids', ()) else None
        if not req:
            raise ValueError('This question has already been answered.')
        result = p.get('result') if isinstance(p.get('result'), dict) else {}
        if result.get('dismiss') is True:
            b.async_drop(key)
            return {'reason': 'dismissed'}
        text = async_answer_text(req, checked_answers(req, result))
        self.action('send', {'threadId': req['params']['threadId'], 'text': text})          # a steer while its turn runs, else a new turn
        b.async_drop(key)
        return self.answer_keep(p, {'reason': 'sent', 'text': text}, req)

    def reply_reason(self, req, result):
        """piece B (after the other version's reply_reason): Codex's decline carries no words, so the line written with a no goes
        to the agent as a message right after it: a steer into the turn the request stopped, or, when that turn has ended (a
        cancel stops it), the next turn. Two sends: if the turn ends between them, the line starts a new turn instead. The
        attention mock records the line instead of sending it. Returns what became of the line, so the page can say so."""
        why = reason_text(result)
        if not (req and why and isinstance(result, dict) and result.get('decision') in ('decline', 'cancel')):
            return {}
        text, tid = 'I declined ' + what_of(req) + '. ' + why, req['params'].get('threadId')
        if str(req['id']) in getattr(self.bridge, 'mock_ids', ()):
            self.bridge.mock_sent.append({'id': req['id'], 'threadId': tid, 'message': text})
            if MOCK:          # the mock's thread gets the line as Codex's would (mock.send: a user message in its rollout)
                import mock
                mock.send(MOCK, tid, text)
            return {'reason': 'sent'}
        try:
            if result.get('decision') == 'cancel':          # a cancel stops the turn: wait (at most 5 s) for it to end, so the line starts the next one
                for _ in range(50):
                    if tid not in getattr(self.bridge, 'active', {}):
                        break
                    time.sleep(0.1)
            self.action('send', {'threadId': tid, 'text': text})
            return {'reason': 'sent'}
        except Exception as e:          # the decline went; the line did not: the page says so
            return {'reason': 'not sent', 'error': str(e)}

    def action(self, op, p):
        if op == 'stop' and not p.get('_kept'):          # stops kept: a stop that went through is recorded for its chat
            at, turn = time.time(), getattr(self.bridge, 'active', {}).get(p.get('threadId'))          # asked for now; the turn it stops (Codex), before the stop clears it
            out = self.action(op, dict(p, _kept=True))
            self.stop_keep(p.get('threadId'), at=at, turn=turn)
            return out
        b = self.bridge
        if op == 'start' and p.get('harness') == 'claude':      # claude drive: a new Claude Code agent
            return self.claude_action(op, p)
        if op == 'start' and p.get('harness') not in (None, 'codex'):      # more providers: Antigravity, Cursor, OpenCode
            return self.start_on(p['harness'], p)
        if op == 'project':
            path = Path(p['path']).expanduser()
            if not path.is_absolute():
                raise ValueError('Choose an absolute folder path.')
            if p.get('create'):
                path.mkdir(parents=True, exist_ok=False)
                if p.get('git', True):
                    run_git(path, 'init')
            path = project_path(str(path))
            return b.call('project/create', {'idempotencyKey': p.get('key') or secrets.token_hex(16), 'name': p.get('name') or path.name, 'roots': [{'path': str(path)}]})
        if op == 'start':
            cwd = str(project_path(p['cwd']))
            params = {'cwd': cwd, 'approvalPolicy': p.get('approvalPolicy') or 'on-request', 'sandbox': p.get('sandbox') or 'workspace-write', 'approvalsReviewer': 'user', 'threadSource': 'user'}
            # chat controls: the approval mode is an approval policy plus a sandbox mode, in Codex's names; thread/start has no
            # effort field, so the effort goes in as the config override model_reasoning_effort (the response reports it back)
            if params['sandbox'] not in SANDBOX_MODES:
                raise ValueError('Unsupported access setting')
            if params['approvalPolicy'] not in APPROVAL_POLICIES:
                raise ValueError('Unsupported approval policy')
            if p.get('model'):
                params['model'] = p['model']
            if p.get('effort'):
                params['config'] = {'model_reasoning_effort': str(p['effort'])}
            plan = plan_of(p, 'codex')
            params.setdefault('config', {})[PLAN_CONFIG] = plan      # plan toggle: this thread's setting either way, whatever config.toml says
            if p.get('projectId'):
                params['projectId'] = p['projectId']
            result = b.call('thread/start', params)
            tid = result['thread']['id']
            b.loaded.add(tid)
            b.threads[tid] = {**result['thread'], 'model': result.get('model')}
            if hasattr(b, 'settings'):
                remember(b, tid, result)      # chat controls
            self.plan_keep(tid, plan)
            return result
        if op == 'reply' and str(p.get('id')).startswith('claude-ask:'):      # attention: a question in a session this server only watches
            raise ValueError('That question is in a Claude Code session this page only watches: answer it in Claude Code.')
        if op == 'reply' and str(p.get('id')) in self.claude.requests:      # claude drive (piece B: a no's line goes in the deny itself)
            return self.answer_keep(p, self.claude.reply(p['id'], p['result']))
        if op == 'reply' and str(p.get('id')).startswith(ASYNC_PREFIX):          # questions: an async question is answered by a message
            return self.async_reply(p)
        if op == 'reply' and str(p.get('id')) not in getattr(b, 'requests', {}) and (owner := self.request_owner(p.get('id'))):      # more providers
            return self.answer_keep(p, owner.reply(p['id'], answer_shape(p.get('result') or {})) or {})
        if op == 'reply':
            req = b.reply(p['id'], p['result'])
            return self.answer_keep(p, self.reply_reason(req, p['result']), req)      # piece B: a no's line, as a message right after the decline
        tid = p.get('threadId')
        if isinstance(tid, str) and getattr(self.provider_of(tid), 'name', None) == 'claude' and self.claude.available().get('ready'):      # claude drive
            return self.claude_action(op, p)
        if isinstance(tid, str) and self.driven(self.provider_of(tid)):      # more providers: sessions this server drives itself
            return self.provider_action(self.provider_of(tid), op, tid, p)
        if isinstance(tid, str) and self.provider_of(tid) is not self.providers[0]:      # providers: other harnesses are watch-only here
            raise ValueError(self.provider_of(tid).label + ' sessions are watch-only here: sending, stopping and approving go through ' + self.provider_of(tid).label + ' itself for now.')
        if not isinstance(tid, str) or not self.session(tid):
            raise ValueError('Unknown agent')
        if MOCK and op in ('fork', 'stop'):          # mock: its app-server knows none of the fixture threads (as a send, below): the rollout records it
            import mock
            if op == 'fork':
                return mock.fork(MOCK, tid)
            turn = b.active.pop(tid, None)
            if not turn:
                raise ValueError('This agent is not running.')
            return mock.stop(MOCK, tid, turn)
        if op == 'fork':
            # Forking reads history; it never takes over the external writer.
            result = b.call('thread/fork', {'threadId': tid, 'excludeTurns': True})
            b.loaded.add(result['thread']['id'])
            b.threads[result['thread']['id']] = result['thread']
            return result
        if MOCK and op == 'send':          # mock: its app-server has an empty home and knows none of the fixture threads; the message is recorded (mock.send)
            import mock
            return mock.send(MOCK, tid, p.get('text', ''))
        self.ensure_control(tid)
        if op == 'send':
            text = p.get('text', '').strip()
            if not text:
                raise ValueError('Write a message first.')
            items = [{'type': 'text', 'text': text}]
            if p.get('skill') and p['skill'].get('name') and p['skill'].get('path'):      # chat controls: a $skill chip
                items.insert(0, {'type': 'skill', 'name': p['skill']['name'], 'path': p['skill']['path']})
            over = overrides(p)      # chat controls: model, effort, approval policy, sandbox for this turn and the ones after
            mode = self.collab_mode(tid, p)          # plan mode switch: Codex's collaboration mode, when the message names one
            if tid in b.active:
                if over or mode:      # turn/steer takes no settings: they go to the thread for its next turn
                    b.call('thread/settings/update', {'threadId': tid, **over, **({'collaborationMode': mode} if mode else {})})
                    remember(b, tid, over)
                    self.mode_keep(tid, mode)
                return b.call('turn/steer', {'threadId': tid, 'expectedTurnId': b.active[tid], 'input': items})
            params = {'threadId': tid, 'input': items, **over}
            # chat-log: ask for reasoning summaries, as the Codex desktop app does (its threads record reasoning_summary "detailed");
            # without it this app's threads run with summary "none" and stream no reasoning text
            params['summary'] = p.get('summary') or 'detailed'
            if p.get('model'):
                params['model'] = p['model']
            if p.get('effort'):
                params['effort'] = p['effort']
            if mode:
                params['collaborationMode'] = mode
            result = b.call('turn/start', params)
            if result.get('turn', {}).get('status') == 'inProgress':
                b.active[tid] = result['turn']['id']
            if over and hasattr(b, 'settings'):
                remember(b, tid, over)      # chat controls
            self.mode_keep(tid, mode)
            return result
        if op == 'stop':
            turn = b.active.get(tid)
            if not turn:
                raise ValueError('This agent is not running.')
            return b.call('turn/interrupt', {'threadId': tid, 'turnId': turn})
        if op == 'rename':
            result = b.call('thread/name/set', {'threadId': tid, 'name': p['name']})
            if tid in b.threads:
                b.threads[tid]['name'] = p['name']
            return result
        if op == 'resume':
            return {}
        # ---- chat controls: compact, review, init ----
        if op == 'compact':
            return b.call('thread/compact/start', {'threadId': tid})
        if op == 'review':
            text = (p.get('text') or '').strip()
            target = {'type': 'custom', 'instructions': text} if text else {'type': 'uncommittedChanges'}
            return b.call('review/start', {'threadId': tid, 'target': target, 'delivery': 'inline'})
        if op == 'init':      # no protocol method: Codex's /init is the TUI sending its built-in prompt as a message
            return self.action('send', {'threadId': tid, 'text': init_prompt()})
        # ---- end chat controls ----
        raise ValueError('Unknown action')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    @property
    def app(self):
        return self.server.workspace

    def log_message(self, fmt, *args):
        pass

    def check_origin(self):
        expected = {'127.0.0.1:' + str(self.server.server_port), 'localhost:' + str(self.server.server_port)}
        if self.headers.get('Host') not in expected:
            raise ValueError('Invalid host')
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + h for h in expected}:
            raise ValueError('Cross-origin access is disabled')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise ValueError('Cross-site access is disabled')

    def output(self, data, status=200, content_type='application/json'):
        raw = json.dumps(data).encode() if content_type == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        # 'wasm-unsafe-eval': the file window's parser (vendor/, WebAssembly) may compile; JavaScript eval stays refused (2026-11-22)
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(raw)

    def output_tagged(self, data, ignore=()):
        """speed (2026-11-06): a polled read carries a tag of its content (keys in `ignore`, such as the clock, left out); when the
        page already holds that content it gets a bodiless 304 instead of the same JSON again."""
        # speed (3rd pass): the body is encoded once and the tag taken from it (keys in their built order, which is fixed), and the
        # left-out keys are added after it: before, the content was encoded twice, once with its keys sorted for the tag
        rest = json.dumps({k: v for k, v in data.items() if k not in ignore})
        extra = ', '.join(json.dumps(k) + ': ' + json.dumps(data[k]) for k in ignore if k in data)
        tag = '"' + hashlib.blake2b(rest.encode(), digest_size=16).hexdigest() + '"'
        if self.headers.get('If-None-Match') == tag:
            self.send_response(304)
            self.send_header('ETag', tag)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        raw = (rest if not extra else rest[:-1] + (', ' if rest != '{}' else '') + extra + '}').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('ETag', tag)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        try:
            self.check_origin()
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == '/api/bootstrap':
                self.output({'token': self.app.token, **self.app.snapshot()})
            elif u.path == '/api/state':
                snap = self.app.snapshot()
                if q.get('lite'):          # speed (2nd pass): the floor never shows a session's preview text, a fifth of the poll
                    snap = {**snap, 'sessions': [{k: v for k, v in s.items() if k != 'preview'} for s in snap['sessions']]}
                self.output_tagged(snap)
            elif u.path == '/api/mock' and MOCK:          # attention mock only: what happens next outside this server (mock.act: the person answering in
                import mock                                # Claude Code, a subagent reporting back), appended to the scenario's own transcript; no such route otherwise
                self.output(mock.act(MOCK, q.get('do', '')))
            elif u.path == '/api/projects':
                self.output(self.app.bridge.call('project/list', {'limit': 100}))
            elif u.path == '/api/models':
                mock_late()
                self.output(self.app.bridge.call('model/list', {}))
            elif u.path == '/api/providers':      # providers panel: Codex and Claude Code, installed, signed in, ready, at once from the cache
                self.output(self.app.provider_setup.get())
            elif u.path == '/api/provider/options':      # more providers: a driven harness's own models, efforts and modes (never waits)
                self.output(self.app.provider_options(q.get('harness'), q.get('id')))
            elif u.path == '/api/provider/models':      # ... the same in the page's model shape
                self.output(self.app.provider_models(q.get('harness'), q.get('id')))
            elif u.path == '/api/provider/controls':      # ... its modes as the approval picker's presets
                self.output(self.app.provider_controls(q.get('harness'), q.get('id')))
            elif u.path == '/api/claude/state':      # claude drive: is the bridge set up, which binary it starts
                self.output(self.app.claude.available())
            elif u.path == '/api/claude/models':      # claude drive: the SDK's model list in the page's model shape
                self.output(self.app.claude.models(str(project_path(q.get('cwd') or str(DEFAULT_PROJECT)))))
            elif u.path == '/api/claude/controls':      # claude drive: permission modes and the commands that mean something here
                self.output({'presets': claude_bridge.PRESETS, 'commands': claude_bridge.COMMANDS, 'skills': [], 'config': {}})
            elif u.path == '/api/controls':      # chat controls: approval modes, commands, skills (&quick=1: the fixed part only)
                if q.get('quick') != '1':
                    mock_late()
                self.output(self.app.controls(q.get('cwd'), quick=q.get('quick') == '1'))
            elif u.path in ('/api/status', '/api/settings') and self.app.provider_of(q.get('id', '')) is not self.app.providers[0]:      # providers: watch-only sessions
                self.output(self.app.watch_settings(q['id'], status=u.path == '/api/status'))
            elif u.path in ('/api/status', '/api/mcp', '/api/settings'):      # chat controls: one session's status card, MCP list, settings
                if not self.app.session(q.get('id', '')):
                    raise ValueError('Unknown agent')
                self.output(self.app.status(q['id']) if u.path == '/api/status' else self.app.mcp_of(q['id']) if u.path == '/api/mcp' else self.app.settings_of(q['id']))
            elif u.path == '/api/history' and self.app.provider_of(q.get('id', '')) is not self.app.providers[0]:      # providers
                log = self.app.provider_of(q['id']).read_history(q['id'])
                self.output(history_since(q['id'], {'messages': log['messages'], 'turnActive': log['turnActive'], 'contexts': log.get('contexts', []), 'compacts': history_compacts(log), 'notes': notes_of(log)[0],      # memory: summaries' first lines, notes
                             'context': log.get('context', 0), 'window': log.get('window'), 'base': log.get('base'), 'truncated': bool(log.get('truncated')),
                             'reasoning': log.get('reasoning', []), 'turns': log.get('turns', []), 'provider': self.app.provider_of(q['id']).name}, q.get('since')))
            elif u.path == '/api/history':
                row = self.app.session(q['id'])
                if not row:
                    raise ValueError('Unknown agent')
                log = self.app.observer.read_log(row) if row.get('rollout_path') else {'messages': [], 'turnActive': False}
                result = {'messages': log['messages'], 'turnActive': log['turnActive'], 'contexts': log.get('contexts', []), 'compacts': history_compacts(log), 'notes': notes_of(log)[0],      # memory
                          'context': log.get('context', 0), 'window': log.get('window'), 'base': log.get('base'), 'truncated': bool(log.get('truncated')),
                          'reasoning': log.get('reasoning', []), 'turns': log.get('turns', [])}  # chat-log: items of kind "reasoning" {text, t, end, id, turn}; turn starts {id, t}
                # Pagination is also available for users who request older turns.
                if q.get('cursor'):
                    result['page'] = self.app.bridge.call('thread/turns/list', {'threadId': q['id'], 'limit': 20, 'itemsView': 'full', 'cursor': q['cursor'], 'sortDirection': 'desc'})
                self.output(history_since(q['id'], result, q.get('since')))
            elif u.path == '/api/files':
                self.output(files_in(q['cwd']))
            elif u.path == '/api/file':
                p = file_path(q['cwd'], q['path'])
                if p.stat().st_size > 2_000_000:
                    raise ValueError('File is too large to edit here (2 MB limit).')
                data = p.read_bytes()
                if b'\0' in data:
                    raise ValueError('Binary file')
                self.output({'text': data.decode('utf-8'), 'version': hashlib.sha256(data).hexdigest(), 'mtime': p.stat().st_mtime})          # mtime: the file window says how old the file is (2026-11-22)
            elif u.path == '/api/diff' and q.get('status'):          # speed (2026-11-06): the page's poll tints changed files and needs nothing else
                root = project_path(q['cwd'])
                try:
                    self.output({'status': git_scan(root)['status']})
                except ValueError:          # a folder outside Git: nothing to tint, and no 400 in the page's console every poll
                    self.output({'status': '', 'repo': False})
            elif u.path == '/api/edits':          # reading what an agent changed: its own recorded edits, read from its record on request (edits.py)
                import edits
                self.output(edits.of_agent(self.app, q.get('id', '')))
            elif u.path == '/api/diff':
                cwd = project_path(q['cwd'])
                try:
                    diff = run_git(cwd, 'diff', 'HEAD', '--', '.')
                except ValueError:
                    diff = run_git(cwd, 'diff', '--cached', '--', '.') + run_git(cwd, 'diff', '--', '.')
                untracked = run_git(cwd, 'ls-files', '-o', '--exclude-standard').splitlines()
                for name in untracked[:50]:
                    f = file_path(str(cwd), name)
                    if f.is_file() and f.stat().st_size < 100_000:
                        try:
                            text = f.read_text()
                        except (UnicodeError, OSError):
                            continue
                        diff += '\n--- /dev/null\n+++ ' + name + '\n' + '\n'.join('+' + line for line in text.splitlines()) + '\n'
                status = run_git(cwd, 'status', '--short')
                git_invalidate(cwd, git_status_sig(cwd))      # git: the poll drops a stale /api/git answer
                self.output({'diff': diff[:2_000_000], 'status': status})
            elif u.path == '/api/memory':      # memory: base, compaction summaries, instruction files, notes of one session
                self.output(memory_of(self.app, q.get('id', '')))
            elif u.path == '/api/git':      # git: branches, worktrees, commits (read only); ?tree=<branch> lists a branch's files
                if not q.get('tree') and not git_repo(q['cwd']):
                    self.output(self.nested_git(q['cwd'], int(q.get('lanes') or GIT_LANES)))          # a folder outside Git: the repositories in it, or none (no error in the page's console)
                else:
                    self.output(git_tree(q['cwd'], q['tree']) if q.get('tree') else git_data(q['cwd'], int(q.get('lanes') or GIT_LANES)))
            elif u.path == '/api/connectors':      # connectors
                self.output(self.app.connectors(q['cwd']))
            elif u.path == '/api/canvas':
                self.output_tagged(self.canvas(q['cwd']), ignore=('now',))
            elif u.path == '/api/usage':          # usage panel: the floor's spend at API prices and every plan's windows, read only
                # no folder (empty or missing cwd): the plan blocks alone, no floor spend (the main line answered "'cwd'" here);
                # look=1: Plan use is open or its gauge hovered, so the slow plan readers may run (providers/planuse.py)
                cwd = (q.get('cwd') or '').strip()
                self.output(usage.collect(self.app, project_path(cwd) if cwd else None, look=q.get('look') == '1'))
            elif u.path == '/api/compaction':          # the columns' scale: each floor session's compaction point, read only
                self.output(compact_points.collect(self.app, project_path(q['cwd'])))
            elif u.path == '/api/events':
                self.stream(int(self.headers.get('Last-Event-ID') or q.get('after', 0)))
            else:
                # The page is live.html, served at / only; the old panel frontend stays on disk but is not served.
                name = 'live.html' if u.path in ('/', '/live.html') else u.path.lstrip('/')
                allowed = {'live.html'} | {'logos/' + f.name for f in (ROOT / 'logos').glob('*.svg')}      # the provider logos, read only
                allowed |= {'engine/' + f.name for f in (ROOT / 'engine').glob('*.js')}      # the map engine's files beside the page, read only
                allowed |= {'brand/' + f.name for f in (ROOT / 'brand').glob('*') if f.suffix in ('.css', '.woff2')}      # the app's title band: the wordmark's style and typeface, read only
                allowed.add('brand/favicon.svg')      # the tab's icon (the solid logo, written by brand/logo-draft/make.py), read only; the rest of brand/logo-draft is not served
                allowed |= {'vendor/' + f.name for f in (ROOT / 'vendor').glob('*') if f.suffix in ('.js', '.wasm')}      # the file window's parser (web-tree-sitter and its grammars, vendor/LICENSES.md), read only
                if name not in allowed:
                    self.output({'error': 'Not found'}, 404)
                    return
                self.output((ROOT / name).read_bytes(), content_type=mimetypes.guess_type(name)[0] or 'text/plain')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.output({'error': str(e)}, 400)

    def do_POST(self):
        try:
            self.check_origin()
            if self.headers.get('X-Workspace-Token') != self.app.token:
                self.output({'error': 'Invalid workspace token'}, 403)
                return
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 2_100_000:
                raise ValueError('Invalid request size')
            p = json.loads(self.rfile.read(length))
            op = urlparse(self.path).path.removeprefix('/api/')
            if op == 'pick-folder':
                result = pick_folder()
            elif op == 'providers':          # providers panel: {name, action: signin | setup | recheck}
                result = self.app.provider_setup.act(p.get('name'), p.get('action'), p.get('code'))          # code: Antigravity's pasted sign-in code (never kept)
            elif op == 'file':
                path = file_path(p['cwd'], p['path'])
                current = path.read_bytes()
                if hashlib.sha256(current).hexdigest() != p['version']:
                    self.output({'error': 'This file changed on disk. Reopen it before saving.'}, 409)
                    return
                path.write_text(p['text'])
                result = {'version': hashlib.sha256(path.read_bytes()).hexdigest(), 'mtime': path.stat().st_mtime}
            else:
                result = self.app.action(op, p)
            self.output(result)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.output({'error': str(e)}, 400)

    def stream(self, cursor):
        b = self.app.bridge
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        def since(c):          # speed (2026-11-06): the buffer is in order, so only its new tail is read
            out = []
            for i, m in reversed(b.events):
                if i <= c:
                    break
                out.append((i, m))
            out.reverse()
            return out
        while True:
            with b.condition:
                events = since(cursor)
                if not events:
                    b.condition.wait(15)
                    events = since(cursor)
            if not events:
                self.wfile.write(b': heartbeat\n\n')
            for i, m in events:
                self.wfile.write(('id: ' + str(i) + '\ndata: ' + json.dumps(m) + '\n\n').encode())
                cursor = i
            self.wfile.flush()

    def nested_git(self, cwd, lanes):
        """A folder outside Git (floor, 2026-11-21): {'repo': False} and, when it holds repositories or their worktrees, `repos`:
        each one's /api/git answer with `name` (its folder's name) and `rel` (its folder inside this one, or None when it is
        elsewhere). Lanes as for any repository: the three newest branches (the agents in worktrees stand on the floor)."""
        root = str(project_path(cwd))
        tops = nested_repos(root)
        if not tops:
            return {'repo': False}
        out = []
        for top in tops:
            try:
                d = git_data(top, lanes)
            except ValueError:
                continue          # a repository git cannot read (no commit yet): no tree for it
            inside = top.startswith(root + '/')
            out.append(dict(d, name=os.path.basename(top), rel=top[len(root) + 1:] if inside else None))
        return {'repo': False, 'repos': out} if out else {'repo': False}

    def canvas(self, cwd):
        cwd = str(project_path(cwd))
        # providers: every harness's sessions in this folder or below it; a subfolder session's paths are re-rooted at the project
        rows = [s for s in self.app.all_sessions(self.app.listings_ready()) if s['cwd'] and (s['cwd'] == cwd or s['cwd'].startswith(cwd.rstrip('/') + '/'))]          # speed (2026-11-21): the first floor does not wait on the first listing
        # Keep the current work legible; archived history remains accessible in the session list.
        order = lambda s: (s['status'] == 'active', s['updatedAt'])
        rows = sorted(rows, key=order, reverse=True)
        if len(rows) > 30:          # providers: each harness keeps an equal share of the 30 columns; the newest of the rest fill what is left
            by = {}
            for s in rows:
                by.setdefault(s['provider'], []).append(s)
            keep = [s for v in by.values() for s in v[:30 // len(by)]]
            keep += [s for s in rows if s not in keep][:30 - len(keep)]
            # families stay whole (2026-11-21: finished subagents vanished from under their parent when newer sessions pushed
            # them out of the 30): every kept session's family (its root's subagents, all levels) rides along; a finished subagent
            # folds into its parent's column, so it takes no column. FAMILY_CAP bounds the whole (newest first)
            by_id = {s['id']: s for s in rows}
            def root_of(s):
                seen = set()
                while s.get('parent') in by_id and s['id'] not in seen:
                    seen.add(s['id']); s = by_id[s['parent']]
                return s['id']
            roots = {root_of(s) for s in keep}
            keep += [s for s in rows if s not in keep and root_of(s) in roots][:max(0, FAMILY_CAP - len(keep))]
            rows = sorted(keep, key=order, reverse=True)
        origin = min([s['createdAt'] for s in rows] or [STARTED])          # an empty floor counts from the server's start: a fixed origin, so its tag holds
        events, sessions = [], []
        for s in rows:
            log = self.app.log_of(s)          # providers
            if s['status'] != 'active' and self.app.asking(s, log):
                s = dict(s, status='active')          # attention: stopped on a question to you is a turn not ended (all_sessions knows it once the history is read)
            home = '' if s['cwd'] == cwd else s['cwd'][len(cwd.rstrip('/')) + 1:]
            re_root = (lambda p: home + '/' + p if home and p else p)
            base = log.get('base')
            at = self.app.attention_of(s, log)          # attention: the last answer, the failed turns and a subagent's hand-back
            sessions.append({'id': s['id'], 'parent': s['parent'], 'label': s['title'][:70], 'request': '', 'handback': at['handback'], 'say': at['say'], 'errors': at['errors'], 'stops': at['stops'], 'ended': None if s['status'] == 'active' else at['ended'], 'start': max(0, s['createdAt'] - origin), 'end': None if s['status'] == 'active' else max(1, s['updatedAt'] - origin), 'state': 'running' if s['status'] == 'active' else 'done',
                             'context': log.get('context', 0), 'window': log.get('window'), 'base': base and {'t': max(0, base['t'] - origin), 'tokens': base['tokens']},
                             'provider': s['provider'], 'home': home, 'title': s['title'], 'copy': bool(s.get('copy')), 'waitingOn': s.get('waitingOn') or 0, **({'continues': s['continues']} if s.get('continues') else {}),
                             **({'plan': plans.relative(log['plan'], origin)} if log.get('plan') else {}),          # plan progress: the plan's states over time
                             'compaction': compact_points.point_of(self.app, s, log),          # the columns' scale: where this session compacts
                             **({'archivedAt': s['archivedAt']} if s.get('archivedAt') else {})})          # archive stack: archived by Codex (epoch seconds)
            # tokens: each item's share of the context growth between the two token readings around it, split by text length
            events.extend({'session': s['id'], 't': max(0, e['at'] - origin), 'kind': e['kind'], 'path': re_root(e['path']), 'text': e['text'], 'tokens': e.get('tokens', 0), 'item': e.get('item'), **({'lines': e['lines']} if e.get('lines') is not None else {}),
                           **({'connector': connector_id(e['connector']), 'tool': e.get('tool'), 'end': max(0, e['end'] - origin) if e.get('end') else None} if e['kind'] == 'mcp' else {})} for e in log.get('events', []))      # connectors: an MCP call's server, tool and end
            events.extend({'session': s['id'], 't': max(0, c['t'] - origin), 'kind': 'compact', 'from': c['from'], 'to': c['to'], 'first': summary_first(c.get('summary')), 'encrypted': bool(c.get('encrypted'))} for c in log.get('compacts', []))      # memory: the summary's first line
        files = files_in(cwd, [(e['t'] + origin, e['path']) for e in events if e['kind'] == 'write' and e.get('path')])      # speed (2nd pass): an agent's new file
        born = births(cwd, origin, [(e['t'], e['path']) for e in events if e['kind'] == 'write' and e.get('path')], files['files'])      # floor grows: when each new file came
        return {'name': Path(cwd).name, 'origin': origin, 'now': max(1, time.time() - origin), 'files': files['files'], 'born': born, 'sessions': sessions, 'events': events, 'asks': [], 'window': None, 'truncated': files['truncated']}


def keep_between_runs(ws, first=30, every=120):
    """speed (3rd pass): the Claude Code provider's settled readings (providers/keep.py) are put back from the last run before the
    first request and written again a little after start, every two minutes when there is something new, and at exit. They live in
    keep.cache_dir(), made here when missing. Returns the file's path (None without a Claude Code provider)."""
    from providers import keep
    p = next((x for x in ws.providers if getattr(x, 'name', None) == 'claude'), None)
    if not p:
        return
    folder = keep.cache_dir()          # packaging (2026-11-09): the user's app data folder, or AGENT_IDE_CACHE_DIR; never the workspace
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass          # a folder that cannot be made only costs each start its reading (save tries again)
    path = folder / ('claude-' + hashlib.blake2b(str(p.home).encode(), digest_size=6).hexdigest() + '.json')
    keep.load(p, keep.read(path))
    state = {'last': None}

    def save():
        f = keep.fingerprint(p)
        if f != state['last']:
            try:
                keep.save(p, path)
                state['last'] = f
            except Exception:
                pass          # a cache that cannot be written only costs the next start its reading

    def loop():
        time.sleep(first)
        while True:
            save()
            time.sleep(every)
    threading.Thread(target=loop, daemon=True).start()
    atexit.register(save)
    return path


# ---------- robust (2026-11-21): stopping ----------
# The app's shell stops the server with SIGTERM to its process group (uv and everything under it); a terminal stops it with
# Ctrl-C (SIGINT); a closed terminal sends SIGHUP. Each ends it the same way, within about CHILD_GRACE + 1 s: the port closes,
# Codex's app-server is stopped for good (no restart: Bridge.closing is set first), the Claude Code worker's process group is
# ended (its CLI launchers end each CLI's own group), the log watch stops. If that has not finished in STOP_LIMIT s, the process
# ends anyway. Handlers are installed whatever the signals' inherited state: a process started in the background by a
# non-interactive shell inherits SIGINT ignored, and Python then never raises KeyboardInterrupt (what left the server running
# after two SIGINTs when the planner tried it on 2026-11-21); and SIGTERM's default ended Python without any of this, leaving
# the Claude worker groups (sessions of their own) behind.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
CHILD_GRACE = 1.0          # s Codex's app-server and the Claude worker group get after SIGTERM before SIGKILL (judgement)
STOP_LIMIT = 4.0          # s after the first stop signal at which the process ends however far the orderly stop got (judgement)
PARENT_EVERY = 1.0          # s between looks at whether the app that started this server is still there


class Stop(BaseException):
    """Raised in the main thread by a stop signal; unwinds serve_forever (or the start) into main's stop_all."""


def stop_on_signals(state):
    def on_signal(sig, frame):
        if state.get('why'):
            return          # a second signal (uv passes its own on while the group gets one too) changes nothing
        state['why'] = signal.Signals(sig).name
        threading.Thread(target=lambda: (time.sleep(STOP_LIMIT), os._exit(1)), name='agent-ide-stop-limit', daemon=True).start()
        if not state.get('stopping'):          # already in stop_all (the server ended another way): let it finish
            raise Stop(state['why'])
    for sig in STOP_SIGNALS:
        signal.signal(sig, on_signal)


def watch_parent(pid=None, every=PARENT_EVERY):
    """robust (2026-11-21): the app's shell names itself in AGENT_IDE_PARENT_PID. When that process is gone (the shell crashed or
    was killed and could not stop the server), or this process's own parent changed (uv ended and it was handed on), the server
    stops itself as on SIGTERM. Not started without the variable: a server run by hand outlives its terminal's tab as before."""
    pid = pid or os.environ.get('AGENT_IDE_PARENT_PID')
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    first = os.getppid()

    def gone():
        if os.getppid() != first:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True          # another user's process: the pid was handed on after ours ended
        return False

    def look():
        while not gone():
            time.sleep(every)
        print('Agent workspace: the app that started this server is gone; stopping.', flush=True)
        os.kill(os.getpid(), signal.SIGTERM)
    t = threading.Thread(target=look, name='agent-ide-parent', daemon=True)
    t.start()
    return t


def stop_all(bridge=None, workspace=None, watcher=None, server=None, grace=CHILD_GRACE):
    """The orderly stop: the port closed first, then Codex's app-server (with any other this server started), the Claude Code
    worker group and the log watch, side by side, each bounded by `grace`."""
    if bridge is not None:
        bridge.closing = True          # before anything ends: no restart from here on
    if workspace is not None and '_claude' in workspace.__dict__:
        workspace._claude.closing = True
    if server is not None:
        try:
            server.server_close()
        except OSError:
            pass

    def codex():
        if bridge is not None:
            bridge.close(wait=grace)
        for proc in list(STARTED_CODEX):          # an app-server a start or a restart left behind
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(grace)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass
    jobs = [codex]
    if workspace is not None and '_claude' in workspace.__dict__:
        jobs.append(lambda: workspace._claude.close(grace=grace))
    if workspace is not None and '_providers' in workspace.__dict__:          # more providers: every ACP agent group and opencode serve
        for p in workspace._providers[1:]:
            if workspace.driven(p) and callable(getattr(p, 'close', None)):
                jobs.append(p.close)
    if watcher is not None:
        jobs.append(watcher.close)

    def quietly(fn):
        try:
            fn()
        except Exception as e:
            print('Agent workspace: while stopping: %s: %s' % (type(e).__name__, str(e)[:200]), flush=True)
    threads = [threading.Thread(target=quietly, args=(j,), daemon=True) for j in jobs]
    for t in threads:
        t.start()
    end = time.time() + 2 * grace + 0.5
    for t in threads:
        t.join(max(0.0, end - time.time()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=4321)
    parser.add_argument('--shared', action='store_true', help='Connect to an already-running Codex daemon instead of owning a headless server')
    args = parser.parse_args()
    state = {}
    stop_on_signals(state)          # robust (stopping): before anything is started
    watch_parent()
    bridge = server = workspace = watcher = None
    try:
        m = mock_switch()          # mock: AGENT_IDE_MOCK=<scenario> (README.md "Mock scenario")
        bridge = start_codex(args.shared)          # providers panel: no Codex is a state the page shows, not a crash
        if isinstance(bridge, CodexAbsent):
            print('Codex: ' + bridge.reason, flush=True)
        if m and m.get('live'):
            bridge.mock_live(m['live'])          # attention mock: its running turns and open requests
        workspace = Workspace(bridge)
        try:
            workspace.sessions()          # speed (2026-11-21): Codex's rollouts read before the port opens (the app shows its start page until then), so the first answer finds them read
        except Exception:
            pass
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
        server.daemon_threads = True
        server.workspace = workspace
        from providers import keep
        workspace.plan_path = keep.cache_dir() / 'plan-toggle.json'      # plan toggle: beside the other kept state (made when first written)
        workspace.answers_path = keep.cache_dir() / ('answers-mock.json' if m else 'answers.json')      # answers kept: beside it
        workspace.stops_path = keep.cache_dir() / ('stops-mock.json' if m else 'stops.json')      # stops kept: beside it
        if m:          # a mock run starts from its own fixtures, never from what an earlier run kept
            workspace.answers_path.unlink(missing_ok=True)
            workspace.stops_path.unlink(missing_ok=True)
        test_worker = os.environ.get('AGENT_IDE_TEST_CLAUDE_WORKER')          # tests only: a stand-in Claude Code worker (a JSON command)
        if test_worker:
            workspace._claude = claude_bridge.ClaudeBridge(publish=bridge.publish, command=json.loads(test_worker))
        keep_between_runs(workspace)          # speed (3rd pass): providers/keep.py
        watcher = workspace.watch_logs()
        workspace.listings_ready(wait=0)          # speed (2026-11-21): the first listing starts now, not at the page's first question
        print(f'Agent workspace: http://127.0.0.1:{args.port}', flush=True)
        server.serve_forever()
    except (Stop, KeyboardInterrupt):
        pass
    finally:
        state['stopping'] = True
        stop_all(bridge, workspace, watcher, server)
        if state.get('why'):
            print('Agent workspace: stopped (%s).' % state['why'], flush=True)


if __name__ == '__main__':
    main()
