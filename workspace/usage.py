"""Use and spend for live.html's usage panel (GET /api/usage?cwd=<floor>), for every registered provider.

Each provider supplies its own part (providers/base.py): its sessions' calls as a ledger (session_usage) and its plan windows
(plan_usage), read only from its own record files; providers/spend.py holds what they share, the one price table keyed by
model among it. This module only asks every provider in turn. A provider that supplies neither is still listed, with no plan
and no spend. Codex and Claude Code are read from their own record files only; the plan readers for the other harnesses
(providers/planuse.py) ask each harness's own service with the sign-in it keeps on this Mac (Cursor) or run its own usage report
(Antigravity), in the background, and send nothing anywhere else.

Answer: {'generatedAt', 'bucket', 'sessions': {sid: ledger view + 'provider'}, 'providers': [{'name', 'label', 'plan' (None: no
figure), 'note' (why there is no figure, in words), 'state' (None, or {'kind': 'refused', 'reason', 'resetsAt', 'at'}: the harness
is refusing requests; providers/planuse.py)}],
'prices', 'priceSources'}, the providers in the server's order.
"""
import threading
import time

from providers.base import under
from providers.spend import BUCKET, PRICES, PRICE_SOURCES

LOCK = threading.Lock()          # one collection at a time: the first reads every recent record file


def set_up(p):
    """The person has this harness on this Mac (its available() finds it). Never waits on a command (available() does not)."""
    try:
        return bool((p.available() or {}).get('found'))
    except Exception:
        return False


def collect(app, cwd, look=False):
    """cwd None: no floor, the plan blocks alone. look: someone has Plan use open (or its gauge hovered), so a harness whose plan
    reading is slow to take (Antigravity's usage report, Cursor's service) may read it now (providers/planuse.Background)."""
    with LOCK:
        cwd = None if cwd is None else (str(cwd).rstrip('/') or '/')
        sessions, floor = {}, set()
        for s in ([] if cwd is None else app.all_sessions()):
            if not under(s.get('cwd'), cwd):
                continue
            p = app.provider_of(s['id'])
            floor.add(s.get('provider') or p.name)
            try:
                led = p.session_usage(s['id'])
            except Exception:
                led = None          # a provider whose records cannot be read shows no spend; the others are unaffected
            if led is not None:
                sessions[s['id']] = dict(led.view(), provider=s.get('provider') or p.name)
        providers = []
        for p in app.providers:
            # plan use for every harness the person has set up (2026-11-22: every provider is checked, Cursor and Antigravity
            # included): installed here, or with sessions on this
            # floor. Each says why it has no figure rather than showing a dash.
            if p.name not in floor and not set_up(p):
                continue
            try:
                plan = p.plan_usage(look=look) if getattr(p, 'plan_reads_on_look', False) else p.plan_usage()
            except Exception as e:
                plan = {'windows': [], 'note': 'could not read the plan figures (%s)' % type(e).__name__}
            plan = dict(plan) if isinstance(plan, dict) else None
            note, state = (plan or {}).pop('note', None), (plan or {}).pop('state', None)
            read = (plan or {}).pop('read', None)          # a slow reader's {'at': when its figures were read, 'busy': reading again now}
            if not plan or not plan.get('windows'):
                plan, note = None, note or getattr(p, 'plan_note', None) or "%s doesn't report its plan use to other apps" % p.label
            providers.append({'name': p.name, 'label': p.label, 'plan': plan, 'note': note, 'state': state, **({'read': read} if read else {})})
        return {'generatedAt': time.time(), 'bucket': BUCKET, 'sessions': sessions, 'providers': providers, 'prices': PRICES, 'priceSources': PRICE_SOURCES}
