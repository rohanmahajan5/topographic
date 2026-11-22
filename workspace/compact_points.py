"""Where each session's context will be compacted, for live.html's columns (each column is drawn on its own scale: floor 0, the
top of the staff its compaction point), for every registered provider.

Each provider supplies its own rule (providers/base.py, compaction_point): Codex's in providers/codex.py, Claude Code's in
providers/claude_code.py. This module only asks the session's provider. The canvas carries the answer on every session
(server.Handler.canvas, its "compaction" key); GET /api/compaction?cwd=<floor> answers the same for one floor on its own. Read only:
the providers read their settings and model lists (only the keys that set the point), never a credential or an environment
value, and nothing is sent anywhere.

Answer of collect: {'generatedAt', 'sessions': {sid: point + 'provider'}}; a session whose provider has no rule is left out.
"""
import time

from providers.base import under


def point_of(app, s, log=None):
    """The compaction point of one session row of app.all_sessions() (its history `log` when the caller holds it), or None."""
    p = app.provider_of(s['id'])
    try:
        return p.compaction_point(s['id'], model=s.get('model') or (log or {}).get('model'), window=(log or {}).get('window'))
    except Exception:
        return None          # a provider whose records cannot be read: its column keeps the shared scale; the others are unaffected


def collect(app, cwd):
    cwd = str(cwd).rstrip('/') or '/'
    out = {}
    for s in app.all_sessions():
        if not under(s.get('cwd'), cwd):
            continue
        pt = point_of(app, s, app.log_of(s))
        if pt:
            out[s['id']] = dict(pt, provider=s.get('provider') or app.provider_of(s['id']).name)
    return {'generatedAt': time.time(), 'sessions': out}
