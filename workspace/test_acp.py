"""The ACP provider (providers/acp.py), by protocol situation, against a scripted agent (providers/fixtures/acp/mock_agent.py)
and against frames recorded from real agents (providers/fixtures/acp/recorded/).

Run: cd agent-ide/workspace && python3 -m unittest test_acp -v
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from providers import acp, core
from providers.acp_flavors import Flavor, AUTO, CURSOR, ANTIGRAVITY, FLAVORS

HERE = Path(__file__).resolve().parent
MOCK = HERE / 'providers' / 'fixtures' / 'acp' / 'mock_agent.py'
RECORDED = HERE / 'providers' / 'fixtures' / 'acp' / 'recorded'


def mock_flavor(scen, **kw):
    kw.setdefault('cancel_timeout', 5.0)
    return Flavor(name='mock', label='Mock agent', binary=sys.executable, args=(str(MOCK), scen), **kw)


def wait_for(pred, timeout=20.0):          # generous: returns as soon as it holds; a loaded machine is slow, not wrong
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    return pred()


class Base(unittest.TestCase):
    scen = 'v1'
    flavor_kw = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='acp-test-'))
        self.cwd = self.tmp / 'project'
        self.cwd.mkdir()
        self.data = self.tmp / 'data'
        self.msgs = []
        self.p = acp.AcpProvider(mock_flavor(self.scen, **self.flavor_kw), publish=self.msgs.append, data_dir=self.data)

    def tearDown(self):
        self.p.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **kw):
        return self.p.start(str(self.cwd), **kw)['threadId']

    def ended(self, sid, n=1, timeout=20.0):
        return wait_for(lambda: [m['params'] for m in self.msgs if m.get('method') == 'provider/turn' and m['params'].get('ended')
                                 and m['params']['threadId'] == sid][n - 1:n], timeout)

    def turn(self, sid, text, n=1, timeout=20.0):
        self.p.send(sid, text)
        got = self.ended(sid, n, timeout)
        self.assertTrue(got, 'turn %d did not end' % n)
        return got[0]

    def requests_seen(self):
        return [m for m in self.msgs if m.get('method', '').startswith('item/agent/request')]

    def record(self, sid):
        key = sid.split(':', 1)[1]
        return [json.loads(x) for x in (self.data / 'acp' / 'mock' / (key + '.ndjson')).read_text().splitlines()]


class Initialize(Base):
    def test_v1_shape_and_superset_request(self):
        sid = self.start()
        s = self.p.live[sid.split(':', 1)[1]]
        self.assertEqual(s.conn.proto, 1)
        self.assertTrue(s.conn.caps['load'])
        self.assertTrue(s.conn.caps['close'])
        init = next(r['msg'] for r in self.record(sid) if r['dir'] == 'out' and r['msg'].get('method') == 'initialize')
        for k in ('protocolVersion', 'info', 'capabilities', 'clientInfo', 'clientCapabilities'):
            self.assertIn(k, init['params'])
        self.assertEqual(init['params']['clientCapabilities'], {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False})
        self.assertEqual(self.p.phase(sid), 'idle')
        self.assertTrue(sid.startswith('mock:mock-'))


class InitializeV2(Base):
    scen = 'v2'

    def test_v2_shape_turn_ends_on_idle_and_echo_dropped(self):
        sid = self.start()
        s = self.p.live[sid.split(':', 1)[1]]
        self.assertEqual(s.conn.proto, 2)
        self.assertTrue(s.conn.caps['resume'])
        end = self.turn(sid, 'hello')
        self.assertEqual(end['stopReason'], 'end_turn')
        h = self.p.read_history(sid)
        self.assertEqual([m['text'] for m in h['messages'] if m['role'] == 'user'], ['hello'])          # the echo is not a second message
        self.assertEqual([m['text'] for m in h['messages'] if m['role'] == 'assistant'], ['Hello'])
        self.assertFalse(h['turnActive'])


class InitializeAntigravityShape(Base):
    scen = 'agy'

    def test_version_2_in_v1_shape_is_v1(self):
        sid = self.start()
        s = self.p.live[sid.split(':', 1)[1]]
        self.assertEqual(s.conn.proto, 1)          # the reply's shape decides, not its number (acp.md §1)
        self.assertEqual(s.conn.caps['protocolVersion'], 2)
        end = self.turn(sid, 'hello')          # so the turn ends on the prompt response
        self.assertEqual(end['stopReason'], 'end_turn')


class Turn(Base):
    def test_text_tool_diff_permission(self):
        sid = self.start()
        self.p.send(sid, 'hello')
        self.assertTrue(self.ended(sid, 1))
        h = self.p.read_history(sid)
        self.assertEqual([m['text'] for m in h['messages'] if m['role'] == 'assistant'], ['Hello'])
        self.assertEqual(h['reasoning'][0]['text'], 'thinking…')
        self.assertTrue(any(e['kind'] == 'say' and e['text'] == 'Hello' for e in h['events']))

        self.p.send(sid, 'edit')
        req = wait_for(lambda: self.p.requests())
        self.assertEqual(len(req), 1)
        r = req[0]
        self.assertEqual(r['method'], 'item/agent/requestApproval')
        self.assertEqual(r['params']['kind'], 'file-change')
        self.assertEqual(r['params']['paths'], ['hello.txt'])
        self.assertEqual([o['id'] for o in r['params']['options']], ['yes', 'always', 'no'])
        self.assertEqual(self.p.phase(sid), 'running')
        with self.assertRaises(ValueError):
            self.p.reply(r['id'], {'optionId': 'not-offered'})
        self.p.reply(r['id'], {'decision': 'allow_once'})
        end = self.ended(sid, 2)
        self.assertEqual(end[0]['stopReason'], 'end_turn')
        self.assertEqual((self.cwd / 'hello.txt').read_text(), 'hi\n')
        h = self.p.read_history(sid)
        w = [e for e in h['events'] if e['kind'] == 'write']
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]['path'], 'hello.txt')
        self.assertEqual(w[0]['status'], 'completed')
        self.assertIn('+hi', w[0]['diff'])
        self.assertIn('/dev/null', w[0]['diff'])
        ask = [e for e in h['events'] if e['kind'] == 'ask']
        self.assertEqual(ask[0]['answer'], 'yes')
        methods = [m.get('method') for m in self.msgs]
        self.assertIn('serverRequest/resolved', methods)
        self.assertLess(methods.index('item/agent/requestApproval'), methods.index('serverRequest/resolved'))
        self.assertEqual(self.p.requests(), [])
        with self.assertRaises(ValueError):          # answered once only
            self.p.reply(r['id'], {'decision': 'allow_once'})

    def test_malformed_update_is_skipped(self):
        sid = self.start()
        end = self.turn(sid, 'malformed')
        self.assertEqual(end['stopReason'], 'end_turn')
        h = self.p.read_history(sid)
        self.assertIn('still here', [m['text'] for m in h['messages']])
        self.assertGreaterEqual(h['unknown'], 3)
        self.assertEqual(self.turn(sid, 'hello', n=2)['stopReason'], 'end_turn')          # the connection lives on

    def test_non_json_line_ends_the_connection(self):
        sid = self.start()
        s = self.p.live[sid.split(':', 1)[1]]
        end = self.turn(sid, 'garbage')
        self.assertEqual(end['stopReason'], 'error')
        self.assertIn('not a protocol message', end['error'])
        self.assertTrue(wait_for(lambda: s.conn.proc.poll() is not None))
        self.assertNotIn(sid.split(':', 1)[1], self.p.live)

    def test_final_line_without_newline(self):
        sid = self.start()
        end = self.turn(sid, 'bye')
        self.assertEqual(end['stopReason'], 'end_turn')
        self.assertIsNone(end['error'])

    def test_usage_update(self):
        sid = self.start()
        self.turn(sid, 'usage')
        h = self.p.read_history(sid)
        self.assertEqual((h['context'], h['window']), (5300, 200000))
        self.assertEqual([c['context'] for c in h['contexts']], [1200, 5300])
        self.assertEqual(h['cost'], {'amount': 0.01, 'currency': 'USD'})
        self.assertEqual(self.p.capabilities(sid)['context'], 'window')

    def test_plan_updates_with_empty_replacement(self):
        sid = self.start()
        self.turn(sid, 'plan')
        plan = self.p.read_history(sid)['plan']
        self.assertEqual([[s['status'] for s in st['steps']] for st in plan],
                         [['pending', 'pending'], ['completed', 'in_progress'], ['completed', 'completed'], []])
        self.assertTrue(plan[2]['steps'][1]['cancelled'])

    def test_tool_output_tail_and_coalescing(self):
        sid = self.start()
        n0 = len(self.msgs)
        self.turn(sid, 'run')
        h = self.p.read_history(sid)
        run = [e for e in h['events'] if e['kind'] == 'run']
        self.assertEqual(len(run), 1)
        self.assertEqual(run[0]['text'], 'seq 1 5000')
        tool = self.p.live[sid.split(':', 1)[1]].h.tools['r1']
        self.assertLessEqual(len(tool['output']), acp.OUTPUT_TAIL + 40)
        self.assertTrue(tool['output'].startswith('[… earlier output cut]'))
        self.assertIn('line 0039', tool['output'])
        activity = [m for m in self.msgs[n0:] if m.get('method') == 'provider/activity']
        self.assertLess(len(activity), 42)          # 42 updates, coalesced and throttled

    def test_history_survives_a_restart(self):
        sid = self.start()
        self.turn(sid, 'hello')
        live = self.p.read_history(sid)
        p2 = acp.AcpProvider(mock_flavor('v1'), data_dir=self.data)
        rows = p2.list_sessions(str(self.tmp))
        self.assertEqual([r['id'] for r in rows], [sid])
        self.assertEqual(rows[0]['title'], 'hello')
        self.assertEqual(rows[0]['cwd'], str(self.cwd))
        self.assertEqual(p2.list_sessions('/somewhere/else'), [])
        h = p2.read_history(sid)
        self.assertEqual([(m['role'], m['text']) for m in h['messages']], [(m['role'], m['text']) for m in live['messages']])
        self.assertFalse(h['turnActive'])

    def test_record_frames_and_sidecar(self):
        sid = self.start()
        self.turn(sid, 'hello')
        rec = self.record(sid)
        self.assertEqual(rec[0]['dir'], 'out')
        self.assertEqual(rec[0]['msg']['method'], 'initialize')
        self.assertTrue(all({'t', 'dir', 'msg'} <= set(r) for r in rec))
        self.assertTrue(any(r['dir'] == 'in' and r['msg'].get('method') == 'session/update' for r in rec))
        side = json.loads((self.data / 'acp' / 'mock' / (sid.split(':', 1)[1] + '.json')).read_text())
        self.assertEqual(set(side), {'cwd', 'title', 'started', 'model', 'flavor'})
        self.assertEqual(side['flavor'], 'mock')

    def test_stderr_tail_is_redacted(self):
        sid = self.start()
        s = self.p.live[sid.split(':', 1)[1]]
        wait_for(lambda: s.conn.stderr_tail)
        self.assertNotIn('sk-abcdefghijklmnopqrstuvwxyz', s.conn.stderr_tail)
        if str(Path.home()) != '/':
            self.assertNotIn(str(Path.home()), s.conn.stderr_tail)

    def test_mid_turn_send_is_refused_and_steer_unsupported(self):
        sid = self.start()
        self.p.send(sid, 'slow')
        with self.assertRaises(ValueError):
            self.p.send(sid, 'again')
        with self.assertRaises(ValueError):
            self.p.send(sid, 'again', how='steer')
        self.p.stop(sid)
        self.assertTrue(self.ended(sid))


class LookAhead(Base):
    """A new agent's window has the harness's lists before any session (2026-11-22: Antigravity's model was "default" and
    greyed out): read once in the background by initialize + session/new with no prompt, kept per harness."""

    def look(self):
        first = self.p.options(None)
        self.assertTrue(first.get('reading') and not first.get('models'), first)          # never waits: says it is reading
        self.assertTrue(wait_for(lambda: not self.p.offer_busy and any(m.get('method') == 'provider/options' for m in self.msgs)))
        return self.p.options(None)

    def test_lists_before_any_session_no_prompt_no_record(self):
        o = self.look()
        self.assertEqual([m['id'] for m in o['models']], ['m1', 'm2'])
        self.assertEqual([m['id'] for m in o['efforts']], ['low', 'medium', 'high'])
        self.assertIn('plan', [m['id'] for m in o['modes']])
        self.assertEqual(o['current'], {'model': 'm1', 'effort': 'medium', 'mode': 'default'})
        self.assertFalse((self.data / 'acp' / 'mock').exists())          # no session record: the look is not a session of the person's
        self.assertEqual(os.listdir(self.data / 'acp-look' / 'mock'), [])          # its folder stays empty
        self.assertEqual(self.p.live, {})
        again = acp.AcpProvider(self.p.flavor, data_dir=self.data)          # kept: a restarted server has them at once, no new look
        self.assertEqual(again.options(None)['models'], o['models'])
        self.assertFalse(again.offer_busy)

    def test_a_choice_from_the_kept_lists_is_applied_at_start(self):
        self.look()
        sid = self.p.start(str(self.cwd), model='m2', effort='high', mode='plan')['threadId']
        self.assertEqual(self.p.read_history(sid)['model'], 'm2')
        sent = [r['msg']['params'] for r in self.record(sid) if r['dir'] == 'out' and r['msg'].get('method') == 'session/set_config_option']
        self.assertEqual([(x['configId'], x['value']) for x in sent], [('model', 'm2'), ('effort', 'high'), ('mode', 'plan')])
        self.assertFalse([r for r in self.record(sid) if r['dir'] == 'out' and r['msg'].get('method') == 'session/prompt'])

    def test_a_record_without_lists_shows_the_harness_lists(self):
        self.look()
        key = 'old-1'
        (self.data / 'acp' / 'mock').mkdir(parents=True)
        self.p.write_side(key, str(self.cwd), model='m1')
        self.p.record_path(key).write_text('')
        o = self.p.options('mock:' + key)
        self.assertEqual([m['id'] for m in o['models']], ['m1', 'm2'])
        self.assertTrue(o['fromHarness'])
        self.assertEqual(o['current']['model'], 'm1')

    def test_not_signed_in_says_so_and_does_not_look_again_at_once(self):
        self.p.flavor.args = (str(MOCK), 'auth')
        self.p.options(None)
        self.assertTrue(wait_for(lambda: not self.p.offer_busy))
        self.assertIs(self.p.signed_in, False)
        self.assertEqual(self.p.options(None).get('models'), None)
        self.assertFalse(self.p.offer_busy)          # OFFER_RETRY: no second look straight away


class Recorded_Offer(unittest.TestCase):
    def test_antigravity_lists_as_the_page_gets_them(self):
        r = json.loads((RECORDED / 'antigravity' / 'agy-1.5.3-offer.json').read_text())['result']
        p = acp.AcpProvider(ANTIGRAVITY, data_dir=tempfile.mkdtemp(prefix='acp-offer-'))
        h = acp.History()
        h.setup(r)
        o = p.shape(h.c['options'], {'model': h.c['model'], 'effort': h.c['effort'], 'mode': h.c['mode']})
        self.assertEqual([m['id'] for m in o['models']], ['gemini-3.8-flash', 'claude-sonnet-4-6', 'claude-opus-4-6-thinking', 'gpt-oss-120b-medium'])
        self.assertEqual([e['id'] for e in o['efforts']], ['low', 'medium', 'high'])
        self.assertEqual([m['id'] for m in o['modes']], ['default', 'accept-edits', 'auto', 'plan', 'full-access'])
        self.assertEqual(o['current'], {'model': 'gemini-3.8-flash', 'effort': 'high', 'mode': 'default'})


class Options(Base):
    def test_model_effort_mode_from_config_options(self):
        sid = self.p.start(str(self.cwd), model='m2', effort='high', mode='plan')['threadId']
        h = self.p.read_history(sid)
        self.assertEqual((h['model'], h['effort'], h['mode']), ('m2', 'high', 'plan'))
        o = self.p.options(sid)
        self.assertEqual([m['id'] for m in o['models']], ['m1', 'm2'])
        self.assertEqual([m['id'] for m in o['efforts']], ['low', 'medium', 'high'])          # a grouped select, flattened
        self.assertIn('full-access', [m['id'] for m in o['modes']])
        sent = [r['msg']['params'] for r in self.record(sid) if r['dir'] == 'out' and r['msg'].get('method') == 'session/set_config_option']
        self.assertEqual([(x['configId'], x['value']) for x in sent], [('model', 'm2'), ('effort', 'high'), ('mode', 'plan')])
        r = self.p.send(sid, 'hello', model='Default')          # the sentinel is never sent
        self.assertEqual(r['skipped'][0]['what'], 'model')
        self.ended(sid)
        r = self.p.send(sid, 'hello', model='m9')
        self.assertEqual(r['skipped'][0]['reason'], 'not offered by this session')
        self.ended(sid, 2)
        caps = self.p.capabilities(sid)
        core.check_capabilities(caps)
        self.assertTrue(caps['models'] and caps['effort'] and caps['modes'])
        self.assertFalse(caps['steer'])

    def test_refused_mode_is_never_set(self):
        self.p.flavor.refused_modes = ('yolo',)
        sid = self.start()
        r = self.p.send(sid, 'hello', mode='yolo')
        self.assertEqual(r['skipped'][0]['what'], 'mode')
        self.assertNotIn('yolo', [m['id'] for m in self.p.options(sid)['modes']])
        sent = [r['msg']['params'] for r in self.record(sid) if r['dir'] == 'out' and r['msg'].get('method') == 'session/set_config_option']
        self.assertEqual(sent, [])

    def test_full_access_auto_answers_inside_the_folder_only(self):
        sid = self.p.start(str(self.cwd), mode='full-access')['threadId']
        self.assertTrue(self.p.live[sid.split(':', 1)[1]].full_access)
        self.turn(sid, 'tools')          # a read and a command: answered here, no card
        self.assertEqual(self.requests_seen(), [])
        self.turn(sid, 'edit', n=2)          # inside the folder: answered here
        self.assertEqual(self.requests_seen(), [])
        self.assertTrue((self.cwd / 'hello.txt').exists())
        self.p.send(sid, 'edit-outside')          # outside: asks
        req = wait_for(lambda: self.p.requests())
        self.assertEqual(len(req), 1)
        self.p.reply(req[0]['id'], {'decision': 'reject_always'})          # falls back to reject_once
        self.assertTrue(self.ended(sid, 3))
        self.assertFalse(list(self.tmp.glob('outside-*.txt')))

    def test_containment_resolves_symlinks(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (self.cwd / 'link').symlink_to(outside)
        self.assertTrue(acp.contained(self.cwd / 'a' / 'b.txt', [self.cwd]))
        self.assertFalse(acp.contained(self.cwd / 'link' / 'x.txt', [self.cwd]))
        self.assertFalse(acp.contained(str(self.cwd) + '/../outside/x.txt', [self.cwd]))
        self.assertFalse(acp.contained(str(self.cwd) + '-sibling/x.txt', [self.cwd]))


class Question(Base):
    flavor_kw = {'question_prefix': 'interaction_'}

    def test_interaction_request_is_a_question(self):
        sid = self.start()
        self.p.send(sid, 'ask')
        req = wait_for(lambda: self.p.requests())
        r = req[0]
        self.assertEqual(r['method'], 'item/agent/requestUserInput')
        self.assertEqual(r['params']['kind'], 'question')
        self.assertEqual([o['label'] for o in r['params']['questions'][0]['options']], ['Red', 'Blue'])
        self.p.reply(r['id'], {'answers': {'0': ['Blue']}})
        self.assertTrue(self.ended(sid))
        self.assertIn('you chose blue', [m['text'] for m in self.p.read_history(sid)['messages']])


class Stop(Base):
    def test_stop_answers_pending_permission_cancelled(self):
        sid = self.start()
        self.p.send(sid, 'edit')
        req = wait_for(lambda: self.p.requests())
        self.p.stop(sid)
        end = self.ended(sid)
        self.assertEqual(end[0]['stopReason'], 'cancelled')
        resolved = [m for m in self.msgs if m.get('method') == 'serverRequest/resolved']
        self.assertEqual(resolved[0]['params']['requestId'], req[0]['id'])
        out = [r['msg'] for r in self.record(sid) if r['dir'] == 'out' and 'result' in r['msg']]
        self.assertEqual(out[-1]['result'], {'outcome': {'outcome': 'cancelled'}})
        h = self.p.read_history(sid)
        self.assertEqual([e['status'] for e in h['events'] if e['kind'] == 'write'], ['interrupted'])          # failed after Stop
        self.assertEqual(h['messages'][-1]['status'], 'interrupted')


class StopEarlyCancel(Base):
    scen = 'drop-early-cancel'
    flavor_kw = {'recancel_after': 3.0}

    def test_cancel_right_after_prompt_is_sent_again(self):
        sid = self.start()
        t0 = time.time()
        self.p.send(sid, 'slow')
        self.p.stop(sid)
        end = self.ended(sid, timeout=4)
        self.assertTrue(end)
        self.assertEqual(end[0]['stopReason'], 'cancelled')
        self.assertLess(time.time() - t0, 2.5)          # resent on the first update, not after recancel_after
        cancels = [r for r in self.record(sid) if r['dir'] == 'out' and r['msg'].get('method') == 'session/cancel']
        self.assertEqual(len(cancels), 2)
        self.assertTrue(all('id' not in r['msg'] for r in cancels))          # notifications carry no id
        self.assertTrue(self.p.live[sid.split(':', 1)[1]].conn.alive())


class StopIgnored(Base):
    scen = 'ignore-cancel'
    flavor_kw = {'cancel_timeout': 0.5, 'recancel_after': 0.2}

    def test_agent_that_ignores_cancel_is_killed(self):
        sid = self.start()
        s = self.p.live[sid.split(':', 1)[1]]
        self.p.send(sid, 'slow')
        wait_for(lambda: self.p.read_history(sid)['turnActive'])
        t0 = time.time()
        self.p.stop(sid)
        self.assertEqual(self.p.phase(sid), 'stopping')
        end = self.ended(sid, timeout=4)
        self.assertEqual(end[0]['stopReason'], 'cancelled')
        self.assertIn('process was stopped', end[0]['error'])
        self.assertLess(time.time() - t0, 3)
        self.assertTrue(wait_for(lambda: s.conn.proc.poll() is not None))
        # the next message reopens the session in a new process (session/resume? none here: session/load)
        self.assertEqual(self.p.send(sid, 'hello')['how'], 'resumed')
        self.assertTrue(self.ended(sid, 2))


class Load(Base):
    scen = 'load-only'

    def test_load_replay_dropped_when_the_record_has_the_conversation(self):
        sid = self.start()
        self.turn(sid, 'hello')
        self.p.release(sid)
        self.assertNotIn(sid.split(':', 1)[1], self.p.live)
        r = self.p.send(sid, 'second')
        self.assertEqual(r['how'], 'resumed')
        self.assertTrue(self.ended(sid, 2))
        texts = [m['text'] for m in self.p.read_history(sid)['messages']]
        self.assertEqual(texts, ['hello', 'Hello', 'second', 'echo: second'])
        self.assertTrue(any(r['msg'].get('method') == 'session/load' for r in self.record(sid) if r['dir'] == 'out'))

    def test_load_replay_kept_without_a_record(self):
        h = acp.History('/p')
        h.feed('out', {'jsonrpc': '2.0', 'id': 1, 'method': 'session/load', 'params': {'sessionId': 's'}}, 1)
        for i, (kind, text) in enumerate((('user_message_chunk', 'q'), ('agent_message_chunk', 'a'))):
            h.feed('in', {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': {
                'sessionUpdate': kind, 'messageId': 'x%d' % i, 'content': {'type': 'text', 'text': text}}}}, 2)
        h.feed('in', {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': {
            'sessionUpdate': 'agent_message_chunk', '_meta': {'isReplay': True}, 'content': {'type': 'text', 'text': 'dup'}}}}, 2)
        h.feed('in', {'jsonrpc': '2.0', 'id': 1, 'result': {}}, 3)
        self.assertEqual([(m['role'], m['text']) for m in h.c['messages']], [('user', 'q'), ('assistant', 'a')])


class LoadNoReply(Base):
    scen = 'load-noreply'
    flavor_kw = {'load_idle': 0.3, 'load_bound': 3.0}

    def test_load_without_answer_ends_by_idle_rule(self):
        sid = self.start()
        self.turn(sid, 'hello')
        self.p.release(sid)
        t0 = time.time()
        self.p.send(sid, 'again')
        self.assertLess(time.time() - t0, 2.5)
        self.assertTrue(self.ended(sid, 2))
        self.assertTrue(any(r['dir'] == 'note' and r['msg'].get('loaded') for r in self.record(sid)))
        self.assertEqual([m['text'] for m in self.p.read_history(sid)['messages']], ['hello', 'Hello', 'again', 'echo: again'])


class Auth(Base):
    scen = 'auth'

    def test_sign_in_required_is_reported_with_the_hint(self):
        self.p.flavor.auth_hint = 'Run `mock login`.'
        with self.assertRaises(ValueError) as cm:
            self.start()
        self.assertIn('Authentication required', str(cm.exception))
        self.assertIn('mock login', str(cm.exception))


class SilentFailure(Base):
    """A turn the agent ends normally that failed (Flavor.silent_failure, acp.md §13): reported as a failed turn with its class,
    so delivery holds the queue instead of sending into the limit."""
    flavor_kw = {'silent_failure': staticmethod(lambda key, turn: {'class': 'usage_limit', 'resetsAt': 123.0, 'message': 'limit hit'}
                                                if turn['text'] == 'Hello' and not turn['tools'] else None)}

    def test_reported_as_failed_with_class_and_kept_in_history(self):
        sid = self.start()
        end = self.turn(sid, 'hello')
        self.assertEqual((end['stopReason'], end['error'], end['failure']), ('error', 'limit hit', {'class': 'usage_limit', 'resetsAt': 123.0, 'refused': True}))          # refused: plan use (test_planuse)
        h = self.p.read_history(sid)
        self.assertEqual([(e['text'], e['code'], e['resetsAt']) for e in h['errors']], [('limit hit', 'usage_limit', 123.0)])
        self.p.close()
        again = acp.AcpProvider(mock_flavor(self.scen), data_dir=self.data)          # replayed from our record: the same error
        self.assertEqual([e['text'] for e in again.read_history(sid)['errors']], ['limit hit'])

    def test_a_check_that_raises_never_fails_a_turn(self):
        self.p.flavor.silent_failure = lambda key, turn: 1 / 0
        sid = self.start()
        end = self.turn(sid, 'hello')
        self.assertFalse(end.get('failure'))
        self.assertIsNone(end['error'])


class Refusals(unittest.TestCase):
    """A start the agent's service refuses: a web page parsed as data is named as such; an account refused outright is not
    offered again for a while (Gemini CLI 0.66, 2026-11-09)."""

    class Conn:
        signin_url = None

    def test_web_page_parsed_as_data_is_named(self):
        p = acp.AcpProvider(mock_flavor('v1'), data_dir=tempfile.mkdtemp(prefix='acp-ref-'))
        e = p.setup_error(self.Conn(), acp.AcpError(-32603, 'Unexpected token \'<\', "<!DOCTYPE "... is not valid JSON'))
        self.assertIn('answered with a web page', str(e))
        self.assertNotIn('DOCTYPE', str(e))
        self.assertTrue(p.capabilities()['start'])          # not a refusal: the next start may work

    def test_retired_client_not_offered_until_the_hold_passes(self):
        p = acp.AcpProvider(mock_flavor('v1'), data_dir=tempfile.mkdtemp(prefix='acp-ref-'))
        msg = 'This client is no longer supported for Gemini Code Assist for individuals. Please migrate to Antigravity.'
        p.setup_error(self.Conn(), acp.AcpError(-32000, msg))
        self.assertFalse(p.capabilities()['start'])
        av = p.available()
        self.assertFalse(av['canStart'])
        self.assertIn('no longer supported', av['reason'])
        p.refused['at'] -= p.REFUSAL_HOLD + 1
        self.assertTrue(p.capabilities()['start'])


class SilentFailureChecks(unittest.TestCase):
    """The two agents' own checks, on stores and answers shaped like the ones seen on this Mac."""

    def step_db(self, steps):
        import sqlite3
        d = Path(tempfile.mkdtemp(prefix='agy-conv-'))
        self.addCleanup(shutil.rmtree, d, True)
        c = sqlite3.connect(str(d / 'k.db'))
        c.execute('create table steps (idx integer primary key, step_type integer not null default 0, status integer, error_details blob, step_payload blob)')
        c.executemany('insert into steps (idx, step_type, status, step_payload) values (?,?,3,?)', [(i, t, p) for i, (t, p) in enumerate(steps)])
        c.commit()
        c.close()
        return (str(d),)

    QUOTA = (b'{"error": {"code": 429, "details": [{"metadata": {"quotaResetTimeStamp": "2026-09-18T07:38:43Z"}, "reason": "QUOTA_EXHAUSTED"},'
             b' {"retryDelay": "16179.79s"}], "message": "Individual quota reached. Resets in 4h29m39s.", "status": "RESOURCE_EXHAUSTED"}}')

    def test_antigravity_quota_as_the_last_step(self):
        from providers.acp_flavors import antigravity_quota
        import calendar
        got = antigravity_quota('k', {}, stores=self.step_db([(15, b'ok'), (17, self.QUOTA)]))
        self.assertEqual(got['class'], 'usage_limit')
        self.assertEqual(got['resetsAt'], calendar.timegm((2026, 9, 18, 7, 38, 43, 0, 0, 0)))
        self.assertIn('Individual quota reached', got['message'])

    def test_antigravity_text_form_and_recovery(self):
        from providers.acp_flavors import antigravity_quota
        text = b'API error (attempt 2): RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1h2m3s.'
        got = antigravity_quota('k', {}, stores=self.step_db([(17, text)]), now=1000.0)
        self.assertEqual(got['resetsAt'], 1000.0 + 3723)
        self.assertIsNone(antigravity_quota('k', {}, stores=self.step_db([(17, text), (15, b'answered after the retry')])))
        self.assertIsNone(antigravity_quota('k', {}, stores=self.step_db([(17, b'some other error')])))
        self.assertIsNone(antigravity_quota('missing', {}, stores=('/nonexistent',)))

    def test_cursor_plan_refusal_only_when_it_is_the_whole_answer(self):
        from providers.acp_flavors import cursor_plan_refusal
        self.assertEqual(cursor_plan_refusal('k', {'text': '\n\nUpgrade your plan to continue', 'tools': 0})['class'], 'usage_limit')
        self.assertIsNone(cursor_plan_refusal('k', {'text': 'Upgrade your plan to continue', 'tools': 2}))
        self.assertIsNone(cursor_plan_refusal('k', {'text': 'To keep going, upgrade your plan to continue using it.', 'tools': 0}))


class SignIn(unittest.TestCase):
    """The agent's own sign-in command run from the page: its link comes back, its end is published; no credential is handled."""

    def provider(self, code, **kw):
        self.msgs = []
        fl = Flavor(name='mock', label='Mock agent', binary=sys.executable, login_args=('-c', code), **kw)
        return acp.AcpProvider(fl, publish=self.msgs.append, data_dir=tempfile.mkdtemp(prefix='acp-signin-'))

    def test_link_returned_and_end_published(self):
        p = self.provider("import os,time; print('Open https://login.example.test/a?b=1. to continue', flush=True); "
                          "print('BROWSER=' + os.environ.get('BROWSER', ''), flush=True); time.sleep(0.3)")
        r = p.sign_in(wait=5)
        self.assertEqual(r['url'], 'https://login.example.test/a?b=1')
        end = wait_for(lambda: [m['params'] for m in self.msgs if m.get('method') == 'provider/signin'], 5)
        self.assertEqual((end[0]['provider'], end[0]['ok']), ('mock', True))
        self.assertIn('BROWSER=/bin/echo', end[0]['tail'])          # the agent's own browser launch is caught, not opened

    def test_failed_sign_in_and_no_command(self):
        p = self.provider("import sys; print('no network'); sys.exit(3)")
        r = p.sign_in(wait=5)
        self.assertIsNone(r['url'])
        end = wait_for(lambda: [m['params'] for m in self.msgs if m.get('method') == 'provider/signin'], 5)
        self.assertFalse(end[0]['ok'])
        self.assertIn('no network', end[0]['tail'])
        q = acp.AcpProvider(mock_flavor('v1', auth_hint='Run `mock login`.'), data_dir=tempfile.mkdtemp(prefix='acp-signin-'))
        with self.assertRaises(ValueError) as cm:
            q.sign_in()
        self.assertIn('mock login', str(cm.exception))

    def test_pasted_code_reaches_the_sign_in_and_nothing_else(self):
        # shaped like agy 1.5.3's Google flow (seen 2026-11-09): link, then a prompt for the code on stdin
        p = self.provider("import sys; print('Open this link: https://accounts.example.test/o?x=1', flush=True); "
                          "print('Then paste the authorization code, or the full URL of the page you land on, and press Enter:', flush=True); "
                          "c = sys.stdin.readline().strip(); print('ok' if c == 'CODE-42' else 'bad'); sys.exit(0 if c == 'CODE-42' else 4)",
                          login_code_prompt=r'paste the authorization code', login_opens_browser=True)
        r = p.sign_in(wait=5)
        self.assertEqual((r['needsCode'], r['opensBrowser'], r['running']), (True, True, True))
        with self.assertRaises(ValueError):
            p.sign_in_code('two\nlines')
        r = p.sign_in_code('CODE-42')
        self.assertEqual((r['running'], r['exit'], r['needsCode']), (False, 0, False))
        end = wait_for(lambda: [m['params'] for m in self.msgs if m.get('method') == 'provider/signin'], 5)
        self.assertTrue(end[0]['ok'])
        self.assertNotIn('CODE-42', end[0]['tail'])
        self.assertTrue(p.available()['signedIn'])
        with self.assertRaises(ValueError):          # nothing waiting any more
            p.sign_in_code('CODE-42')

    def test_status_command_shows_the_sign_in(self):
        for out, want in (("print('\u2713 Logged in as someone')", True), ("print('Not logged in')", False)):
            fl = Flavor(name='mock', label='Mock agent', binary=sys.executable, status_args=('-c', out), status_ok=r'logged in as')
            acp.LOOKS.pop('mock', None)          # looks are kept per agent name for the process: each case looks afresh
            p = acp.AcpProvider(fl, data_dir=tempfile.mkdtemp(prefix='acp-status-'))
            self.assertIsNone(p.sign_in_status(sys.executable))          # never blocks: the first look answers later
            self.assertEqual(wait_for(lambda: acp.LOOKS['mock']['signed'] is not None and [p.sign_in_status(sys.executable)], 10), [want])
            self.assertTrue(acp.LOOKS['mock']['version'] is not None)          # --version looked at on the same thread
        self.assertEqual(CURSOR.status_args, ('status',))
        self.assertEqual(ANTIGRAVITY.status_args, ())          # agy 1.5.3 has no status command

    def test_no_code_box_for_a_browser_only_sign_in(self):
        p = self.provider("print('Then paste the authorization code', flush=True)")          # no login_code_prompt: stdin is closed
        r = p.sign_in(wait=5)
        wait_for(lambda: p._login['proc'].poll() is not None, 5)
        self.assertFalse(p._login_state(p._login)['needsCode'])
        with self.assertRaises(ValueError):
            p.sign_in_code('x')

    def test_agents_have_their_sign_in_commands(self):
        self.assertEqual((ANTIGRAVITY.login_args, CURSOR.login_args), (('acp', '--login'), ('login',)))
        self.assertTrue(ANTIGRAVITY.login_opens_browser and ANTIGRAVITY.login_code_prompt)
        self.assertFalse(CURSOR.login_code_prompt)


class Subagents(unittest.TestCase):
    """Antigravity's children, from its own files laid out as seen live (agy 1.5.3, 2026-11-09), shown read only under the parent."""

    def brain(self, parent='P1', child='C1', final=True):
        root = Path(tempfile.mkdtemp(prefix='agy-brain-'))
        self.addCleanup(shutil.rmtree, root, True)
        g = root / child / '.system_generated'
        (g / 'messages').mkdir(parents=True)
        (g / 'logs').mkdir()
        (g / 'messages' / 'm1.json').write_text(json.dumps({
            'id': 'm1', 'recipient': child, 'sender': parent, 'timestamp': '2026-11-10T03:07:08.1Z', 'content': 'read a.txt',
            'renderDetails': {'messageTitle': 'Message from Parent Agent'},
            'sourceMetadata': {'tool': {'conversationId': parent, 'toolCall': {'id': 'call_9', 'name': 'invoke_subagent',
                                                                              'argumentsJson': json.dumps({'Prompt': 'read a.txt', 'Role': 'Reader', 'TypeName': 'research'})}}}}))
        steps = [{'step_index': 0, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'created_at': '2026-11-10T03:07:09Z', 'thinking': 'reading',
                  'tool_calls': [{'name': 'read_file', 'args': {'Path': '/x/a.txt'}}]},
                 {'step_index': 1, 'source': 'MODEL', 'type': 'GENERIC', 'created_at': '2026-11-10T03:07:10Z', 'content': '1: alpha'}]
        if final:
            steps.append({'step_index': 2, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'created_at': '2026-11-10T03:07:11Z', 'content': 'a.txt says alpha'})
        (g / 'logs' / 'transcript_full.jsonl').write_text('\n'.join(json.dumps(x) for x in steps) + '\n')
        return (str(root),)

    def test_children_found_and_read(self):
        from providers.acp_flavors import antigravity_subagents, antigravity_subagent_history
        roots = self.brain()
        kids = antigravity_subagents('P1', roots=roots)
        self.assertEqual([(k['key'], k['toolCallId'], k['role'], k['type'], k['running']) for k in kids], [('C1', 'call_9', 'Reader', 'research', False)])
        self.assertEqual(antigravity_subagents('someone-else', roots=roots), [])
        h = antigravity_subagent_history('C1', roots=roots)
        self.assertEqual([(m['role'], m.get('text') or m.get('tool')) for m in h['messages']],
                         [('user', 'read a.txt'), ('tool', 'read_file /x/a.txt'), ('assistant', 'a.txt says alpha')])
        self.assertEqual(h['reasoning'][0]['text'], 'reading')

    def test_unfinished_child_is_running_while_fresh(self):
        from providers.acp_flavors import antigravity_subagent_history
        roots = self.brain(final=False)
        self.assertTrue(antigravity_subagent_history('C1', roots=roots)['running'])
        self.assertFalse(antigravity_subagent_history('C1', roots=roots, now=time.time() + 600)['running'])

    def test_provider_lists_children_read_only(self):
        tmp = Path(tempfile.mkdtemp(prefix='acp-kids-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        kid = {'key': 'C1', 'toolCallId': 'call_9', 'role': 'Reader', 'type': 'research', 'started': 1.0, 'updated': 2.0, 'running': True}
        fl = mock_flavor('v1', subagents=lambda key, since=None: [kid] if key == 'P1' else [],
                         subagent_history=lambda key: {'messages': [{'role': 'assistant', 'text': 'hi', 't': 1.0, 'id': 'a'}], 'reasoning': [], 'running': True})
        p = acp.AcpProvider(fl, data_dir=tmp)
        (tmp / 'acp' / 'mock').mkdir(parents=True)
        (tmp / 'acp' / 'mock' / 'P1.json').write_text(json.dumps({'cwd': str(tmp), 'started': 0.5}))
        rows = {r['id']: r for r in p.list_sessions()}
        self.assertEqual((rows['mock:C1']['parent'], rows['mock:C1']['title'], rows['mock:C1']['running']), ('mock:P1', 'Reader · research', True))
        self.assertTrue(p.owns('mock:P1'))
        self.assertFalse(p.owns('mock:C1'))          # shown, never driven
        h = p.read_history('mock:C1')
        self.assertEqual(([m['text'] for m in h['messages']], h['parent'], h['turnActive']), (['hi'], 'mock:P1', True))


class Flavors(unittest.TestCase):
    def test_not_installed(self):
        f = Flavor(**{**CURSOR.__dict__, 'binary': 'cursor-agent-not-here-xyz', 'search': ()})
        a = acp.AcpProvider(f, data_dir=tempfile.mkdtemp()).available()
        self.assertFalse(a['found'])
        self.assertEqual(a['reason'], 'not installed')
        self.assertIn('cursor.com/install', a['hint'])

    def test_flavors_do_not_set_yolo_on_antigravity(self):
        self.assertIn('yolo', ANTIGRAVITY.refused_modes)
        self.assertEqual(ANTIGRAVITY.page_modes['full-access'], AUTO)
        self.assertNotIn('gemini', [f.name for f in FLAVORS])          # Gemini CLI removed 2026-11-22 (retired)
        self.assertEqual(CURSOR.argv('/x/cursor-agent', 'full-access'), ['/x/cursor-agent', '--force', 'acp'])

    def test_generic_code_never_names_an_agent(self):
        src = (HERE / 'providers' / 'acp.py').read_text().split('"""', 2)[2]          # past the module docstring
        code = '\n'.join(ln.split('#')[0] for ln in src.splitlines()).lower()          # comments may cite agents; code may not
        for name in ('antigravity', 'gemini', 'cursor', 'agy', 'kiro'):
            self.assertNotIn(name, code)

    def test_redact(self):
        s = acp.redact('Bearer abcdefghijklmnop key=AIzaSyA1234567890abcdefghijk url https://accounts.google.com/o/oauth2?x=1 '
                       + str(Path.home()) + '/x "access_token": "ya29.zzzzzzzzzzzz"')
        self.assertNotIn('abcdefghijklmnop', s)
        self.assertNotIn('AIzaSy', s)
        self.assertNotIn('accounts.google.com/o', s)
        self.assertNotIn('ya29.zz', s)
        if str(Path.home()) != '/':
            self.assertIn('~/x', s)


class Recorded(unittest.TestCase):
    """Frames recorded from real agents (home paths as ~, token-like values redacted), replayed through the same mapper."""

    def files(self, flavor):
        return sorted((RECORDED / flavor).glob('*.ndjson')) if (RECORDED / flavor).is_dir() else []

    def history(self, path):
        side = path.with_suffix('.json')
        cwd = json.loads(side.read_text())['cwd'] if side.exists() else None
        return acp.replay(path, cwd), [json.loads(x) for x in path.read_text().splitlines()]

    def test_antigravity_live_checks(self):
        files = self.files('antigravity')
        if not files:
            self.skipTest('no Antigravity recording')
        for path in files:
            h, frames = self.history(path)
            label = json.loads(path.with_suffix('.json').read_text()).get('check')
            c = h.c
            self.assertEqual(h.gen, 1, path.name)
            self.assertFalse(c['turnActive'])
            if label == 'ok':
                self.assertEqual(c['stopReason'], 'end_turn')
                said = [m['text'] for m in c['messages'] if m['role'] == 'assistant']
                self.assertTrue(any('ok' in t.lower() for t in said), said)
            elif label == 'write':
                self.assertTrue(any(f['dir'] == 'in' and f['msg'].get('method') == 'session/request_permission' for f in frames))
                self.assertTrue([e for e in c['events'] if e['kind'] == 'write' and e['path'] == 'hello.txt'], c['events'])
                self.assertTrue([e for e in c['events'] if e['kind'] == 'ask' and e.get('answer')])
            elif label == 'stop':
                self.assertEqual(c['stopReason'], 'cancelled')
                self.assertTrue(any(f['dir'] == 'out' and f['msg'].get('method') == 'session/cancel' for f in frames))
                run = [e for e in c['events'] if e['kind'] == 'run']
                self.assertEqual([e['status'] for e in run], ['interrupted'])          # reported failed after our cancel
                self.assertIn('sleep 1', run[0]['text'])          # the command from rawInput.CommandLine
            self.assertEqual(len([m for m in c['messages'] if m['role'] == 'user']), 1)          # its user_message_chunk echo dropped
            # every frame passes the line rule: nothing in a recording is left unread by the mapper beyond the skipped count
            self.assertLessEqual(c['unknown'], 5)


if __name__ == '__main__':
    unittest.main()
