"""The provider interface: what the workspace needs from any agent harness (Codex, Claude Code, ...).

A provider observes one harness's sessions on this machine. Every session it lists carries `provider`.
History is returned in the shape server.Observer.read_log already gives for Codex, so the canvas and
/api/history treat every provider alike:
  {'messages': [{role, text, t, id, tokens}], 'events': [{at, kind, path, text, item, tokens}],
   'contexts': [{t, context}], 'compacts': [{t, from, to}], 'context', 'window', 'base', 'turnActive',
   'start', 'last', 'parent', 'truncated'}
Event kinds: read / write / run / say / ask / delegate (only read and write carry a path the floor uses).
"""
from pathlib import Path
import re
import time


class Provider:
    name = 'base'                 # the id carried on every session ('codex', 'claude')
    label = 'Agent'               # how the page names it ("Claude Code session")
    can_start = can_send = can_stop = can_reply = False

    def available(self):
        """{'name', 'label', 'found': bool, 'store': path or None, 'binary': path or None}"""
        return {'name': self.name, 'label': self.label, 'found': False, 'store': None, 'binary': None}

    def list_sessions(self, project_root=None):
        """[{id, provider, title, cwd, started, updated, running, parent}] for sessions whose cwd is project_root
        or below it (all sessions when project_root is None)."""
        raise NotImplementedError

    def read_history(self, sid):
        raise NotImplementedError

    def watch(self, sid, interval=1.0, stop=None):
        """Yield new events of a session as its store grows (a tail; ends when stop() is true)."""
        seen = len(self.read_history(sid).get('events', []))
        while not (stop and stop()):
            time.sleep(interval)
            evs = self.read_history(sid).get('events', [])
            for e in evs[seen:]:
                yield e
            seen = len(evs)

    def flags(self):
        return {'canStart': self.can_start, 'canSend': self.can_send, 'canStop': self.can_stop, 'canReply': self.can_reply}

    # ---- use and spend (the usage panel, GET /api/usage): optional; a harness that keeps no token records supplies neither, and
    # the panel still lists it, with "—" for its plan and no spend ----
    def session_usage(self, sid):
        """One session's model calls so far as a spend.Ledger (tokens, dollars at API prices, per-minute buckets), or None when
        this harness keeps no token records for it."""
        return None

    def plan_usage(self):
        """This harness's plan as it last reported it, or None:
        {'plan': name or None, 'at': epoch of the reading, 'windows': [{'label', 'percent' (None: no figure), 'resetsAt'}],
         'detail': {...}} with the windows in the harness's own order and names ('5-hour', 'weekly', ...); 'detail' carries what
        only this harness has (the page does not read it)."""
        return None

    # ---- where a session's context will be compacted (the columns' scale, GET /api/canvas and /api/compaction): optional; a
    # harness that does not compact, or whose rule is not known, supplies nothing and its columns keep the shared token scale ----
    def compaction_point(self, sid, model=None, window=None):
        """{'threshold': tokens at which the harness compacts this session's context by itself, 'window': the window that point
        is taken from, 'model', 'rule': how the point follows from it, in words, 'source': where the window was read,
        'estimate': True when the record names no model this harness knows and the point is a stand-in}, or None.
        model and window are what the server already holds for the session (its list row, its history); either may be None."""
        return None


class FileCache:
    """A small file's parsed contents, read again only when its size or modification time changes (settings and model lists
    are read on every canvas poll). parse(text) -> value; a missing or unreadable file gives `default`."""

    def __init__(self, parse, default=None):
        self.parse, self.default, self.seen = parse, default, {}

    def get(self, path):
        path = Path(path)
        try:
            st = path.stat()
        except OSError:
            return self.default
        key = (st.st_mtime_ns, st.st_size)
        hit = self.seen.get(str(path))
        if hit and hit[0] == key:
            return hit[1]
        try:
            value = self.parse(path.read_text())
        except (OSError, ValueError):
            value = self.default
        self.seen[str(path)] = (key, value)
        return value


def fmt_k(k):
    """Tokens as the page writes them: 367k, 1M, 1.5M."""
    return (str(round(k / 1e6, 1)).rstrip('0').rstrip('.') + 'M') if k >= 1e6 else str(round(k / 1000)) + 'k'


def under(cwd, root):
    """True when the folder cwd is root or inside it (no symlink resolution: both are recorded paths)."""
    if not cwd or not root:
        return False
    c, r = str(cwd).rstrip('/') or '/', str(root).rstrip('/') or '/'
    return c == r or c.startswith(r + '/') or r == '/'


def relative(path, cwd):
    """A tool's path relative to the session's folder, or None when it is outside it."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        rel = str(p.resolve().relative_to(Path(cwd).resolve()))
    except (ValueError, OSError):
        return None
    return None if rel == '.' or rel.startswith('..') else rel


# map engine (standing, plan A19): lines a change touched, added plus removed (total, not net). Read only from
# what the transcript already holds; None when it cannot be told.
def diff_lines(text):
    """+ and - lines of a unified diff or an apply_patch section (headers left out)."""
    n = 0
    for ln in str(text or '').splitlines():
        if ln.startswith(('+++', '---', '***', '@@')):
            continue
        if ln.startswith(('+', '-')):
            n += 1
    return n


def patch_lines(patch):
    """{path: lines} for each file of an apply_patch text."""
    out, cur = {}, None
    for ln in str(patch or '').splitlines():
        m = re.match(r'\*\*\* (?:Update|Add|Delete) File: (.+)', ln)
        if m:
            cur = m.group(1).strip(); out.setdefault(cur, 0); continue
        if cur and ln.startswith(('+', '-')) and not ln.startswith(('+++', '---')):
            out[cur] += 1
    return out


def change_lines(change):
    """lines of one entry of a FileChange item's changes (a unified diff, or a whole file added or deleted)."""
    if isinstance(change, str):
        return diff_lines(change)
    if not isinstance(change, dict):
        return None
    for k in ('unified_diff', 'diff'):
        if isinstance(change.get(k), str):
            return diff_lines(change[k])
    for k in ('content', 'new_content'):
        if isinstance(change.get(k), str):
            return len(change[k].splitlines())
    for v in change.values():
        if isinstance(v, dict):
            r = change_lines(v)
            if r is not None:
                return r
    return None


def edit_lines(name, inp):
    """lines a Claude Code edit tool's input touches: removed old text plus added new text."""
    L = lambda t: len(str(t).splitlines()) if t else 0
    if name == 'Edit':
        return L(inp.get('old_string')) + L(inp.get('new_string'))
    if name == 'MultiEdit':
        return sum(L(e.get('old_string')) + L(e.get('new_string')) for e in inp.get('edits') or [] if isinstance(e, dict))
    if name == 'Write':
        return L(inp.get('content'))
    if name == 'NotebookEdit':
        return L(inp.get('new_source'))
    return None
