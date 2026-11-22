"""edits: what an agent changed, read from its own record file on request (GET /api/edits?id=<agent>).

The answer is the agent's OWN recorded edits (its write and edit calls), never a diff of the folder:
  {files: [{path, edits: [{t, kind: "add"|"update"|"replace", hunks: [{oldStart?, newStart?, lines: [" x", "-y", "+z"]}],
            numbered}], more}], commands: n, cut: {files, lines}}
- files in the order first edited, each once, its edits in time order (several edits to one file are kept apart: the file as
  it was before is not reliably in the record, so they are never combined).
- numbered false: an edit rebuilt from the call's own old and new strings (no line numbers, no surrounding lines recorded).
- an edit whose result is an error (refused, old string not found, a failed or declined Codex change) is left out altogether.
- commands: the commands the agent ran that may have changed files, which leave no record of what they changed (Codex: those
  its parsed_cmd does not mark as a read, a search or a listing; Claude Code: every Bash call).
- cuts: CHANGE_LINES_MAX lines per file (the approval card's bound); AGENT_LINES_MAX lines per agent. A file's `more` is its
  lines not sent; `cut` counts the files left out altogether (the agent's bound reached before them) and every line not sent.
Only the lines holding the key words are parsed (as find_record does); each record file's reading is cached by its size and
modification time, and nothing else is kept between requests.
"""
import json
import os
import re
from pathlib import Path

from claude_bridge import CHANGE_LINES_MAX          # 400 lines per file, as the approval card
from providers.base import relative

AGENT_LINES_MAX = 3000          # naive: a named round number, not measured; lines per agent's answer
CACHE_MAX = 64                  # record files whose reading is kept (each replaced when its file changes)
HUNK = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')
CLAUDE_EDITS = {'Edit', 'Write', 'MultiEdit'}
CLAUDE_KEYS = (b'"Edit"', b'"Write"', b'"MultiEdit"', b'"Bash"')
RESULT_ID = re.compile(rb'"tool_use_id":\s*"([^"]+)"')
RESULT_MARKS = (b'"is_error": true', b'"is_error":true', b'"structuredPatch"')          # results that matter to an edit made in another file
CODEX_KEYS = (b'"FileChange"', b'"CommandExecution"')
CODEX_LOOKS = ('read', 'search', 'list_files')          # parsed_cmd types that change nothing
CODEX_FAILED = ('failed', 'declined')

_cache = {}


def _ts(s):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def cached(path, read):
    """read(path)'s answer, kept while the file's size and modification time stay the same."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime_ns)
    hit = _cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    val = read(path)
    _cache[str(path)] = (key, val)
    return val


def _lines(path, keys, want=None):
    """The parsed records of the lines holding any of keys and passing want(line) (byte search first; only those lines are parsed)."""
    with open(path, 'rb') as f:
        for raw in f:
            if any(k in raw for k in keys) and (want is None or want(raw)):
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    yield raw, r


def unified(text):
    """Hunks of a unified diff ([{oldStart, newStart, lines}]); a diff without @@ headers is one hunk without numbers."""
    hunks, cur = [], None
    rows = str(text or '').split('\n')
    if rows and rows[-1] == '':
        rows.pop()
    for ln in rows:
        m = HUNK.match(ln)
        if m:
            cur = {'oldStart': int(m.group(1)), 'newStart': int(m.group(2)), 'lines': []}
            hunks.append(cur)
            continue
        if cur is None and ln.startswith(('--- ', '+++ ', 'diff ', 'index ')):
            continue
        if ln.startswith('\\'):          # "\ No newline at end of file"
            continue
        if cur is None:
            cur = {'lines': []}
            hunks.append(cur)
        cur['lines'].append(ln if ln[:1] in (' ', '-', '+') else ' ' + ln)
    return hunks


def whole(text, sign='+'):
    rows = str(text or '').split('\n')
    if len(rows) > 1 and rows[-1] == '':
        rows.pop()
    return [sign + x for x in rows]


# ---- Claude Code: Edit / Write / MultiEdit calls and their results ----
def read_claude(path):
    """One transcript's edit calls {id: (t, name, input, cwd)} in order, their results {id: (is_error, toolUseResult)}, and
    the ids of its Bash calls."""
    uses, results, bash = {}, {}, set()

    def want(raw):          # a result line is parsed when it answers an edit call above it, or may answer one in an earlier file of
        return (any(k in raw for k in CLAUDE_KEYS) or any(m.decode() in uses for m in RESULT_ID.findall(raw))          # the chain:
                or any(k in raw for k in RESULT_MARKS))          # an error, or a result carrying a patch (a plain one changes nothing here)
    for raw, r in _lines(path, CLAUDE_KEYS + (b'"tool_result"',), want):
        if r.get('type') == 'assistant' and any(k in raw for k in CLAUDE_KEYS):
            for b in (r.get('message') or {}).get('content') or []:
                if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('id'):
                    if b.get('name') == 'Bash':
                        bash.add(b['id'])
                    elif b.get('name') in CLAUDE_EDITS and isinstance(b.get('input'), dict):
                        uses.setdefault(b['id'], (_ts(r.get('timestamp')), b['name'], b['input'], r.get('cwd')))
        elif r.get('type') == 'user':
            for b in (r.get('message') or {}).get('content') or []:
                if isinstance(b, dict) and b.get('type') == 'tool_result' and b.get('tool_use_id'):
                    results[b['tool_use_id']] = (bool(b.get('is_error')), r.get('toolUseResult'))
    return uses, results, bash


def claude_edit(name, inp, tur):
    """{kind, hunks, numbered} of one edit call, from its result's structuredPatch when recorded, else from its own strings."""
    tur = tur if isinstance(tur, dict) else {}
    patch = tur.get('structuredPatch')
    if name == 'Write':
        if tur.get('type') == 'create':
            return {'kind': 'add', 'hunks': [{'oldStart': 0, 'newStart': 1, 'lines': whole(tur.get('content', inp.get('content')))}], 'numbered': True}
        if tur.get('type') == 'update' and isinstance(patch, list) and patch:
            return {'kind': 'replace', 'hunks': [_hunk(h) for h in patch], 'numbered': True}
        return {'kind': 'replace', 'hunks': [{'lines': whole(inp.get('content'))}], 'numbered': False}
    if isinstance(patch, list) and patch:
        return {'kind': 'update', 'hunks': [_hunk(h) for h in patch], 'numbered': True}
    hunks = []
    for e in (inp.get('edits') if name == 'MultiEdit' else [inp]) or []:
        if isinstance(e, dict):
            ls = (whole(e['old_string'], '-') if e.get('old_string') else []) + (whole(e['new_string'], '+') if e.get('new_string') else [])
            if ls:
                hunks.append({'lines': ls})
    return {'kind': 'update', 'hunks': hunks, 'numbered': False}


def _hunk(h):
    return {'oldStart': h.get('oldStart'), 'newStart': h.get('newStart'), 'lines': [str(x) for x in h.get('lines') or []]}


def claude_edits(paths):
    """The edits and the Bash count of a Claude Code agent whose record is paths (a continuation's files oldest first; their
    copied records share ids, so each call counts once)."""
    uses, results, bash = {}, {}, set()
    for p in paths:
        got = cached(p, read_claude)
        if got:
            for k, v in got[0].items():
                uses.setdefault(k, v)
            results.update(got[1])
            bash |= got[2]
    out = []
    for i, (t, name, inp, cwd) in uses.items():
        err, tur = results.get(i, (False, None))
        if err:
            continue          # refused, or old string not found: not a change
        path = relative(inp.get('file_path'), cwd) if cwd else None
        if not path:
            continue          # outside the agent's folder: left out, as from "N files changed"
        out.append(dict(claude_edit(name, inp, tur), t=t, path=path))
    out.sort(key=lambda e: e['t'] or 0)
    return out, len(bash)


# ---- Codex: FileChange items and the commands that may have changed files ----
def read_codex(path):
    """One rollout's FileChange entries [(t, id, status, path, change)], with the apply_patch calls no FileChange item covers
    added as entries of their own (change {'patch': (kind, hunks)}), and the ids of commands that may change files."""
    changes, cmds, items, calls = [], set(), [], []          # items: FileChange (position, id, paths); calls: apply_patch (position, entry)
    for n, (_, r) in enumerate(_lines(path, CODEX_KEYS + (b'apply_patch',))):
        p = r.get('payload') or {}
        if r.get('type') == 'response_item' and p.get('type') in ('function_call', 'custom_tool_call') and 'patch' in str(p.get('name', '')):
            calls.append((n, _ts(r.get('timestamp')), p.get('call_id'), patch_files(p.get('input', p.get('arguments')))))
            continue
        if r.get('type') != 'event_msg' or p.get('type') != 'item_completed' or not isinstance(p.get('item'), dict):
            continue
        item = p['item']
        t = p['completed_at_ms'] / 1000 if isinstance(p.get('completed_at_ms'), (int, float)) else _ts(r.get('timestamp'))
        if item.get('type') == 'FileChange':
            ch = item.get('changes') or {}
            pairs = list(ch.items()) if isinstance(ch, dict) else [(c.get('path'), c) for c in ch if isinstance(c, dict)]
            items.append((n, item.get('id'), [str(fp) for fp, _ in pairs]))
            for fp, c in pairs:
                changes.append((t, item.get('id'), item.get('status'), fp, c))
        elif item.get('type') == 'CommandExecution':
            parsed = item.get('parsed_cmd') or []
            if not parsed or any(not isinstance(x, dict) or x.get('type') not in CODEX_LOOKS for x in parsed):
                cmds.add(item.get('id') or len(cmds))
    for n, t, cid, files in calls:
        nxt = next((it for it in items if it[0] > n), None)
        same = nxt and files and all(any(q == fp or q.endswith('/' + fp) for q in nxt[2]) for fp, _, _ in files)
        if any(it[1] == cid for it in items if cid) or same:
            continue          # recorded as a FileChange item too (its id the call's, or the next one with the same files): not doubled
        changes += [(t, cid, 'completed', fp, {'patch': (kind, hunks)}) for fp, kind, hunks in files]
    return changes, cmds


def patch_files(raw):
    """[(path, kind, hunks)] of an apply_patch call's own text (*** Update / Add / Delete File, @@ sections, - and + lines),
    without line numbers: an added file is kind add, an update or a delete kind update."""
    if isinstance(raw, str) and raw.lstrip().startswith('{'):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if isinstance(raw, dict):
        raw = raw.get('input') or raw.get('patch') or ''
    out, cur = [], None
    for ln in str(raw or '').split('\n'):
        m = re.match(r'\*\*\* (Update|Add|Delete) File: (.+)', ln)
        if m:
            cur = (m.group(2).strip(), 'add' if m.group(1) == 'Add' else 'update', [])
            out.append(cur)
        elif ln.startswith('***') or cur is None:
            continue          # Begin / End Patch, Move to, End of File
        elif ln.startswith('@@'):
            cur[2].append({'lines': []})          # a new section
        elif ln[:1] in (' ', '-', '+'):
            if not cur[2]:
                cur[2].append({'lines': []})
            cur[2][-1]['lines'].append(ln)
    return [(fp, kind, [h for h in hunks if h['lines']]) for fp, kind, hunks in out]


def codex_edit(c):
    """{kind, hunks, numbered} of one entry of a FileChange's changes: an update's unified diff, or an added (deleted) file's
    whole content as + (-) lines. Shapes seen: {type, unified_diff | content}, {kind: {type}, diff}, {add: {content}}."""
    if isinstance(c, str):
        hs = unified(c)
        return {'kind': 'update', 'hunks': hs, 'numbered': bool(hs) and all('oldStart' in h for h in hs)}
    if not isinstance(c, dict):
        return None
    if isinstance(c.get('patch'), tuple):          # an apply_patch call's own text: no line numbers
        return {'kind': c['patch'][0], 'hunks': c['patch'][1], 'numbered': False}
    kind = c.get('kind')
    typ = c.get('type') or (kind.get('type') if isinstance(kind, dict) else kind)
    if not typ:
        for k in ('add', 'update', 'delete'):
            if isinstance(c.get(k), dict):
                return codex_edit(dict(c[k], type=k))
    text = next((c[k] for k in ('unified_diff', 'diff', 'content', 'new_content') if isinstance(c.get(k), str)), '')
    if typ in ('add', 'delete') and not HUNK.search(text or ''):
        sign = '+' if typ == 'add' else '-'
        return {'kind': 'add' if typ == 'add' else 'update', 'hunks': [{'oldStart': 0 if sign == '+' else 1, 'newStart': 1 if sign == '+' else 0, 'lines': whole(text, sign)}],
                'numbered': True}
    hs = unified(text)
    return {'kind': 'add' if typ == 'add' else 'update', 'hunks': hs, 'numbered': bool(hs) and all('oldStart' in h for h in hs)}


def codex_edits(path, cwd):
    got = cached(path, read_codex) or ([], set())
    out = []
    for t, _, status, fp, c in got[0]:
        if status in CODEX_FAILED or not fp:
            continue
        rel = relative(fp, cwd) if cwd else None
        if not rel:
            continue
        e = codex_edit(c)
        if e:
            out.append(dict(e, t=t, path=rel))
    out.sort(key=lambda e: e['t'] or 0)
    return out, len(got[1])


# ---- the answer ----
def answer(edits, commands, file_max=None, agent_max=None):
    """The route's answer from edits in time order; cut at file_max lines per file and agent_max per agent."""
    file_max = CHANGE_LINES_MAX if file_max is None else file_max
    agent_max = AGENT_LINES_MAX if agent_max is None else agent_max
    by = {}
    for e in edits:
        by.setdefault(e['path'], []).append(e)
    files, budget, cut_files, cut_lines = [], agent_max, 0, 0
    for path, es in by.items():
        total = sum(len(h['lines']) for e in es for h in e['hunks'])
        if budget <= 0:
            cut_files += 1
            cut_lines += total
            continue
        room = min(file_max, budget)
        kept = []
        for e in es:
            hs = []
            for h in e['hunks']:
                if room <= 0:
                    break
                take = h['lines'][:room]
                room -= len(take)
                hs.append(dict(h, lines=take))
            if hs:
                kept.append({'t': e['t'], 'kind': e['kind'], 'hunks': hs, 'numbered': e['numbered']})
        sent = sum(len(h['lines']) for e in kept for h in e['hunks'])
        budget -= sent
        cut_lines += total - sent
        files.append({'path': path, 'edits': kept, 'more': total - sent})
    return {'files': files, 'commands': commands, 'cut': {'files': cut_files, 'lines': cut_lines}}


def of_agent(app, sid):
    """GET /api/edits?id=<agent>: Claude Code agents from their transcript (and the files it continues), Codex agents from the
    rollout. ValueError for an agent this server does not know."""
    p = app.provider_of(sid)
    if p is not app.providers[0]:
        if getattr(p, 'name', '') != 'claude':
            raise ValueError('No recorded edits for this kind of agent')
        path = p.path_of(sid)
        if not path or not Path(path).is_file():
            raise ValueError('Unknown agent')
        chain = p.chains.get(sid) if '/' not in str(sid) else None
        return answer(*claude_edits([q for q, _ in chain or []] + [path]))
    row = app.session(sid)
    if not row:
        raise ValueError('Unknown agent')
    if not row.get('rollout_path'):
        return answer([], 0)          # started, nothing recorded yet
    return answer(*codex_edits(row['rollout_path'], row.get('cwd')))
