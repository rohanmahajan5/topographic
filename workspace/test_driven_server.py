"""The workspace server driving a harness other than Codex and Claude Code, end to end, against the scripted ACP agent
(providers/fixtures/acp/mock_agent.py): start from the page's new-agent call, send, a send mid-turn refused with what to do,
approvals answered with the agent's own option id, Stop, a request lost with its process said in its chat, the harness's
own lists in the page's shapes. Ported 2026-11-21 from the branch ide-providers-2026-11-09 (test_providers_server.py), without
its queue, checkpoints and handoff (not ported)."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

os.environ.setdefault('AGENT_IDE_DATA', tempfile.mkdtemp(prefix='agent-ide-test-'))

import server
from providers import acp
from providers.acp_flavors import Flavor

HERE = Path(__file__).resolve().parent
MOCK = HERE / 'providers' / 'fixtures' / 'acp' / 'mock_agent.py'


def wait_for(pred, timeout=20.0):          # generous: returns as soon as it holds; a loaded machine is slow, not wrong
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    return pred()


class Bridge:
    ready = True
    loaded = set()
    threads = {}
    active = {}
    requests = {}
    event_seq = 0

    def __init__(self):
        self.events = []

    def publish(self, msg):
        self.events.append(msg)

    def call(self, method, params=None, timeout=45):
        raise AssertionError('Codex was not to be called: ' + method)


class DrivenTests(unittest.TestCase):
    scen = 'v1'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='ws-acp-'))
        self.cwd = self.tmp / 'project'
        self.cwd.mkdir()
        self.b = Bridge()
        self.ws = server.Workspace(self.b)
        self.ws.answers_path = self.tmp / 'answers.json'
        flavor = Flavor(name='mock', label='Mock agent', binary=sys.executable, args=(str(MOCK), self.scen), cancel_timeout=5.0)
        self.p = acp.AcpProvider(flavor, publish=self.ws.provider_publish, data_dir=self.tmp / 'data')
        codex = SimpleNamespace(name='codex', label='Codex', owns=lambda sid: False, available=lambda: {'name': 'codex'})
        self.ws._providers = [codex, self.p]
        self.ws.sessions = lambda: []
        self.ws.session = lambda tid: None

    def tearDown(self):
        self.p.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ends(self, sid):
        return [m['params'] for m in self.b.events if m.get('method') == 'provider/turn' and m['params'].get('ended') and m['params'].get('threadId') == sid]

    def start(self):
        r = self.ws.action('start', {'harness': 'mock', 'cwd': str(self.cwd)})
        self.assertEqual(r['thread']['harness'], 'mock')
        return r['thread']['id']

    def test_start_send_and_the_row(self):
        sid = self.start()
        r = self.ws.action('send', {'threadId': sid, 'text': 'hello'})
        self.assertEqual(r['how'], 'sent')
        self.assertTrue(wait_for(lambda: self.ends(sid)))
        row = next(s for s in self.ws.all_sessions() if s['id'] == sid)
        self.assertEqual((row['provider'], row['drivable'], row['status']), ('mock', True, 'idle'))
        msgs = self.p.read_history(sid)['messages']
        self.assertTrue(any(m['role'] == 'assistant' for m in msgs))

    def test_a_send_mid_turn_is_refused_with_what_to_do(self):
        sid = self.start()
        self.ws.action('send', {'threadId': sid, 'text': 'slow'})
        self.assertTrue(wait_for(lambda: self.p.phase(sid) == 'running'))
        with self.assertRaises(ValueError) as e:
            self.ws.action('send', {'threadId': sid, 'text': 'more'})
        self.assertIn('press Stop', str(e.exception))
        self.assertIn('still in the box', str(e.exception))
        self.ws.action('stop', {'threadId': sid})
        self.assertTrue(wait_for(lambda: self.ends(sid), 10))
        self.assertEqual(self.p.phase(sid), 'idle')
        with self.assertRaises(ValueError):
            self.ws.action('stop', {'threadId': sid})          # nothing runs: said so

    def test_an_approval_answered_with_the_agents_own_option(self):
        sid = self.start()
        self.ws.action('send', {'threadId': sid, 'text': 'edit'})
        reqs = wait_for(lambda: self.ws.provider_requests())
        self.assertTrue(reqs)
        card = reqs[0]['params']
        self.assertTrue(card['agentOptions'] and card['itemId'] == str(reqs[0]['id']))
        self.assertIn('accept', card['availableDecisions'])
        snap = self.ws.snapshot()
        self.assertIn(str(reqs[0]['id']), [str(r['id']) for r in snap['requests']])
        allow = next(o for o in card['agentOptions'] if o['kind'] == 'allow_once')
        self.ws.action('reply', {'id': reqs[0]['id'], 'result': {'optionId': allow['id']},
                                 'kept': {'threadId': sid, 'key': 'item:' + str(reqs[0]['id']), 'said': 'y', 't': time.time()}})
        self.assertTrue(wait_for(lambda: self.ends(sid)))
        self.assertFalse(self.ws.provider_requests())
        self.assertEqual([x['said'] for x in self.ws.answered() if x['threadId'] == sid], ['y'])

    def test_a_codex_decision_maps_to_the_agents_option(self):
        sid = self.start()
        self.ws.action('send', {'threadId': sid, 'text': 'edit'})
        reqs = wait_for(lambda: self.ws.provider_requests())
        self.ws.action('reply', {'id': reqs[0]['id'], 'result': {'decision': 'decline', 'message': 'not now'}})
        self.assertTrue(wait_for(lambda: self.ends(sid)))

    def test_a_request_lost_with_its_process_is_said_in_its_chat(self):
        sid = self.start()
        self.ws.action('send', {'threadId': sid, 'text': 'edit'})
        reqs = wait_for(lambda: self.ws.provider_requests())
        self.assertTrue(reqs)
        self.p.live[sid.split(':', 1)[1]].conn.kill()          # the agent's process dies while it waits on you
        ended = wait_for(lambda: [x for x in self.ws.answered() if x['threadId'] == sid and x['said'] == 'ended'])
        self.assertTrue(ended)
        self.assertEqual(ended[0]['key'], 'item:' + str(reqs[0]['id']))
        self.assertTrue(wait_for(lambda: self.ends(sid)))
        r = self.ws.action('send', {'threadId': sid, 'text': 'hello'})          # the next send starts a new process
        self.assertEqual(r['how'], 'resumed')

    def test_lists_in_the_pages_shapes_never_wait(self):
        empty = self.ws.provider_models('mock')          # nothing started yet: empty lists at once, not "loading"
        self.assertEqual((empty['data'], empty['known'], empty['reading']), ([], False, True))
        ahead = wait_for(lambda: self.ws.provider_models('mock')['known'] and self.ws.provider_models('mock'))          # read ahead, no session
        self.assertEqual([m['id'] for m in ahead['data']], ['m1', 'm2'])
        self.assertTrue(ahead['data'][0]['supportedReasoningEfforts'] and not ahead['reading'])
        self.assertTrue(any(m.get('method') == 'provider/options' for m in self.b.events))
        self.assertTrue([p for p in self.ws.provider_controls('mock')['presets'] if p['approvalPolicy'] == 'plan'])
        sid = self.start()
        m = self.ws.provider_models(sid=sid)
        self.assertTrue(m['data'] and m['data'][0]['supportedReasoningEfforts'] is not None)
        c = self.ws.provider_controls(sid=sid)
        self.assertTrue(all(p['approvalPolicy'] and p['sandbox'] is None for p in c['presets']))
        self.assertEqual(sum(1 for p in c['presets'] if p.get('default')), 1)
        self.assertEqual({x['name'] for x in c['commands']}, {'status', 'diff'})
        st = self.ws.watch_settings(sid)
        self.assertEqual(st['provider'], 'mock')

    def test_unknown_harness_and_not_installed(self):
        with self.assertRaises(ValueError):
            self.ws.action('start', {'harness': 'nope', 'cwd': str(self.cwd)})
        self.p.flavor = Flavor(name='mock', label='Mock agent', binary='mock-agent-not-here-xyz', search=())
        with self.assertRaises(ValueError) as e:
            self.ws.action('start', {'harness': 'mock', 'cwd': str(self.cwd)})
        self.assertIn('not installed', str(e.exception))


class Cards(unittest.TestCase):
    def test_generic_card_and_answer_shape(self):
        from providers import core
        req = core.request_message('opencode', 'opencode:s', 'opencode:r1', 'command', 'shell: ls',
                                   [{'id': 'once', 'label': 'Allow once', 'kind': 'allow_once'}, {'id': 'reject', 'label': 'Decline', 'kind': 'reject_once'}],
                                   command='ls')
        c = server.generic_card(req)['params']
        self.assertEqual((c['availableDecisions'], c['itemId'], c['command']), (['accept', 'decline'], 'opencode:r1', 'ls'))
        self.assertEqual(server.answer_shape({'answers': {'0': {'answers': ['yes']}}}), {'answers': {'0': ['yes']}})
        self.assertEqual(server.answer_shape({'decision': 'acceptForSession'}), {'decision': 'acceptForSession'})
        req['params']['answerVia'] = 'elsewhere'
        self.assertTrue(server.generic_card(req)['params']['watchOnly'])
        self.assertEqual(server.generic_card(req)['params']['harness'], 'OpenCode')
        w = server.generic_words(req)
        self.assertEqual((w['key'], w['verb'], w['cmd']), ('item:opencode:r1', 'run', 'ls'))



class PanelRows(unittest.TestCase):
    """The Providers panel's rows for the driven harnesses (provider_setup.ProviderSetup.driven_row): never a command on the
    asking thread, the state in words, the one step that fixes it."""

    def setup_with(self, *provs, no_windows=True):
        import provider_setup
        return provider_setup.ProviderSetup(lambda: None, lambda: None, lambda: {}, lambda: {}, runner=lambda *a, **k: (None, '', 'not here'),
                                            no_windows=lambda: no_windows, driven=lambda: list(provs))

    def row(self, ps, name):
        return next(r for r in ps.get()['providers'] if r['name'] == name)

    def test_missing_refused_unknown_and_signed_out(self):
        tmp = tempfile.mkdtemp(prefix='panel-')
        gone = acp.AcpProvider(Flavor(name='cursor', label='Cursor Agent', binary='cursor-agent-not-here-xyz', search=(), login_args=('login',)), data_dir=tmp)
        r = self.row(self.setup_with(gone), 'cursor')
        self.assertEqual((r['state'], r['fix']['action'], r['fix']['url']), ('missing', 'install', 'https://cursor.com/install'))
        acp.LOOKS.pop('acp-mock', None)
        gem = acp.AcpProvider(Flavor(name='acp-mock', label='Some ACP agent', binary=sys.executable), data_dir=tmp)
        r = self.row(self.setup_with(gem), 'acp-mock')
        self.assertEqual((r['state'], r['fix']), ('unknown', None))          # no sign-in of its own and no command to copy
        gem.refused = {'reason': 'This client is no longer supported for Gemini Code Assist for individuals.', 'at': time.time()}
        r = self.row(self.setup_with(gem), 'acp-mock')
        self.assertEqual((r['state'], r['say']), ('refused', 'Its service refused this account'))
        self.assertIn('no longer supported', r['error'])
        acp.LOOKS.pop('agy-mock', None)
        agy = acp.AcpProvider(Flavor(name='agy-mock', label='Antigravity', binary=sys.executable, login_args=('-c', 'pass')), data_dir=tmp)
        agy.signed_in = False
        ps = self.setup_with(agy)
        r = self.row(ps, 'agy-mock')
        self.assertEqual((r['state'], r['fix']['action']), ('signed-out', 'signin'))
        with self.assertRaises(ValueError) as e:          # no windows: the sign-in (which opens a browser) is not started
            ps.act('agy-mock', 'signin')
        self.assertIn('no windows', str(e.exception))

    def test_a_row_never_waits_on_a_slow_version(self):
        acp.LOOKS.pop('slow', None)
        slow = acp.AcpProvider(Flavor(name='slow', label='Slow agent', binary=sys.executable, args=('-c', 'import time; time.sleep(5)'),
                                      status_args=('-c', 'import time; time.sleep(5)'), status_ok='x'), data_dir=tempfile.mkdtemp())
        t = time.time()
        self.row(self.setup_with(slow), 'slow')
        self.assertLess(time.time() - t, 1.0)


if __name__ == '__main__':
    unittest.main()
