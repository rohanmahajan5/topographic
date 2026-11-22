"""Plan use for the harnesses beyond Codex and Claude Code (the usage panel's "Plan use", GET /api/usage): where each one's plan
figures come from, and what the panel says when a harness refuses requests.

A plan reading has the shape Provider.plan_usage documents ({'plan', 'at', 'windows': [{label, percent, resetsAt, text?}],
'detail'}) plus two fields every harness may carry:
  note:  why there is no figure, in plain words ("not signed in", "Cursor didn't report usage (HTTP 500)"), or None
  state: {'kind': 'refused', 'reason': the harness's own words, 'resetsAt': epoch or None, 'at': epoch} while the harness is
         refusing requests (its latest failed turn was a plan, quota or usage-limit refusal and no turn has gone through since)

Sources (each read in the background only while Plan use is open or its gauge hovered, or right after a refusal, at most once
per READ_EVERY seconds, never while a page request waits):
- Cursor: GET https://cursor.com/api/usage-summary (the plan's included use, Auto and API shares, on-demand spend, billing cycle) and,
  for request-based plans, GET https://cursor.com/api/usage?user=<id> (requests used of the plan's limit), with the sign-in Cursor
  keeps on this Mac: Cursor.app's state database (ItemTable cursorAuth/accessToken, read only) or the agent CLI's file store (~/.cursor/auth.json on macOS,
  ~/.config/cursor/auth.json on Linux; its default macOS Keychain item is not opened). This is
  how CodexBar reads Cursor (github.com/steipete/CodexBar, docs/cursor.md and Providers/Cursor/*.swift, read 2026-11-22). The token
  goes to cursor.com only; an encrypted app token (cursor-safe-storage:, kept in the Keychain) is not decrypted here.
- Antigravity: `agy -p /usage --output-format json`, the CLI's own non-interactive usage report (agy 1.1.11 and later; CodexBar's
  AntigravityProviderDescriptor.swift and AntigravityQuotaSummaryParser.swift). It sends no model prompt; agy talks to Google itself.
- OpenCode: no plan. It uses the model providers the person set up in it (their own API keys or sign-ins); its spend is in the
  ledger. Its refusals still show as its state.
"""
import base64
import calendar
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

READ_EVERY = 300.0          # s at least between reads of a harness's plan figures, each only while someone looks [coordinator 2026-11-22]
REFUSAL_CLASSES = ('usage_limit',)          # core.FAILURE_CLASSES that are refusals by definition

# A failed turn whose text says the harness refused the request for the account's plan, quota or limit (not a crash, a network
# fault or a bad prompt). [ours, judgement: built from the refusals seen on this Mac (Cursor's "Upgrade your plan to continue",
# Antigravity's RESOURCE_EXHAUSTED, Claude's "You've hit your limit") and the usual wording of 402/429 answers; not a published list]
REFUSAL = re.compile(
    r"usage[ _-]?limit|rate[ _-]?limit|\bquota\b|quota_exhausted|resource[_ ]exhausted|too many requests|\b429\b|\b402\b|payment required"
    r"|out of (?:credits|requests|usage|tokens)|insufficient (?:credits?|balance|quota|funds)|credit balance|spend(?:ing)? limit"
    r"|(?:hit|reached|exceeded|used up|run out of|ran out of) (?:your|the) [^.\n]{0,40}?(?:limit|quota|requests|credits|allowance)"
    r"|premium (?:model )?requests?|upgrade (?:your|to a|to) [^.\n]{0,24}?plan|upgrade to (?:pro|premium)"
    r"|not (?:available|included|supported|allowed) (?:on|in|with|for) (?:your|the|a) [^.\n]{0,30}?(?:plan|tier|subscription)"
    r"|requires? (?:a )?(?:paid|pro|premium|higher) (?:plan|tier|subscription)", re.I)


def is_refusal(text, cls=None):
    return cls in REFUSAL_CLASSES or bool(text and REFUSAL.search(str(text)))


def resets_in_text(text, now=None):
    """When a refusal lifts, from the harness's own words, or None: an ISO time after "reset(s)", "Resets in 4h29m", a retryDelay."""
    now = time.time() if now is None else now
    text = str(text or '')
    m = re.search(r'(?i)reset\w*\W{0,3}(?:at|on)?\W{0,3}(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?', text) or \
        re.search(r'"quotaResetTimeStamp"\s*:\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z)?"', text)
    if m:
        return iso_epoch(m.group(1) + (m.group(2) or 'Z'))
    m = re.search(r'[Rr]esets? in ((?:\d+\s*h)?\s*(?:\d+\s*m)?\s*(?:\d+(?:\.\d+)?\s*s)?)', text)
    if m and m.group(1).strip():
        parts = dict((u, float(v)) for v, u in re.findall(r'(\d+(?:\.\d+)?)\s*([hms])', m.group(1)))
        return now + parts.get('h', 0) * 3600 + parts.get('m', 0) * 60 + parts.get('s', 0)
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text)
    return now + float(m.group(1)) if m else None


def iso_epoch(s):
    """An ISO 8601 time ('2026-09-17T18:40:27Z', '...+02:00', '2025-02-01T00:00:00.000Z', or a date alone) as epoch seconds."""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r'(\d{4}-\d\d-\d\d)(?:[T ](\d\d:\d\d(?::\d\d)?)(?:\.\d+)?)?\s*(Z|[+-]\d\d:?\d\d)?$', s)
    if not m:
        return None
    clock = m.group(2) or '00:00:00'
    if len(clock) == 5:
        clock += ':00'
    try:
        t = calendar.timegm(time.strptime(m.group(1) + 'T' + clock, '%Y-%m-%dT%H:%M:%S'))
    except ValueError:
        return None
    z = m.group(3)
    if z and z != 'Z':
        sign = 1 if z[0] == '+' else -1
        hh, mm = int(z[1:3]), int(z[-2:])
        t -= sign * (hh * 3600 + mm * 60)
    return float(t)


class Refusals:
    """One harness's refusal state: set when a turn fails with a refusal, cleared when a later turn goes through or the refusal's
    reset time has passed."""

    def __init__(self):
        self.state = None
        self.lock = threading.Lock()

    def turn_ended(self, error=None, cls=None, resets_at=None, session=None, cancelled=False, now=None):
        """A turn ended: error is its failure text (None when it went through). Returns the refusal it recorded, or None."""
        now = time.time() if now is None else now
        if isinstance(resets_at, str):
            resets_at = iso_epoch(resets_at)          # OpenCode keeps Claude's reset as written (an ISO time)
        with self.lock:
            if error and is_refusal(error, cls):
                self.state = {'kind': 'refused', 'reason': str(error).strip()[:600], 'at': now, 'session': session,
                              'resetsAt': resets_at if resets_at is not None else resets_in_text(error, now)}
                return dict(self.state)
            if not error and not cancelled:
                self.state = None          # a turn went through: the harness takes requests again
            return None

    def current(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            s = self.state
            if s and s.get('resetsAt') and s['resetsAt'] <= now:
                self.state = s = None          # its limit has lifted
            return dict(s) if s else None


class Background:
    """A slow reading (Antigravity's usage report starts a whole language server; Cursor's is a call to its service), taken only
    when someone is looking: get(look=True) when Plan use is open or its gauge hovered, or due() right after a refusal, and never
    more often than once per `every` seconds. get() always answers at once with the newest value (None before the first) and
    never starts a read when look is False. Coordinator 2026-11-22: "never while no one is looking".
    read() of {'at': when the value was read, 'busy': a read is running} says how old the shown figures are."""

    def __init__(self, read, every=READ_EVERY):
        self.read, self.every = read, every
        self.value, self.at, self.started, self.busy, self.owed = None, 0.0, None, False, False
        self.lock = threading.Lock()

    def _may(self, now):
        if self.busy:
            return False
        if self.started is None or now - self.started >= self.every:
            return True
        return isinstance(self.value, dict) and bool(self.value.get('retry')) and now - self.started >= 5          # nothing ran: try soon

    def _start(self, now):
        self.busy, self.started, self.owed = True, now, False
        threading.Thread(target=self._run, name='agent-ide-plan-read', daemon=True).start()

    def due(self):
        """Its figures have probably changed (a refusal): read now if the cap allows, else at the first look once it does."""
        with self.lock:
            now = time.time()
            if self._may(now):
                self._start(now)
            else:
                self.owed = True

    invalidate = due

    def get(self, look=False):
        with self.lock:
            now = time.time()
            if look and self._may(now):
                self._start(now)
            return self.value

    def state(self):
        with self.lock:
            return {'at': self.at or None, 'busy': self.busy}

    def _run(self):
        try:
            v = self.read()
        except Exception as e:          # a reader's own bug says so on the panel; it never fails the page
            v = {'plan': None, 'at': time.time(), 'windows': [], 'note': 'could not read the plan figures (%s)' % type(e).__name__}
        with self.lock:
            self.value, self.busy, self.at = v, False, time.time()


def reading(note=None, plan=None, windows=None, detail=None, at=None):
    return {'plan': plan, 'at': at if at is not None else time.time(), 'windows': windows or [], 'detail': detail or {}, 'note': note}


# ---------- Cursor ----------
CURSOR_APP_DB = '~/Library/Application Support/Cursor/User/globalStorage/state.vscdb'
CURSOR_BASE = 'https://cursor.com'
CURSOR_PLANS = {'enterprise': 'Enterprise', 'express': 'Start', 'free': 'Free', 'free_trial': 'Pro Trial', 'hobby': 'Hobby', 'pro': 'Pro',
                'pro_student': 'Pro', 'pro_plus': 'Pro+', 'team': 'Team', 'ultra': 'Ultra'}          # CodexBar formatMembershipType


def cursor_agent_auth(env=None):
    """Where `cursor-agent login` may keep its session in a file, first found wins:
    - ~/.cursor/auth.json: macOS with AGENT_CLI_CREDENTIAL_STORE=file (a reply on Cursor's forum, @mohitjain, 2026-08-04, thread
      "errSecItemNotFound couldn't find your saved login in the macOS keychain": "Tokens land in ~/.cursor/auth.json"). By default
      on macOS the agent keeps it in the login Keychain (service cursor-access-token, account cursor-user, same reply), which this
      page does not open.
    - <config home>/cursor/auth.json ($XDG_CONFIG_HOME when absolute, else ~/.config): Linux (CodexBar CursorAgentAuthStore)."""
    env = os.environ if env is None else env
    xdg = (env.get('XDG_CONFIG_HOME') or '').strip()
    base = xdg if xdg.startswith('/') else os.path.expanduser('~/.config')
    return [os.path.expanduser('~/.cursor/auth.json'), os.path.join(base, 'cursor', 'auth.json')]


def jwt_claims(token):
    try:
        part = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        return None


def _blob_text(v):
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        return b.decode('utf-16-le', 'replace') if len(b) > 1 and b[1:2] == b'\x00' else b.decode('utf-8', 'replace')
    return v if isinstance(v, str) else None


def cursor_app_token(path=CURSOR_APP_DB):
    """(token, why): the Cursor app's saved access token from its state database, opened read only; why says what was missing."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None, 'no Cursor app sign-in on this Mac'
    row = None
    for uri in ('file:%s?mode=ro' % path, 'file:%s?immutable=1' % path):          # immutable: a WAL file with no sidecars, never written
        try:
            c = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                row = c.execute("select value from ItemTable where key = 'cursorAuth/accessToken' limit 1").fetchone()
            finally:
                c.close()
            break
        except sqlite3.Error:
            continue
    tok = (_blob_text(row[0]) or '').strip().strip('\x00') if row else ''
    if not tok:
        return None, 'the Cursor app is not signed in'
    if tok.startswith('cursor-safe-storage:'):
        return None, "the Cursor app keeps its sign-in encrypted in the Keychain, which this page does not open"
    return tok, None


def cursor_agent_token(path=None):
    """(token, why) from the agent CLI's file store; path: one file or a list (default cursor_agent_auth())."""
    paths = [path] if isinstance(path, str) else (path or cursor_agent_auth())
    for p in paths:
        try:
            with open(p, encoding='utf-8') as f:
                tok = (json.load(f) or {}).get('accessToken')
        except (OSError, ValueError, AttributeError):
            continue
        return (tok.strip(), None) if isinstance(tok, str) and tok.strip() else (None, 'the Cursor agent is not signed in')
    return None, ('no Cursor agent sign-in in a file (on macOS the agent keeps it in the Keychain, which this page does not open; '
                  'AGENT_CLI_CREDENTIAL_STORE=file before `cursor-agent login` keeps it in ~/.cursor/auth.json)')


def cursor_session(app_db=CURSOR_APP_DB, agent_auth=None, now=None):
    """({'cookie', 'user'}, None) for the first usable saved sign-in, else (None, why)."""
    now = time.time() if now is None else now
    whys = []
    for tok, why in (cursor_app_token(app_db), cursor_agent_token(agent_auth)):
        if not tok:
            whys.append(why)
            continue
        c = jwt_claims(tok) or {}
        sub = str(c.get('sub') or '').split('|')[-1]
        if not sub or not re.fullmatch(r'[A-Za-z0-9._-]+', sub):
            whys.append('the saved Cursor sign-in has no user id')
            continue
        if not isinstance(c.get('exp'), (int, float)) or c['exp'] - now <= 60:
            whys.append('the saved Cursor sign-in has expired (open Cursor or run `cursor-agent login`)')
            continue
        return {'cookie': 'WorkosCursorSessionToken=%s%%3A%%3A%s' % (sub, tok), 'user': sub}, None
    return None, 'not signed in: ' + '; '.join(w for w in whys if w)


class CursorError(Exception):
    def __init__(self, status, text=''):
        super().__init__(status)
        self.status, self.text = status, text


def cursor_fetch(path, cookie, timeout=10.0):
    """GET a cursor.com JSON endpoint with the session cookie; the answer's JSON, or CursorError(HTTP status)."""
    req = urllib.request.Request(CURSOR_BASE + path, headers={'Accept': 'application/json', 'Cookie': cookie, 'User-Agent': 'agent-ide'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(1 << 20).decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        raise CursorError(e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CursorError(None, type(e).__name__)


def _pct(v):
    return max(0.0, min(100.0, float(v))) if isinstance(v, (int, float)) else None


def _usd(cents):
    return '$%.2f' % (cents / 100.0)


def cursor_reading(summary, requests=None, now=None):
    """The plan reading from /api/usage-summary (and /api/usage for request plans), as CodexBar's CursorStatusProbe maps them:
    a request plan shows its requests; otherwise the plan's included use this cycle (totalPercentUsed, else Auto and API's mean,
    else used / limit), its Auto and API shares, and on-demand spend against its limit. Cursor's percent fields are already in
    percent units. Every window resets at the end of the billing cycle."""
    s = summary if isinstance(summary, dict) else {}
    end = iso_epoch(s.get('billingCycleEnd'))
    iu = s.get('individualUsage') or {}
    plan = iu.get('plan') or {}
    od = iu.get('onDemand') or {}
    kind = str(s.get('membershipType') or '')
    name = CURSOR_PLANS.get(kind.lower(), kind or None)
    wins = []
    g4 = (requests or {}).get('gpt-4') if isinstance(requests, dict) else None
    used_r = (g4 or {}).get('numRequestsTotal', (g4 or {}).get('numRequests'))
    lim_r = (g4 or {}).get('maxRequestUsage')
    if isinstance(lim_r, int) and lim_r > 0 and isinstance(used_r, int):
        wins.append({'label': 'requests', 'percent': _pct(100.0 * used_r / lim_r), 'resetsAt': end, 'text': '%d of %d requests' % (used_r, lim_r)})
    else:
        auto, api = _pct(plan.get('autoPercentUsed')), _pct(plan.get('apiPercentUsed'))
        total = _pct(plan.get('totalPercentUsed'))
        if total is None and auto is not None and api is not None:
            total = (auto + api) / 2
        total = total if total is not None else api if api is not None else auto
        used, lim = plan.get('used'), plan.get('limit')
        if total is None and isinstance(lim, (int, float)) and lim > 0 and isinstance(used, (int, float)):
            total = _pct(100.0 * used / lim)
        if total is None:
            ov = iu.get('overall') or {}
            if isinstance(ov.get('limit'), (int, float)) and ov['limit'] > 0 and isinstance(ov.get('used'), (int, float)):
                total, used, lim = _pct(100.0 * ov['used'] / ov['limit']), ov['used'], ov['limit']
        if total is not None:
            text = '%s of %s included' % (_usd(used), _usd(lim)) if isinstance(lim, (int, float)) and lim > 0 and isinstance(used, (int, float)) else None
            wins.append({'label': 'included', 'percent': total, 'resetsAt': end, **({'text': text} if text else {})})
        if auto is not None:
            wins.append({'label': 'Auto', 'percent': auto, 'resetsAt': end})
        if api is not None:
            wins.append({'label': 'API', 'percent': api, 'resetsAt': end})
    if isinstance(od.get('limit'), (int, float)) and od['limit'] > 0:
        u = od.get('used') or 0
        wins.append({'label': 'on-demand', 'percent': _pct(100.0 * u / od['limit']), 'resetsAt': end, 'text': '%s of %s' % (_usd(u), _usd(od['limit']))})
    detail = {'source': 'cursor.com/api/usage-summary' + (' and /api/usage' if g4 else ''), 'membershipType': kind or None,
              'billingCycleEnd': end, 'onDemandEnabled': od.get('enabled'), 'onDemandUsedCents': od.get('used')}
    note = None
    if not wins:
        note = "Cursor didn't report usage for this plan"
        if od.get('enabled') is False and kind.lower() == 'free':
            note = 'Cursor reports no included use for the Free plan and on-demand use is off'
    return reading(note, name, wins, detail)


def cursor_plan(provider=None, fetch=cursor_fetch, app_db=CURSOR_APP_DB, agent_auth=None, now=None):
    sess, why = cursor_session(app_db, agent_auth, now)
    if not sess:
        return reading(why)
    try:
        summary = fetch('/api/usage-summary', sess['cookie'])
    except CursorError as e:
        if e.status in (401, 403):
            return reading('not signed in: Cursor did not accept the saved sign-in (open Cursor or run `cursor-agent login`)')
        return reading("Cursor didn't report usage (%s)" % ('HTTP %s' % e.status if e.status else 'no connection'))
    requests = None
    if not str((summary or {}).get('membershipType') or '').lower().startswith('unified_'):
        try:
            requests = fetch('/api/usage?user=' + sess['user'], sess['cookie'])          # request plans only; a failure leaves the summary
        except CursorError:
            requests = None
    return cursor_reading(summary, requests, now)


# ---------- Antigravity ----------
AGY_MIN = (1, 1, 11)          # the first agy with `-p /usage` (Google's changelog, via CodexBar): earlier ones could send it as a prompt


def agy_version(text):
    m = re.search(r'(\d+)\.(\d+)\.(\d+)', str(text or ''))
    return tuple(int(x) for x in m.groups()) if m else None


def antigravity_reading(report, now=None):
    """agy's usage report ({status: SUCCESS, command: {name: usage, data: {groups: [{name, buckets: [{id, name, window,
    remaining_fraction, reset_time, disabled}]}]}}}) as windows: one per bucket, '<group> <window>', used = 1 - remaining."""
    r = report if isinstance(report, dict) else {}
    cmd = r.get('command') or {}
    if r.get('status') != 'SUCCESS' or cmd.get('name') != 'usage':
        return reading("Antigravity didn't report usage (its usage report ended %s)" % (r.get('status') or 'without a status'))
    wins = []
    for g in (cmd.get('data') or {}).get('groups') or []:
        gname = re.sub(r'(?i)\s+models?$', '', str(g.get('display_name') or g.get('displayName') or g.get('name') or 'Quota').strip())
        for b in g.get('buckets') or []:
            if b.get('disabled'):
                continue
            rem = b.get('remaining_fraction', b.get('remainingFraction'))
            if rem is None and isinstance(b.get('remaining'), dict):
                rem = b['remaining'].get('remaining_fraction', b['remaining'].get('remainingFraction'))
            w = str(b.get('window') or '').lower()
            wl = '5-hour' if w in ('5h', 'five_hour', 'fivehour', '5-hour') else 'weekly' if w in ('weekly', 'week', '7d') else (w or str(b.get('display_name') or b.get('name') or b.get('id') or ''))
            wins.append({'label': '%s %s' % (gname, wl), 'percent': _pct(100.0 * (1 - float(rem))) if isinstance(rem, (int, float)) else None,
                         'resetsAt': iso_epoch(b.get('reset_time') or b.get('resetTime'))})
    order = {'5-hour': 0, 'weekly': 1}
    wins.sort(key=lambda x: order.get(x['label'].rsplit(' ', 1)[-1], 2))          # stable: groups keep their order within a window
    return reading(None if wins else "Antigravity's usage report had no quota in it", None, wins,
                   {'source': 'agy -p /usage --output-format json', 'description': (cmd.get('data') or {}).get('description')})


def agy_usage(binary, timeout=100.0):
    """Run agy's own usage report in a private folder where it cannot open a browser (open/xdg-open answer failure, BROWSER too)
    and does not update itself; stdin is empty. Returns its parsed JSON, or raises ValueError with a plain reason."""
    d = tempfile.mkdtemp(prefix='agent-ide-agy-usage-')
    try:
        for name in ('open', 'xdg-open'):
            p = os.path.join(d, name)
            with open(p, 'w') as f:
                f.write('#!/bin/sh\nexit 1\n')
            os.chmod(p, 0o700)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'), BROWSER=os.path.join(d, 'open'),
                   NO_OPEN_BROWSER='1', AGY_CLI_DISABLE_AUTO_UPDATE='true')
        proc = subprocess.Popen([binary, '-p', '/usage', '--output-format', 'json', '--print-timeout', '90s'], cwd=d, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
            proc.communicate()
            raise ValueError('its usage report did not finish within %ds' % timeout)
        text = out[:1 << 20].decode('utf-8', 'replace').strip()
        start = text.find('{')
        if start < 0:
            raise ValueError('its usage report printed no JSON (exit %s)' % proc.returncode)
        try:
            return json.loads(text[start:])
        except ValueError:
            raise ValueError('its usage report was not readable JSON')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def antigravity_plan(provider=None, run=agy_usage, binary=None, version=None, signed_in=None):
    if provider is not None:
        binary = binary or provider.flavor.find()
        version = version if version is not None else provider.version
        signed_in = signed_in if signed_in is not None else provider.signed_in
    if not binary:
        return reading('Antigravity is not installed')
    if signed_in is False:
        return reading('not signed in (run `agy acp --login`, or Sign in in Providers)')
    v = agy_version(version)
    if v is None:
        if version is None:
            return dict(reading('checking which Antigravity this is…'), retry=True)
        return reading("Antigravity didn't say its version, so its usage report was not run")
    if v < AGY_MIN:
        return reading('this Antigravity (%s) has no usage report; it needs agy 1.1.11 or later' % '.'.join(map(str, v)))
    try:
        rep = run(binary)
    except ValueError as e:
        return reading("Antigravity didn't report usage (%s)" % e)
    return antigravity_reading(rep)


# ---------- OpenCode ----------
OPENCODE_NOTE = 'OpenCode uses your own API keys and sign-ins: no plan of its own (its spend is below)'
