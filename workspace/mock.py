"""mock: the fake scenarios AGENT_IDE_MOCK=<name> opens (README.md "Mock scenario"; fixtures/<name>/FIXTURE.md).

A scenario is a folder of fixtures: `repo/` (the project as it stands at the end), `before/` (the files it replaced),
`sessions/` (a Claude Code transcript and its subagent, a Codex rollout and its thread row, the git history) written with
{{PROJECT}} for the project's path and times on a reference afternoon. setup() lays them out under the scenario's `.run/`
(ignored by Git, rebuilt at every start) in the shapes the real stores have (~/.claude/projects/<encoded cwd>/, a Codex
home with state_5.sqlite and sessions/YYYY/MM/DD/), every time moved so the afternoon ends a minute before the server
starts, and returns the paths server.py points its providers at. Nothing here is read by the providers themselves.
"""
import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import random
import subprocess
import sys
import threading
import time

from providers.claude_code import encode

ROOT = Path(__file__).resolve().parent
SCENARIOS = {'junior': ROOT / 'fixtures' / 'junior-swe', 'attention': ROOT / 'fixtures' / 'junior-swe'}
# attention: the junior afternoon plus the sessions in junior-swe/attention/ (FIXTURE.md "Attention"): four running Codex threads (two
# waiting on you, one quiet, one with two failed turns and two subagents that handed back) and a Claude Code session that stopped on an
# API error; live.json holds what the app-server would hold for the running ones (their turns, the requests waiting on you)
OVERLAYS = {'attention': 'attention'}
# attention: Claude Code sessions laid in beside the overlay's, from the providers' own test fixtures: "Plan the RCP-142 release",
# stopped on AskUserQuestion with a background subagent not yet reported back; their later/ records are what act() appends
EXTRA_CLAUDE = {'attention': [ROOT / 'providers' / 'fixtures' / 'claude-code-asking']}
LEAD = 60          # seconds between the afternoon's last record and the server's start
# the Claude Code bridge in a mock: a stand-in worker, so a Claude Code column is drivable (its chat shows what a send would do)
# and nothing real starts: it answers every call with nothing (a send "goes" nowhere; a copy is listed but never written)
STAND_IN = r'''
import json, sys
def out(m):
    sys.stdout.write(json.dumps(m) + '\n'); sys.stdout.flush()
out({'method': 'ready', 'params': {}})
for line in sys.stdin:
    m = json.loads(line)
    out({'id': m['id'], 'result': {'models': []} if m.get('method') == 'models' else {}})
    if m.get('method') == 'interrupt':          # a stop ends the turn, as Claude Code's own result after a stop does (round 3: checks stop what they start)
        out({'method': 'message', 'params': {'key': (m.get('params') or {}).get('key'), 'message': {'_type': 'ResultMessage', 'subtype': 'error_during_execution', 'is_error': False}}})
'''
# what another program holding the Claude Code session does (act): the Claude app opens it (held), works a turn in it (busy), the
# turn ends (idle), the app lets it go (released). A registration as Claude Code writes it, with this server's pid (alive)
HOLD = {'held': 'idle', 'busy': 'busy', 'idle': 'idle', 'released': None}


def iso_to(s):
    return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


def to_iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def shift(x, d):
    """Every recorded time in a record moved by d seconds: ISO 'timestamp' fields and *_at_ms integers."""
    if isinstance(x, dict):
        return {k: (to_iso(iso_to(v) + d) if k == 'timestamp' and isinstance(v, str) else
                    v + int(d * 1000) if k.endswith('_at_ms') and isinstance(v, int) else shift(v, d)) for k, v in x.items()}
    if isinstance(x, list):
        return [shift(v, d) for v in x]
    return x


def jsonl(src, dst, project, d):
    """A template JSONL with its project path filled in and its times shifted; the file's mtime is its last record's time."""
    last, out = None, []
    for line in src.read_text().replace('{{PROJECT}}', str(project)).splitlines():
        if not line.strip():
            continue
        r = shift(json.loads(line), d)
        last = r.get('timestamp') or last
        out.append(json.dumps(r, ensure_ascii=False))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text('\n'.join(out) + '\n')
    if last:
        os.utime(dst, (iso_to(last), iso_to(last)))
    return dst


def lay(src, dst, skip=()):
    """Copy a tree's files over dst, leaving out the relative paths (files or folders) in skip."""
    for f in sorted(src.rglob('*')):
        rel = f.relative_to(src)
        if f.is_dir() or any(str(rel) == s or str(rel).startswith(s.rstrip('/') + '/') for s in skip):
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dst / rel)


def git(project, *args, at=None, author=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null')
    if at:
        name, _, mail = author.partition(' <')
        stamp = to_iso(at).replace('.000Z', '+0000').replace('Z', '+0000')
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=mail.rstrip('>'), GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=mail.rstrip('>'),
                   GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    subprocess.run(['git', '-C', str(project), '-c', 'init.defaultBranch=main', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args],
                   check=True, capture_output=True, env=env)


def build_repo(fx, project, plan, d):
    """The project's Git history from sessions/git.json: commits of the before-state on main, the fix on its branch; the
    working tree ends as repo/ with the untracked paths left out of every commit."""
    project.mkdir(parents=True)
    git(project, 'init', '-q')
    git(project, 'symbolic-ref', 'HEAD', 'refs/heads/main')      # older Git ignores init.defaultBranch
    for c in plan['commits']:
        if c['branch'] != 'main':
            git(project, 'checkout', '-q', '-b', c['branch'])
        lay(fx / 'repo', project, c.get('except', []))
        if c['files'] == 'before':
            lay(fx / 'before', project)
        for s in c.get('except', []):
            p = project / s
            shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
        git(project, 'add', '-A', *(['--', *c['only']] if c.get('only') else []))
        git(project, 'commit', '-q', '-m', c['message'], at=iso_to(c['at']) + d, author=plan['author'])
    lay(fx / 'repo', project)


def setup(name, now=None, run=None):
    """Lay the scenario out under run (default: its .run/, emptied first) and return the paths and the time shift."""
    fx = SCENARIOS.get(name)
    if not fx:
        raise SystemExit(f'AGENT_IDE_MOCK={name}: no such scenario (known: {", ".join(SCENARIOS)})')
    run_default = not run
    run = Path(run) if run else fx / ('.run' if name == 'junior' else '.run-' + name)          # one folder per scenario: both can run at once
    shutil.rmtree(run, ignore_errors=True)
    plan = json.loads((fx / 'sessions' / 'git.json').read_text())
    d = (now or time.time()) - LEAD - iso_to(plan['end'])
    run.mkdir(parents=True)
    project = run.resolve() / json.loads((fx / 'sessions' / 'project.json').read_text())['name']
    build_repo(fx, project, plan, d)
    # Claude Code: ~/.claude/projects/<encoded cwd>/<uuid>.jsonl and <uuid>/subagents/
    claude = run / 'claude'
    over = fx / OVERLAYS[name] if name in OVERLAYS else None
    for src in [fx / 'sessions' / 'claude' / 'projects' / 'project'] + ([over / 'claude' / 'projects' / 'project'] if over else []) + EXTRA_CLAUDE.get(name, []):
        for f in src.rglob('*'):
            if f.is_file() and 'later' not in f.relative_to(src).parts:
                dst = claude / 'projects' / encode(project) / f.relative_to(src)
                if f.suffix == '.jsonl':
                    jsonl(f, dst, project, d)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, dst)
    claude_json = run / 'claude.json'
    claude_json.write_text('{}\n')      # no configured Claude Code servers: the connectors come from the sessions' calls
    # Codex: a home with state_5.sqlite (the threads table's columns the observer reads) and sessions/YYYY/MM/DD/rollout-*.jsonl
    codex = run / 'codex'
    rows = [dict(r, dir=fx / 'sessions' / 'codex') for r in json.loads((fx / 'sessions' / 'codex' / 'threads.json').read_text().replace('{{PROJECT}}', str(project)))]
    if over:
        rows += [dict(r, dir=over / 'codex') for r in json.loads((over / 'codex' / 'threads.json').read_text().replace('{{PROJECT}}', str(project)))]
    ticking = []          # the running threads that are meant to be heard from (threads.json "tick": true)
    live_raw = json.loads((over / 'live.json').read_text()) if over and (over / 'live.json').is_file() else {}
    codex.mkdir(parents=True)
    db = sqlite3.connect(codex / 'state_5.sqlite')
    db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, source TEXT NOT NULL, '
               'model_provider TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL, approval_mode TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, '
               'git_branch TEXT, first_user_message TEXT NOT NULL DEFAULT \'\', model TEXT, thread_source TEXT, preview TEXT NOT NULL DEFAULT \'\', name TEXT, project_id TEXT, originator TEXT)')
    for r in rows:
        created, updated = iso_to(r['created_at']) + d, iso_to(r['updated_at']) + d
        day = datetime.datetime.fromtimestamp(created).strftime('%Y/%m/%d')
        path = jsonl(r['dir'] / r['rollout'], codex / 'sessions' / day / Path(r['rollout']).name, project, d)
        if r.get('tick'):
            ticking.append((path, r['id'], (live_raw.get('active') or {}).get(r['id'])))
        db.execute('INSERT INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, git_branch, first_user_message, model, '
                   'thread_source, preview, originator) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (r['id'], str(path), int(created), int(updated), r['source'], r['model_provider'], r['cwd'], r['title'], r['sandbox_policy'], r['approval_mode'], r.get('git_branch'),
                    r.get('first_user_message', ''), r.get('model'), r.get('thread_source'), r.get('preview', ''), r.get('originator')))
    db.commit()
    db.close()
    runtime = run / 'codex-runtime'      # the app-server's own home: empty, so the person's Codex threads and servers stay out of the mock
    runtime.mkdir()
    live = {'active': {}, 'requests': []}
    if over and (over / 'live.json').is_file():          # attention: the running turns and open requests, times moved like the records'
        raw = json.loads((over / 'live.json').read_text().replace('{{PROJECT}}', str(project)))
        live = {'active': raw.get('active', {}), 'requests': [dict(q, params=dict(q['params'], **({'startedAtMs': q['params']['startedAtMs'] + int(d * 1000)} if q['params'].get('startedAtMs') else {})))
                                                               for q in raw.get('requests', [])]}
    later = {f.stem: (f, claude / 'projects' / encode(project) / (next(src.glob('*.jsonl')).name)) for src in EXTRA_CLAUDE.get(name, []) for f in (src / 'later').glob('*.jsonl')}
    (run / 'claude-stand-in.py').write_text(STAND_IN)
    if ticking and run_default:          # a server run (not a test's own copy): the running agents tick while it lives
        threading.Thread(target=tick_loop, args=(ticking, project, threading.Event()), daemon=True).start()
    held = [next(src.glob('*.jsonl')).stem for src in EXTRA_CLAUDE.get(name, [])]
    return {'project': project, 'codex_home': codex, 'codex_runtime': runtime, 'claude_home': claude, 'claude_json': claude_json, 'shift': d, 'live': live,
            'later': later, 'done': set(), 'held': held[0] if held else None,
            'claude_command': [sys.executable, '-I', str((run / 'claude-stand-in.py').resolve())]}


# ---- running agents tick (integration piece 4): a thread whose row in threads.json has "tick": true is a running agent that is meant to be
# heard from; while the server runs, a new action is appended to its rollout every 4 to 9 s (a file read, a file change, a command) and a new
# message every 20 to 40 s, in the shapes of the records above, timed now. Agents that wait on you or are silent (the attention mock's other
# running threads) are never ticked: they are meant to stay as they are. The words are a pool for the fixture's small Python service.
TICK_FILES = ['src/receipts/store.py', 'src/receipts/totals.py', 'src/receipts/currency.py', 'src/receipts/api.py', 'tests/test_totals.py', 'tests/test_api.py', 'tests/conftest.py', 'docs/architecture.md', 'README.md']
TICK_CMDS = ['python -m pytest -q', 'python -m pytest -q tests/test_totals.py', 'git diff --stat', 'git status --short', 'rg "quantize" src', 'rg -n "def export" src', 'ruff check src',
             'python -m mypy src/receipts', 'git log --oneline -5 -- src/receipts/store.py', 'git diff src/receipts/store.py', 'ls tests', 'python -m pytest -q -k export']
TICK_SAYS = ['The CSV is written row by row in export_rows; the header is a plain list above it.', 'Added the currency to each row; the header now has a fifth column.',
             'Ran the tests: 31 passed, 1 failed in tests/test_api.py (it counts four columns).', 'The failing test hard-codes the header; updating it to the new one.',
             'Checked totals.py: the currency is already on each receipt, so nothing to look up.', 'Type check is clean after the change to store.py.',
             'Two callers build rows by hand; moving them onto export_rows so they get the column too.', 'The README shows the old header; updating the example.',
             'Tests pass. Checking that an empty export still writes the header.', 'Wrote a test for a receipt with no currency; it exports an empty cell.']
TICK_EDITS = [(['    header = ["id", "date", "subtotal", "total"]'], ['    header = ["id", "date", "subtotal", "total", "currency"]']),
              (['        rows.append([r["id"], r["date"], r["subtotal"], r["total"]])'], ['        rows.append([r["id"], r["date"], r["subtotal"], r["total"], r.get("currency", "")])']),
              (['    assert len(rows[0]) == 4'], ['    assert len(rows[0]) == 5', '    assert rows[0][-1] == "currency"']),
              ([], ['def test_export_without_currency(store):', '    rows = export_rows(store, [{"id": 1, "date": "2026-11-05", "subtotal": 1, "total": 1}])',
                    '    assert rows[1][-1] == ""', '']),
              (['Exports have four columns.'], ['Exports have five columns: id, date, subtotal, total and currency.']),
              (['def export_rows(store, receipts):', '    rows = [header]'], ['def export_rows(store, receipts, currency=True):', '    """Rows for the CSV export, the header first."""', '    rows = [header]'])]


def _line(ts, typ, payload):
    return json.dumps({'timestamp': to_iso(ts), 'type': typ, 'payload': payload}, ensure_ascii=False) + '\n'


def tick_records(ident, turn, n, now, project, rng, kind):
    """The records one new action (kind 'read', 'write', 'run') or message ('say') leaves in a Codex rollout; n numbers the items."""
    ms = lambda ts: int(ts * 1000)
    done = lambda item: _line(now, 'event_msg', {'type': 'item_completed', 'thread_id': ident, 'turn_id': turn, 'started_at_ms': ms(now - 1), 'completed_at_ms': ms(now), 'item': item})
    if kind == 'say':
        text = rng.choice(TICK_SAYS)
        return (_line(now, 'response_item', {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}) +
                done({'type': 'AgentMessage', 'id': f'msg-tick-{n}', 'content': [{'type': 'Text', 'text': text}]}))
    path = rng.choice(TICK_FILES)
    if kind == 'write':          # a varied change (the change view shows these): one of TICK_EDITS at a random line, one line around it
        old, new = rng.choice(TICK_EDITS)
        at = rng.randint(3, 120)
        diff = (f'@@ -{at},{len(old) + 2} +{at},{len(new) + 2} @@\n \n' + ''.join('-' + x + '\n' for x in old) + ''.join('+' + x + '\n' for x in new) + ' \n')
        return done({'type': 'FileChange', 'id': f'patch-tick-{n}', 'changes': {path: {'type': 'update', 'unified_diff': diff}}, 'status': 'completed'})
    cmd = f"sed -n '1,80p' {path}" if kind == 'read' else rng.choice(TICK_CMDS)
    parsed = [{'type': 'read', 'cmd': cmd, 'name': path.split('/')[-1], 'path': path}] if kind == 'read' else [{'type': 'unknown', 'cmd': cmd}]
    return (_line(now - 1, 'response_item', {'type': 'function_call', 'name': 'exec_command', 'arguments': json.dumps({'cmd': cmd, 'workdir': str(project)}), 'call_id': f'call-tick-{n}'}) +
            done({'type': 'CommandExecution', 'id': f'exec-tick-{n}', 'command': ['/bin/zsh', '-lc', cmd], 'cwd': 'file://' + str(project), 'parsed_cmd': parsed, 'status': 'completed',
                  'aggregated_output': '(file contents)\n' if kind == 'read' else 'ok\n', 'exit_code': 0}) +
            _line(now, 'response_item', {'type': 'function_call_output', 'call_id': f'call-tick-{n}', 'output': 'ok\n'}))


STOPPED = set()          # threads stopped from the page (stop): their turn is over, so they tick no more


def tick_loop(items, project, stop):
    """items: [(rollout path, thread id, turn id)]; each ticks at its own pace until stop is set, or until its turn is stopped."""
    rng = random.Random(7)
    due = {i: [time.time() + rng.uniform(4, 9), time.time() + rng.uniform(20, 40), rng.uniform(0.9, 1.1), 0] for i in range(len(items))}          # next action, next message, pace, count
    while not stop.wait(0.5):
        now = time.time()
        for i, (path, ident, turn) in enumerate(items):
            if ident in STOPPED:
                continue
            d = due[i]
            for slot in (0, 1):
                if now < d[slot]:
                    continue
                d[3] += 1
                kind = 'say' if slot else rng.choices(['read', 'write', 'run'], [0.4, 0.25, 0.35])[0]
                try:
                    with open(path, 'a') as f:
                        f.write(tick_records(ident, turn, d[3], now, project, rng, kind))
                except OSError:
                    return
                d[slot] = now + (rng.uniform(20, 40) if slot else rng.uniform(4, 9) * d[2])


def send(m, tid, text, now=None):
    """A send to a Codex thread of the mock: the app-server the mock starts has an empty home (no fixture thread, no sign-in), so
    nothing runs; the message is appended to the thread's rollout as a user message of a turn that ends at once, timed now, so the
    page shows it arrive. Its answer is turn/start's shape."""
    text = str(text or '').strip()
    if not text:
        raise ValueError('Write a message first.')
    db = sqlite3.connect(Path(m['codex_home']) / 'state_5.sqlite')
    try:
        row = db.execute('SELECT rollout_path FROM threads WHERE id = ?', (tid,)).fetchone()
    finally:
        db.close()
    if not row:
        raise ValueError('Unknown agent')
    now = now or time.time()
    turn = 'mock-turn-' + format(int(now * 1000), 'x')
    with open(row[0], 'a') as f:
        f.write(_line(now, 'event_msg', {'type': 'task_started', 'turn_id': turn}) +
                _line(now, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}) +
                _line(now, 'event_msg', {'type': 'item_completed', 'thread_id': tid, 'turn_id': turn, 'started_at_ms': int(now * 1000), 'completed_at_ms': int(now * 1000),
                                         'item': {'type': 'UserMessage', 'id': 'um-' + turn, 'content': [{'type': 'text', 'text': text}]}}) +
                _line(now, 'event_msg', {'type': 'task_complete', 'turn_id': turn}))
    return {'turn': {'id': turn, 'status': 'completed', 'items': []}, 'mock': True}



def stop(m, tid, turn, now=None):
    """A stop of a running Codex thread of the mock (the attention scenario's running turns, which its app-server never started):
    the turn is ended as Codex ends an interrupted one, a turn_aborted record appended to the thread's rollout, timed now."""
    db = sqlite3.connect(Path(m['codex_home']) / 'state_5.sqlite')
    try:
        row = db.execute('SELECT rollout_path FROM threads WHERE id = ?', (tid,)).fetchone()
    finally:
        db.close()
    if not row:
        raise ValueError('Unknown agent')
    STOPPED.add(tid)
    with open(row[0], 'a') as f:
        f.write(_line(now or time.time(), 'event_msg', {'type': 'turn_aborted', 'turn_id': turn, 'reason': 'interrupted'}))
    return {'mock': True}


def fork(m, tid, now=None):
    """A fork of a Codex thread of the mock: a new thread row beside it whose rollout is a copy of the original's under the new id,
    timed now; nothing runs. Its answer is thread/fork's shape ({thread: {id, ...}})."""
    now = now or time.time()
    db = sqlite3.connect(Path(m['codex_home']) / 'state_5.sqlite')
    db.row_factory = sqlite3.Row
    try:
        row = db.execute('SELECT * FROM threads WHERE id = ?', (tid,)).fetchone()
        if not row:
            raise ValueError('Unknown agent')
        new = 'mock-fork-' + format(int(now * 1000), 'x') + '-' + tid[-12:]
        src = Path(row['rollout_path'])
        dst = src.with_name(src.name.replace(tid, new) if tid in src.name else 'rollout-' + new + '.jsonl')
        dst.write_text(src.read_text().replace(tid, new))
        d = dict(row)
        d.update(id=new, rollout_path=str(dst), created_at=int(now), updated_at=int(now))
        db.execute('INSERT INTO threads (' + ', '.join(d) + ') VALUES (' + ', '.join('?' * len(d)) + ')', list(d.values()))
        db.commit()
    finally:
        db.close()
    return {'thread': {'id': new, 'cwd': d.get('cwd'), 'name': d.get('title')}, 'mock': True}


def act(m, what, now=None):
    """attention mock: what happens next outside this server, once each: 'answer' (the person answers the Claude Code question in
    Claude Code: the AskUserQuestion's tool result) and 'handback' (the background subagent's report reaches its parent: the
    task notification). The record is appended to the scenario's own transcript (under its .run folder), timed now."""
    if what in HOLD and m.get('held'):          # the Claude app holding the scenario's Claude Code session (any number of times)
        reg = Path(m['claude_home']) / 'sessions' / (str(os.getpid()) + '.json')
        if HOLD[what] is None:
            reg.unlink(missing_ok=True)
        else:
            reg.parent.mkdir(parents=True, exist_ok=True)
            reg.write_text(json.dumps({'pid': os.getpid(), 'sessionId': m['held'], 'cwd': str(m['project']), 'status': HOLD[what], 'entrypoint': 'claude-desktop', 'kind': 'interactive'}))
        return {'done': what}
    if what not in m.get('later', {}):
        raise ValueError('mock: nothing to do for ' + repr(what) + ' (known: ' + ', '.join(sorted(m.get('later', {}))) + ')')
    if what in m['done']:
        return {'done': what, 'again': True}
    src, dst = m['later'][what]
    at = to_iso(now or time.time())
    with dst.open('a') as f:
        for line in src.read_text().replace('{{PROJECT}}', str(m['project'])).splitlines():
            if line.strip():
                f.write(json.dumps(dict(json.loads(line), timestamp=at), ensure_ascii=False) + '\n')
    m['done'].add(what)
    return {'done': what, 'at': at}
