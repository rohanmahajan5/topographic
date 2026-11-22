"""Plan use for every harness (providers/planuse.py), and a refusal reaching the usage panel and the chat.

Fixtures (providers/fixtures/planuse/): the response shapes CodexBar decodes (github.com/steipete/CodexBar, read 2026-11-22:
Tests/CodexBarTests/CursorStatusProbeTests.swift for cursor.com/api/usage-summary and /api/usage, and
AntigravityCLIUsageReportTests.swift for `agy -p /usage --output-format json`), with made-up values. No real store, token or
endpoint is touched: every sign-in here is a made-up JWT in a temporary folder and every fetch is a stand-in.

Run: cd agent-ide/workspace && python3 -m unittest test_planuse -v
"""
import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from providers import acp, planuse
from providers.acp_flavors import CURSOR, ANTIGRAVITY, cursor_plan_refusal
from test_acp import mock_flavor, wait_for

HERE = Path(__file__).resolve().parent
FX = HERE / 'providers' / 'fixtures' / 'planuse'


def fx(name):
    return json.loads((FX / name).read_text())


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
    return enc({'alg': 'none'}) + '.' + enc(claims) + '.sig'


class Refusal(unittest.TestCase):
    def test_refusals_and_not(self):
        yes = ['Upgrade your plan to continue', "You've hit your usage limit", 'RESOURCE_EXHAUSTED (code 429): quota exceeded',
               'This model is not available on your current plan.', 'You have run out of premium requests', 'HTTP 402 Payment Required',
               'Rate limit reached for requests']
        no = ['The agent process ended before the turn finished.', 'ECONNRESET', 'Invalid params: prompt', 'Planned the refactor']
        for t in yes:
            self.assertTrue(planuse.is_refusal(t), t)
        for t in no:
            self.assertFalse(planuse.is_refusal(t), t)
        self.assertTrue(planuse.is_refusal('anything', 'usage_limit'))          # the class alone is enough

    def test_reset_times_from_the_words(self):
        self.assertEqual(planuse.resets_in_text('Resets in 2h30m.', now=1000.0), 1000.0 + 9000)
        self.assertEqual(planuse.resets_in_text("You've hit your limit. Resets at 2026-11-22T23:00:00Z", now=0), planuse.iso_epoch('2026-11-22T23:00:00Z'))
        self.assertIsNone(planuse.resets_in_text('no time here'))
        self.assertEqual(planuse.iso_epoch('2026-11-22T23:00:00+01:00'), planuse.iso_epoch('2026-11-22T22:00:00Z'))

    def test_state_set_cleared_and_lifted(self):
        r = planuse.Refusals()
        self.assertIsNone(r.turn_ended('ECONNRESET'))
        self.assertIsNone(r.current())
        self.assertTrue(r.turn_ended('Upgrade your plan to continue', session='cursor:a', now=100.0))          # the refusal it recorded
        self.assertEqual((r.current(now=101)['reason'], r.current(now=101)['session']), ('Upgrade your plan to continue', 'cursor:a'))
        r.turn_ended('ECONNRESET')          # another failure that is not a refusal: still refusing as far as we know
        self.assertIsNotNone(r.current())
        r.turn_ended(None, cancelled=True)          # a stopped turn says nothing
        self.assertIsNotNone(r.current())
        r.turn_ended(None)          # a turn went through
        self.assertIsNone(r.current())
        r.turn_ended('quota exhausted', resets_at='2026-11-22T23:00:00Z', now=0)          # OpenCode's ISO reset
        self.assertEqual(r.current(now=1)['resetsAt'], planuse.iso_epoch('2026-11-22T23:00:00Z'))
        self.assertIsNone(r.current(now=planuse.iso_epoch('2026-11-22T23:00:01Z')))          # its limit has lifted


class Cursor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='planuse-cursor-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = time.time()

    def app_db(self, value):
        p = Path(tempfile.mkstemp(suffix='.vscdb', dir=self.tmp)[1])
        p.unlink()
        c = sqlite3.connect(p)
        c.execute('create table ItemTable (key text unique on conflict replace, value blob)')
        if value is not None:
            c.execute('insert into ItemTable values (?, ?)', ('cursorAuth/accessToken', value))
        c.commit(); c.close()
        return str(p)

    def test_pro_plan_from_the_summary(self):
        r = planuse.cursor_reading(fx('cursor-usage-summary-pro.json'))
        self.assertEqual(r['plan'], 'Pro')
        self.assertIsNone(r['note'])
        end = planuse.iso_epoch('2026-12-01T00:00:00Z')
        self.assertEqual([(w['label'], w['percent'], w['resetsAt']) for w in r['windows']],
                         [('included', 75.0, end), ('Auto', 40.0, end), ('API', 100.0, end), ('on-demand', 16.0, end)])
        self.assertEqual(r['windows'][0]['text'], '$15.00 of $20.00 included')
        self.assertEqual(r['windows'][3]['text'], '$3.20 of $20.00')

    def test_free_plan_shows_its_premium_requests(self):
        # a real case: Free, no premium requests left (50 of 50), on-demand off
        r = planuse.cursor_reading(fx('cursor-usage-summary-free.json'), fx('cursor-usage-free-requests.json'))
        self.assertEqual(r['plan'], 'Free')
        self.assertEqual([(w['label'], w['percent'], w.get('text')) for w in r['windows']], [('requests', 100.0, '50 of 50 requests')])

    def test_free_plan_without_figures_says_why(self):
        r = planuse.cursor_reading(fx('cursor-usage-summary-free.json'), None)
        self.assertEqual(r['windows'], [])
        self.assertEqual(r['note'], 'Cursor reports no included use for the Free plan and on-demand use is off')

    def test_session_from_the_app_database(self):
        tok = jwt({'sub': 'auth0|user_ABC123', 'exp': self.now + 3600})
        sess, why = planuse.cursor_session(self.app_db(tok), str(self.tmp / 'none.json'), now=self.now)
        self.assertIsNone(why)
        self.assertEqual(sess['user'], 'user_ABC123')
        self.assertEqual(sess['cookie'], 'WorkosCursorSessionToken=user_ABC123%3A%3A' + tok)

    def test_agent_file_stores_in_order(self):
        paths = planuse.cursor_agent_auth({'XDG_CONFIG_HOME': '/x/cfg'})
        self.assertEqual(paths, [os.path.expanduser('~/.cursor/auth.json'), '/x/cfg/cursor/auth.json'])
        a, b = self.tmp / 'mac.json', self.tmp / 'linux.json'
        b.write_text(json.dumps({'accessToken': 'tok-b'}))
        self.assertEqual(planuse.cursor_agent_token([str(a), str(b)]), ('tok-b', None))
        a.write_text(json.dumps({'accessToken': 'tok-a'}))
        self.assertEqual(planuse.cursor_agent_token([str(a), str(b)]), ('tok-a', None))
        tok, why = planuse.cursor_agent_token([str(self.tmp / 'n1'), str(self.tmp / 'n2')])
        self.assertIsNone(tok)
        self.assertIn('Keychain', why)

    def test_session_utf16_blob_and_agent_fallback(self):
        tok = jwt({'sub': 'user_X', 'exp': self.now + 3600})
        sess, _ = planuse.cursor_session(self.app_db(tok.encode('utf-16-le')), None, now=self.now)
        self.assertEqual(sess['user'], 'user_X')
        auth = self.tmp / 'auth.json'
        auth.write_text(json.dumps({'accessToken': jwt({'sub': 'user_Y', 'exp': self.now + 3600})}))
        sess, _ = planuse.cursor_session(self.app_db(None), str(auth), now=self.now)
        self.assertEqual(sess['user'], 'user_Y')

    def test_missing_expired_and_encrypted_sign_ins_say_why(self):
        none, why = planuse.cursor_session(str(self.tmp / 'nothing.vscdb'), str(self.tmp / 'none.json'), now=self.now)
        self.assertIsNone(none)
        self.assertTrue(why.startswith('not signed in'), why)
        _, why = planuse.cursor_session(self.app_db(jwt({'sub': 'u', 'exp': self.now - 10})), str(self.tmp / 'none.json'), now=self.now)
        self.assertIn('expired', why)
        _, why = planuse.cursor_session(self.app_db('cursor-safe-storage:v1:AAAA'), str(self.tmp / 'none.json'), now=self.now)
        self.assertIn('Keychain', why)

    def test_plan_through_a_stand_in_fetch(self):
        db = self.app_db(jwt({'sub': 'user_ABC', 'exp': self.now + 3600}))
        calls = []

        def fetch(path, cookie):
            calls.append(path)
            self.assertTrue(cookie.startswith('WorkosCursorSessionToken=user_ABC%3A%3A'))
            return fx('cursor-usage-summary-free.json') if path == '/api/usage-summary' else fx('cursor-usage-free-requests.json')
        r = planuse.cursor_plan(fetch=fetch, app_db=db, agent_auth=str(self.tmp / 'x'), now=self.now)
        self.assertEqual(calls, ['/api/usage-summary', '/api/usage?user=user_ABC'])
        self.assertEqual(r['windows'][0]['text'], '50 of 50 requests')

        def refused(path, cookie):
            raise planuse.CursorError(401)
        self.assertIn('did not accept the saved sign-in', planuse.cursor_plan(fetch=refused, app_db=db, agent_auth='x', now=self.now)['note'])

        def down(path, cookie):
            raise planuse.CursorError(503)
        self.assertEqual(planuse.cursor_plan(fetch=down, app_db=db, agent_auth='x', now=self.now)['note'], "Cursor didn't report usage (HTTP 503)")

    def test_the_app_database_is_opened_read_only(self):
        db = self.app_db(jwt({'sub': 'u1', 'exp': self.now + 3600}))
        os.chmod(db, stat.S_IRUSR)          # a write would fail; a read-only open does not
        self.addCleanup(os.chmod, db, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(planuse.cursor_app_token(db)[0].count('.'), 2)


class Antigravity(unittest.TestCase):
    def test_report_as_windows(self):
        r = planuse.antigravity_reading(fx('agy-usage-report.json'))
        self.assertIsNone(r['note'])
        self.assertEqual([(w['label'], round(w['percent'])) for w in r['windows']],
                         [('Gemini 5-hour', 5), ('Claude and GPT 5-hour', 100), ('Gemini weekly', 14), ('Claude and GPT weekly', 11)])
        self.assertEqual(r['windows'][1]['resetsAt'], planuse.iso_epoch('2026-11-22T23:29:19Z'))

    def test_failed_report_says_so(self):
        self.assertIn("didn't report usage", planuse.antigravity_reading({'status': 'ERROR', 'command': {'name': 'usage'}})['note'])

    def test_version_and_sign_in_gate_the_report(self):
        ran = []
        run = lambda b: ran.append(b) or fx('agy-usage-report.json')
        self.assertEqual(planuse.antigravity_plan(run=run, binary=None)['note'], 'Antigravity is not installed')
        self.assertIn('not signed in', planuse.antigravity_plan(run=run, binary='/x/agy', version='1.5.3', signed_in=False)['note'])
        r = planuse.antigravity_plan(run=run, binary='/x/agy', version=None)
        self.assertTrue(r['retry'])          # the version look has not answered yet: asked again in a few seconds
        self.assertIn('needs agy 1.1.11', planuse.antigravity_plan(run=run, binary='/x/agy', version='agy 1.1.10')['note'])
        self.assertEqual(ran, [])          # an agy that might take /usage as a prompt is never run
        self.assertEqual(len(planuse.antigravity_plan(run=run, binary='/x/agy', version='1.5.3')['windows']), 4)

    def test_report_run_cannot_open_a_browser(self):
        # a stand-in agy: notes which `open` and BROWSER it would reach (never running either) and prints the report; both must
        # be the private folder's stubs that answer with failure
        d = Path(tempfile.mkdtemp(prefix='planuse-agy-'))
        self.addCleanup(shutil.rmtree, d, True)
        agy = d / 'agy'
        agy.write_text('#!/bin/sh\ncommand -v open > %s/open\necho "$BROWSER" > %s/browser\necho "$AGY_CLI_DISABLE_AUTO_UPDATE" > %s/auto\n'
                       'echo "$@" > %s/args\ncat %s\n' % (d, d, d, d, FX / 'agy-usage-report.json'))
        agy.chmod(0o700)
        rep = planuse.agy_usage(str(agy), timeout=20)
        self.assertEqual(rep['status'], 'SUCCESS')
        opener, browser = (d / 'open').read_text().strip(), (d / 'browser').read_text().strip()
        self.assertIn('agent-ide-agy-usage-', opener)
        self.assertEqual(os.path.dirname(browser), os.path.dirname(opener))
        self.assertEqual((d / 'args').read_text().split(), ['-p', '/usage', '--output-format', 'json', '--print-timeout', '90s'])
        self.assertEqual((d / 'auto').read_text().strip(), 'true')


class Background(unittest.TestCase):
    """Slow readings run only while someone looks, at most once per `every`, and right after a refusal (coordinator 2026-11-22)."""

    def test_never_read_while_no_one_looks(self):
        n = []
        b = planuse.Background(lambda: n.append(1) or {'windows': [1]}, every=60)
        for _ in range(3):
            self.assertIsNone(b.get())
        time.sleep(0.2)
        self.assertEqual(n, [])
        self.assertEqual(b.state(), {'at': None, 'busy': False})

    def test_a_look_reads_once_per_interval(self):
        n = []
        b = planuse.Background(lambda: n.append(1) or {'windows': [1]}, every=60)
        self.assertIsNone(b.get(look=True))          # nothing waits on the read
        self.assertTrue(wait_for(lambda: b.get() is not None, 5))
        b.get(look=True); b.get(look=True)
        self.assertEqual(len(n), 1)          # within the interval: the cached reading
        self.assertIsNotNone(b.state()['at'])
        b.started -= 61
        b.get(look=True)
        self.assertTrue(wait_for(lambda: len(n) == 2, 5))

    def test_a_refusal_reads_now_or_at_the_first_look_the_cap_allows(self):
        n = []
        b = planuse.Background(lambda: n.append(1) or {'windows': [1]}, every=60)
        b.due()          # nothing read yet: now
        self.assertTrue(wait_for(lambda: len(n) == 1, 5))
        b.due()          # read a moment ago: owed, not now
        time.sleep(0.2)
        self.assertEqual((len(n), b.owed), (1, True))
        b.started -= 61
        b.get()          # still no one looking: nothing
        time.sleep(0.2)
        self.assertEqual(len(n), 1)
        b.get(look=True)
        self.assertTrue(wait_for(lambda: len(n) == 2, 5))

    def test_a_waiting_reading_is_tried_again_soon(self):
        n = []
        b = planuse.Background(lambda: n.append(1) or dict(planuse.reading('checking'), retry=True), every=300)
        b.get(look=True)
        self.assertTrue(wait_for(lambda: b.get() is not None, 5))
        b.started -= 6
        b.get(look=True)
        self.assertTrue(wait_for(lambda: len(n) == 2, 5))


class AcpRefusal(unittest.TestCase):
    """A fake ACP child that refuses: the error text reaches the turn's end and the chat whole, and the provider's plan-use state
    says so until a turn goes through."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='planuse-acp-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / 'p').mkdir()
        self.msgs = []
        reader = lambda p: planuse.reading("Cursor didn't report usage (HTTP 503)")
        self.p = acp.AcpProvider(mock_flavor('v1', silent_failure=cursor_plan_refusal, plan_reader=reader), publish=self.msgs.append,
                                 data_dir=self.tmp / 'data')
        self.addCleanup(self.p.close)

    def end(self, sid, n):
        got = wait_for(lambda: [m['params'] for m in self.msgs if m.get('method') == 'provider/turn' and m['params'].get('ended')][n - 1:n], 20)
        self.assertTrue(got, 'turn %d did not end' % n)
        return got[0]

    def test_error_answer_is_shown_whole_and_sets_the_state(self):
        sid = self.p.start(str(self.tmp / 'p'))['threadId']
        self.p.send(sid, 'refuse')
        end = self.end(sid, 1)
        words = "You've hit your usage limit for premium models on the Free plan.\nUpgrade to Pro or switch to Auto. Resets in 2h30m."
        self.assertEqual((end['stopReason'], end['error']), ('error', words))
        self.assertTrue(end['failure']['refused'])
        self.assertAlmostEqual(end['failure']['resetsAt'], time.time() + 9000, delta=60)
        h = self.p.read_history(sid)
        self.assertEqual([e['text'] for e in h['errors']], [words])          # kept whole in the record
        st = self.p.plan_usage()['state']
        self.assertEqual((st['kind'], st['reason'], st['session']), ('refused', words, sid))
        # the chat's row (server.attention_of -> err_view): its first line for the floor, the whole words and the reset for the chat
        import server
        v = server.err_view(h['errors'][0], 0)
        self.assertEqual(v['text'], "You've hit your usage limit for premium models on the Free plan.")
        self.assertEqual(v['full'], words)
        self.assertTrue(v['refused'])
        self.assertAlmostEqual(v['resetsAt'], h['errors'][0]['t'] + 9000, delta=1)
        # a turn that goes through clears it
        self.p.send(sid, 'hello')
        self.end(sid, 2)
        self.assertIsNone(self.p.plan_usage()['state'])

    def test_cursor_one_line_refusal_sets_the_state(self):
        sid = self.p.start(str(self.tmp / 'p'))['threadId']
        self.p.send(sid, 'upgrade')
        end = self.end(sid, 1)
        self.assertIn('Upgrade your plan to continue', end['error'])
        self.assertTrue(end['failure']['refused'])
        u = wait_for(lambda: (lambda u: u if u['note'] != "reading Mock agent's plan figures…" else None)(self.p.plan_usage()), 5)
        self.assertIn('Upgrade your plan to continue', u['state']['reason'])
        self.assertEqual((u['windows'], u['note']), ([], "Cursor didn't report usage (HTTP 503)"))          # figures say why they are missing

    def test_flavors_name_their_readers(self):
        self.assertIs(CURSOR.plan_reader, planuse.cursor_plan)
        self.assertIs(ANTIGRAVITY.plan_reader, planuse.antigravity_plan)
        p = acp.AcpProvider(mock_flavor('v1'), data_dir=self.tmp / 'd2')
        self.assertIn("doesn't report its plan use", p.plan_usage()['note'])          # an agent with no reader says so

    def test_reader_runs_only_on_a_look_and_says_how_old(self):
        u = self.p.plan_usage()
        self.assertEqual((u['note'], u['read']), ('not read yet: read when Plan use is open', None))
        self.p.plan_usage(look=True)
        u = wait_for(lambda: (lambda u: u if u['read'] and u['read']['at'] else None)(self.p.plan_usage()), 5)
        self.assertEqual(u['note'], "Cursor didn't report usage (HTTP 503)")
        self.assertFalse(u['read']['busy'])


class OpenCodePlan(unittest.TestCase):
    def test_no_plan_and_its_refusals(self):
        from providers.opencode import OpenCodeProvider
        p = OpenCodeProvider(binary=['/nonexistent/opencode'], data_dir=tempfile.mkdtemp(prefix='planuse-oc-'))
        u = p.plan_usage()
        self.assertEqual((u['windows'], u['note'], u['state']), ([], planuse.OPENCODE_NOTE, None))
        p.refusals.turn_ended('provider quota exceeded', 'usage_limit', '2099-01-01T00:00:00Z', session='opencode:s')
        self.assertEqual(p.plan_usage()['state']['resetsAt'], planuse.iso_epoch('2099-01-01T00:00:00Z'))


if __name__ == '__main__':
    unittest.main()
