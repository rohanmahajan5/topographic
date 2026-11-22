"""The OpenCode provider (providers/opencode.py) against a fake OpenCode 2 server (providers/fixtures/opencode/fake_opencode.py)
playing event streams cut from T3 Code's recorded OpenCode 2.0.18 sessions. No OpenCode install and no model usage.
One test per situation the brief and T3's fix history name (docs/providers-design.md "OpenCode")."""
import http.client
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

from providers import core
from providers import opencode as oc

FAKE = str(Path(__file__).resolve().parent / 'providers' / 'fixtures' / 'opencode' / 'fake_opencode.py')


def wait(cond, timeout=20.0):          # generous: returns as soon as it holds; a loaded machine is slow, not wrong
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class Harness(unittest.TestCase):
    modes = ''
    provider_args = {}

    def setUp(self):
        self.events, self.elock = [], threading.Lock()
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.path.realpath(self.tmp.name)
        env = dict(os.environ, FAKE_OPENCODE=self.modes, OPENCODE_SERVER_PASSWORD='inherited-should-be-replaced')
        args = dict(reconnect_delay=0.05, stop_wait=3.0, startup_timeout=oc.STARTUP_TIMEOUT, data_dir=os.path.join(self.cwd, 'no-store'))
        args.update(self.provider_args)
        self.p = oc.OpenCodeProvider(publish=self.collect, binary=[sys.executable, '-u', FAKE], env=env, **args)

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def collect(self, msg):
        with self.elock:
            self.events.append(msg)

    def seen(self, method, pred=lambda p: True):
        with self.elock:
            return [m['params'] for m in self.events if m.get('method') == method and pred(m['params'])]

    def ctl(self, what, body=None):
        h = self.p.server.http
        conn = http.client.HTTPConnection(h.host, h.port, timeout=5)
        conn.request('POST', '/__test/' + what, body=json.dumps(body or {}), headers={'Content-Type': 'application/json'})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return json.loads(data or b'null')

    def log(self):
        return self.ctl('log')

    def ended(self, tid, n=1, timeout=20.0):
        ok = wait(lambda: len(self.seen('provider/turn', lambda p: p['threadId'] == tid and p.get('ended'))) >= n, timeout)
        self.assertTrue(ok, 'turn did not end: %s' % self.events[-5:])
        return self.seen('provider/turn', lambda p: p['threadId'] == tid and p.get('ended'))[n - 1]

    def turn(self, scenario):
        tid = self.p.start(self.cwd)['threadId']
        self.p.send(tid, 'scenario:' + scenario)
        return tid


class Startup(Harness):
    def test_startup_and_auth(self):
        self.p.ensure()
        self.assertTrue(self.p.server.alive())
        self.assertEqual(self.p.server.version, '2.0.28')
        info = [r for r in self.log() if r['path'] == '/api/info']
        self.assertTrue(info and all(r['auth'] for r in info))
        env = self.ctl('env')
        self.assertEqual(env['OPENCODE_PASSWORD'], 'set')
        self.assertTrue(env['same'])          # our own password under both names, the inherited one replaced
        wrong = oc.Http(self.p.server.http.host, self.p.server.http.port, 'not-the-password')
        with self.assertRaises(oc.HttpError) as e:
            wrong.call('GET', '/api/info')
        self.assertEqual(e.exception.status, 401)
        self.assertIn('rejected the server password', str(e.exception))
        self.assertTrue(self.p.available()['found'])
        core.check_capabilities(self.p.capabilities())
        pgid = os.getpgid(self.p.server.proc.pid)
        inner = self.p.server.inner
        self.assertNotEqual(pgid, os.getpgid(0))          # its own process group
        self.assertNotEqual(os.getpgid(inner), os.getpgid(0))          # and OpenCode in one of its own under the wrapper
        self.p.close()
        self.assertTrue(wait(lambda: self._gone(pgid) and self._gone(inner)))

    @staticmethod
    def _gone(pgid):
        try:
            os.killpg(pgid, 0)
            return False
        except ProcessLookupError:
            return True


class Locked(Harness):
    def setUp(self):
        self.flag = tempfile.mktemp()
        self.modes = 'locked=' + self.flag
        super().setUp()

    def test_database_lock_at_start_is_retried(self):          # T3 b058babb1
        self.p.ensure()
        self.assertTrue(os.path.exists(self.flag))
        self.assertTrue(self.p.server.alive())
        os.unlink(self.flag)


class DiesWithStarter(unittest.TestCase):
    """The OpenCode server goes when the process that started it is killed outright (no atexit, no close): one left behind
    keeps working the same sessions beside the next one."""
    def test_server_ends_when_its_starter_is_killed(self):
        import subprocess
        here = str(Path(__file__).resolve().parent)
        code = ('import os, sys, time; sys.path.insert(0, %r)\n'
                'from providers import opencode as oc\n'
                'p = oc.OpenCodeProvider(binary=[sys.executable, "-u", %r], data_dir=%r)\n'
                'p.ensure(); print(p.server.proc.pid, flush=True); time.sleep(60)\n') % (here, FAKE, tempfile.mkdtemp())
        starter = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, cwd=here)
        try:
            wrapper = int(starter.stdout.readline())
            inner = []
            self.assertTrue(wait(lambda: inner.extend(int(x) for x in subprocess.run(['pgrep', '-P', str(wrapper)], capture_output=True, text=True).stdout.split()) or inner, 5))
            starter.kill()
            starter.wait()
            gone = lambda pid: subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True).stdout.strip() in ('', 'Z')
            self.assertTrue(wait(lambda: gone(inner[0]) and gone(wrapper), 10), 'OpenCode server outlived its starter')
        finally:
            starter.kill()


class NotInstalled(unittest.TestCase):
    def test_not_installed(self):
        p = oc.OpenCodeProvider(env={'PATH': '/nonexistent', 'HOME': tempfile.gettempdir()}, data_dir='/nonexistent')
        a = p.available()
        self.assertFalse(a['found'])
        self.assertIn('opencode.ai/v2/install', a['install'])
        caps = core.check_capabilities(p.capabilities())
        self.assertFalse(caps['start'])
        with self.assertRaises(RuntimeError):
            p.start('/tmp')

    def test_installed_while_running_is_found_at_the_next_look(self):
        home = tempfile.mkdtemp()
        p = oc.OpenCodeProvider(env={'PATH': '/nonexistent', 'HOME': home}, data_dir='/nonexistent')
        self.assertFalse(p.available()['found'])
        os.makedirs(os.path.join(home, '.opencode', 'bin'))
        binp = os.path.join(home, '.opencode', 'bin', 'opencode')
        with open(binp, 'w') as f:
            f.write('#!/bin/sh\necho "opencode v2.2.6"\n')
        os.chmod(binp, 0o755)
        self.assertFalse(p.available()['found'])          # looked a moment ago: not again yet
        p._probe_at -= p.MISSING_RECHECK + 1
        a = p.available()
        self.assertTrue(a['found'], a)
        self.assertEqual((a['binary'], a['version']), (binp, '2.2.6'))

    def test_opencode_1_is_found_but_refused(self):
        p = oc.OpenCodeProvider(binary=[sys.executable, '-u', FAKE], env=dict(os.environ, FAKE_OPENCODE='v1'), data_dir='/nonexistent')
        a = p.available()          # the first look never waits on the version: found, being checked
        self.assertTrue(a.get('checking'), a)
        for _ in range(100):
            if p._probe is not None:
                break
            time.sleep(0.05)
        a = p.available()
        self.assertFalse(a['found'])
        self.assertEqual(a['version'], '1.14.39')
        self.assertIn('OpenCode 2', a['reason'])


class Turns(Harness):
    def test_turn_with_text_tools_and_diff(self):
        tid = self.turn('tool_call')
        self.assertEqual(self.ended(tid)['status'], 'completed')
        self.assertEqual(self.seen('provider/turn', lambda p: p.get('started'))[0]['threadId'], tid)
        self.assertEqual(self.p.phase(tid), 'idle')
        h = self.p.read_history(tid)
        roles = [m['role'] for m in h['messages']]
        self.assertEqual(roles[0], 'user')
        self.assertIn('assistant', roles)
        tools = {m['tool']: m for m in h['messages'] if m['role'] == 'tool'}
        self.assertEqual(tools['shell']['kind'], 'run')
        self.assertEqual(tools['read']['kind'], 'read')
        self.assertTrue(h['context'] > 0)
        self.assertFalse(h['turnActive'])
        # a written file: an event on the floor with its diff
        self.p.send(tid, 'scenario:write_file')
        self.ended(tid, 2)
        h = self.p.read_history(tid)
        w = [e for e in h['events'] if e['kind'] == 'write']
        self.assertEqual(w[-1]['path'], 'reverted.txt')
        self.assertEqual(w[-1]['diff'], {'old': None, 'new': 'ALPHA\n'})
        # an edit that deletes text keeps its empty replacement (T3 28aed0dcb); a non-zero exit is failed (T3 0992f5169)
        self.p.send(tid, 'scenario:edit_delete')
        self.ended(tid, 3)
        h = self.p.read_history(tid)
        edit = [e for e in h['events'] if e.get('tool') == 'edit'][-1]
        self.assertEqual(edit['diff'], {'old': 'DELETE ME\n', 'new': ''})
        self.assertEqual(edit['path'], 'notes.txt')
        self.assertEqual([m for m in h['messages'] if m.get('tool') == 'shell'][-1]['status'], 'failed')
        # sessions list with this folder
        rows = self.p.list_sessions(self.cwd)
        self.assertEqual([r['id'] for r in rows], [tid])
        self.assertFalse(rows[0]['external'])
        self.assertEqual(self.p.list_sessions('/somewhere/else'), [])

    def test_permission_ask_and_reply(self):
        tid = self.turn('permission')
        self.assertTrue(wait(lambda: self.p.requests()))
        req = self.p.requests()[0]
        self.assertEqual(req['method'], 'item/agent/requestApproval')
        self.assertEqual(req['params']['kind'], 'command')
        self.assertEqual(req['params']['threadId'], tid)
        ids = [o['id'] for o in req['params']['options']]
        self.assertEqual(ids, ['once', 'always', 'reject'])          # the agent's own decision ids; 'always' since it offers patterns
        self.assertEqual(self.p.phase(tid), 'running')
        self.p.reply(req['id'], {'optionId': 'once'})
        sent = [r for r in self.log() if r['path'].endswith('/reply')]
        self.assertEqual(sent[-1]['body'], {'decision': 'once'})
        self.assertTrue(self.seen('serverRequest/resolved', lambda p: p['requestId'] == req['id']))
        self.assertEqual(self.p.requests(), [])
        self.assertEqual(self.ended(tid)['status'], 'completed')
        with self.assertRaises(KeyError):
            self.p.reply(req['id'], {'optionId': 'once'})

    def test_question_form(self):
        tid = self.turn('question')
        self.assertTrue(wait(lambda: self.p.requests()))
        req = self.p.requests()[0]
        self.assertEqual(req['method'], 'item/agent/requestUserInput')
        q = req['params']['questions'][0]
        self.assertEqual((q['id'], q['multiSelect']), ('q0', False))
        self.p.reply(req['id'], {'answers': {'q0': ['Blue']}})
        sent = [r for r in self.log() if r['path'].endswith('/reply')]
        self.assertEqual(sent[-1]['body'], {'answer': {'q0': 'Blue'}})
        self.assertEqual(self.ended(tid)['status'], 'completed')

    def test_abort_during_a_tool(self):
        tid = self.turn('interrupt')
        self.assertTrue(wait(lambda: self.seen('provider/activity', lambda p: p['threadId'] == tid and p.get('tool') == 'shell')))
        self.assertEqual(self.p.stop(tid), {'settled': 'confirmed'})
        self.assertEqual(self.ended(tid)['status'], 'interrupted')
        h = self.p.read_history(tid)
        self.assertEqual([m for m in h['messages'] if m.get('tool') == 'shell'][-1]['status'], 'interrupted')          # T3 43dfdcc84
        self.assertEqual(self.p.phase(tid), 'idle')
        self.assertEqual(self.p.stop(tid), {'settled': 'idle'})

    def test_stop_reaches_running_child_sessions(self):
        tid = self.turn('interrupt')
        root = oc.OpenCodeProvider.native(tid)
        h = self.p.server.http
        child = h.call('POST', '/api/session', {'parentID': root, 'location': {'directory': self.cwd}})['data']['id']
        grand = h.call('POST', '/api/session', {'parentID': child, 'location': {'directory': self.cwd}})['data']['id']
        idle = h.call('POST', '/api/session', {'parentID': root, 'location': {'directory': self.cwd}})['data']['id']
        for c in (child, grand):
            h.call('POST', '/api/session/%s/prompt' % c, {'text': 'scenario:interrupt'})
        self.assertTrue(wait(lambda: self.seen('provider/activity', lambda p: p['threadId'] == tid and p.get('tool') == 'shell')))
        self.assertTrue(wait(lambda: {child, grand} <= set(h.call('GET', '/api/session/active')['data'])))
        r = self.p.stop(tid)
        self.assertEqual(r['settled'], 'confirmed')
        self.assertEqual(sorted(r['children']), sorted([oc.PREFIX + child, oc.PREFIX + grand]))
        hit = [x['path'] for x in self.log() if x['path'].endswith('/interrupt')]
        self.assertNotIn('/api/session/%s/interrupt' % idle, hit)          # only running ones are interrupted

    def test_retry_notice_and_unknown_events(self):
        tid = self.turn('retry')
        self.assertTrue(wait(lambda: self.seen('provider/activity', lambda p: p.get('retry'))))
        r = self.seen('provider/activity', lambda p: p.get('retry'))[0]['retry']
        self.assertEqual((r['attempt'], r['delayMs'], r['code']), (1, 2000, 'provider.rate-limit'))
        self.assertIn('Rate limit exceeded', r['message'])
        self.assertTrue(wait(lambda: '<unreadable>' in self.p.skipped))
        self.assertIn('session.future.thing', self.p.skipped)          # skipped, not fatal
        self.assertEqual(self.p.read_history(tid)['retry']['attempt'], 1)
        self.ctl('release')
        self.assertEqual(self.ended(tid)['status'], 'completed')
        self.assertTrue(self.seen('provider/activity', lambda p: 'retry' in p and p['retry'] is None))          # the retry went through
        self.assertEqual(self.p.read_history(tid)['context'], 120 + 900 + 10)

    def test_steer_joins_the_running_turn(self):
        tid = self.turn('retry')
        self.assertTrue(wait(lambda: self.p.phase(tid) == 'running'))
        self.assertEqual(core.route('auto', self.p.phase(tid), self.p.capabilities(tid)), 'steer')
        self.p.send(tid, 'also say hi', how='steer')
        prompt = [r for r in self.log() if r['path'].endswith('/prompt')][-1]
        self.assertEqual(prompt['body']['delivery'], 'steer')
        self.ctl('release')
        self.ended(tid)
        self.assertEqual(len(self.seen('provider/turn', lambda p: p['threadId'] == tid and p.get('ended'))), 1)
        self.assertIn('also say hi', [m['text'] for m in self.p.read_history(tid)['messages']])

    def test_fork(self):
        tid = self.turn('tool_call')
        self.ended(tid)
        fid = self.p.fork(tid)['threadId']
        self.assertNotEqual(fid, tid)
        self.assertTrue(wait(lambda: self.seen('provider/activity', lambda p: p['threadId'] == fid and p.get('forkedFrom') == tid)))
        self.assertTrue(self.p.owns(fid))
        rows = {r['id']: r for r in self.p.list_sessions(self.cwd)}
        self.assertEqual(rows[fid]['copyOf'], tid)
        self.assertEqual(len(self.p.read_history(fid)['messages']), len(self.p.read_history(tid)['messages']))
        self.assertTrue(self.p.capabilities(fid)['fork_at_turn'])

    def test_unowned_session_is_watch_only(self):
        self.p.ensure()
        self.assertFalse(self.p.capabilities('opencode:ses_elsewhere')['send'])
        with self.assertRaises(PermissionError):
            self.p.send('opencode:ses_elsewhere', 'hi')

    def test_options(self):
        tid = self.p.start(self.cwd, model='opencode/space-bunny-free', effort='high', mode='plan')['threadId']
        created = [r for r in self.log() if r['path'] == '/api/session' and r['method'] == 'POST'][-1]['body']
        self.assertEqual(created['model'], {'providerID': 'opencode', 'id': 'space-bunny-free', 'variant': 'high'})
        self.assertEqual(created['agent'], 'plan')
        o = self.p.options(tid)
        self.assertEqual(o['efforts'], ['low', 'medium', 'high'])
        self.assertEqual([m['id'] for m in o['modes']], ['build', 'plan'])
        self.assertEqual(self.p.capabilities(tid)['context'], 'window')


class Reconnect(Harness):
    provider_args = {'reconnect_attempts': 400}

    def test_dropped_stream_reconnects_and_finds_the_lost_end(self):          # T3 cb7d07393
        tid = self.turn('retry')
        self.assertTrue(wait(lambda: self.p.phase(tid) == 'running'))
        self.ctl('refuse')
        self.ctl('drop')
        self.assertTrue(wait(lambda: not self.p.connected.is_set()))
        self.ctl('release')          # the turn ends while nobody listens: its end event is lost
        time.sleep(0.3)
        self.assertEqual(self.p.phase(tid), 'running')
        self.ctl('accept')
        self.assertEqual(self.ended(tid)['status'], 'completed')
        self.assertTrue(wait(self.p.connected.is_set))
        self.p.send(tid, 'again')          # the stream works after the reconnect
        self.ended(tid, 2)


class ServerDeath(Harness):
    provider_args = {'reconnect_attempts': 3}

    def test_server_death_fails_turns_and_requests(self):
        tid = self.turn('permission')
        self.assertTrue(wait(lambda: self.p.requests()))
        os.killpg(self.p.server.inner, signal.SIGKILL)          # OpenCode itself dies (the wrapper then exits)
        end = self.ended(tid)
        self.assertEqual((end['status'], end['failure']['class']), ('failed', 'transport_error'))
        self.assertEqual(self.p.requests(), [])
        self.assertTrue(self.seen('serverRequest/resolved', lambda p: 'not_resumable' in (p.get('reason') or '')))
        tid2 = self.p.start(self.cwd)['threadId']          # the next call starts a fresh server
        self.p.send(tid2, 'hello')
        self.assertEqual(self.ended(tid2)['status'], 'completed')


class HeldEnd(unittest.TestCase):
    def test_execution_end_waits_for_undelivered_steer(self):          # T3 v2/adapter.ts endExecution
        out = []
        p = oc.OpenCodeProvider(publish=out.append, data_dir='/nonexistent')
        s = p.live('ses_x')
        s.owned = True
        s.steers.add('msg_aide_1')
        feed = lambda t, **d: p._handle(json.dumps({'type': t, 'created': 1, 'data': {'sessionID': 'ses_x', **d}}))
        feed('session.execution.started')
        feed('session.execution.succeeded')
        self.assertEqual([m for m in out if m['params'].get('ended')], [])
        self.assertEqual(s.phase, 'running')
        feed('session.execution.started')
        feed('session.inbox.delivered', inboxID='msg_aide_1')
        feed('session.execution.succeeded')
        ends = [m['params'] for m in out if m['params'].get('ended')]
        self.assertEqual([e['status'] for e in ends], ['completed'])


class Pure(unittest.TestCase):
    def test_failure_classes(self):
        self.assertEqual(oc.classify({'type': 'provider.quota', 'status': 429, 'message': 'x'})['class'], 'usage_limit')
        self.assertEqual(oc.classify({'type': 'provider.quota', 'status': 402, 'message': 'no funds'})['class'], 'provider_error')          # T3 2f021ee7d
        c = oc.classify({'type': 'provider.unknown', 'message': "You've hit your weekly limit · Resets at 2026-11-10T08:00:00.000Z."})
        self.assertEqual((c['class'], c['resetsAt']), ('usage_limit', '2026-11-10T08:00:00.000Z'))          # T3 6f4136480
        self.assertEqual(oc.readable_error('<none>', 503, '<html><title>Service Unavailable</title></html>'), 'Service Unavailable (HTTP 503).')

    def test_tool_rendering(self):
        self.assertEqual(oc.clean_output('read', '<path>/a.txt</path>\n<type>file</type>\n<content>\n1: hi\n</content>\nnote'), '1: hi\nnote')
        self.assertEqual(oc.tool_status('shell', {'status': 'completed', 'metadata': {'exit': 2}}), 'failed')
        self.assertEqual(oc.tool_status('read', {'status': 'error', 'error': 'Tool execution aborted'}), 'interrupted')
        self.assertEqual(oc.kind_of('subagent'), 'delegate')
        form = {'fields': [{'key': 'q0', 'type': 'multiselect', 'options': [{'label': 'Red', 'value': 'red'}]}]}
        self.assertEqual(oc.form_answer(form, {'q0': ['Red', 'green']}), {'q0': ['red', 'green']})

    def test_sse_frames(self):
        import io
        raw = io.BytesIO(b': ping\n\ndata: {"a":\ndata: 1}\n\nevent: x\ndata: two\n\n')
        self.assertEqual(list(oc.sse_frames(raw)), [None, '{"a":\n1}', 'two'])

    def test_storage_read_only_without_a_server(self):
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, 'opencode.db'))
            db.execute('CREATE TABLE session (id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INT, time_updated INT)')
            db.execute('CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, data TEXT, time_created INT)')
            db.execute("INSERT INTO session VALUES ('ses_a', 'stored', '/work/p', NULL, 1790000000000, 1790000001000)")
            db.execute("INSERT INTO session_message VALUES ('msg_1', 'ses_a', 'user', ?, 1790000000000)", (json.dumps({'text': 'hi'}),))
            db.execute("INSERT INTO session_message VALUES ('msg_2', 'ses_a', 'assistant', ?, 1790000000500)",
                       (json.dumps({'content': [{'type': 'text', 'text': 'hello'}], 'tokens': {'input': 5, 'cache': {'read': 7, 'write': 0}}}),))
            db.commit()
            db.close()
            p = oc.OpenCodeProvider(env={'PATH': '/nonexistent'}, data_dir=d)
            rows = p.list_sessions('/work')
            self.assertEqual([(r['id'], r['title'], r['cwd']) for r in rows], [('opencode:ses_a', 'stored', '/work/p')])
            h = p.read_history('opencode:ses_a')
            self.assertEqual([(m['role'], m['text']) for m in h['messages']], [('user', 'hi'), ('assistant', 'hello')])
            self.assertEqual(h['context'], 12)


    def test_storage_session_v2_as_in_opencode_2_2_6(self):
        # OpenCode 2.2.6 (read 2026-11-09) keeps sessions in session_v2; the old `session` table is gone
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(os.path.join(d, 'opencode.db'))
            db.execute('CREATE TABLE session_v2 (id TEXT, project_id TEXT, parent_id TEXT, fork_session_id TEXT, directory TEXT, title TEXT, '
                       'time_created INT, time_updated INT)')
            db.execute("INSERT INTO session_v2 VALUES ('ses_old', 'p', NULL, NULL, '/work/p', 'older', 1790000000000, 1790000001000)")
            db.execute("INSERT INTO session_v2 VALUES ('ses_new', 'p', NULL, 'ses_old', '/work/p', 'newer', 1790000002000, 1790000003000)")
            db.commit()
            db.close()
            p = oc.OpenCodeProvider(env={'PATH': '/nonexistent'}, data_dir=d)
            rows = p.list_sessions('/work')
            self.assertEqual([(r['id'], r['title'], r.get('copyOf')) for r in rows],
                             [('opencode:ses_new', 'newer', 'opencode:ses_old'), ('opencode:ses_old', 'older', None)])

    def test_sessions_started_here_stay_driven_after_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, 'driven.json')
            p = oc.OpenCodeProvider(env={'PATH': '/nonexistent'}, data_dir=d, ledger=ledger)
            s = p.live('ses_mine')
            s.owned, s.cwd, s.model = True, '/work/p', 'opencode/x'
            p._save_ledger()
            q = oc.OpenCodeProvider(env={'PATH': '/nonexistent'}, data_dir=d, ledger=ledger)          # the workspace restarted
            self.assertTrue(q.owns('opencode:ses_mine'))
            self.assertEqual((q.live('ses_mine', create=False).cwd, q.live('ses_mine', create=False).model), ('/work/p', 'opencode/x'))
            self.assertFalse(q.owns('opencode:ses_other'))
            q.release('opencode:ses_mine')
            r = oc.OpenCodeProvider(env={'PATH': '/nonexistent'}, data_dir=d, ledger=ledger)
            self.assertFalse(r.owns('opencode:ses_mine'))


if __name__ == '__main__':
    unittest.main()
