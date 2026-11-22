"""Use and spend, the parts every provider shares (the usage panel, GET /api/usage).

A provider that keeps token records supplies its sessions' calls as a Ledger (Provider.session_usage) and its plan windows
(Provider.plan_usage); this module holds what they have in common: the one price table, keyed by model, the ledger that
prices each call and keeps it per minute, and a record file read once and then only from where it was left.

"API-equivalent" dollars are what the same tokens would cost at the public API list prices (PRICE_SOURCES), not what a
subscription costs. Nothing here reads a credential, token or key file, and nothing is sent anywhere.
"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path

CHUNK = 8 << 20          # speed (3rd pass): bytes a record file is read in (Tail.lines)
BUCKET = 60          # seconds: spend over time is kept per minute
WEEK = 8 * 86400     # how far back the plan readings and the measured ratio look

# $ per million tokens. inp = fresh (uncached) input, read = cache read, w5 / w1 = cache write (Claude: 5-minute / 1-hour TTL;
# OpenAI: 30-minute / 12-hour), out = output (thinking / reasoning tokens are part of output on both). long = (threshold on
# the call's whole input, the prices above it).
PRICES = {
    'claude-fable-5-1': dict(inp=10, read=.25, w5=12.5, w1=20, out=50),
    'claude-opus-5-5': dict(inp=4, read=.20, w5=5, w1=8, out=20),
    'claude-sonnet-5-5': dict(inp=2, read=.10, w5=2.5, w1=4, out=10),
    'claude-haiku-5-5': dict(inp=.10, read=.01, w5=.125, w1=.20, out=.50, long=(100_000, dict(inp=.50, read=.05, w5=.625, w1=1, out=2.50))),
    'claude-sonnet-5': dict(inp=2, read=.20, w5=2.5, w1=4, out=10),
    'claude-haiku-4-5': dict(inp=1, read=.10, w5=1.25, w1=2, out=5),
    'gpt-6.1-sol': dict(inp=2, read=.10, w5=2.5, w1=5, out=10, long=(272_000, dict(inp=4, read=.20, w5=2.5, w1=5, out=15))),
    'gpt-6-sol': dict(inp=2, read=.20, w5=2.5, w1=5, out=10, long=(272_000, dict(inp=4, read=.40, w5=2.5, w1=5, out=15))),
    'gpt-6.1-terra': dict(inp=1, read=.05, w5=1.25, w1=2.5, out=5, long=(272_000, dict(inp=2, read=.10, w5=1.25, w1=2.5, out=7.5))),
    'gpt-6-luna': dict(inp=.10, read=.01, w5=.125, w1=.25, out=.50, long=(272_000, dict(inp=.20, read=.02, w5=.125, w1=.25, out=.75))),
    'gpt-6-astra': dict(inp=10, read=1, w5=12.5, w1=25, out=50, long=(272_000, dict(inp=20, read=2, w5=12.5, w1=25, out=75))),
}
PRICE_SOURCES = {          # by the model ids' prefix
    'claude-': 'platform.claude.com/docs/en/about-claude/pricing, read 2026-11-07',
    'gpt-': 'developers.openai.com/api/docs/pricing, read 2026-11-07 (standard tier)',
}
FAST = 2.0          # Claude fast mode (usage.speed == 'fast'): Opus 5.5 is $8/$40, twice the standard price; applied to every part


def model_key(m):
    m = (m or '').split('[')[0]
    return re.sub(r'-20\d{6}$', '', m)


def price(model, tokens_in):
    p = PRICES.get(model_key(model))
    if p and p.get('long') and tokens_in > p['long'][0]:
        return p['long'][1]
    return p


def cost(model, fresh, read, w5, w1, out, fast=False):
    """The API-equivalent dollars of one call, split into its parts; None when the model has no public price."""
    p = price(model, fresh + read + w5 + w1)
    if not p:
        return None
    k = (FAST if fast else 1) / 1e6
    return {'fresh': fresh * p['inp'] * k, 'read': read * p['read'] * k, 'write': (w5 * p['w5'] + w1 * p['w1']) * k, 'out': out * p['out'] * k}


def ts(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError):
        return None


def needle_lines(data, end, need):
    """The lines of data[:end] that hold any of the byte strings in `need`, in file order (speed, 3rd pass: found with
    bytes.find over the whole read instead of testing every line in Python; the same lines as before, half or less of them)."""
    starts = set()
    for n in need:
        i = data.find(n, 0, end)
        while i >= 0:
            s = data.rfind(b'\n', 0, i) + 1
            starts.add(s)
            e = data.find(b'\n', i, end)
            i = data.find(n, e + 1, end) if e >= 0 else -1
    for s in sorted(starts):
        e = data.find(b'\n', s, end)
        yield data[s:e if e >= 0 else end]


class Tail:
    """A record file read once and then only from where it was left (as the server's own observer does)."""

    def __init__(self, path):
        self.path, self.offset, self.inode = Path(path), 0, None

    def lines(self, need=None):
        """The new whole lines' records, in file order. speed (3rd pass): read and handed over a block at a time (CHUNK bytes,
        a line longer than that carried over whole), so a first read of a large file never holds all of it, or all of its
        records, at once: the same records as one read, the offset moved past each block's last whole line once it is handed over."""
        try:
            st = self.path.stat()
        except OSError:
            return
        if self.inode != st.st_ino or st.st_size < self.offset:
            self.offset, self.inode = 0, st.st_ino
            self.reset()
        if st.st_size == self.offset:
            return
        with open(self.path, 'rb') as f:
            f.seek(self.offset)
            carry = b''
            while True:
                block = f.read(CHUNK)
                if not block:
                    return
                data = carry + block if carry else block
                end = data.rfind(b'\n') + 1
                carry = data[end:]
                if not end:
                    continue
                raws = data[:end].split(b'\n') if need is None else needle_lines(data, end, need)
                out = []
                for raw in raws:
                    if raw:
                        try:
                            out.append(json.loads(raw))
                        except ValueError:
                            pass
                self.offset += end
                yield from out

    def reset(self):
        pass


class Ledger:
    """One session's calls: totals, per-minute buckets, and the cache hit rate."""

    def __init__(self):
        self.tok = dict(fresh=0, read=0, write=0, out=0, reasoning=0)
        self.usd = dict(fresh=0.0, read=0.0, write=0.0, out=0.0)
        self.calls, self.unpriced, self.models, self.buckets, self.last = 0, 0, {}, {}, None

    def add(self, t, model, fresh, read, w5, w1, out, reasoning, fast=False):
        c = cost(model, fresh, read, w5, w1, out, fast)
        self.calls += 1
        self.models[model] = self.models.get(model, 0) + 1
        for k, v in (('fresh', fresh), ('read', read), ('write', w5 + w1), ('out', out), ('reasoning', reasoning)):
            self.tok[k] += v
        b = self.buckets.setdefault(int(t // BUCKET) * BUCKET, [0.0] * 4 + [0] * 4)          # $ fresh, read, write, out; tokens fresh, read, write, out
        if c is None:
            self.unpriced += 1
        else:
            for i, k in enumerate(('fresh', 'read', 'write', 'out')):
                self.usd[k] += c[k]
                b[i] += c[k]
        b[4] += fresh; b[5] += read; b[6] += w5 + w1; b[7] += out
        self.last = {'t': t, 'model': model, 'fresh': fresh, 'read': read, 'write': w5 + w1, 'out': out}
        return c

    def view(self):
        inp = self.tok['fresh'] + self.tok['read'] + self.tok['write']
        return {'tokens': self.tok, 'usd': {k: round(v, 5) for k, v in self.usd.items()}, 'total': round(sum(self.usd.values()), 5),
                'calls': self.calls, 'unpriced': self.unpriced, 'models': self.models, 'hit': (self.tok['read'] / inp) if inp else None,
                'last': self.last, 'buckets': sorted([k] + [round(x, 6) for x in v[:4]] + v[4:] for k, v in self.buckets.items())}


def merge(ledgers):
    """One ledger from several (a conversation that went on in new transcript files): totals, models and minutes added up."""
    out = Ledger()
    for led in ledgers:
        for k in out.tok:
            out.tok[k] += led.tok[k]
        for k in out.usd:
            out.usd[k] += led.usd[k]
        out.calls += led.calls
        out.unpriced += led.unpriced
        for m, n in led.models.items():
            out.models[m] = out.models.get(m, 0) + n
        for t, v in led.buckets.items():
            b = out.buckets.setdefault(t, [0.0] * 4 + [0] * 4)
            for i, x in enumerate(v):
                b[i] += x
        if led.last and (not out.last or led.last['t'] >= out.last['t']):
            out.last = led.last
    return out


class Files:
    """Record readers kept between calls, one per file: each reads only what was appended since (thread safe)."""

    def __init__(self, cls):
        self.cls, self.files, self.lock = cls, {}, threading.Lock()

    def read(self, path):
        with self.lock:
            key = str(path)
            f = self.files.get(key)
            if f is None:
                f = self.files[key] = self.cls(path)
            return f.read()
