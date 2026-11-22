"""A fake harness for tests only: one session and a plan reading, so the usage panel can be checked with a third provider.

Never registered in normal use. The server registers it only under AGENT_IDE_TEST_PROVIDERS (a comma list of provider names,
e.g. "codex,claude,fake"; server.py, Workspace.providers); test_server.py builds it directly. Its numbers are made up and
fixed: three calls on a priced model, and two plan windows named the harness's own way ("daily", "monthly"). For the columns'
compaction scale its one session is running, has read README.md four times for 97k tokens in all, and compacts at a fixed 100k
point (so its column is drawn 97% full, with the burnt-orange rim; agent-ide/test/plans/compaction.json).
"""
import os
import time

from .base import Provider, under
from .spend import Ledger

PREFIX = 'fake:'
READS = (40000, 30000, 20000, 7000)          # 97k tokens read: 97% of POINT
POINT = 100000


class FakeProvider(Provider):
    name = 'fake'
    label = 'Test harness'

    def __init__(self, cwd, now=None, model='gpt-6-luna'):
        self.cwd, self.now = str(cwd), now or time.time()
        self.led = Ledger()
        for k, (fresh, read, out) in enumerate([(4000, 20000, 900), (2000, 60000, 1500), (1000, 90000, 2200)]):
            self.led.add(self.now - 1500 + 600 * k, model, fresh, read, 0, 0, out, 0)

    def available(self):
        return {'name': self.name, 'label': self.label, 'found': True, 'store': None, 'binary': None, **self.flags()}

    def owns(self, sid):
        return str(sid).startswith(PREFIX)

    def list_sessions(self, project_root=None):
        if project_root is not None and not under(self.cwd, project_root):
            return []
        return [{'id': PREFIX + 'one', 'provider': self.name, 'title': 'Fake session', 'cwd': self.cwd, 'started': self.now - 1800,
                 'updated': self.now - 60, 'running': True, 'parent': None}] + [s for s in self.extra() if project_root is None or under(s['cwd'], project_root)]

    def extra(self):
        """AGENT_IDE_FAKE_CWDS (tests only; folders split by os.pathsep): one more made-up session in each folder, each reading its
        README.md, so a floor can be checked with columns standing in worktrees and nested repositories (floor-case, 2026-11-21)."""
        cwds = [c for c in os.environ.get('AGENT_IDE_FAKE_CWDS', '').split(os.pathsep) if c]
        return [{'id': PREFIX + 'w' + str(k + 1), 'provider': self.name, 'title': 'Fake task ' + str(k + 1) + ' in ' + os.path.basename(c.rstrip('/')), 'cwd': c,
                 'started': self.now - 1700 + 20 * k, 'updated': self.now - 30, 'running': k % 2 == 0, 'parent': None} for k, c in enumerate(cwds)]

    def read_history(self, sid):
        if sid != PREFIX + 'one' and any(s['id'] == sid for s in self.extra()):
            t = self.now - 1600
            return {'messages': [], 'events': [{'at': t, 'kind': 'read', 'path': 'README.md', 'text': 'README.md', 'tokens': 30000, 'item': 'fake-x-' + sid}],
                    'turnActive': sid.endswith(('1', '3', '5', '7', '9')), 'parent': None, 'contexts': [{'t': t, 'context': 30000}], 'compacts': [], 'window': None,
                    'context': 30000, 'base': None, 'reasoning': [], 'turns': [], 'model': 'fake-model'}
        if sid != PREFIX + 'one':
            return {'messages': [], 'events': [], 'turnActive': False, 'parent': None, 'contexts': [], 'compacts': [], 'window': None,
                    'context': 0, 'base': None, 'reasoning': [], 'turns': []}
        reads = [(self.now - 1500 + 400 * k, tok) for k, tok in enumerate(READS)]
        events = [{'at': t, 'kind': 'read', 'path': 'README.md', 'text': 'README.md', 'tokens': tok, 'item': 'fake-read-' + str(k)} for k, (t, tok) in enumerate(reads)]
        return {'messages': [], 'events': events, 'turnActive': True, 'parent': None, 'contexts': [{'t': t, 'context': sum(READS[:k + 1])} for k, (t, _) in enumerate(reads)],
                'compacts': [], 'window': None, 'context': sum(READS), 'base': None, 'reasoning': [], 'turns': [], 'model': 'fake-model'}

    def compaction_point(self, sid, model=None, window=None):
        if sid != PREFIX + 'one':
            return None
        return {'threshold': POINT, 'window': POINT, 'model': 'fake-model', 'rule': 'a fixed test point', 'source': 'providers/fake.py', 'estimate': False}

    def session_usage(self, sid):
        return self.led if sid == PREFIX + 'one' else None

    def plan_usage(self):
        return {'plan': 'test', 'at': self.now - 60, 'windows': [{'label': 'daily', 'percent': 30, 'resetsAt': self.now + 3600},
                                                                  {'label': 'monthly', 'percent': 8, 'resetsAt': self.now + 20 * 86400}], 'detail': {}}
