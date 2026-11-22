"""robust (2026-11-21): the agent connections survive failures, and no agent request is refused without the person seeing it.

Fake app-servers and fake workers only (no Codex, no Claude Code, no browser). Run one class at a time, e.g.
    python3 -m unittest test_robust.AppServerRestartTests
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

import server
import claude_bridge

server.claude_bridge.VENV = Path('/nonexistent/claude-bridge-venv')      # never a real venv

# A fake `codex app-server --stdio`: answers initialize and a few test methods; counts its starts in STARTS (argv[1]).
FAKE = textwrap.dedent('''
    import json, sys
    starts = sys.argv[1]
    with open(starts, 'a') as f:
        f.write('x')
    got = {}
    def out(m):
        print(json.dumps(m), flush=True)
    for line in sys.stdin:
        m = json.loads(line)
        meth = m.get('method')
        if meth == 'initialize':
            out({'id': m['id'], 'result': {}})
        elif meth == 'ping':
            out({'id': m['id'], 'result': {'pong': True}})
        elif meth == 'die':
            sys.stderr.write('fake app-server: told to die\\n'); sys.stderr.flush()
            sys.exit(3)
        elif meth == 'garbage':
            print('this is not json', flush=True)
            print('[1, 2, 3]', flush=True)
            out({'method': 'turn/started', 'params': {'threadId': 'a', 'turn': 'not an object'}})
            out({'id': m['id'], 'result': {'after': 'garbage'}})
        elif meth == 'turn':
            out({'method': 'turn/started', 'params': {'threadId': 'a', 'turn': {'id': 't1'}}})
            out({'id': 50, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'a', 'turnId': 't1', 'itemId': 'call_9', 'startedAtMs': 1700000000000,
                 'command': "/bin/zsh -lc 'make test'", 'cwd': '/p', 'availableDecisions': ['accept', 'decline']}})
            out({'id': m['id'], 'result': {}})
        elif meth == 'asks':
            for r in json.loads(m['params']['reqs']):
                out(r)
            out({'id': m['id'], 'result': {}})
        elif meth == 'replies':
            out({'id': m['id'], 'result': {'got': got}})
        elif 'method' not in m and 'id' in m:
            got[str(m['id'])] = m.get('result', m.get('error'))
''')


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class FakeAppServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'fake_app_server.py').write_text(FAKE)
        self.starts = self.root / 'starts'
        for name, value in (('RESTART_BACKOFF', (0.05, 0.1, 0.2)), ('RESTART_LIMIT', 3), ('RESTART_WAIT', 5.0)):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.b = server.Bridge([sys.executable, '-I', str(self.root / 'fake_app_server.py'), str(self.starts)])
        self.addCleanup(self.close)

    def close(self):
        self.b.close()
        if self.b.process:
            try:
                self.b.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.b.process.kill()
            for f in (self.b.process.stdin, self.b.process.stdout, self.b.process.stderr):
                try:
                    f.close()
                except OSError:
                    pass

    def methods(self):
        return [m.get('method') for _, m in list(self.b.events)]

    def kill(self):
        try:
            self.b.call('die', timeout=3)
        except RuntimeError:
            pass

    def workspace(self):
        w = server.Workspace(self.b)
        w.answers_path = self.root / 'cache' / 'answers.json'
        return w


class AppServerRestartTests(FakeAppServer):
    def test_app_server_dies_and_comes_back(self):
        self.assertEqual(self.b.call('ping'), {'pong': True})
        self.kill()
        self.assertTrue(wait_for(lambda: 'bridge/reconnected' in self.methods()), self.methods())
        ev = [m for _, m in self.b.events if m.get('method') == 'bridge/disconnected'][0]['params']
        self.assertTrue(ev['restarting'])
        self.assertIn('exited with code 3', ev['text'])
        self.assertIn('told to die', ev['text'])          # the reason, in the line the page shows
        self.assertEqual(self.b.call('ping'), {'pong': True})
        self.assertEqual(self.starts.read_text(), 'xx')
        self.assertEqual(self.b.status(), {'state': 'up'})

    def test_reader_survives_bad_lines(self):
        self.assertEqual(self.b.call('garbage'), {'after': 'garbage'})
        self.assertEqual(self.b.call('ping'), {'pong': True})          # the reader is still there
        self.assertTrue(any('not JSON' in e for e in self.b.errors), list(self.b.errors))
        self.assertTrue(self.b.ready)
        self.assertEqual(self.starts.read_text(), 'x')          # no restart was needed

    def test_send_while_down_is_delivered_after_the_restart(self):
        with patch.object(server, 'RESTART_BACKOFF', (0.4, 0.4, 0.4)):
            self.kill()
            self.assertTrue(wait_for(lambda: not self.b.ready))
            t = time.time()
            self.assertEqual(self.b.call('ping'), {'pong': True})          # waited for the restart, then went
            self.assertGreater(time.time() - t, 0.1)

    def test_send_after_it_gave_up_is_refused_with_the_reason(self):
        with patch.object(server, 'RESTART_LIMIT', 0):          # the first end is already too many
            self.kill()
            self.assertTrue(wait_for(lambda: 'bridge/gaveup' in self.methods()), self.methods())
        self.assertEqual(self.b.status()['state'], 'down')
        self.assertIn('stopped 1 times', self.b.status()['text'])
        self.b.command = [str(self.root / 'no-such-codex')]          # the person's retry cannot start it either
        with self.assertRaises(RuntimeError) as e:
            self.b.call('ping')
        self.assertIn('nothing was sent', str(e.exception))
        self.b.command = [sys.executable, '-I', str(self.root / 'fake_app_server.py'), str(self.starts)]
        self.assertEqual(self.b.call('ping'), {'pong': True})          # a later action tries once more, and it starts
        self.assertIn('bridge/reconnected', self.methods())

    def test_a_running_turn_is_settled_as_failed_when_it_dies(self):
        self.b.call('turn')
        self.assertEqual(self.b.active, {'a': 't1'})
        self.kill()
        self.assertTrue(wait_for(lambda: 'bridge/reconnected' in self.methods()))
        self.assertEqual(self.b.active, {})
        done = [m for _, m in self.b.events if m.get('method') == 'turn/completed']
        self.assertEqual(done[0]['params']['turn']['status'], 'failed')
        self.assertEqual(self.b.failures['a'][-1]['code'], 'appServerExited')


class OpenRequestAcrossRestartTests(FakeAppServer):
    def test_open_request_ends_when_the_app_server_dies(self):
        w = self.workspace()
        self.b.call('turn')
        self.assertIn('50', self.b.requests)
        self.kill()
        self.assertTrue(wait_for(lambda: 'bridge/reconnected' in self.methods()))
        self.assertNotIn('50', self.b.requests)
        with self.assertRaises(ValueError):          # its id belonged to the process that ended: nothing can answer it
            self.b.reply('50', {'decision': 'accept'})
        got = [r for r in w.answered_now() if r['said'] == 'ended']
        self.assertEqual([(r['threadId'], r['key'], r['ended'], r['kind'], r['verb'], r['cmd']) for r in got],
                         [('a', 'item:call_9', 'dropped', 'approve', 'run', 'make test')])
        self.assertEqual(self.b.call('replies')['got'], {})          # nothing was sent for it

    def test_open_request_ends_when_the_workspace_server_restarts(self):
        w = self.workspace()
        self.b.call('turn')
        w.requests_sync()          # the hook ran on arrival too; the kept list is written
        kept = json.loads((self.root / 'cache' / 'open-requests.json').read_text())
        self.assertEqual([k['key'] for k in kept], ['item:call_9'])
        # a new server: a new app-server holding nothing, the same kept files
        self.close()
        b2 = server.Bridge([sys.executable, '-I', str(self.root / 'fake_app_server.py'), str(self.starts)])
        self.b = b2
        w2 = server.Workspace(b2)
        w2.answers_path = w.answers_path
        got = [r for r in w2.answered_now() if r['said'] == 'ended']
        self.assertEqual([(r['key'], r['ended'], r['cmd']) for r in got], [('item:call_9', 'restart', 'make test')])
        self.assertEqual(json.loads((self.root / 'cache' / 'open-requests.json').read_text()), [])
        w3 = server.Workspace(b2)          # and once more: shown once, not again as new
        w3.answers_path = w.answers_path
        self.assertEqual(len([r for r in w3.answered_now() if r['said'] == 'ended']), 1)


PERM = {'id': 60, 'method': 'item/permissions/requestApproval', 'params': {'threadId': 'a', 'turnId': 't', 'itemId': 'p1', 'startedAtMs': 1, 'cwd': '/p', 'reason': 'fetch',
        'permissions': {'network': {'enabled': True}, 'fileSystem': {'write': ['/p/vendor']}}}}
FORM = {'id': 61, 'method': 'mcpServer/elicitation/request', 'params': {'threadId': 'a', 'turnId': 't', 'serverName': 'tickets', 'mode': 'form', 'message': 'File the ticket?',
        'requestedSchema': {'type': 'object', 'required': ['title', 'urgent'], 'properties': {
            'title': {'type': 'string', 'title': 'Title'},
            'urgent': {'type': 'boolean', 'title': 'Urgent'},
            'size': {'type': 'integer', 'title': 'Size'},
            'team': {'type': 'string', 'title': 'Team', 'oneOf': [{'const': 'core', 'title': 'Core team'}, {'const': 'web', 'title': 'Web team'}]},
            'tags': {'type': 'array', 'title': 'Tags', 'items': {'type': 'string', 'enum': ['bug', 'ui']}}}}}}
URL = {'id': 62, 'method': 'mcpServer/elicitation/request', 'params': {'threadId': 'a', 'serverName': 'auth', 'mode': 'url', 'message': 'Sign in', 'url': 'https://example.com/x', 'elicitationId': 'e1'}}
VERIFY = {'id': 63, 'method': 'mcpServer/elicitation/request', 'params': {'threadId': 'a', 'serverName': 'bank', 'mode': 'openai/userVerification', 'title': 'Confirm', 'description': 'd', 'challenge': 'c'}}
SECRET = {'id': 64, 'method': 'item/tool/requestUserInput', 'params': {'threadId': 'a', 'turnId': 't', 'itemId': 's1', 'isBlocking': True, 'questions': [
    {'id': 'tok', 'header': 'Token', 'question': 'Paste the deploy token', 'isOther': True, 'isSecret': True, 'options': None}]}}


class RequestCardTests(FakeAppServer):
    def ask(self, *reqs):
        self.b.call('asks', {'reqs': json.dumps(list(reqs))})

    def test_permissions_request_reaches_the_card_with_its_answer_set(self):
        self.ask(PERM)
        card = server.card_of(self.b.requests['60'])
        self.assertEqual(card['params']['availableDecisions'], ['accept', 'acceptForSession', 'decline'])
        with self.assertRaises(ValueError):
            self.b.reply('60', {'decision': 'cancel'})
        self.b.reply('60', {'decision': 'acceptForSession'})
        self.assertEqual(self.b.call('replies')['got']['60'], {'permissions': PERM['params']['permissions'], 'scope': 'session'})
        self.ask(dict(PERM, id=65))
        self.b.reply('65', {'decision': 'decline'})
        self.assertEqual(self.b.call('replies')['got']['65'], {'permissions': {}, 'scope': 'turn'})

    def test_form_elicitation_reaches_the_card_as_questions(self):
        self.ask(FORM)
        p = server.card_of(self.b.requests['61'])['params']
        self.assertEqual(p['elicit'], 'form')
        qs = {q['id']: q for q in p['questions']}
        self.assertEqual(list(qs), ['title', 'urgent', 'size', 'team', 'tags'])
        self.assertEqual([o['label'] for o in qs['urgent']['options']], ['yes', 'no'])
        self.assertEqual([o['label'] for o in qs['team']['options']], ['Core team', 'Web team'])
        self.assertIn('optional', qs['size']['question'])
        self.assertNotIn('optional', qs['title']['question'])
        a = lambda **kw: {'answers': {k: {'answers': [v]} for k, v in kw.items()}}
        for bad in (a(title='x', urgent='maybe'), a(title='x', urgent='yes', size='1.5'), a(urgent='yes'), a(title='x', urgent='no', team='Ops'),
                    a(title='x', urgent='no', tags='bug, docs'), {'action': 'accept'}):
            with self.assertRaises(ValueError, msg=bad):
                self.b.reply('61', bad)
        self.b.reply('61', a(title='Broken login', urgent='yes', size='-', team='Web team', tags='bug, ui'))
        self.assertEqual(self.b.call('replies')['got']['61'], {'action': 'accept', 'content': {'title': 'Broken login', 'urgent': True, 'team': 'web', 'tags': ['bug', 'ui']}, '_meta': None})

    def test_url_and_verification_elicitations_offer_only_what_the_protocol_allows(self):
        self.ask(URL, VERIFY, dict(FORM, id=66))
        self.assertEqual(server.card_of(self.b.requests['62'])['params']['elicit'], 'url')
        self.assertEqual(server.card_of(self.b.requests['63'])['params']['elicit'], 'decline')
        with self.assertRaises(ValueError):
            self.b.reply('63', {'action': 'accept'})          # a device-verified approval: this page cannot give the proof
        self.b.reply('63', {'action': 'decline'})
        self.b.reply('62', {'action': 'accept'})
        self.b.reply('66', {'action': 'cancel'})
        got = self.b.call('replies')['got']
        self.assertEqual((got['62'], got['63'], got['66']), ({'action': 'accept', 'content': None, '_meta': None}, {'action': 'decline', 'content': None, '_meta': None},
                                                              {'action': 'cancel', 'content': None, '_meta': None}))

    def test_secret_question_answer_is_not_stored(self):
        self.ask(SECRET)
        w = self.workspace()
        self.assertTrue(server.secret_asked(self.b.requests['64']))
        w.action('reply', {'id': '64', 'result': {'answers': {'tok': {'answers': ['hunter2-token']}}},
                           'kept': {'threadId': 'a', 'key': 'item:s1', 'said': 'a', 'kind': 'ask', 'question': 'Paste the deploy token', 'answerText': ['hunter2-token']}})
        self.assertEqual(self.b.call('replies')['got']['64'], {'answers': {'tok': {'answers': ['hunter2-token']}}})          # Codex got it
        w.requests_sync()
        for f in (self.root / 'cache').iterdir():
            self.assertNotIn('hunter2', f.read_text(), f.name)
        self.assertNotIn('hunter2', json.dumps(w.answered()))
        self.assertEqual([r['answerText'] for r in w.answered() if r['key'] == 'item:s1'], [['(hidden)']])

    def test_secret_question_in_the_mock_is_not_recorded(self):
        self.b.mock_live({'requests': [dict(SECRET, id=9200)]})
        self.b.reply('9200', {'answers': {'tok': {'answers': ['hunter2-token']}}})
        self.assertNotIn('hunter2', json.dumps(self.b.mock_sent))


# A fake Claude Code worker: starts a grandchild (standing in for the CLI that writes the transcript) and prints its pid.
WORKER = textwrap.dedent('''
    import json, subprocess, sys, time
    kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    print(json.dumps({'method': 'log', 'params': {'key': None, 'line': 'kid %d' % kid.pid}}), flush=True)
    for line in sys.stdin:
        m = json.loads(line)
        print(json.dumps({'id': m['id'], 'result': {'kid': kid.pid}}), flush=True)
''')


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:          # a zombie still answers kill(0); it is gone for our purpose
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


class ClaudeWorkerGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        f = Path(self.tmp.name) / 'worker.py'
        f.write_text(WORKER)
        self.cb = claude_bridge.ClaudeBridge(publish=lambda m: None, command=[sys.executable, '-I', str(f)])
        self.addCleanup(self.cb.close)

    def test_close_ends_the_whole_group(self):
        kid = self.cb.call('hello', timeout=10)['kid']
        self.assertTrue(alive(kid))
        self.cb.close()
        self.assertTrue(wait_for(lambda: not alive(kid), 8), 'the worker\'s child outlived close()')

    def test_a_lost_worker_takes_its_group_with_it(self):
        kid = self.cb.call('hello', timeout=10)['kid']
        os.kill(self.cb.process.pid, signal.SIGKILL)          # the worker alone dies (a crash)
        self.assertTrue(wait_for(lambda: not alive(kid), 8), 'the worker\'s child outlived the worker')


class ClaudeWrapTests(unittest.TestCase):
    def test_cli_group_ends_when_the_worker_it_watches_is_gone(self):
        import claude_wrap
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cli = Path(tmp.name) / 'cli.py'
        pidf = Path(tmp.name) / 'pids'
        cli.write_text('import os, subprocess, sys, time\nkid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                       'open(sys.argv[1], "w").write("%d %d" % (os.getpid(), kid.pid))\ntime.sleep(60)\n')
        cli.chmod(0o700)
        watched = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])          # stands in for the worker
        wrap = subprocess.Popen([sys.executable, '-I', claude_wrap.__file__, sys.executable, str(cli), str(pidf)], env={**os.environ, 'AGENT_IDE_WORKER_PID': str(watched.pid)},
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(wait_for(lambda: pidf.exists() and len(pidf.read_text().split()) == 2, 10))
        pids = [int(x) for x in pidf.read_text().split()]
        watched.kill()
        watched.wait()
        self.assertTrue(wait_for(lambda: wrap.poll() is not None, 10), 'the wrapper did not end')
        self.assertTrue(wait_for(lambda: not any(alive(p) for p in pids), 5), 'the CLI or its child outlived the worker')



# ---- robust (2026-11-21): stopping. The real server.py in a process of its own, on a free port, with a fake `codex` (AGENT_IDE_CODEX)
# that records each start's pid, and a fake Claude Code worker (AGENT_IDE_TEST_CLAUDE_WORKER) that starts a child, records both
# pids, and ignores both its stdin closing and SIGTERM (so only the group's SIGKILL ends it). Everything reads a temporary home.
FAKE_CODEX = textwrap.dedent("""
    import json, os, sys
    with open(os.environ['FAKE_CODEX_PIDS'], 'a') as f:
        f.write('%d\\n' % os.getpid())
    for line in sys.stdin:
        m = json.loads(line)
        if 'id' in m and 'method' in m:
            print(json.dumps({'id': m['id'], 'result': {'data': []}}), flush=True)
""")
STUBBORN_WORKER = textwrap.dedent("""
    import json, os, signal, subprocess, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    with open(os.environ['FAKE_WORKER_PIDS'], 'w') as f:
        f.write('%d %d' % (os.getpid(), kid.pid))
    for line in sys.stdin:
        m = json.loads(line)
        print(json.dumps({'id': m['id'], 'result': {'models': []}}), flush=True)
    time.sleep(60)          # its stdin closed: it stays anyway
""")


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def listening(port):
    import socket
    try:
        socket.create_connection(('127.0.0.1', port), 0.3).close()
        return True
    except OSError:
        return False


class ServerStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        codex = self.root / 'codex'
        codex.write_text('#!' + sys.executable + ' -I\n' + FAKE_CODEX)
        codex.chmod(0o700)
        (self.root / 'worker.py').write_text(STUBBORN_WORKER)
        (self.root / 'home').mkdir()
        (self.root / 'project').mkdir()
        self.codex_pids, self.worker_pids = self.root / 'codex-pids', self.root / 'worker-pids'
        self.port = free_port()
        self.env = {**os.environ, 'HOME': str(self.root / 'home'), 'CODEX_HOME': str(self.root / 'codex-home'), 'CLAUDE_CONFIG_DIR': str(self.root / 'claude-home'),
                    'AGENT_IDE_CODEX': str(codex), 'AGENT_IDE_CACHE_DIR': str(self.root / 'cache'), 'AGENT_IDE_PROJECT': str(self.root / 'project'),
                    'AGENT_IDE_NO_WINDOWS': '1', 'AGENT_IDE_TEST_PROVIDERS': 'codex', 'FAKE_CODEX_PIDS': str(self.codex_pids), 'FAKE_WORKER_PIDS': str(self.worker_pids),
                    'AGENT_IDE_TEST_CLAUDE_WORKER': json.dumps([sys.executable, '-I', str(self.root / 'worker.py')])}
        self.env.pop('AGENT_IDE_MOCK', None)
        self.env.pop('AGENT_IDE_PARENT_PID', None)
        self.proc = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for pid in self.pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.proc:
            self.proc.stdout.close()

    def pids(self):
        out = [int(x) for x in self.codex_pids.read_text().split()] if self.codex_pids.exists() else []
        return out + ([int(x) for x in self.worker_pids.read_text().split()] if self.worker_pids.exists() else [])

    def start(self, ignore_sigint=False, env=None):
        pre = (lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)) if ignore_sigint else None          # as a background job of a non-interactive shell
        self.proc = subprocess.Popen([sys.executable, str(Path(server.__file__)), '--port', str(self.port)], cwd=str(Path(server.__file__).parent), env={**self.env, **(env or {})},
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, preexec_fn=pre)
        self.assertTrue(wait_for(lambda: listening(self.port) or self.proc.poll() is not None, 20), 'the server did not start')
        self.assertIsNone(self.proc.poll(), 'the server ended at start: ' + (self.proc.stdout.read() if self.proc.poll() is not None else ''))

    def start_worker(self):
        import urllib.request
        url = 'http://127.0.0.1:%d/api/claude/models?cwd=%s' % (self.port, self.root / 'project')
        urllib.request.urlopen(urllib.request.Request(url, headers={'Host': '127.0.0.1:%d' % self.port}), timeout=15).read()
        self.assertTrue(wait_for(lambda: self.worker_pids.exists() and len(self.worker_pids.read_text().split()) == 2, 10), 'the fake worker did not start')

    def stops_everything(self, sig):
        self.assertEqual(len(self.pids()), 3)          # one app-server, the worker and its child
        self.assertTrue(all(alive(p) for p in self.pids()))
        t = time.time()
        self.proc.send_signal(sig)
        self.assertTrue(wait_for(lambda: self.proc.poll() is not None, 4), 'the server did not end')
        took = time.time() - t
        self.assertLess(took, 3.0)
        self.assertFalse(listening(self.port))
        self.assertTrue(wait_for(lambda: not any(alive(p) for p in self.pids()), 1), 'a child outlived the server')
        self.assertEqual(len(self.codex_pids.read_text().split()), 1, 'Codex was started again while stopping')
        self.assertIn('stopped (' + signal.Signals(sig).name + ')', self.proc.stdout.read())
        return took

    def test_sigint_ends_server_app_server_and_worker_group(self):
        self.start()
        self.start_worker()
        self.stops_everything(signal.SIGINT)

    def test_sigterm_ends_server_app_server_and_worker_group(self):
        self.start()
        self.start_worker()
        self.stops_everything(signal.SIGTERM)

    def test_sigint_ends_it_even_when_started_with_sigint_ignored(self):
        self.start(ignore_sigint=True)
        self.start_worker()
        self.stops_everything(signal.SIGINT)

    def test_no_restart_when_the_app_server_ends_during_the_stop(self):
        self.start()
        codex = self.pids()[0]
        self.proc.send_signal(signal.SIGTERM)
        os.kill(codex, signal.SIGKILL)          # the app-server's own end races the stop
        self.assertTrue(wait_for(lambda: self.proc.poll() is not None, 4), 'the server did not end')
        time.sleep(0.6)          # past the first restart's delay (RESTART_BACKOFF)
        self.assertEqual(len(self.codex_pids.read_text().split()), 1, 'Codex was started again while stopping')

    def test_the_server_stops_when_the_app_that_started_it_is_gone(self):
        parent = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])          # stands in for the app's shell
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        self.start(env={'AGENT_IDE_PARENT_PID': str(parent.pid)})
        self.start_worker()
        time.sleep(1.5)
        self.assertIsNone(self.proc.poll(), 'the server stopped while its parent lived')
        parent.kill()
        parent.wait()
        self.assertTrue(wait_for(lambda: self.proc.poll() is not None, 5), 'the server outlived the app that started it')
        self.assertFalse(listening(self.port))
        self.assertTrue(wait_for(lambda: not any(alive(p) for p in self.pids()), 1), 'a child outlived the server')


# ---- speed (2026-11-21): the first answer does not wait on a slow provider's first listing
class SlowProvider:
    name, label = 'slow', 'Slow harness'

    def __init__(self, cwd, delay):
        self.cwd, self.delay, self.calls = cwd, delay, 0

    def available(self):
        return {'name': self.name, 'label': self.label, 'found': True}

    def list_sessions(self, project_root=None):
        self.calls += 1
        time.sleep(self.delay)
        return [{'id': 'slow:one', 'title': 'Slow session', 'cwd': self.cwd, 'started': 1, 'updated': 2, 'running': False, 'parent': None}]


class FirstListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(server, 'CODEX_HOME', Path(self.tmp.name) / 'codex-home')
        p.start()
        self.addCleanup(p.stop)
        self.ws = server.Workspace(server.CodexAbsent())
        self.ws._providers = [server.CodexProvider(self.ws, server.CODEX_HOME, server.binary)]

    def with_provider(self, delay):
        sp = SlowProvider(self.tmp.name, delay)
        self.ws._providers.append(sp)
        return sp

    def test_bootstrap_answers_before_a_slow_listing_then_the_page_hears(self):
        sp = self.with_provider(2.0)
        t = time.time()
        snap = self.ws.snapshot()
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(snap['loading'], ['slow'])
        self.assertNotIn('slow:one', [s['id'] for s in snap['sessions']])
        t = time.time()
        self.assertNotIn('slow:one', [s['id'] for s in self.ws.all_sessions(self.ws.listings_ready())])          # the canvas's way: no wait either
        self.assertLess(time.time() - t, 1.0)
        self.assertTrue(wait_for(lambda: self.ws._warm['done'].is_set(), 5))
        heard = [m for _, m in self.ws.bridge.events if m.get('method') == 'provider/changed']
        self.assertEqual([m['params']['providers'] for m in heard], [['slow']])
        snap = self.ws.snapshot()
        self.assertEqual(snap['loading'], [])
        self.assertIn('slow:one', [s['id'] for s in snap['sessions']])
        self.assertEqual(sp.calls, 2)          # the first listing once in the background, then as before: each answer lists

    def test_a_quick_listing_is_in_the_first_answer(self):
        self.with_provider(0.0)
        snap = self.ws.snapshot()
        self.assertEqual(snap['loading'], [])
        self.assertIn('slow:one', [s['id'] for s in snap['sessions']])
        self.assertFalse([m for _, m in self.ws.bridge.events if m.get('method') == 'provider/changed'])          # nothing to read again

    def test_other_callers_still_wait_for_the_full_listing(self):
        self.with_provider(0.5)
        self.assertIn('slow:one', [s['id'] for s in self.ws.all_sessions()])          # send, connectors, usage, compaction: as before


if __name__ == '__main__':
    unittest.main()
