"""Antigravity's pasted sign-in code, end to end through the providers panel's own path (ProviderSetup.act, which
POST /api/providers calls), with a fake sign-in shaped like agy 1.5.3's `agy acp --login` (measured here 2026-11-22 with a
fresh HOME and a made-up code): a link block, the prompt line, one line read from stdin, and on a refused code
`couldn't log in: token exchange failed: oauth2: "invalid_grant" "Bad Request"` with exit 1.
Written for a bug of 2026-11-22: a pasted Antigravity sign-in code did nothing."""
import json
import sys
import tempfile
import time
import unittest

import provider_setup
from providers import acp
from providers.acp_flavors import Flavor

FAKE_AGY = r'''
import sys, time
print('Open this link in a browser to log in with Google:\n\n  https://accounts.example.test/o/oauth2/auth?x=1\n', flush=True)
print('Then paste the authorization code, or the full URL of the page you land on, and press Enter:', flush=True)
c = sys.stdin.readline().strip()
if c == 'SLOW-CODE':
    print('exchanging the code', flush=True); time.sleep(30)
if c != 'GOOD-CODE-9':
    print('\ncouldn\'t log in: token exchange failed: oauth2: "invalid_grant" "Bad Request"', flush=True); sys.exit(1)
print('Logged in.', flush=True)
'''


def wait_for(pred, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return pred()


class PastedSignInCode(unittest.TestCase):
    def setUp(self):
        acp.LOOKS.pop('fakeagy', None)
        fl = Flavor(name='fakeagy', label='Antigravity', binary=sys.executable, login_args=('-c', FAKE_AGY),
                    login_code_prompt=r'paste the authorization code', login_opens_browser=True)
        self.p = acp.AcpProvider(fl, data_dir=tempfile.mkdtemp(prefix='signin-code-'))
        self.p.CODE_WAIT = 3.0
        self.s = provider_setup.ProviderSetup(lambda: None, lambda: None, lambda: {'present': False, 'ready': False, 'reason': None},
                                              lambda: {'ready': False, 'reason': 'not set up'}, runner=lambda *a, **k: (None, '', ''),
                                              no_windows=lambda: False, driven=lambda: [self.p])

    def tearDown(self):
        st = getattr(self.p, '_login', None)
        if st and st['proc'].poll() is None:
            self.s.act('fakeagy', 'cancel')
            st['proc'].wait(5)

    def row(self, answer=None):
        return next(r for r in (answer or self.s.get())['providers'] if r['name'] == 'fakeagy')

    def ask_for_code(self):
        self.s.act('fakeagy', 'signin')
        r = wait_for(lambda: (self.row().get('job') or {}).get('needsCode') and self.row(), 10)
        self.assertTrue(r, 'the code box never showed')
        self.assertEqual(r['say'], 'Signing in: paste the code Google shows below')
        return r

    def test_refused_code_shows_the_reason_and_sign_in_again_works(self):
        self.ask_for_code()
        r = self.row(self.s.act('fakeagy', 'code', 'BAD-CODE-123'))
        j = r['job']
        self.assertEqual((j['running'], j['ok'], j['needsCode'], j['codeSent']), (False, False, False, True))
        self.assertIn('That code was not accepted: couldn\'t log in: token exchange failed', j['result'])
        self.assertIn('Press Sign in to try again with a new link and code.', j['result'])
        self.assertEqual(r['error'], j['result'])
        self.assertEqual(r['fix']['action'], 'signin')          # tried again from the same row
        self.assertNotIn('BAD-CODE-123', json.dumps(self.s.get()))          # the code is in no answer
        self.ask_for_code()          # a fresh sign-in asks again
        r = self.row(self.s.act('fakeagy', 'code', 'GOOD-CODE-9'))
        self.assertEqual((r['job']['ok'], r['job']['result'], r['state']), (True, 'Signed in.', 'ready'))
        self.assertIsNone(r['error'])

    def test_code_still_being_checked_says_so(self):
        self.ask_for_code()
        r = self.row(self.s.act('fakeagy', 'code', 'SLOW-CODE'))
        self.assertTrue(r['job']['running'])
        self.assertEqual(r['say'], 'Signing in: code sent, waiting for Antigravity to answer')
        self.assertEqual(r['job']['said'], 'exchanging the code')
        self.assertFalse(r['job']['needsCode'])
        with self.assertRaises(ValueError):          # sent once; a second paste is refused with words, not dropped
            self.s.act('fakeagy', 'code', 'SLOW-CODE')
        self.s.act('fakeagy', 'cancel')
        r = wait_for(lambda: not self.row()['job']['running'] and self.row(), 10)
        self.assertEqual(r['fix']['action'], 'signin')

    def test_empty_or_multiline_code_is_refused_with_words(self):
        self.ask_for_code()
        for bad in ('', '   ', 'a\nb'):
            with self.assertRaises(ValueError) as cm:
                self.s.act('fakeagy', 'code', bad)
            self.assertIn('one line', str(cm.exception))
        self.assertTrue(self.row()['job']['needsCode'])          # still waiting for a good paste

    def test_cancelled_sign_in_says_stopped(self):
        self.ask_for_code()
        self.s.act('fakeagy', 'cancel')
        r = wait_for(lambda: not self.row()['job']['running'] and self.row(), 10)
        self.assertEqual(r['job']['result'], 'Sign-in did not finish: it was stopped. Press Sign in to try again.')


if __name__ == '__main__':
    unittest.main()
