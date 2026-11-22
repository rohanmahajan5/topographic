"""speed (3rd pass): what the Claude Code provider worked out from its transcripts and that does not change once worked out,
kept in a file between runs of the server, so a restart (or an app launch) does not read gigabytes again.

Kept: each transcript's head (its folder, first time and first prompt), each continuation's join and predecessor once its own
records have begun, what was read of a predecessor after a join, and the usage panel's per-file reading (its byte offset, the
calls counted so far, the plan readings). Every entry carries the file's inode, and the predecessor's, and is dropped on load
when either changed or the file is shorter than what was read; a reading goes on from its byte offset, as it does while the
server runs. The file is written by this server only (JSON, no code), in the user's own app data folder (cache_dir below; it
holds session titles and first prompts, so it never sits next to the code), and is ignored whole when the provider's code changed
(a hash of claude_code.py and spend.py is part of it).

Histories (the chat and the floor's calls) are not kept: they are larger and the server rereads only the floor's.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

from . import spend

HERE = Path(__file__).resolve().parent
VERSION = hashlib.blake2b((HERE / 'claude_code.py').read_bytes() + (HERE / 'spend.py').read_bytes() + Path(__file__).read_bytes(), digest_size=8).hexdigest()
_save_lock = threading.Lock()


APP = 'agent-ide'


def cache_dir(environ=None, platform=None, home=None):
    """The folder the kept state lives in (packaging, 2026-11-09): AGENT_IDE_CACHE_DIR when set (a packaged app points it at its
    own data folder), else the user's app data folder: ~/Library/Application Support/agent-ide/cache on macOS,
    %APPDATA%\\agent-ide\\cache on Windows, $XDG_CONFIG_HOME/agent-ide/cache (~/.config/agent-ide/cache) elsewhere; the folders an
    Electron app's userData sits in on each. The old in-repo folder (agent-ide/workspace/.cache) is neither read nor removed."""
    env = os.environ if environ is None else environ
    if env.get('AGENT_IDE_CACHE_DIR'):
        return Path(env['AGENT_IDE_CACHE_DIR']).expanduser()
    platform, home = platform or sys.platform, Path(home) if home else Path.home()
    if platform == 'darwin':
        base = home / 'Library' / 'Application Support'
    elif platform.startswith('win'):
        base = Path(env['APPDATA']) if env.get('APPDATA') else home / 'AppData' / 'Roaming'
    else:
        base = Path(env['XDG_CONFIG_HOME']) if env.get('XDG_CONFIG_HOME') else home / '.config'
    return base / APP / 'cache'


def _ino(p):
    try:
        st = os.stat(p)
        return st.st_ino, st.st_size
    except OSError:
        return None, None


def _ledger_out(led):
    return {'tok': led.tok, 'usd': led.usd, 'calls': led.calls, 'unpriced': led.unpriced, 'models': led.models,
            'buckets': [[k, v] for k, v in led.buckets.items()], 'last': led.last}


def _ledger_in(d):
    led = spend.Ledger()
    led.tok, led.usd, led.calls, led.unpriced, led.models, led.last = dict(d['tok']), dict(d['usd']), d['calls'], d['unpriced'], dict(d['models']), d['last'] and dict(d['last'])
    led.buckets = {k: list(v) for k, v in d['buckets']}
    return led


def _reader_out(r):
    if r.inode is None or not hasattr(r, 'led'):
        return None
    return {'offset': r.offset, 'inode': r.inode, 'before': r.before, 'led': _ledger_out(r.led), 'seen': list(r.seen), 'plan': r.plan, 'quota': r.quota}


def _readers_in(files, saved):
    for key, d in saved.items():
        ino, size = _ino(key)
        if ino != d['inode'] or size is None or size < d['offset']:
            continue          # replaced or cut since: read again from the start
        r = files.cls(key)
        if r.before != d['before']:
            continue
        r.reset()
        r.offset, r.inode = d['offset'], d['inode']
        r.led, r.seen, r.plan, r.quota = _ledger_in(d['led']), dict.fromkeys(d['seen'], 1), copy.deepcopy(d['plan']), copy.deepcopy(d['quota'])
        files.files[key] = r


def dump(p):
    """The provider's kept state as a JSON-ready dict (taken under its locks)."""
    with p.lock:
        heads = {str(k): dict(h, ino=_ino(k)[0]) for k, h in p.heads.items()}
        links = {}
        for k, c in p.links.items():
            if c.get('final') and c.get('join') and c.get('pred') is not None:
                links[str(k)] = dict(c, pred=str(c['pred']), predIno=_ino(c['pred'])[0], tried=None)
        branches = [[str(pred), born, b['off'], b['yes'], _ino(pred)[0]] for (pred, born), b in p.branches.items()]
    out = {'version': VERSION, 'home': str(p.home), 'heads': heads, 'links': links, 'branches': branches}
    for name in ('transcripts', 'own_calls'):
        files = getattr(p, name)
        with files.lock:
            out[name] = {k: d for k, d in ((k, _reader_out(r)) for k, r in files.files.items()) if d}
    return out


def load(p, data):
    """Put a kept state back into a new provider; entries whose files changed underneath are left out."""
    if not data or data.get('version') != VERSION or data.get('home') != str(p.home):
        return False
    with p.lock:
        for k, h in data.get('heads', {}).items():
            h = dict(h)
            if _ino(k)[0] == h.pop('ino', None):
                p.heads[Path(k)] = h
        for k, c in data.get('links', {}).items():
            ino, _ = _ino(k)
            if ino != c['ino'] or _ino(c['pred'])[0] != c.get('predIno'):
                continue
            c = dict(copy.deepcopy(c), pred=Path(c['pred']))
            c.pop('predIno', None)
            p.links[Path(k)] = c
        for pred, born, off, yes, ino in data.get('branches', []):
            pi, size = _ino(pred)
            if pi == ino and size is not None and size >= off:
                p.branches[(Path(pred), born)] = {'off': off, 'yes': yes}
    for name in ('transcripts', 'own_calls'):
        files = getattr(p, name)
        with files.lock:
            _readers_in(files, data.get(name, {}))
    return True


def fingerprint(p):
    """Changes when there is something new to keep (cheap: counts and offsets only)."""
    n = (len(p.heads), len(p.links), len(p.branches))
    offs = tuple(sum(getattr(r, 'offset', 0) for r in getattr(p, name).files.values()) for name in ('transcripts', 'own_calls'))
    return n + offs + tuple(b['off'] for b in list(p.branches.values()))[-1:]


def save(p, path):
    with _save_lock:
        data = json.dumps(dump(p), separators=(',', ':'))
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(data)
        os.replace(tmp, path)          # whole or not at all
        return len(data)


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
