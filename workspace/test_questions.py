# questions (2026-11-21): Codex's async (non-blocking) questions as question cards and their answers as messages; multi-select
# answers for Codex and Claude Code; the start-time checkbox's word per harness; Codex's Plan mode sent from the reply box's switch.
# Fixtures only: no Codex process, no agent. Run: python3 -m pytest agent-ide/workspace/test_questions.py
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import server
import claude_bridge

server.claude_bridge.VENV = Path('/nonexistent/claude-bridge-venv')      # tests never use a real venv

HERE = Path(__file__).resolve().parent
PAGE = (HERE / 'live.html').read_text()


def bare_bridge(replies=None):
    """A Bridge with no app-server process: its own message handling (route), the calls it would make and the frames it would
    send recorded instead."""
    b = server.Bridge.__new__(server.Bridge)
    b.init_state()
    b.ready = True
    b.calls, b.sent, replies = [], [], replies or {}

    def call(method, params=None, timeout=45):
        b.calls.append((method, params))
        r = replies.get(method, {})
        return r(params) if callable(r) else r
    b.call = call
    b.send = lambda msg: b.sent.append(msg)
    return b


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / 'codex'
        (home / 'thread-writer-locks').mkdir(parents=True)
        p = patch.object(server, 'CODEX_HOME', home)
        p.start()
        self.addCleanup(p.stop)
        self.b = bare_bridge({'turn/start': {'turn': {'id': 't2', 'status': 'inProgress'}}, 'turn/steer': {'turnId': 't1'}})
        self.b.threads['th'] = {'id': 'th', 'model': 'gpt-6-luna'}
        self.b.loaded.add('th')
        self.w = server.Workspace(self.b)


ASYNC_ITEM = {'type': 'agentMessage', 'id': 'msg_1', 'text': 'Two quick questions while I work.', 'delivery': 'async',
              'questions': [{'title': 'Which database should the tests use?', 'options': ['SQLite in memory', 'Postgres in Docker']},
                            {'title': 'Anything else I should know?', 'options': None}]}


class AsyncQuestionTests(Base):
    def test_async_question_becomes_a_question_card(self):
        self.b.route({'method': 'item/completed', 'params': {'threadId': 'th', 'turnId': 't1', 'item': ASYNC_ITEM}})
        key = 'codex-async:th:msg_1'
        self.assertIn(key, self.b.requests)
        r = self.b.requests[key]
        self.assertEqual((r['method'], r['params']['isBlocking'], r['params']['delivery'], r['params']['itemId']), ('item/tool/requestUserInput', False, 'async', 'msg_1'))
        qs = r['params']['questions']
        self.assertEqual([(q['id'], q['question'], q['isOther']) for q in qs], [('q0', 'Which database should the tests use?', True), ('q1', 'Anything else I should know?', True)])
        self.assertEqual([o['label'] for o in qs[0]['options']], ['SQLite in memory', 'Postgres in Docker'])
        self.assertIsNone(qs[1]['options'])
        self.assertEqual(server.card_of(r)['params']['questions'], qs)          # the page gets it as it is
        self.assertEqual(self.b.open_words(), [])          # never the app-server's: a restart does not end it

    def test_plain_messages_and_empty_questions_ask_nothing(self):
        self.b.route({'method': 'item/completed', 'params': {'threadId': 'th', 'item': {'type': 'agentMessage', 'id': 'm2', 'text': 'done'}}})
        self.b.route({'method': 'item/completed', 'params': {'threadId': 'th', 'item': dict(ASYNC_ITEM, id='m3', questions=[{'title': '  '}])}})
        self.assertEqual(self.b.requests, {})

    def test_answer_goes_as_a_new_turn_when_idle(self):
        self.b.route({'method': 'item/completed', 'params': {'threadId': 'th', 'item': ASYNC_ITEM}})
        out = self.w.action('reply', {'id': 'codex-async:th:msg_1', 'result': {'answers': {'q0': {'answers': ['Postgres in Docker']}, 'q1': {'answers': ['no']}}}})
        self.assertEqual(out['reason'], 'sent')
        method, params = self.b.calls[-1]
        self.assertEqual(method, 'turn/start')
        self.assertEqual(params['input'], [{'type': 'text', 'text': 'Answer to your question "Which database should the tests use?": Postgres in Docker\nAnswer to your question "Anything else I should know?": no'}])
        self.assertNotIn('codex-async:th:msg_1', self.b.requests)          # answered: the card goes
        with self.assertRaises(ValueError):
            self.w.action('reply', {'id': 'codex-async:th:msg_1', 'result': {'answers': {}}})

    def test_answer_steers_the_running_turn(self):
        self.b.active['th'] = 't1'
        self.b.route({'method': 'item/completed', 'params': {'threadId': 'th', 'item': ASYNC_ITEM}})
        self.w.action('reply', {'id': 'codex-async:th:msg_1', 'result': {'answers': {'q0': {'answers': ['SQLite in memory']}, 'q1': {'answers': ['']}}}})
        method, params = self.b.calls[-1]
        self.assertEqual((method, params['expectedTurnId']), ('turn/steer', 't1'))
        self.assertIn('SQLite in memory', params['input'][0]['text'])
        self.assertIn('(no answer)', params['input'][0]['text'])

    def test_every_question_needs_an_answer_and_dismiss_sends_nothing(self):
        self.b.route({'method': 'item/completed', 'params': {'threadId': 'th', 'item': ASYNC_ITEM}})
        with self.assertRaises(ValueError):
            self.w.action('reply', {'id': 'codex-async:th:msg_1', 'result': {'answers': {'q0': {'answers': ['x']}}}})
        self.assertEqual(self.b.calls, [])
        self.assertEqual(self.w.action('reply', {'id': 'codex-async:th:msg_1', 'result': {'dismiss': True}}), {'reason': 'dismissed'})
        self.assertEqual(self.b.calls, [])
        self.assertNotIn('codex-async:th:msg_1', self.b.requests)
        resolved = [m for _, m in self.b.events if m.get('method') == 'serverRequest/resolved']
        self.assertEqual(resolved[-1]['params'], {'threadId': 'th', 'requestId': 'codex-async:th:msg_1'})

    def test_blocking_reply_path_refuses_an_async_question(self):
        self.b.route({'method': 'item/completed', 'params': {'threadId': 'th', 'item': ASYNC_ITEM}})
        with self.assertRaises(ValueError):
            self.b.reply('codex-async:th:msg_1', {'answers': {'q0': {'answers': ['x']}, 'q1': {'answers': ['y']}}})
        self.assertEqual(self.b.sent, [])


class MultiSelectAnswerTests(Base):
    def test_codex_question_takes_several_answers(self):
        q = {'id': 7, 'method': 'item/tool/requestUserInput', 'params': {'threadId': 'th', 'turnId': 't1', 'itemId': 'i7', 'isBlocking': True,
             'questions': [{'id': 'langs', 'header': 'Langs', 'question': 'Which?', 'isOther': False, 'isSecret': False,
                            'options': [{'label': 'Python', 'description': 'the server'}, {'label': 'JS', 'description': 'the page'}]}]}}
        self.b.route(q)
        self.w.action('reply', {'id': '7', 'result': {'answers': {'langs': {'answers': ['Python', 'JS']}}}})
        self.assertEqual(self.b.sent[-1], {'id': 7, 'result': {'answers': {'langs': {'answers': ['Python', 'JS']}}}})

    def test_claude_code_multi_select_keeps_its_shape_and_joins_answers(self):
        inp = {'questions': [{'question': 'Which parts?', 'header': 'Parts', 'multiSelect': True,
                              'options': [{'label': 'Server', 'description': 'server.py'}, {'label': 'Page', 'description': 'live.html'}]}]}
        r = claude_bridge.as_request({'key': 'k', 'requestId': 'r1', 'toolName': 'AskUserQuestion', 'input': inp}, '/p')
        q0 = r['params']['questions'][0]
        self.assertEqual((q0['multiSelect'], q0['isOther'], [o['description'] for o in q0['options']]), (True, True, ['server.py', 'live.html']))
        r['params']['original'] = inp
        a = claude_bridge.answer_of(r, {'answers': {'q0': {'answers': ['Server', 'Page']}}})
        self.assertEqual(a['updatedInput']['answers'], {'Which parts?': 'Server, Page'})


def node(src):
    """Runs a snippet of the page's own code in node; its printed JSON back."""
    if not shutil.which('node'):
        raise unittest.SkipTest('node is not installed')
    out = subprocess.run(['node', '-e', src], capture_output=True, text=True, timeout=20)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def page_fn(start, end):
    i = PAGE.index(start)
    return PAGE[i:PAGE.index(end, i)]


class PlanLabelTests(unittest.TestCase):
    def test_checkbox_word_and_tooltip_per_harness(self):
        src = page_fn('function ptgName(h)', '\n') + '\n' + page_fn('const ptgMeans = h =>', "\nconst ptgOn") + '\n'
        got = node(src + 'console.log(JSON.stringify([ptgName("codex"), ptgName("claude"), ptgMeans("codex"), ptgMeans("claude")]))')
        self.assertEqual(got[:2], ['checklist', 'plan mode'])
        self.assertIn('checklist', got[2])
        self.assertIn('not Codex\'s Plan mode', got[2])
        self.assertIn('plan mode', got[3])

    def test_settings_line_uses_the_harness_word(self):
        rj = (HERE / 'engine' / 'reply.js').read_text()
        i = rj.index('function engSettingsWords(v)')
        src = rj[i:rj.index('\n}\n', i) + 3]
        got = node(src + 'console.log(JSON.stringify([engSettingsWords({harness:"Codex",model:"m",plan:true,planWord:"checklist",planMode:true}), engSettingsWords({harness:"Claude Code",model:"m",plan:false,planWord:"plan mode"})]))')
        self.assertEqual(got, ['Codex · m · checklist on · Plan mode', 'Claude Code · m · plan mode off'])


class QuestionCardTests(unittest.TestCase):
    def buttons(self, q):
        src = page_fn('function cardButtons(q) {', '\n// where:')
        return node('const askAlways = () => null;\n' + src + '\nconsole.log(JSON.stringify(cardButtons(' + json.dumps(q) + ')))')

    def test_whole_labels_descriptions_other_and_set_aside(self):
        long = 'Rewrite the whole importer so it streams rows instead of loading the file'
        q = {'kind': 'ask', 'qi': 0, 'nb': True, 'qs': [{'id': 'q0', 'question': '?', 'isOther': True, 'options': [{'label': long, 'description': 'slower to build'}, {'label': 'Keep it', 'description': ''}]}]}
        bs = self.buttons(q)
        self.assertEqual([(b['label'], b.get('desc'), b['act']) for b in bs],
                         [(long, 'slower to build', 'opt'), ('Keep it', '', 'opt'), ('Type an answer', None, 'chat'), ('Set aside', None, 'dismiss')])

    def test_multi_select_ticks_and_send(self):
        q = {'kind': 'ask', 'qi': 0, 'qs': [{'id': 'q0', 'question': '?', 'multiSelect': True, 'options': [{'label': 'A', 'description': 'a'}, {'label': 'B', 'description': ''}]}]}
        self.assertEqual([(b['label'], b['act']) for b in self.buttons(q)], [('☐ A', 'pick'), ('☐ B', 'pick'), ('Send', 'picks')])


class PlanModeSendTests(Base):
    def test_switch_on_sends_plan_mode_with_the_sessions_model(self):
        self.w.action('send', {'threadId': 'th', 'text': 'look first', 'mode': 'plan'})
        method, params = self.b.calls[-1]
        self.assertEqual(method, 'turn/start')
        self.assertEqual(params['collaborationMode'], {'mode': 'plan', 'settings': {'model': 'gpt-6-luna', 'reasoning_effort': 'medium', 'developer_instructions': None}})
        self.assertEqual(self.w.settings_of('th')['mode'], 'plan')

    def test_switch_off_sends_default_mode_and_reported_settings_win(self):
        self.b.settings['th'] = {'model': 'gpt-6-sol', 'effort': 'high'}
        self.w.action('send', {'threadId': 'th', 'text': 'go', 'mode': 'default'})
        self.assertEqual(self.b.calls[-1][1]['collaborationMode'], {'mode': 'default', 'settings': {'model': 'gpt-6-sol', 'reasoning_effort': 'high', 'developer_instructions': None}})

    def test_no_model_known_says_so_and_sends_nothing(self):
        self.b.threads['th'] = {'id': 'th'}
        with self.assertRaisesRegex(ValueError, 'Plan mode needs a model'):
            self.w.action('send', {'threadId': 'th', 'text': 'x', 'mode': 'plan'})
        self.assertEqual(self.b.calls, [])

    def test_mode_while_running_goes_to_the_thread_for_its_next_turn(self):
        self.b.active['th'] = 't1'
        self.w.action('send', {'threadId': 'th', 'text': 'and plan it', 'mode': 'plan'})
        self.assertEqual([m for m, _ in self.b.calls], ['thread/settings/update', 'turn/steer'])
        self.assertEqual(self.b.calls[0][1]['collaborationMode']['mode'], 'plan')

    def test_no_mode_sends_none(self):
        self.w.action('send', {'threadId': 'th', 'text': 'hi'})
        self.assertNotIn('collaborationMode', self.b.calls[-1][1])

    def test_the_page_sends_the_switch_with_each_message(self):
        self.assertIn("o.mode = PM.get(a.sid) ? 'plan' : 'default'", PAGE)          # ccOver: the switch rides on every message once switched


if __name__ == '__main__':
    unittest.main()
