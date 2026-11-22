"""Which providers (Codex, Claude Code) are here, signed in and ready to drive, and the one step that fixes each (2026-11-21). Stdlib only, like server.py.

Every fact comes from the tools' own public commands, run in background threads and cached for a short while, so a page load
never waits on them:
- installed: the binary server.py finds (binary(), claude_binary()) and `<tool> --version`;
- signed in: Codex's running app-server `account/read` (signed out when account is null and requiresOpenaiAuth is true), else
  `codex login status` ("Logged in using ChatGPT" / "Not logged in"); `claude auth status --json` (loggedIn, authMethod, email).
  Only the method, the account's email and plan are kept; whatever else the commands print (Codex prints a masked API key after
  " - ") is dropped at once. No file under ~/.codex or ~/.claude is read here.
- ready to drive: Codex's app-server bridge is running; Claude Code's bridge venv is set up (claude_bridge.ClaudeBridge.available()).

The fixes (nothing here sees or handles a credential):
- Codex sign-in: the app-server's `account/login/start` {type: chatgpt} gives the sign-in page, which the page shows as a link for
  the person to open; its `account/login/completed` notification ends it (`account/login/cancel` on Cancel or after
  SIGNIN_TIMEOUT). Without a running app-server, `codex login`, which opens the system browser itself.
- Claude Code sign-in (2026-11-21: may be started from the app): `claude auth login`, which opens the system browser. It has
  no documented mode for running without a terminal; if it ends without signing in, the row offers the command to copy instead.
  CLAUDE_SIGNIN switches to that command alone.
- A process sign-in is never started under AGENT_IDE_NO_WINDOWS or a mock (it opens a browser).
- Set up runs claude_bridge_setup.sh found beside this file, from its own folder.
- A missing tool gets its official install page.

More providers (port 2026-11-21 from branch ide-providers-2026-11-09): Antigravity, Cursor Agent (over ACP) and OpenCode,
each a row from its own provider object (providers/acp.py, providers/opencode.py), whose available() never runs a command on the
caller's thread (version and Cursor's `cursor-agent status` are looked at in the background, at most once a minute):
- installed: the binary the flavor finds on PATH or in its usual folders (~/.local/bin, /opt/homebrew/bin, /usr/local/bin;
  ~/.opencode/bin for OpenCode);
- signed in: Cursor from `cursor-agent status` ("Logged in as ..."; its help: "View authentication status"); Antigravity has no
  status command, so unknown until a start here is accepted (signed in) or refused for sign-in (signed out);
  OpenCode's providers sign in inside OpenCode (`opencode auth login`), and some models need none: not shown;
- sign in: the agent's own login (`agy acp --login`, `cursor-agent login`) run by the provider (acp.AcpProvider.sign_in) with its
  browser launch caught where the agent allows it; the link it prints is shown; Antigravity's asks for the code Google shows to
  be pasted back (the panel's code box writes it to the sign-in's stdin only). OpenCode: the command to copy.
- An agent whose service refused this account (a start here said "no longer supported ... migrate") says so plainly.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parent
SETUP_SCRIPT = ROOT / 'claude_bridge_setup.sh'
INSTALL = {          # the vendors' own install pages (proposed by the builder, 2026-11-21)
    'codex': 'https://developers.openai.com/codex/cli',
    'claude': 'https://docs.anthropic.com/en/docs/claude-code/setup',
    'antigravity': 'https://antigravity.google',          # acp_flavors.py install hints (branch ide-providers-2026-11-09)
    'cursor': 'https://cursor.com/install',
    'opencode': 'https://opencode.ai/docs',
}
DRIVEN_COMMAND = {          # a sign-in the panel cannot start: the command to run in a terminal
    'opencode': ('opencode auth login', 'Run it in a terminal to add a model provider\'s key; OpenCode\'s free models need none.'),
}
LABEL = {'codex': 'Codex', 'claude': 'Claude Code'}
SIGNIN_HOSTS = ('auth.openai.com', 'chatgpt.com', 'claude.ai', 'claude.com', 'console.anthropic.com', 'platform.claude.com')
CLAUDE_SIGNIN = 'run'          # 'run': the page's button starts `claude auth login`; 'command': the row shows that command to copy instead (planner 11-21: one switch)
SIGNIN_COMMAND = {'codex': 'codex login', 'claude': 'claude auth login'}
TTL = 30          # seconds a check is trusted
RUN_TIMEOUT = 15          # seconds for one status command
SIGNIN_TIMEOUT = 600          # a sign-in left open this long is stopped
URL = re.compile(r'https://[^\s"\'<>]+')


def run(cmd, timeout=RUN_TIMEOUT, env=None):
    """(returncode, stdout, stderr); returncode None with a plain reason in stderr when it could not run or timed out."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return p.returncode, p.stdout or '', p.stderr or ''
    except subprocess.TimeoutExpired:
        return None, '', 'did not answer within ' + str(timeout) + ' seconds'
    except OSError as e:
        return None, '', 'could not be run (' + (e.strerror or type(e).__name__) + ')'


def first_line(text, n=160):
    for line in str(text or '').splitlines():
        if line.strip():
            return line.strip()[:n]
    return ''


def codex_version(out):
    """`codex --version` -> "0.169.0-alpha.11.1" (the line without its "codex-cli " name)."""
    line = first_line(out)
    return line.split(' ', 1)[1] if line.startswith('codex-cli ') else line or None


def claude_version(out):
    """`claude --version` -> "2.1.326" (the line without " (Claude Code)")."""
    line = first_line(out)
    return line.split(' (', 1)[0] or None


def codex_signin(rc, out, err):
    """(signed in True/False/None for unknown, how): from `codex login status`. Only the words before " - " are kept: Codex
    puts a masked API key after them."""
    for line in (str(out) + '\n' + str(err)).splitlines():
        t = line.strip()
        if t.lower().startswith('logged in using'):
            how = t[len('logged in using'):].split(' - ', 1)[0].strip()
            return True, ('an API key' if 'api key' in how.lower() else how) or None
        if t.lower().startswith('not logged in'):
            return False, None
    return (False, None) if rc not in (0, None) and 'not logged' in str(err).lower() else (None, None)


def claude_signin(rc, out, err):
    """(signed in True/False/None, how, who, plan): from `claude auth status --json`; every other field is dropped."""
    try:
        d = json.loads(out)
    except (TypeError, ValueError):
        return None, None, None, None
    if not isinstance(d, dict) or not isinstance(d.get('loggedIn'), bool):
        return None, None, None, None
    if not d['loggedIn']:
        return False, None, None, None
    s = lambda k: d.get(k) if isinstance(d.get(k), str) and d.get(k) else None
    return True, s('authMethod'), s('email') or s('orgName'), s('subscriptionType')


class ProviderSetup:
    """The providers' state for the page and the steps that fix it. `codex_path`/`claude_path`: callables returning the binary or
    None; `codex_bridge`: callable -> {present, ready, reason} (is this server's Codex service running); `claude_bridge`: callable ->
    ClaudeBridge.available() ({ready, reason}); `runner`, `spawn`, `no_windows`, `env`: replaced in tests."""

    def __init__(self, codex_path, claude_path, codex_bridge, claude_bridge, runner=None, spawn=None, no_windows=None,
                 claude_env=None, setup_script=SETUP_SCRIPT, ttl=TTL, driven=None):
        self.paths = {'codex': codex_path, 'claude': claude_path}
        self.driven = driven or (lambda: [])          # more providers: the driven provider objects (Workspace.driven_providers)
        self.codex_bridge, self.claude_bridge = codex_bridge, claude_bridge
        self.runner, self.spawn = runner or run, spawn or subprocess.Popen
        self.no_windows = no_windows or (lambda: bool(os.environ.get('AGENT_IDE_MOCK') or os.environ.get('AGENT_IDE_NO_WINDOWS')))
        self.claude_env = claude_env or (lambda: None)
        self.setup_script, self.ttl = Path(setup_script), ttl
        self.facts, self.busy, self.jobs = {}, set(), {}
        self.lock = threading.Lock()

    # ---------- the checks ----------
    def check(self, name):
        """One provider's facts, from its own commands (blocking; get() runs it in a thread)."""
        try:
            path = self.paths[name]()
        except Exception:
            path = None
        f = {'installed': bool(path), 'path': path, 'version': None, 'signedIn': None, 'how': None, 'who': None, 'plan': None, 'error': None}
        if path:
            env = self.claude_env() if name == 'claude' else None
            rc, out, err = self.runner([path, '--version'], env=env)
            f['version'] = (codex_version if name == 'codex' else claude_version)(out) if rc == 0 else None
            if name == 'codex' and self.codex_account(f):
                what = 'account/read'
            elif name == 'codex':
                rc, out, err = self.runner([path, 'login', 'status'])
                f['signedIn'], f['how'] = codex_signin(rc, out, err)
                what = 'codex login status'
            else:
                rc, out, err = self.runner([path, 'auth', 'status', '--json'], env=env)
                f['signedIn'], f['how'], f['who'], f['plan'] = claude_signin(rc, out, err)
                what = 'claude auth status'
            if f['signedIn'] is None:
                f['error'] = what + ': ' + (err.strip().splitlines()[-1][:160] if rc is None and err.strip() else 'gave an answer this page cannot read' if rc == 0 else 'failed' + (' (' + first_line(err) + ')' if first_line(err) else ''))
        with self.lock:
            self.facts[name] = (time.time(), f)
            self.busy.discard(name)
        return f

    def codex_account(self, f):
        """Signed in from the running app-server's own `account/read` (T3 Code's rule: signed out when account is null and
        requiresOpenaiAuth is true); False when the service is not up or did not answer (then `codex login status` is asked)."""
        b = self.codex_bridge()
        if not (b.get('present') and b.get('ready') and b.get('call')):
            return False
        try:
            r = b['call']('account/read', {}, timeout=8)
        except Exception:
            return False
        a = r.get('account') if isinstance(r, dict) else None
        if not isinstance(r, dict):
            return False
        if a is None:
            f['signedIn'] = not r.get('requiresOpenaiAuth')
            return True
        t = a.get('type') if isinstance(a, dict) else None
        f['signedIn'] = True
        f['how'] = {'chatgpt': 'ChatGPT', 'apiKey': 'an API key', 'amazonBedrock': 'Amazon Bedrock', 'gcp': 'Google Cloud'}.get(t, t)
        f['who'] = a.get('email') if isinstance(a.get('email'), str) else None
        f['plan'] = a.get('planType') if isinstance(a.get('planType'), str) else None
        return True

    def refresh(self, name, wait=False):
        with self.lock:
            if name in self.busy:
                return
            self.busy.add(name)
        if wait:
            self.check(name)
        else:
            threading.Thread(target=self.check, args=(name,), daemon=True).start()

    def forget(self, name=None):
        with self.lock:
            for n in ([name] if name else list(self.facts)):
                self.facts.pop(n, None)

    def get(self, wait=False):
        """{providers: [row, ...], anyReady, checking}: at once from what is known; a missing or stale check starts in the background
        (wait=True: runs it first, for tests and the first answer after an action)."""
        for name in self.paths:
            with self.lock:
                at = (self.facts.get(name) or (0, None))[0]
            if time.time() - at > self.ttl:
                self.refresh(name, wait=wait)
        rows = [self.row(n) for n in self.paths] + [self.driven_row(p) for p in self.driven()]
        return {'providers': rows, 'anyReady': any(r['ready'] for r in rows), 'checking': any(r['state'] == 'checking' for r in rows)}

    # ---------- more providers: a driven provider's row, from its own available() (never blocks) ----------
    def driven_of(self, name):
        return next((p for p in self.driven() if getattr(p, 'name', None) == name), None)

    def driven_row(self, p):
        name = p.name
        try:
            av = p.available() or {}
        except Exception as e:
            av = {'found': False, 'reason': str(e)[:160]}
        st = getattr(p, '_login', None)
        job = None
        if st and isinstance(st, dict) and st.get('proc') is not None:
            run_ = st['proc'].poll() is None
            ok = None if run_ else st['proc'].returncode == 0
            from providers.acp import login_said
            said = login_said(st.get('tail') or ())          # its last line, redacted: the reason a code was refused
            sent = bool(st.get('codeSent'))
            if not run_ and (st['proc'].returncode or 0) < 0:          # ended by Cancel (or the 10-minute bound), not by itself
                said = 'it was stopped' if not sent else said
            job = {'kind': 'signin', 'running': run_, 'url': st.get('url'), 'how': None if p.flavor.login_opens_browser else 'link',
                   'needsCode': bool(st.get('needsCode') and not sent and run_), 'codeSent': sent, 'ok': ok, 'said': said if sent and not (p.flavor.login_code_prompt and re.search(p.flavor.login_code_prompt, said, re.I)) else '',
                   'result': None if run_ else 'Signed in.' if ok else
                   ('That code was not accepted' if sent else 'Sign-in did not finish') + (': ' + said.rstrip('.') if said else ' (it ended with code %s)' % st['proc'].returncode) +
                   '. Press Sign in to try again' + (' with a new link and code.' if sent else '.')}
        r = {'name': name, 'label': p.label, 'installed': bool(av.get('found') or av.get('binary')), 'path': av.get('binary'), 'version': av.get('version'),
             'signedIn': av.get('signedIn'), 'account': None, 'ready': False, 'state': 'checking', 'say': 'Checking…', 'fix': None, 'error': None,
             'job': job, 'checkedAt': time.time(), 'driven': True}
        reason = str(av.get('reason') or '')
        if av.get('checking'):
            return r
        if not r['installed']:
            r.update(state='missing', say='Not installed', fix={'action': 'install', 'label': 'How to install', 'url': INSTALL.get(name, '')})
            return r
        if not av.get('found'):          # found but not usable (OpenCode 1.x, or its server stayed down)
            r.update(state='stopped', say='Installed, cannot be driven', error=reason[:300] or None)
            return r
        if reason.startswith('refused: '):
            r.update(state='refused', say='Its service refused this account', error=reason[len('refused: '):][:300],
                     fix={'action': 'command', 'label': 'Copy command', 'command': DRIVEN_COMMAND[name][0], 'note': DRIVEN_COMMAND[name][1]} if name in DRIVEN_COMMAND else None)
            return r
        if job and job['running']:
            r.update(state='signing-in', say='Signing in: paste the code Google shows below' if job['needsCode'] else
                     'Signing in: code sent, waiting for ' + p.label + ' to answer' if job['codeSent'] else
                     'Signing in: open the sign-in page below' if job['url'] and job['how'] == 'link' else 'Signing in: finish in the browser window it opened',
                     fix={'action': 'cancel', 'label': 'Cancel'})
            return r
        signin = ({'action': 'signin', 'label': 'Sign in'} if av.get('canSignIn') else
                  {'action': 'command', 'label': 'Copy command', 'command': DRIVEN_COMMAND[name][0], 'note': DRIVEN_COMMAND[name][1]} if name in DRIVEN_COMMAND else None)
        if r['signedIn'] is False:
            r.update(state='signed-out', say='Installed, not signed in', fix=signin)
        elif r['signedIn'] is None:
            r.update(ready=True, state='unknown', say='Installed; signed in? known after its first start here' if name != 'opencode' else 'Installed · its models sign in inside OpenCode',
                     fix=signin)
        else:
            r.update(ready=True, state='ready', say='Ready', fix={'action': 'recheck', 'label': 'Check again'})
        if job and job.get('ok') is False:
            r['error'] = job['result']
        return r

    def driven_act(self, p, action, code=None):
        if action == 'recheck':
            import providers.acp as acp_mod
            acp_mod.LOOKS.pop(p.name, None)          # looked at again on the next ask
            if hasattr(p, '_probe'):
                p._probe = None
        elif action == 'signin':
            if not callable(getattr(p, 'sign_in', None)):
                raise ValueError(p.label + ' signs in in a terminal: ' + DRIVEN_COMMAND.get(p.name, ('its own command',))[0] + '.')
            if self.no_windows():
                raise ValueError('Not started: sign-in opens a browser, and this server puts no windows on the screen (a mock or AGENT_IDE_NO_WINDOWS).')
            p.sign_in(wait=0)          # starts it; the panel asks again every 2 s for the link and the code box
        elif action == 'code':
            p.sign_in_code(code)
        elif action == 'cancel':
            st = getattr(p, '_login', None)
            if st and st['proc'].poll() is None:
                try:
                    os.killpg(os.getpgid(st['proc'].pid), 15)
                except OSError:
                    pass
        else:
            raise ValueError('Unknown provider action: ' + str(action) + '.')
        return self.get()

    # ---------- a row: its state in a few words and the one step that fixes it ----------
    def row(self, name):
        if name == 'codex':
            self.codex_login_poll()
        with self.lock:
            at, f = self.facts.get(name) or (None, None)
            job = dict(self.jobs[name]) if name in self.jobs else None
        if job:
            job.pop('proc', None)
        r = {'name': name, 'label': LABEL[name], 'installed': None, 'path': None, 'version': None, 'signedIn': None, 'account': None,
             'ready': False, 'state': 'checking', 'say': 'Checking…', 'fix': None, 'error': None, 'job': job, 'checkedAt': at}
        if f is None:
            return r
        acct = ' · '.join(x for x in (f['who'], f['how'] and 'with ' + f['how'], f['plan']) if x) or None
        r.update(installed=f['installed'], path=f['path'], version=f['version'], signedIn=f['signedIn'], account=acct, error=f['error'])
        running = job and job.get('running')
        if not f['installed']:
            r.update(state='missing', say='Not installed', fix={'action': 'install', 'label': 'How to install', 'url': INSTALL[name]})
            if name == 'codex' and self.codex_bridge().get('present'):
                r.update(say='Not found now (its service still runs)', fix=None)
            return r
        if running and job['kind'] == 'signin':
            r.update(state='signing-in', say='Signing in: open the sign-in page below' if job.get('how') == 'app-server' else 'Signing in: finish in the browser window it opened',
                     fix={'action': 'cancel', 'label': 'Cancel'})
            return r
        if f['signedIn'] is False:
            r.update(state='signed-out', say='Installed, not signed in', fix=self.signin_fix(name, job))
            if job and job.get('ok') is False:
                r['error'] = job.get('result')
            return r
        if name == 'codex':
            b = self.codex_bridge()
            if not b.get('present'):
                r.update(state='restart', say='Installed after the server started: restart the workspace server to use it',
                         error=r['error'] or b.get('reason'))
                return r
            if not b.get('ready'):
                r.update(state='stopped', say='Its service stopped: restart the workspace server', error=r['error'] or b.get('reason'))
                return r
        else:
            if running and job['kind'] == 'setup':
                r.update(state='setting-up', say='Setting up driving…')
                return r
            st = self.claude_bridge() or {}
            if not st.get('ready'):
                r.update(state='not-set-up', say='Signed in; driving is not set up (watch only)' if f['signedIn'] else 'Driving is not set up (watch only)',
                         fix={'action': 'setup', 'label': 'Set up driving'})
                if job and job.get('kind') == 'setup' and job.get('ok') is False:
                    r['error'] = job.get('result')
                return r
        if f['signedIn'] is None:
            r.update(state='unknown', say='Installed; sign-in state unknown', fix=self.signin_fix(name, job))
            return r
        r.update(ready=True, state='ready', say='Ready' + (' · ' + acct if acct else ''), fix={'action': 'recheck', 'label': 'Check again'})
        if job and job.get('ok') is False:
            r['error'] = job.get('result')
        return r

    def signin_fix(self, name, job=None):
        """The sign-in button; for Claude Code under CLAUDE_SIGNIN 'command', or after a started sign-in failed, the command to run
        in the person's own terminal (it may need one)."""
        if name == 'claude' and (CLAUDE_SIGNIN == 'command' or (job and job.get('kind') == 'signin' and job.get('ok') is False)):
            return {'action': 'command', 'label': 'Copy command', 'command': SIGNIN_COMMAND[name],
                    'note': 'Run it in a terminal; it opens a browser to sign in. An Anthropic API key also works: set ANTHROPIC_API_KEY and AGENT_IDE_CLAUDE_API_KEY=pass before starting the server.'}
        return {'action': 'signin', 'label': 'Sign in'}

    # ---------- the steps ----------
    def act(self, name, action, code=None):
        if name not in self.paths and self.driven_of(name) is not None:          # more providers
            return self.driven_act(self.driven_of(name), action, code)
        if name not in self.paths:
            raise ValueError('There is no provider called ' + str(name) + '.')
        if action == 'recheck':
            self.forget(name)
            self.refresh(name, wait=True)
        elif action == 'signin':
            self.sign_in(name)
        elif action == 'cancel':
            self.cancel(name)
        elif action == 'setup':
            if name != 'claude':
                raise ValueError('Only Claude Code has a driving set-up.')
            self.set_up()
        else:
            raise ValueError('Unknown provider action: ' + str(action) + '.')
        return self.get()

    def busy_job(self, name):
        with self.lock:
            j = self.jobs.get(name)
            return bool(j and j.get('running'))

    def sign_in(self, name):
        """Starts the tool's own sign-in, which opens the system browser. Only a sign-in link on the vendor's own site is kept from
        what it prints (for a browser that did not open); nothing it prints is written anywhere."""
        if self.busy_job(name):
            return
        path = self.paths[name]()
        if not path:
            raise ValueError(LABEL[name] + ' is not installed here: install it first (How to install).')
        if name == 'claude' and CLAUDE_SIGNIN == 'command':
            raise ValueError('Run ' + SIGNIN_COMMAND[name] + ' in a terminal to sign in to Claude Code.')
        b = self.codex_bridge() if name == 'codex' else {}
        if b.get('present') and b.get('ready') and b.get('call'):
            # Codex's own app-server: it returns the sign-in page and finishes on its own; the page shows the link for the
            # person to open (nothing on the screen is opened from here)
            seq = b.get('seq', lambda: 0)()
            r = b['call']('account/login/start', {'type': 'chatgpt'}, timeout=20)
            if not isinstance(r, dict) or not r.get('authUrl'):
                raise ValueError('Codex did not give a sign-in page.')
            with self.lock:
                self.jobs[name] = {'kind': 'signin', 'how': 'app-server', 'running': True, 'started': time.time(), 'url': r['authUrl'],
                                   'loginId': r.get('loginId'), 'seq': seq, 'result': None, 'ok': None}
            return
        if self.no_windows():
            raise ValueError('Not started: sign-in opens a browser, and this server puts no windows on the screen (a mock or AGENT_IDE_NO_WINDOWS).')
        cmd = [path, 'login'] if name == 'codex' else [path, 'auth', 'login']
        proc = self.spawn(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                          env=self.claude_env() if name == 'claude' else None)
        job = {'kind': 'signin', 'running': True, 'started': time.time(), 'url': None, 'result': None, 'ok': None, 'proc': proc}
        with self.lock:
            self.jobs[name] = job

        def read():
            for line in proc.stdout:
                for u in URL.findall(line):
                    host = re.sub(r'^https://([^/:?#]+).*$', r'\1', u).lower()
                    if not job['url'] and any(host == h or host.endswith('.' + h) for h in SIGNIN_HOSTS):
                        job['url'] = u.rstrip('.,)')
            rc = proc.wait()
            self.finish(name, job, rc == 0, 'Signed in.' if rc == 0 else 'Sign-in did not finish (the sign-in command ended with code ' + str(rc) + ').')
        threading.Thread(target=read, daemon=True).start()
        t = threading.Timer(SIGNIN_TIMEOUT, lambda: proc.poll() is None and proc.terminate())
        t.daemon = True          # never holds the server open at exit
        t.start()

    def codex_login_poll(self):
        """An app-server sign-in ends with its account/login/completed notification (on the bridge's event list), or after
        SIGNIN_TIMEOUT, when it is cancelled."""
        with self.lock:
            job = self.jobs.get('codex')
        if not (job and job.get('running') and job.get('how') == 'app-server'):
            return
        b = self.codex_bridge()
        for i, m in list(b.get('events', lambda: [])()):
            if i <= job['seq'] or m.get('method') != 'account/login/completed':
                continue
            p = m.get('params') or {}
            if p.get('loginId') not in (None, job['loginId']):
                continue
            ok = p.get('success') is True
            self.finish('codex', job, ok, 'Signed in.' if ok else 'Sign-in did not finish' + (': ' + str(p['error'])[:160] if p.get('error') else '') + '.')
            return
        if time.time() - job['started'] > SIGNIN_TIMEOUT:
            self.cancel('codex')

    def cancel(self, name):
        with self.lock:
            job = self.jobs.get(name)
        if not (job and job.get('running')):
            return
        if job.get('how') == 'app-server':
            try:
                self.codex_bridge()['call']('account/login/cancel', {'loginId': job['loginId']}, timeout=8)
            except Exception:
                pass
        elif job.get('proc') is not None:
            try:
                job['proc'].terminate()
            except OSError:
                pass
        self.finish(name, job, False, 'Sign-in cancelled.')

    def set_up(self):
        """Runs claude_bridge_setup.sh (beside this file; it moves to its own folder itself), keeping its last lines as progress."""
        if self.busy_job('claude'):
            return
        if not self.setup_script.is_file():
            raise ValueError('The set-up script is missing: ' + self.setup_script.name + ' should be beside server.py.')
        zsh = shutil.which('zsh')
        if not zsh:
            raise ValueError('Setting up Claude Code driving needs zsh, which this computer does not have.')
        proc = self.spawn([zsh, str(self.setup_script)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                          cwd=str(self.setup_script.parent))
        job = {'kind': 'setup', 'running': True, 'started': time.time(), 'lines': [], 'result': None, 'ok': None, 'proc': proc}
        with self.lock:
            self.jobs['claude'] = job

        def read():
            for line in proc.stdout:
                if line.strip():
                    job['lines'] = (job['lines'] + [line.strip()[:200]])[-4:]
            rc = proc.wait()
            last = job['lines'][-1] if job['lines'] else ''
            self.finish('claude', job, rc == 0, 'Driving is set up.' if rc == 0 else 'Set-up failed' + (': ' + last.rstrip('.') if last else ' (code ' + str(rc) + ')') + '.')
        threading.Thread(target=read, daemon=True).start()

    def finish(self, name, job, ok, result):
        with self.lock:
            if not job.get('running'):
                return          # already ended (a cancel, then the process's own end)
            job.update(running=False, ok=ok, result=result, ended=time.time())
            job.pop('proc', None)
            if name in self.facts:          # what it fixed is checked again on the next ask; the row keeps showing the last check till then
                self.facts[name] = (0, self.facts[name][1])
