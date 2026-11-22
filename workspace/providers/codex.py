"""Codex behind the provider interface. A wrapper only: discovery and log parsing stay in server.Observer,
unchanged; this module adapts their output to the interface in base.py."""
import glob
import json
import os
import time
import tomllib
from pathlib import Path

from .base import FileCache, Provider, fmt_k, under
from . import spend
from .spend import BUCKET, WEEK, Ledger, Tail, ts

# Where Codex compacts by itself (openai/codex at 57a808a3, 2026-11-07, codex-rs/protocol/src/openai_models.rs,
# auto_compact_token_limit): 90% of the model's context window, lowered (never raised) by `model_auto_compact_token_limit` in
# config.toml or by the model list's own `auto_compact_token_limit`. The window is the model list's `context_window`
# (~/.codex/models_cache.json), capped by `model_context_window` in config.toml; the rollouts' token_count reports 95% of it
# (`effective_context_window_percent`). Seen here: one automatic compaction of gpt-6-luna at 244,889 against 244,800.
AUTO_PCT = 90
STAND_IN = 272_000          # the window of most models in the list; used, marked as an estimate, when the record names no model it has
CONFIG_KEYS = ('model_context_window', 'model_auto_compact_token_limit')          # the only keys of config.toml read for this
_config = FileCache(lambda t: {k: v for k, v in tomllib.loads(t).items() if k in CONFIG_KEYS and isinstance(v, int)}, {})
_models = FileCache(lambda t: {m['slug']: m for m in json.loads(t).get('models', []) if isinstance(m, dict) and m.get('slug')}, {})


class Rollout(Tail):
    """A Codex rollout's calls (token_count events) and the plan windows each one reports (rate_limits)."""

    def reset(self):
        self.led, self.model, self.total, self.rates = Ledger(), None, None, []

    def read(self):
        if self.inode is None:
            self.reset()
        for d in self.lines((b'"token_count"', b'"turn_context"')):
            p = d.get('payload') or {}
            if d.get('type') == 'turn_context':
                self.model = p.get('model') or self.model
                continue
            if d.get('type') != 'event_msg' or p.get('type') != 'token_count':
                continue
            t = ts(d.get('timestamp'))
            r = p.get('rate_limits') or {}
            if t and r.get('limit_id') == 'codex' and r.get('primary'):
                self.rates.append((t, r))
            info = p.get('info') or {}
            tot, last = info.get('total_token_usage'), info.get('last_token_usage') or {}
            if not tot or tot == self.total or not t:
                continue          # a token_count that repeats the previous totals (a rate-limit-only update) is not a new call
            self.total = tot
            i, c = last.get('input_tokens', 0), last.get('cached_input_tokens', 0)
            w = last.get('cache_write_input_tokens', 0) or 0
            self.led.add(t, self.model, max(0, i - c - w), c, w, 0, last.get('output_tokens', 0), last.get('reasoning_output_tokens', 0))
        return self


class CodexProvider(Provider):
    name = 'codex'
    label = 'Codex'
    can_start = can_send = can_stop = can_reply = True     # through the app-server bridge (server.Bridge)

    def __init__(self, workspace, home, binary=None):
        self.ws, self.home, self.find_binary = workspace, home, binary
        self.rollouts = spend.Files(Rollout)          # usage panel

    def available(self):
        found = any(self.home.glob('state_*.sqlite'))
        try:
            binary = self.find_binary() if self.find_binary else None
        except Exception:
            binary = None
        return {'name': self.name, 'label': self.label, 'found': found, 'store': str(self.home), 'binary': binary, **self.flags()}

    def owns(self, sid):
        return not str(sid).startswith('claude:')

    def list_sessions(self, project_root=None):
        out = []
        for s in self.ws.sessions():
            if project_root is None or under(s['cwd'], project_root):
                out.append({'id': s['id'], 'provider': self.name, 'title': s['title'], 'cwd': s['cwd'], 'started': s['createdAt'],
                            'updated': s['updatedAt'], 'running': s['status'] == 'active', 'parent': s['parent']})
        return out

    def read_history(self, sid):
        row = self.ws.session(sid)
        if row and row.get('rollout_path'):
            return self.ws.observer.read_log(row)
        return self.ws.observer.cache.get(sid, {'messages': [], 'events': [], 'turnActive': False})

    # ---- where this session compacts (the columns' scale) ----
    def compaction_point(self, sid, model=None, window=None):
        if not model:
            try:
                row = self.ws.session(sid) or {}
            except Exception:
                row = {}
            model = row.get('model')
            if not model and row.get('rollout_path'):
                model = self.rollouts.read(row['rollout_path']).model          # the newest turn_context's model
        return codex_point(model, window, self.home)

    # ---- use and spend (usage panel) ----
    def session_usage(self, sid):
        try:
            row = self.ws.observer.row(sid)
        except Exception:
            row = None          # no Codex state database (Codex not installed): no Codex sessions to read
        path = (row or {}).get('rollout_path')
        return self.rollouts.read(path).led if path else None

    def recent_rollouts(self):
        """Every rollout written in the last WEEK: the account's rate-limit readings and the measured dollars per point."""
        cut = time.time() - WEEK
        out = []
        for p in glob.glob(str(Path(self.home) / 'sessions' / '20*' / '*' / '*' / 'rollout-*.jsonl')):
            try:
                if os.path.getmtime(p) < cut:
                    continue
            except OSError:
                continue
            out.append(self.rollouts.read(p))
        return out

    plan_note = 'no reading yet: Codex reports its plan with each call an agent makes'

    def plan_usage(self):
        d = self.codex_plan(self.recent_rollouts())
        if not d.get('windows'):
            return None
        return {'plan': d.get('plan'), 'at': d.get('at'), 'windows': [{'label': w['label'], 'percent': w['percent'], 'resetsAt': w['resetsAt']} for w in d['windows']],
                'detail': {'source': d['source'], 'series': d['series'], 'measured': d['measured'], 'minutes': {w['label']: w['minutes'] for w in d['windows']}}}

    def codex_plan(self, files):
        rates = sorted((x for f in files for x in f.rates), key=lambda x: x[0])
        series, last = [], None
        for t, r in rates:
            v = (r['primary']['used_percent'], (r.get('secondary') or {}).get('used_percent'))
            if last is None or v != last[1] or t - last[0] > 600:
                series.append([round(t), v[0], v[1], r['primary'].get('resets_at'), (r.get('secondary') or {}).get('resets_at')])
                last = (t, v)
        latest = rates[-1] if rates else None
        out = {'source': 'rate_limits on the newest token_count in the Codex rollouts (the plan as Codex reports it)', 'series': series[-3000:]}
        if latest:
            t, r = latest
            out.update(at=t, plan=r.get('plan_type'), windows=[
                {'label': '5-hour', 'percent': r['primary']['used_percent'], 'minutes': r['primary'].get('window_minutes'), 'resetsAt': r['primary'].get('resets_at')},
                {'label': 'weekly', 'percent': (r.get('secondary') or {}).get('used_percent'), 'minutes': (r.get('secondary') or {}).get('window_minutes'), 'resetsAt': (r.get('secondary') or {}).get('resets_at')}])
        # the app-server's account/rateLimits (live.html's status card) carries no time of reading, so the newest rollout reading is used
        out['measured'] = self.codex_ratio(files, rates)
        return out

    def codex_ratio(self, files, rates):
        """Dollars per point measured here: for each 5-hour (and weekly) window, the API-equivalent dollars of every call this Mac
        recorded in it, over the points the window rose. Only windows that rose 10 points or more count."""
        calls = []
        for f in files:
            for k, b in f.led.buckets.items():
                calls.append((k, b[0] + b[1] + b[2] + b[3]))
        out = {}
        for idx, name, step in ((('primary',), '5-hour', 600), (('secondary',), 'weekly', 3600)):
            win = {}
            for t, r in rates:
                w = r.get(idx[0]) or {}
                if w.get('resets_at') is None:
                    continue
                key = round(w['resets_at'] / step)
                lo, hi, tl, th = win.get(key, (999, -1, t, t))
                win[key] = (min(lo, w['used_percent']), max(hi, w['used_percent']), min(tl, t), max(th, t))
            ratios = []
            for lo, hi, tl, th in win.values():
                if hi - lo < 10:
                    continue
                usd = sum(c for t, c in calls if tl - BUCKET <= t <= th)
                ratios.append(usd / (hi - lo))
            ratios.sort()
            if ratios:
                out[name] = {'windows': len(ratios), 'low': round(ratios[0], 3), 'median': round(ratios[len(ratios) // 2], 3), 'high': round(ratios[-1], 3)}
        out['note'] = 'API-equivalent dollars of the calls recorded on this Mac per point the window rose; usage on other devices or in ChatGPT is not in these records'
        return out


def codex_point(model, reported, home):
    """Codex's auto-compact point for a model: see AUTO_PCT. reported: the window the session's rollout reported (95% of the
    model window), used when the model list does not have the model."""
    cfg, info = _config.get(Path(home) / 'config.toml'), _models.get(Path(home) / 'models_cache.json').get(model or '')
    estimate = False
    if info and info.get('context_window'):
        raw, src = info['context_window'], "Codex's model list for " + model
    elif reported:
        pct = 95
        raw, src = round(reported * 100 / pct), 'the window this session reported (95% of the model window)'
    else:
        raw, src, estimate = STAND_IN, 'no model in the record that Codex lists: the usual ' + fmt_k(STAND_IN) + ' window', True
    if cfg.get('model_context_window') and cfg['model_context_window'] < raw:
        raw, src = cfg['model_context_window'], 'model_context_window in config.toml'
    thr, rule = raw * AUTO_PCT // 100, str(AUTO_PCT) + '% of the ' + fmt_k(raw) + ' window'
    if info and isinstance(info.get('auto_compact_token_limit'), int) and info['auto_compact_token_limit'] < thr:
        thr, rule = info['auto_compact_token_limit'], "the model list's auto_compact_token_limit"
    if cfg.get('model_auto_compact_token_limit') and cfg['model_auto_compact_token_limit'] < thr:
        thr, rule = cfg['model_auto_compact_token_limit'], 'model_auto_compact_token_limit in config.toml'
    return {'threshold': thr, 'window': raw, 'model': model, 'rule': rule, 'source': src, 'estimate': estimate}
