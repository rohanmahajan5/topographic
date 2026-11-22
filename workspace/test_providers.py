"""providers panel (2026-11-21): the server with no Codex, the /api/providers shapes, and the sign-in and
set-up steps, all with fake commands. No real sign-in, set-up script, agent or browser is ever started here.
Run: python3 -m unittest test_providers"""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import server
import claude_bridge
import provider_setup

server.claude_bridge.VENV = Path('/nonexistent/claude-bridge-venv')      # tests never use a real venv


def runner_for(answers):
    """A fake run(): answers keyed by the command's words after the binary; records what was asked."""
    asked = []

    def run(cmd, timeout=None, env=None):
        asked.append(cmd)
        return answers.get(' '.join(cmd[1:]), (None, '', 'could not be run (No such file or directory)'))
    run.asked = asked
    return run


CODEX_OK = {'--version': (0, 'codex-cli 0.169.0\n', ''), 'login status': (0, 'Logged in using ChatGPT\n', '')}
CLAUDE_OK = {'--version': (0, '2.1.326 (Claude Code)\n', ''),
             'auth status --json': (0, json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'email': 'person@example.com', 'orgId': 'org-secret-id',
                                                   'orgName': 'Example Org', 'subscriptionType': 'max', 'configDirectory': '/x'}), '')}


def setup_with(codex=None, claude=None, codex_bridge=None, claude_ready=False, **kw):
    """A ProviderSetup whose tools answer from `codex` / `claude` (None: not installed)."""
    answers = {}
    paths = {}
    for name, a in (('codex', codex), ('claude', claude)):
        if a is not None:
            paths[name] = '/fake/bin/' + name
            answers.update({k: v for k, v in a.items()})
    # one runner serving both binaries: key by binary too
    asked = []

    def run(cmd, timeout=None, env=None):
        asked.append(cmd)
        a = codex if cmd[0].endswith('codex') else claude
        return (a or {}).get(' '.join(cmd[1:]), (None, '', 'could not be run'))
    run.asked = asked
    s = provider_setup.ProviderSetup(lambda: paths.get('codex'), lambda: paths.get('claude'),
                                     lambda: codex_bridge or {'present': True, 'ready': True, 'reason': None},
                                     lambda: {'ready': claude_ready, 'reason': 'not set up'}, runner=run, **kw)
    s.run = run
    return s


def rows(s):
    return {r['name']: r for r in s.get(wait=True)['providers']}


class NoCodexStartTests(unittest.TestCase):
    """The server starts with no Codex CLI at all (AGENT_IDE_CODEX pointing nowhere, an empty PATH, no Codex.app)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        is_file = Path.is_file
        for p in (patch.dict(os.environ, {'AGENT_IDE_CODEX': str(Path(self.tmp.name) / 'no-codex'), 'PATH': self.tmp.name,
                                          'AGENT_IDE_TEST_PROVIDERS': 'codex'}),          # the person's Claude Code store is never read here
                  patch.object(Path, 'is_file', lambda self: False if str(self).startswith('/Applications/Codex.app') else is_file(self)),
                  patch.object(server, 'CODEX_HOME', Path(self.tmp.name) / 'codex-home')):
            p.start()
            self.addCleanup(p.stop)

    def test_start_codex_without_a_binary_gives_a_stand_in(self):
        b = server.start_codex()
        self.assertIsInstance(b, server.CodexAbsent)
        self.assertFalse(b.ready)
        self.assertEqual(b.reason, server.CODEX_MISSING)
        self.assertIsNone(server.codex_path())
        b.close()          # nothing to stop

    def test_shared_mode_without_a_binary_also_starts(self):
        self.assertIsInstance(server.start_codex(shared=True), server.CodexAbsent)

    def test_a_codex_that_will_not_start_says_so(self):
        fake = Path(self.tmp.name) / 'codex'
        fake.write_text('')
        with patch.dict(os.environ, {'AGENT_IDE_CODEX': str(fake)}), patch.object(server, 'Bridge', side_effect=OSError(13, 'Permission denied')):
            b = server.start_codex()
        self.assertIsInstance(b, server.CodexAbsent)
        self.assertIn('Codex did not start', b.reason)

    def test_snapshot_is_not_disconnected_and_names_why(self):
        w = server.Workspace(server.CodexAbsent())
        snap = w.snapshot()
        self.assertTrue(snap['connected'])
        self.assertEqual(snap['codexAbsent'], server.CODEX_MISSING)
        self.assertEqual(snap['requests'], [])

    def test_a_codex_action_returns_one_plain_sentence(self):
        w = server.Workspace(server.CodexAbsent())
        for op, p in (('start', {'cwd': self.tmp.name}), ('start', {'cwd': self.tmp.name, 'harness': 'codex'})):
            with self.assertRaises(Exception) as e:
                w.action(op, p)
            self.assertEqual(str(e.exception), server.CODEX_MISSING, op)

    def test_routes_over_http_with_no_codex(self):
        w = server.Workspace(server.CodexAbsent())
        w._provider_setup = setup_with(codex=None, claude=CLAUDE_OK, codex_bridge={'present': False, 'ready': False, 'reason': server.CODEX_MISSING})
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads, http.workspace = True, w
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        base = 'http://127.0.0.1:%d/api/' % http.server_port
        boot = json.load(urllib.request.urlopen(base + 'bootstrap', timeout=5))
        self.assertTrue(boot['connected'])
        w._provider_setup.get(wait=True)
        got = json.load(urllib.request.urlopen(base + 'providers', timeout=5))
        self.assertEqual([r['name'] for r in got['providers']], ['codex', 'claude'])
        self.assertEqual(got['providers'][0]['state'], 'missing')
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + 'models', timeout=5)
        self.assertEqual(json.load(e.exception)['error'], server.CODEX_MISSING)
        req = urllib.request.Request(base + 'start', data=json.dumps({'cwd': self.tmp.name}).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Workspace-Token': boot['token']})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(json.load(e.exception), {'error': server.CODEX_MISSING})

    def test_mock_requests_still_answer_on_the_stand_in(self):
        b = server.CodexAbsent()
        b.mock_live({'active': {'t1': 'turn1'}, 'requests': [{'id': 'm1', 'method': 'item/tool/requestUserInput', 'params': {'threadId': 't1'}}]})
        b.reply('m1', {'answers': {}})
        self.assertEqual(b.requests, {})
        self.assertEqual(b.events[-1][1]['method'], 'serverRequest/resolved')


class ProviderRouteShapeTests(unittest.TestCase):
    def test_both_ready(self):
        r = rows(setup_with(codex=CODEX_OK, claude=CLAUDE_OK, claude_ready=True))
        self.assertEqual(r['codex']['state'], 'ready')
        self.assertTrue(r['codex']['ready'])
        self.assertEqual(r['codex']['version'], '0.169.0')
        self.assertEqual(r['codex']['account'], 'with ChatGPT')
        self.assertEqual(r['claude']['version'], '2.1.326')
        self.assertEqual(r['claude']['account'], 'person@example.com · with claude.ai · max')
        self.assertEqual(r['claude']['fix']['action'], 'recheck')
        for k in ('name', 'label', 'installed', 'path', 'version', 'signedIn', 'account', 'ready', 'state', 'say', 'fix', 'error', 'job', 'checkedAt'):
            self.assertIn(k, r['codex'])

    def test_only_what_is_needed_is_kept(self):
        s = setup_with(codex={**CODEX_OK, 'login status': (0, 'Logged in using an API key - sk-proj-***abcd\n', '')}, claude=CLAUDE_OK)
        out = json.dumps(s.get(wait=True))
        self.assertNotIn('sk-', out)
        self.assertNotIn('org-secret-id', out)
        self.assertNotIn('configDirectory', out)
        self.assertEqual(rows(s)['codex']['account'], 'with an API key')

    def test_missing(self):
        s = setup_with(codex=None, claude=None, codex_bridge={'present': False, 'ready': False, 'reason': 'no codex'})
        r = rows(s)
        for n in ('codex', 'claude'):
            self.assertEqual(r[n]['state'], 'missing')
            self.assertFalse(r[n]['installed'])
            self.assertEqual(r[n]['fix']['action'], 'install')
            self.assertTrue(r[n]['fix']['url'].startswith('https://'))
        self.assertFalse(s.get()['anyReady'])
        self.assertEqual(s.run.asked, [])          # nothing run for a tool that is not there

    def test_signed_out(self):
        r = rows(setup_with(codex={**CODEX_OK, 'login status': (1, 'Not logged in\n', '')},
                            claude={**CLAUDE_OK, 'auth status --json': (1, '{"loggedIn": false, "authMethod": "none"}', '')}))
        for n in ('codex', 'claude'):
            self.assertEqual(r[n]['state'], 'signed-out')
            self.assertIs(r[n]['signedIn'], False)
            self.assertEqual(r[n]['fix'], {'action': 'signin', 'label': 'Sign in'})

    def test_claude_signed_in_but_not_set_up(self):
        r = rows(setup_with(codex=CODEX_OK, claude=CLAUDE_OK, claude_ready=False))
        self.assertEqual(r['claude']['state'], 'not-set-up')
        self.assertEqual(r['claude']['fix']['action'], 'setup')
        self.assertFalse(r['claude']['ready'])

    def test_codex_installed_after_start_and_stopped_service(self):
        r = rows(setup_with(codex=CODEX_OK, codex_bridge={'present': False, 'ready': False, 'reason': 'x'}))
        self.assertEqual(r['codex']['state'], 'restart')
        r = rows(setup_with(codex=CODEX_OK, codex_bridge={'present': True, 'ready': False, 'reason': None}))
        self.assertEqual(r['codex']['state'], 'stopped')

    def test_unreadable_status_and_timeouts_are_plain_errors(self):
        r = rows(setup_with(codex={'--version': (0, 'codex-cli 1\n', ''), 'login status': (0, 'something new\n', '')},
                            claude={'--version': (None, '', 'did not answer within 15 seconds'), 'auth status --json': (None, '', 'did not answer within 15 seconds')}))
        self.assertEqual(r['codex']['state'], 'unknown')
        self.assertEqual(r['codex']['error'], 'codex login status: gave an answer this page cannot read')
        self.assertEqual(r['claude']['error'], 'claude auth status: did not answer within 15 seconds')

    def test_get_never_waits(self):
        gate = threading.Event()
        s = setup_with(codex=CODEX_OK, claude=CLAUDE_OK)
        slow = s.runner
        s.runner = lambda *a, **k: (gate.wait(5), slow(*a, **k))[1]
        t = time.time()
        out = s.get()
        self.assertLess(time.time() - t, 0.5)
        self.assertTrue(out['checking'])
        self.assertEqual({r['state'] for r in out['providers']}, {'checking'})
        gate.set()

    def test_cached_for_the_ttl(self):
        s = setup_with(codex=CODEX_OK, claude=CLAUDE_OK)
        s.get(wait=True)
        n = len(s.run.asked)
        s.get(wait=True)
        self.assertEqual(len(s.run.asked), n)
        s.act('codex', 'recheck')
        self.assertGreater(len(s.run.asked), n)


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout, self.rc, self.done = io.StringIO(''.join(lines)), rc, False

    def wait(self):
        self.done = True
        return self.rc

    def poll(self):
        return self.rc if self.done else None

    def terminate(self):
        self.done = True


class ProviderActionTests(unittest.TestCase):
    def spawned(self, lines, rc=0):
        calls = []

        def spawn(cmd, **kw):
            calls.append((cmd, kw))
            return FakeProc(lines, rc)
        return spawn, calls

    def wait_job(self, s, name):
        for _ in range(100):
            j = s.jobs.get(name)
            if j and not j['running']:
                return j
            time.sleep(0.02)
        self.fail('job did not end')

    def test_sign_in_runs_the_tools_own_command_and_keeps_only_its_link(self):
        spawn, calls = self.spawned(['Starting local login server\n', 'see https://example.com/x and\n',
                                     'If your browser did not open, navigate to https://auth.openai.com/oauth/authorize?client_id=a&state=b\n'])
        s = setup_with(codex={**CODEX_OK, 'login status': (1, 'Not logged in\n', '')}, claude=CLAUDE_OK, spawn=spawn, no_windows=lambda: False)
        s.act('codex', 'signin')
        self.assertEqual(calls[0][0], ['/fake/bin/codex', 'login'])
        j = self.wait_job(s, 'codex')
        self.assertTrue(j['ok'])
        self.assertEqual(j['result'], 'Signed in.')
        self.assertTrue(j['url'].startswith('https://auth.openai.com/'))
        self.assertNotIn('proc', rows(s)['codex']['job'])
        s.act('claude', 'signin')
        self.assertEqual(calls[1][0], ['/fake/bin/claude', 'auth', 'login'])

    def test_sign_in_that_fails(self):
        spawn, _ = self.spawned(['error\n'], rc=1)
        s = setup_with(codex=CODEX_OK, spawn=spawn, no_windows=lambda: False)
        s.act('codex', 'signin')
        j = self.wait_job(s, 'codex')
        self.assertFalse(j['ok'])
        self.assertIn('code 1', j['result'])

    def test_sign_in_refused_without_windows_or_tool(self):
        spawn, calls = self.spawned([])
        s = setup_with(codex=CODEX_OK, spawn=spawn, no_windows=lambda: True)
        with self.assertRaises(ValueError) as e:
            s.act('codex', 'signin')
        self.assertIn('no windows', str(e.exception))
        s = setup_with(codex=None, spawn=spawn, no_windows=lambda: False)
        with self.assertRaises(ValueError) as e:
            s.act('codex', 'signin')
        self.assertEqual(str(e.exception), 'Codex is not installed here: install it first (How to install).')
        self.assertEqual(calls, [])

    def test_set_up_runs_the_script_beside_it_from_its_own_folder(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / 'claude_bridge_setup.sh'
            script.write_text('# fake\n')
            spawn, calls = self.spawned(['Collecting claude-agent-sdk\n', 'claude-agent-sdk 0.2.178 ready in .claude-bridge/venv\n'])
            s = setup_with(claude=CLAUDE_OK, spawn=spawn, setup_script=script)
            s.act('claude', 'setup')
            cmd, kw = calls[0]
            self.assertTrue(cmd[0].endswith('zsh'))
            self.assertEqual(cmd[1], str(script))
            self.assertEqual(kw['cwd'], d)
            j = self.wait_job(s, 'claude')
            self.assertEqual(j['result'], 'Driving is set up.')
            self.assertEqual(j['lines'][-1], 'claude-agent-sdk 0.2.178 ready in .claude-bridge/venv')
            spawn, _ = self.spawned(['Needs Python 3.10 or newer (pass its path as the first argument).\n'], rc=1)
            s = setup_with(claude=CLAUDE_OK, spawn=spawn, setup_script=script)
            s.act('claude', 'setup')
            j = self.wait_job(s, 'claude')
            self.assertEqual(j['result'], 'Set-up failed: Needs Python 3.10 or newer (pass its path as the first argument).')
            self.assertEqual(rows(s)['claude']['error'], j['result'])

    def app_server(self, account=None, requires=True, login=None):
        """A fake running Codex service: answers account/read and account/login/start, records the calls, holds events."""
        calls, events = [], []

        def call(method, params=None, timeout=45):
            calls.append((method, params))
            if method == 'account/read':
                return {'account': account, 'requiresOpenaiAuth': requires}
            if method == 'account/login/start':
                return login or {'type': 'chatgpt', 'loginId': 'L1', 'authUrl': 'https://auth.openai.com/oauth/authorize?x=1'}
            return {}
        return {'present': True, 'ready': True, 'reason': None, 'call': call, 'seq': lambda: max([i for i, _ in events] or [0]), 'events': lambda: list(events)}, calls, events

    def test_codex_signed_in_from_the_app_server(self):
        b, calls, _ = self.app_server(account={'type': 'chatgpt', 'email': 'person@example.com', 'planType': 'pro'})
        s = setup_with(codex=CODEX_OK, codex_bridge=b)
        r = rows(s)['codex']
        self.assertEqual((r['state'], r['account']), ('ready', 'person@example.com · with ChatGPT · pro'))
        self.assertIn(('account/read', {}), calls)
        self.assertNotIn(['/fake/bin/codex', 'login', 'status'], s.run.asked)          # the service answered: no shelling out
        b, _, _ = self.app_server(account=None, requires=True)
        self.assertEqual(rows(setup_with(codex=CODEX_OK, codex_bridge=b))['codex']['state'], 'signed-out')
        b, _, _ = self.app_server(account=None, requires=False)          # a provider that needs no OpenAI sign-in
        self.assertEqual(rows(setup_with(codex=CODEX_OK, codex_bridge=b))['codex']['state'], 'ready')

    def test_codex_sign_in_through_the_app_server(self):
        b, calls, events = self.app_server(account=None)
        s = setup_with(codex=CODEX_OK, codex_bridge=b, no_windows=lambda: True,          # nothing is opened from here, so allowed
                       spawn=lambda *a, **k: self.fail('no process for an app-server sign-in'))
        s.act('codex', 'signin')
        self.assertIn(('account/login/start', {'type': 'chatgpt'}), calls)
        r = rows(s)['codex']
        self.assertEqual(r['state'], 'signing-in')
        self.assertEqual(r['job']['url'], 'https://auth.openai.com/oauth/authorize?x=1')
        self.assertEqual(r['fix']['action'], 'cancel')
        events.append((5, {'method': 'account/login/completed', 'params': {'loginId': 'other', 'success': True}}))
        self.assertTrue(rows(s)['codex']['job']['running'])          # someone else's sign-in
        events.append((6, {'method': 'account/login/completed', 'params': {'loginId': 'L1', 'success': False, 'error': 'denied'}}))
        r = rows(s)['codex']
        self.assertEqual((r['job']['ok'], r['job']['result']), (False, 'Sign-in did not finish: denied.'))
        self.assertEqual(r['error'], 'Sign-in did not finish: denied.')
        s.act('codex', 'signin')
        s.act('codex', 'cancel')
        self.assertIn(('account/login/cancel', {'loginId': 'L1'}), calls)
        self.assertEqual(s.jobs['codex']['result'], 'Sign-in cancelled.')

    def test_claude_sign_in_that_fails_offers_the_command(self):
        spawn, _ = self.spawned(['needs a terminal\n'], rc=1)
        s = setup_with(claude={**CLAUDE_OK, 'auth status --json': (1, '{"loggedIn": false}', '')}, spawn=spawn, no_windows=lambda: False)
        self.assertEqual(rows(s)['claude']['fix'], {'action': 'signin', 'label': 'Sign in'})
        s.act('claude', 'signin')
        self.wait_job(s, 'claude')
        fix = rows(s)['claude']['fix']
        self.assertEqual((fix['action'], fix['command']), ('command', 'claude auth login'))
        with patch.object(provider_setup, 'CLAUDE_SIGNIN', 'command'):
            s2 = setup_with(claude={**CLAUDE_OK, 'auth status --json': (1, '{"loggedIn": false}', '')}, spawn=spawn, no_windows=lambda: False)
            self.assertEqual(rows(s2)['claude']['fix']['action'], 'command')
            with self.assertRaises(ValueError):
                s2.act('claude', 'signin')

    def test_unknown_names_and_actions(self):
        s = setup_with(codex=CODEX_OK)
        for n, a in (('gemini', 'signin'), ('codex', 'setup'), ('codex', 'delete')):
            with self.assertRaises(ValueError):
                s.act(n, a)

    def test_setup_hint_works_from_any_folder(self):
        self.assertIn(str(claude_bridge.ROOT / 'claude_bridge_setup.sh'), claude_bridge.SETUP)
        self.assertTrue(Path(claude_bridge.SETUP.split(' ', 1)[1].strip("'")).is_absolute())


if __name__ == '__main__':
    unittest.main()
