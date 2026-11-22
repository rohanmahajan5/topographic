import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

import server

server.claude_bridge.VENV = Path('/nonexistent/claude-bridge-venv')      # claude drive: tests never use a real venv (the bridge reads as not set up)


class FakeBridge:
    ready = True
    loaded = set()
    threads = {}
    active = {}
    requests = {}
    event_seq = 0

    def call(self, method, params):
        raise AssertionError('Unexpected mutation: ' + method)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        (self.home / 'thread-writer-locks').mkdir()
        self.patch = patch.object(server, 'CODEX_HOME', self.home)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def record(self, typ, payload):
        return json.dumps({'timestamp': '2026-11-04T17:00:00Z', 'type': typ, 'payload': payload}) + '\n'

    def row(self):
        return {'id': 'test', 'rollout_path': str(self.root / 'rollout.jsonl'), 'created_at': 1, 'updated_at': 2, 'cwd': str(self.root)}

    def test_incremental_partial_lines_and_turn_lifecycle(self):
        row = self.row()
        path = Path(row['rollout_path'])
        start = self.record('event_msg', {'type': 'task_started', 'turn_id': 'turn1'})
        end = self.record('event_msg', {'type': 'task_complete'})
        path.write_text(start + end[:30])
        observer = server.Observer()
        first = observer.read_log(row)
        self.assertTrue(first['turnActive'])
        self.assertEqual(first['offset'], len(start))
        with path.open('a') as f:
            f.write(end[30:])
        self.assertFalse(observer.read_log(row)['turnActive'])

    def test_codex_archived_threads_join_flagged_when_their_rollout_is_on_disk(self):
        """Archive stack: a thread Codex archived is listed with archivedAt (its rollout still on disk); one whose file is gone is not."""
        import sqlite3
        kept, gone = self.root / 'kept.jsonl', self.root / 'gone.jsonl'
        kept.write_text(self.record('event_msg', {'type': 'task_complete'}))
        with sqlite3.connect(self.home / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, source TEXT, cwd TEXT, title TEXT, archived INTEGER, archived_at INTEGER)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)', [('live', str(kept), 10, 20, 'cli', str(self.root), 'on the floor', 0, None),
                                                                              ('arch', str(kept), 11, 21, 'cli', str(self.root), 'archived', 1, 30),
                                                                              ('lost', str(gone), 12, 22, 'cli', str(self.root), 'file gone', 1, 31)])
        o = server.Observer()
        self.assertEqual(sorted(r['id'] for r in o.rows()), ['arch', 'live'])
        self.assertEqual(o.row('arch')['archived'], 1)
        by = {s['id']: s for s in o.sessions(FakeBridge())}
        self.assertEqual(by['arch'].get('archivedAt'), 30)
        self.assertNotIn('archivedAt', by['live'])

    def test_codex_store_without_archived_at_still_lists_its_threads(self):
        """Archive stack: a threads table with `archived` but no `archived_at` (the mocks' fixtures): an archived thread's time is its last update."""
        import sqlite3
        kept = self.root / 'kept.jsonl'
        kept.write_text(self.record('event_msg', {'type': 'task_complete'}))
        with sqlite3.connect(self.home / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, source TEXT, cwd TEXT, title TEXT, archived INTEGER)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)', [('live', str(kept), 10, 20, 'cli', str(self.root), 'on the floor', 0),
                                                                            ('arch', str(kept), 11, 21, 'cli', str(self.root), 'archived', 1)])
        o = server.Observer()
        self.assertEqual(sorted(r['id'] for r in o.rows()), ['arch', 'live'])
        by = {s['id']: s for s in o.sessions(FakeBridge())}
        self.assertEqual(by['arch'].get('archivedAt'), 21)

    def test_log_replacement_resets_cache(self):
        row = self.row()
        path = Path(row['rollout_path'])
        path.write_text(self.record('event_msg', {'type': 'task_started', 'turn_id': 't1'}))
        o = server.Observer()
        self.assertTrue(o.read_log(row)['turnActive'])
        other = self.root / 'new'
        other.write_text(self.record('event_msg', {'type': 'task_complete'}))
        other.replace(path)
        self.assertFalse(o.read_log(row)['turnActive'])

    def test_stale_lock_file_is_not_live_owner(self):
        p = self.home / 'thread-writer-locks/test.lock'
        p.touch()
        self.assertFalse(server.writer_locked('test'))
        with p.open('r') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertTrue(server.writer_locked('test'))
        self.assertFalse(server.writer_locked('test'))

    def test_never_resume_an_external_writer(self):
        w = server.Workspace(FakeBridge())
        with patch.object(server, 'writer_locked', return_value=True):
            with self.assertRaisesRegex(ValueError, 'another Codex client'):
                w.ensure_control('external')

    def test_file_paths_reject_traversal_and_symlink_escapes(self):
        project = self.root / 'project'
        project.mkdir()
        (project / 'escape').symlink_to(self.home, target_is_directory=True)
        for value in ('../codex/auth.json', 'escape/auth.json', '.git/config'):
            with self.assertRaises(ValueError):
                server.file_path(str(project), value)
        self.assertEqual(server.file_path(str(project), 'ok.txt'), project.resolve() / 'ok.txt')

    def test_only_structured_file_activity_is_reported(self):
        row = self.row()
        Path(row['rollout_path']).write_text(
            self.record('event_msg', {'type': 'item_completed', 'item': {'type': 'FileChange', 'changes': [{'path': str(self.root / 'app.py')}], 'id': 'f1'}})
            + self.record('event_msg', {'type': 'item_completed', 'item': {'type': 'CommandExecution', 'command': ['sh', '-c', 'cat app.py'], 'parsed_cmd': [{'type': 'read', 'path': 'app.py'}], 'aggregated_output': 'hello', 'id': 'c1'}})
            + self.record('response_item', {'type': 'message', 'role': 'assistant', 'content': [{'text': 'I might edit imaginary.py'}]})
        )
        log = server.Observer().read_log(row)
        self.assertEqual([(e['kind'], e['path']) for e in log['events']], [('write', 'app.py'), ('read', 'app.py')])
        self.assertIn('hello', log['messages'][0]['text'])

    def test_a_turn_you_stopped_reads_stopped_with_its_moment(self):          # walk 2026-11-19: a stop leaves a trace
        row = self.row()
        ev = lambda typ, **k: self.record('event_msg', {'type': typ, **k})
        Path(row['rollout_path']).write_text(ev('task_started', turn_id='t1') + ev('task_complete', turn_id='t1') + ev('task_started', turn_id='t2') + ev('turn_aborted', turn_id='t2', reason='interrupted'))
        log = server.Observer().read_log(row)
        self.assertEqual((log['ended'], [x['turn'] for x in log['stops']], log['turnActive']), ('stopped', ['t2'], False))
        at = server.Workspace(FakeBridge()).attention_of({'id': row['id'], 'parent': None}, log)
        self.assertEqual((at['ended'], len(at['stops'])), ('stopped', 1))
        with Path(row['rollout_path']).open('a') as f:          # a turn after it, which ends of itself: finished again, the stop still in its chat
            f.write(ev('task_started', turn_id='t3') + ev('task_complete', turn_id='t3'))
        log = server.Observer().read_log(row)
        self.assertEqual((log['ended'], len(log['stops'])), ('finished', 1))

    def test_a_stop_through_the_page_is_kept_when_the_record_shows_none(self):          # walk 2026-11-19: Claude Code stops
        tmp = self.root / 'cache' / 'stops.json'
        w = server.Workspace(FakeBridge()); w.stops_path = tmp
        log = {'messages': [{'role': 'assistant', 'text': 'Reading the notes', 't': 1000.0}], 'turns': [{'t': 990.0}]}          # a Claude Code transcript: no interrupted mark
        w.stop_keep('cc1', at=1010.0)
        w2 = server.Workspace(FakeBridge()); w2.stops_path = tmp          # the server started again: read back
        at = w2.attention_of({'id': 'cc1', 'parent': None}, log)
        self.assertEqual((at['ended'], at['stops']), ('stopped', [{'at': 1010.0}]))
        log['messages'].append({'role': 'user', 'text': 'go on', 't': 1020.0})          # a message after it: no longer how it stands, still in its chat
        at = w2.attention_of({'id': 'cc1', 'parent': None}, log)
        self.assertEqual((at['ended'], len(at['stops'])), (None, 1))
        codex = {'messages': [], 'turns': [{'t': 990.0}], 'stops': [{'t': 1031.0, 'turn': 't2', 'reason': 'interrupted'}], 'ended': 'stopped'}
        w2.stop_keep('cx1', at=1030.0)          # a Codex stop: its own turn_aborted too, one stop, not two
        self.assertEqual(w2.attention_of({'id': 'cx1', 'parent': None}, codex)['stops'], [{'at': 1031.0}])
        slow = {'messages': [], 'turns': [{'t': 990.0}], 'stops': [{'t': 1100.0, 'turn': 't2', 'reason': 'interrupted'}], 'ended': 'stopped'}
        w2.stop_keep('cx2', at=1070.0)          # a slow interrupt: its record 30 s later, no turn started between: the same stop
        self.assertEqual(w2.attention_of({'id': 'cx2', 'parent': None}, slow)['stops'], [{'at': 1100.0}])
        w2.stop_keep('cx3', at=1070.0, turn='t2')          # the same turn named on both sides: one stop, whenever its record came
        self.assertEqual(w2.attention_of({'id': 'cx3', 'parent': None}, dict(slow, turns=[{'t': 990.0}, {'t': 1080.0}]))['stops'], [{'at': 1100.0}])
        later = dict(slow, turns=[{'t': 990.0}, {'t': 1080.0}])          # a turn started between them, no turn named: two stops
        w2.stop_keep('cx4', at=1070.0)
        self.assertEqual(w2.attention_of({'id': 'cx4', 'parent': None}, later)['stops'], [{'at': 1070.0}, {'at': 1100.0}])

    def test_a_message_in_both_forms_is_one_message_in_either_order(self):
        row = self.row()
        ri = lambda role, text: self.record('response_item', {'type': 'message', 'role': role, 'content': [{'text': text}]})
        ic = lambda kind, text: self.item({'type': kind, 'content': [{'text': text}]})
        Path(row['rollout_path']).write_text(
            ri('user', 'Open the PR') + ic('UserMessage', 'Open the PR')              # response_item first (the fixtures, mock.py)
            + ic('AgentMessage', 'Done.') + ri('assistant', 'Done.')                  # item_completed first
            + ri('assistant', 'Done.') + ic('AgentMessage', 'Done.')                  # the same words said again, on purpose
        )
        log = server.Observer().read_log(row)
        self.assertEqual([(m['role'], m['text']) for m in log['messages']], [('user', 'Open the PR'), ('assistant', 'Done.'), ('assistant', 'Done.')])

    def count(self, total, window=475000):
        usage = {'input_tokens': total, 'cached_input_tokens': 0, 'output_tokens': 5, 'reasoning_output_tokens': 0, 'total_tokens': total + 5}
        return self.record('event_msg', {'type': 'token_count', 'info': {'total_token_usage': usage, 'last_token_usage': usage, 'model_context_window': window}})

    def item(self, item):
        return self.record('event_msg', {'type': 'item_completed', 'item': item})

    def test_token_readings_give_each_item_its_share_by_text_length(self):
        row = self.row()
        Path(row['rollout_path']).write_text(
            self.record('event_msg', {'type': 'task_started', 'turn_id': 't1'})
            + self.item({'type': 'UserMessage', 'id': 'u1', 'content': [{'text': 'x' * 40}]})
            + self.count(18000)
            + self.item({'type': 'AgentMessage', 'id': 'a1', 'content': [{'text': 'y' * 300}]})
            + self.item({'type': 'CommandExecution', 'id': 'c1', 'command': 'cat app.py', 'parsed_cmd': [{'type': 'read', 'path': 'app.py'}], 'aggregated_output': 'z' * 288})  # 300 characters with the command
            + self.count(18600)
            + self.item({'type': 'AgentMessage', 'id': 'a2', 'content': [{'text': 'w' * 80}]})
        )
        log = server.Observer().read_log(row)
        by = {m['id']: m for m in log['messages']}
        self.assertEqual(log['base']['tokens'], 18000 - 10)          # the first reading less the user's text / 4
        self.assertEqual(by['u1']['tokens'], 10)
        self.assertEqual(by['a1']['tokens'] + by['c1']['tokens'], 600)  # the growth between the two readings, split by length
        self.assertEqual(by['a1']['tokens'], 300)
        self.assertEqual(log['events'][0]['tokens'], by['c1']['tokens'])  # the read shares its command's tokens
        self.assertEqual(log['events'][0]['item'], 'c1')
        self.assertEqual(by['a2']['tokens'], 20)                     # after the last reading: text length / 4 until the next
        self.assertEqual([c['context'] for c in log['contexts']], [18000, 18600])
        self.assertEqual((log['context'], log['window'], log['compacts']), (18600, 475000, []))

    def test_compaction_marker_and_a_context_drop_become_compactions(self):
        row = self.row()
        path = Path(row['rollout_path'])
        path.write_text(
            self.item({'type': 'UserMessage', 'id': 'u1', 'content': [{'text': 'x' * 40}]})
            + self.count(108000)
            + self.record('compacted', {'message': '', 'replacement_history': []})
            + self.item({'type': 'ContextCompaction', 'id': 'k1'})
            + self.count(0)                                          # Codex writes a zero reading right after a compaction
            + self.item({'type': 'UserMessage', 'id': 'u2', 'content': [{'text': 'v' * 400}]})
            + self.count(22000)
        )
        o = server.Observer()
        log = o.read_log(row)
        self.assertEqual(len(log['compacts']), 1)                   # one compaction, though both markers appear
        c = log['compacts'][0]
        self.assertEqual((c['from'], c['to']), (108000, 22000 - 100))
        self.assertEqual(log['context'], 22000)
        with path.open('a') as f:
            f.write(self.item({'type': 'AgentMessage', 'id': 'a1', 'content': [{'text': 'y' * 40}]}) + self.count(9000))
        log = o.read_log(row)
        self.assertEqual([(c['from'], c['to']) for c in log['compacts']], [(108000, 21900), (22000, 9000 - 10)])

    # chat-log: reasoning summaries ------------------------------------------------------------------------------------------
    def reasoning_log(self):
        row = self.row()
        rs = 'rs_1'
        lines = [
            self.record('event_msg', {'type': 'task_started', 'turn_id': 'turn1'}),
            self.record('event_msg', {'type': 'item_completed', 'turn_id': 'turn1', 'item': {'type': 'UserMessage', 'id': 'u1', 'content': [{'type': 'text', 'text': 'List the files'}]}}),
            # the event form first (it carries the times), then the response form of the same item: one entry
            self.record('event_msg', {'type': 'item_completed', 'turn_id': 'turn1', 'item': {'type': 'Reasoning', 'id': rs, 'summary_text': ['**Listing files**', 'Then one sentence.'], 'raw_content': []},
                                      'started_at_ms': 1793811522248, 'completed_at_ms': 1793811545182}),
            self.record('response_item', {'type': 'reasoning', 'id': rs, 'summary': [{'type': 'summary_text', 'text': '**Listing files**'}, {'type': 'summary_text', 'text': 'Then one sentence.'}], 'encrypted_content': 'x'}),
            # encrypted only: nothing to show, not kept
            self.record('event_msg', {'type': 'item_completed', 'turn_id': 'turn1', 'item': {'type': 'Reasoning', 'id': 'rs_2', 'summary_text': [], 'raw_content': []}}),
            self.record('response_item', {'type': 'reasoning', 'id': 'rs_2', 'summary': [], 'encrypted_content': 'x'}),
            # a response form alone (older logs), and raw reasoning text when there is no summary
            self.record('response_item', {'type': 'reasoning', 'id': 'rs_3', 'summary': [{'type': 'summary_text', 'text': 'Only the response form'}]}),
            self.record('event_msg', {'type': 'item_completed', 'turn_id': 'turn1', 'item': {'type': 'Reasoning', 'id': 'rs_4', 'summary_text': [], 'raw_content': ['raw thought']}}),
            self.record('event_msg', {'type': 'item_completed', 'turn_id': 'turn1', 'item': {'type': 'AgentMessage', 'id': 'a1', 'content': [{'type': 'text', 'text': 'Two files.'}]}}),
        ]
        Path(row['rollout_path']).write_text(''.join(lines))
        return row

    def test_reasoning_summaries_are_kept_once_per_item_with_their_times(self):
        log = server.Observer().read_log(self.reasoning_log())
        self.assertEqual([(r['id'], r['text']) for r in log['reasoning']],
                         [('rs_1', '**Listing files**\n\nThen one sentence.'), ('rs_3', 'Only the response form'), ('rs_4', 'raw thought')])
        first = log['reasoning'][0]
        self.assertEqual((first['kind'], first['t'], first['end'], first['turn']), ('reasoning', 1793811522.248, 1793811545.182, 'turn1'))
        stamp = server.timestamp('2026-11-04T17:00:00Z')
        self.assertEqual((log['reasoning'][1]['t'], log['reasoning'][1]['end']), (stamp, stamp))   # no times recorded: the record's
        # reasoning stays out of the messages (and so out of the preview, the turn labels and the token shares)
        self.assertEqual([m['role'] for m in log['messages']], ['user', 'assistant'])

    def test_history_exposes_reasoning_items(self):
        row = self.reasoning_log()
        w = server.Workspace(FakeBridge())
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        http.workspace = w
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        with patch.object(w, 'session', return_value=row):
            body = json.load(urllib.request.urlopen('http://127.0.0.1:%d/api/history?id=test' % http.server_port, timeout=3))
        self.assertEqual([r['kind'] for r in body['reasoning']], ['reasoning'] * 3)
        self.assertEqual(body['reasoning'][0]['text'], '**Listing files**\n\nThen one sentence.')
        self.assertEqual(len(body['messages']), 2)
        self.assertEqual(body['turns'], [{'id': 'turn1', 't': server.timestamp('2026-11-04T17:00:00Z')}])

    def test_send_asks_for_reasoning_summaries(self):
        calls = []
        class B(FakeBridge):
            loaded = {'t1'}
            active = {}
            threads = {'t1': {'id': 't1', 'cwd': '/x'}}
            def call(self, method, params):
                calls.append((method, params))
                return {'turn': {'id': 'turn1', 'status': 'inProgress'}}
        w = server.Workspace(B())
        w.action('send', {'threadId': 't1', 'text': 'hello'})
        self.assertEqual(calls[0][0], 'turn/start')
        self.assertEqual(calls[0][1]['summary'], 'detailed')
    # end chat-log -------------------------------------------------------------------------------------------------------------

    def test_empty_new_agent_is_visible_before_first_turn(self):
        b = FakeBridge()
        b.threads = {'new': {'id': 'new', 'cwd': str(self.root), 'name': 'New work'}}
        w = server.Workspace(b)
        self.assertEqual(w.sessions()[0]['title'], 'New work')
        self.assertTrue(w.sessions()[0]['managed'])
        self.assertIsNotNone(w.session('new'))


class SubagentNameTests(unittest.TestCase):
    def test_task_name_then_nickname(self):
        self.assertEqual(server.subagent_name({'agent_path': '/root/page_ui', 'agent_nickname': 'Harvey'}), 'page ui')
        self.assertEqual(server.subagent_name({'agent_path': '/root', 'agent_nickname': 'Harvey'}), 'Harvey')
        self.assertEqual(server.subagent_name({'agent_nickname': 'Harvey'}), 'Harvey')
        self.assertIsNone(server.subagent_name({'cwd': '/x'}))

    def test_thread_spawn_from_row(self):
        row = {'source': json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': 'p1', 'agent_path': '/root/readme', 'agent_nickname': 'Ada'}}})}
        self.assertEqual(server.thread_spawn(row)['parent_thread_id'], 'p1')
        self.assertEqual(server.subagent_name(server.thread_spawn(row)), 'readme')
        self.assertEqual(server.thread_spawn({'source': 'vscode'}), {})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / 'hello.txt'
        self.file.write_text('initial')
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.http.daemon_threads = True
        self.http.workspace = server.Workspace(FakeBridge())
        self.base = 'http://127.0.0.1:' + str(self.http.server_port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers or {})
        return urllib.request.urlopen(req, timeout=3)

    def test_cross_origin_and_rebinding_rejected(self):
        for headers in ({'Origin': 'https://evil.example'}, {'Host': 'evil.example'}):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.request('/api/models', headers=headers)
            self.assertEqual(e.exception.code, 400)

    def test_mutation_requires_workspace_token(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request('/api/file', {})
        self.assertEqual(e.exception.code, 403)

    def test_folder_chooser_requires_workspace_token(self):
        with patch.object(server.subprocess, 'run', side_effect=AssertionError('the chooser must not open')):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.request('/api/pick-folder', {})
            self.assertEqual(e.exception.code, 403)

    def test_folder_chooser_returns_path_or_cancelled(self):
        token = {'X-Workspace-Token': self.http.workspace.token}
        done = subprocess.CompletedProcess([], 0, stdout='/Users/me/project/\n', stderr='')
        with patch.object(server.subprocess, 'run', return_value=done) as run:
            self.assertEqual(json.load(self.request('/api/pick-folder', {}, token)), {'path': '/Users/me/project'})
            self.assertEqual(run.call_args.kwargs['timeout'], 120)
        cancel = subprocess.CompletedProcess([], 1, stdout='', stderr='User canceled. (-128)')
        with patch.object(server.subprocess, 'run', return_value=cancel):
            self.assertEqual(json.load(self.request('/api/pick-folder', {}, token)), {'cancelled': True})

    def test_file_save_rejects_concurrent_agent_edits(self):
        params = {'cwd': str(self.root), 'path': 'hello.txt', 'text': 'user edit', 'version': hashlib.sha256(b'initial').hexdigest()}
        self.file.write_text('agent edit')
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request('/api/file', params, {'X-Workspace-Token': self.http.workspace.token})
        self.assertEqual(e.exception.code, 409)
        self.assertEqual(self.file.read_text(), 'agent edit')
        params['version'] = hashlib.sha256(b'agent edit').hexdigest()
        self.request('/api/file', params, {'X-Workspace-Token': self.http.workspace.token}).close()
        self.assertEqual(self.file.read_text(), 'user edit')


    def test_file_read_carries_its_modified_time_and_a_save_the_new_one(self):          # the file window's title: how old the file is
        os.utime(self.file, (1_700_000_000, 1_700_000_000))
        got = json.load(self.request('/api/file?' + urllib.parse.urlencode({'cwd': str(self.root), 'path': 'hello.txt'})))
        self.assertEqual(got['text'], 'initial')
        self.assertEqual(got['version'], hashlib.sha256(b'initial').hexdigest())
        self.assertEqual(got['mtime'], 1_700_000_000)
        params = {'cwd': str(self.root), 'path': 'hello.txt', 'text': 'saved', 'version': got['version']}
        saved = json.load(self.request('/api/file', params, {'X-Workspace-Token': self.http.workspace.token}))
        self.assertEqual(saved['version'], hashlib.sha256(b'saved').hexdigest())
        self.assertAlmostEqual(saved['mtime'], self.file.stat().st_mtime, places=3)

    def test_favicon_is_served_and_the_logo_drafts_are_not(self):          # the tab's icon beside the page; brand/logo-draft stays out
        with self.request('/brand/favicon.svg') as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), (server.ROOT / 'brand' / 'favicon.svg').read_bytes())
            self.assertEqual(r.headers['Content-Type'], 'image/svg+xml')
        for name in ('brand/logo-draft/mark-small.svg', 'brand/logo-draft/make.py', 'brand/ATTRIBUTION.md', 'brand/nothing.svg'):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.request('/' + name)
            self.assertEqual(e.exception.code, 404)

    def test_vendor_parser_files_are_served_read_only_and_nothing_else_there(self):          # the file window's parser beside the page
        for name in ('vendor/tree-sitter.js', 'vendor/tree-sitter.wasm', 'vendor/tree-sitter-swift.wasm'):
            with self.request('/' + name) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.read(), (server.ROOT / name).read_bytes())
                if name.endswith('.wasm'):
                    self.assertEqual(r.headers['Content-Type'], 'application/wasm')
                csp = r.headers['Content-Security-Policy']
                self.assertIn("'wasm-unsafe-eval'", csp)          # WebAssembly may compile
                self.assertNotIn("'unsafe-eval'", csp.replace("'wasm-unsafe-eval'", ''))          # JavaScript eval may not
        for name in ('vendor/LICENSES.md', 'vendor/../server.py', 'vendor/nothing.js'):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.request('/' + name)
            self.assertEqual(e.exception.code, 404)


class ProtocolTests(unittest.TestCase):
    def test_rpc_responses_notifications_and_approvals(self):
        code = '''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize':
  print(json.dumps({'id':m['id'],'result':{}}),flush=True)
 elif m.get('method')=='test':
  print(json.dumps({'method':'turn/started','params':{'threadId':'a','turn':{'id':'t'}}}),flush=True)
  print(json.dumps({'id':88,'method':'item/commandExecution/requestApproval','params':{'threadId':'a','command':'echo hello'}}),flush=True)
  print(json.dumps({'id':m['id'],'result':{'ok':True}}),flush=True)
 elif m.get('id')==88 and 'result' in m:
  print(json.dumps({'method':'serverRequest/resolved','params':{'threadId':'a','requestId':88}}),flush=True)
'''
        b = server.Bridge([sys.executable, '-u', '-c', code])
        try:
            self.assertEqual(b.call('test'), {'ok': True})
            self.assertEqual(b.active, {'a': 't'})
            self.assertIn('88', b.requests)
            with self.assertRaises(ValueError):
                b.reply('88', {'decision': 'invalid'})
            b.reply('88', {'decision': 'decline'})
            self.assertNotIn('88', b.requests)
        finally:
            b.close()
            b.process.wait(timeout=3)
            b.process.stdin.close()
            b.process.stdout.close()
            b.process.stderr.close()


# ---- chat controls (branch ide-chat-controls): start parameters, per-turn overrides, compact / review / init, status, MCP, commands ----
class RecordingBridge(FakeBridge):
    """Answers the calls the chat controls make and records every one."""
    def __init__(self, replies=None):
        self.loaded, self.threads, self.active, self.requests = set(), {}, {}, {}
        self.settings, self.mcp, self.usage, self.rate = {}, {}, {}, None
        self.calls, self.replies = [], replies or {}

    def call(self, method, params=None, timeout=45):
        self.calls.append((method, params))
        r = self.replies.get(method, {})
        return r(params) if callable(r) else r


class ChatControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        (self.home / 'thread-writer-locks').mkdir(parents=True)
        self.patch = patch.object(server, 'CODEX_HOME', self.home)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)
        started = {'thread': {'id': 'n1', 'cwd': str(self.root)}, 'model': 'gpt-6-luna', 'reasoningEffort': 'low', 'approvalPolicy': 'on-request',
                   'sandbox': {'type': 'readOnly', 'networkAccess': False}, 'activePermissionProfile': {'id': ':read-only', 'extends': None}, 'instructionSources': ['/x/AGENTS.md'], 'cwd': str(self.root)}
        self.b = RecordingBridge({'thread/start': started, 'turn/start': {'turn': {'id': 't1', 'status': 'inProgress'}}})
        self.w = server.Workspace(self.b)

    def start(self, **extra):
        return self.w.action('start', {'cwd': str(self.root), **extra})

    def test_start_passes_model_effort_and_approval_mode_in_codex_names(self):
        self.start(model='gpt-6-luna', effort='low', approvalPolicy='on-request', sandbox='read-only')
        method, params = self.b.calls[-1]
        self.assertEqual(method, 'thread/start')
        self.assertEqual((params['model'], params['approvalPolicy'], params['sandbox']), ('gpt-6-luna', 'on-request', 'read-only'))
        self.assertEqual(params['config'], {'model_reasoning_effort': 'low', 'tools.update_plan.enabled': True})      # thread/start has no effort field; plan on by default
        st = self.w.settings_of('n1')
        self.assertEqual((st['model'], st['effort'], st['sandbox'], st['profile'], st['instructionSources']), ('gpt-6-luna', 'low', 'read-only', ':read-only', ['/x/AGENTS.md']))

    def test_start_defaults_and_full_access(self):
        self.start()
        params = self.b.calls[-1][1]
        self.assertEqual((params['approvalPolicy'], params['sandbox']), ('on-request', 'workspace-write'))
        self.assertEqual(params['config'], {'tools.update_plan.enabled': True})      # plan toggle: Codex's default is on
        self.start(approvalPolicy='never', sandbox='danger-full-access')
        self.assertEqual(self.b.calls[-1][1]['sandbox'], 'danger-full-access')

    def test_start_rejects_names_codex_does_not_have(self):
        with self.assertRaises(ValueError):
            self.start(sandbox='everything')
        with self.assertRaises(ValueError):
            self.start(approvalPolicy='always')
        self.assertEqual(self.b.calls, [])

    def test_start_carries_the_plan_toggle_to_codex(self):
        # plan toggle: the setting goes to this thread only, as a thread/start config override, on or off as asked
        self.start(plan=False)
        self.assertEqual(self.b.calls[-1][1]['config'], {'tools.update_plan.enabled': False})
        self.assertIs(self.w.settings_of('n1')['plan'], False)
        self.start(plan=True, effort='high')
        self.assertEqual(self.b.calls[-1][1]['config'], {'model_reasoning_effort': 'high', 'tools.update_plan.enabled': True})
        self.assertIs(self.w.settings_of('n1')['plan'], True)
        with self.assertRaises(ValueError):
            self.start(plan='yes')
        self.assertIsNone(self.w.plan_known('other'))
        self.assertNotIn('plan', self.w.settings_of('other'))          # not started here: nothing shown

    def test_plan_toggle_is_kept_between_runs_and_goes_again_on_resume(self):
        self.w.plan_path = self.root / 'cache' / 'plan-toggle.json'
        self.start(plan=False)
        w2 = server.Workspace(self.b)          # the server started again: the record is read back
        w2.plan_path = self.w.plan_path
        self.assertIs(w2.plan_known('n1'), False)
        self.b.calls.clear()
        self.b.loaded.clear()          # its app-server started again too: nothing loaded
        w2.ensure_control('n1')
        self.assertEqual(self.b.calls[-1], ('thread/resume', {'threadId': 'n1', 'excludeTurns': True, 'config': {'tools.update_plan.enabled': False}}))
        w2.ensure_control('n9')          # a thread this server never started: resumed as it is
        self.assertEqual(self.b.calls[-1], ('thread/resume', {'threadId': 'n9', 'excludeTurns': True}))

    def test_send_carries_per_turn_overrides(self):
        self.start()
        self.w.action('send', {'threadId': 'n1', 'text': 'hi', 'model': 'gpt-6-sol', 'effort': 'high', 'approvalPolicy': 'never', 'sandbox': 'danger-full-access'})
        method, params = self.b.calls[-1]
        self.assertEqual(method, 'turn/start')
        self.assertEqual((params['model'], params['effort'], params['approvalPolicy'], params['sandboxPolicy']), ('gpt-6-sol', 'high', 'never', {'type': 'dangerFullAccess'}))
        self.assertEqual(self.w.settings_of('n1')['sandbox'], 'danger-full-access')

    def test_plain_send_sends_no_overrides(self):
        self.start()
        self.w.action('send', {'threadId': 'n1', 'text': 'Continue'})
        params = self.b.calls[-1][1]
        self.assertEqual(set(params), {'threadId', 'input', 'summary'})      # summary is always asked for (chat-log)
        self.assertEqual(params['input'], [{'type': 'text', 'text': 'Continue'}])

    def test_steer_puts_overrides_on_the_thread(self):
        self.start()
        self.b.active['n1'] = 't9'
        self.w.action('send', {'threadId': 'n1', 'text': 'also this', 'effort': 'max', 'sandbox': 'workspace-write'})
        (m1, p1), (m2, p2) = self.b.calls[-2:]
        self.assertEqual((m1, m2), ('thread/settings/update', 'turn/steer'))
        self.assertEqual((p1['effort'], p1['sandboxPolicy']['type']), ('max', 'workspaceWrite'))
        self.assertEqual(p2['expectedTurnId'], 't9')

    def test_skill_chip_becomes_a_skill_input_item(self):
        self.start()
        self.w.action('send', {'threadId': 'n1', 'text': '$doc write it', 'skill': {'name': 'doc', 'path': '/s/doc/SKILL.md'}})
        self.assertEqual(self.b.calls[-1][1]['input'][0], {'type': 'skill', 'name': 'doc', 'path': '/s/doc/SKILL.md'})

    def test_compact_review_and_init(self):
        self.start()
        self.w.action('compact', {'threadId': 'n1'})
        self.assertEqual(self.b.calls[-1], ('thread/compact/start', {'threadId': 'n1'}))
        self.w.action('review', {'threadId': 'n1', 'text': ''})
        self.assertEqual(self.b.calls[-1][1]['target'], {'type': 'uncommittedChanges'})
        self.w.action('review', {'threadId': 'n1', 'text': 'the parser'})
        self.assertEqual(self.b.calls[-1][1]['target'], {'type': 'custom', 'instructions': 'the parser'})
        with patch.object(server, '_init_text', 'INIT PROMPT'):
            self.w.action('init', {'threadId': 'n1'})
        self.assertEqual(self.b.calls[-1][0], 'turn/start')
        self.assertEqual(self.b.calls[-1][1]['input'][0]['text'], 'INIT PROMPT')

    def test_notifications_update_settings_mcp_usage_and_limits(self):
        server.track(self.b, 'thread/settings/updated', {'threadId': 'n1', 'threadSettings': {'model': 'gpt-6-sol', 'effort': 'xhigh', 'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}}})
        server.track(self.b, 'mcpServer/startupStatus/updated', {'threadId': 'n1', 'name': 'docs', 'status': 'failed', 'error': 'no binary'})
        server.track(self.b, 'thread/tokenUsage/updated', {'threadId': 'n1', 'tokenUsage': {'last': {'inputTokens': 12000}, 'total': {'totalTokens': 30000}, 'modelContextWindow': 475000}})
        server.track(self.b, 'account/rateLimits/updated', {'rateLimits': {'primary': {'usedPercent': 4}, 'planType': None}})
        self.assertEqual(self.b.settings['n1']['sandbox'], 'danger-full-access')
        self.assertEqual(self.b.mcp['n1']['docs']['status'], 'failed')
        self.assertEqual(self.b.rate, {'primary': {'usedPercent': 4}})

    def test_status_card_data(self):
        self.start(model='gpt-6-luna', effort='low', approvalPolicy='on-request', sandbox='read-only')
        self.b.replies.update({'config/read': {'config': {'web_search': 'live'}}, 'account/rateLimits/read': {'rateLimits': {'primary': {'usedPercent': 2}}},
                               'mcpServerStatus/list': {'data': [{'name': 'docs', 'runtimeStatus': 'connected', 'tools': {'a': {}, 'b': {}}, 'toolsError': None, 'authStatus': 'unsupported', 'pluginId': None}]}})
        server.track(self.b, 'thread/tokenUsage/updated', {'threadId': 'n1', 'tokenUsage': {'last': {'inputTokens': 12000}, 'total': {'totalTokens': 30000}, 'modelContextWindow': 475000}})
        st = self.w.status('n1')
        self.assertEqual((st['context']['tokens'], st['context']['window']), (12000, 475000))
        self.assertEqual(st['webSearch']['on'], True)
        self.assertEqual(st['mcp']['servers'][0], {'name': 'docs', 'status': 'connected', 'error': None, 'tools': 2, 'auth': 'unsupported', 'plugin': None})
        self.assertTrue(st['mcp']['threadScoped'])
        self.assertIn(('mcpServerStatus/list', {'detail': 'toolsAndAuthOnly', 'threadId': 'n1'}), self.b.calls)
        self.assertEqual(st['rateLimits'], {'primary': {'usedPercent': 2}})

    def test_web_search_reading(self):
        self.assertEqual(server.web_search_of({'web_search': 'disabled'})['on'], False)
        self.assertEqual(server.web_search_of({'web_search': 'cached'})['on'], True)
        self.assertIsNone(server.web_search_of({})['on'])      # not set: Codex's default is not reported by the protocol

    def test_saved_sandbox_policies_read_as_mode_names(self):
        managed = {'type': 'managed', 'file_system': {'type': 'restricted', 'entries': [{'path': {'type': 'special', 'value': {'kind': 'root'}}, 'access': 'read'}, {'path': {'type': 'path', 'path': '/p'}, 'access': 'write'}]}}
        self.assertEqual(server.sandbox_mode(json.dumps(managed)), 'workspace-write')
        managed['file_system']['entries'].pop()
        self.assertEqual(server.sandbox_mode(managed), 'read-only')
        self.assertEqual(server.sandbox_mode('{"type":"disabled"}'), 'danger-full-access')
        self.assertEqual(server.sandbox_policy('read-only'), {'type': 'readOnly', 'networkAccess': False})

    def test_archive_is_a_mark_on_the_page_and_never_reaches_the_harness(self):
        # archiving is for now only a visual cue (2026-11-19): /archive is handled on the page (a mark it keeps); no
        # command of either harness names thread/archive, and the server has no archive op left to reach it
        import claude_bridge
        for table in (server.COMMANDS, claude_bridge.COMMANDS):
            arch = [c for c in table if c['name'] == 'archive']
            self.assertEqual([c['how'] for c in arch], ['local'])
            self.assertFalse([c for c in table if 'archive' in str(c.get('method', '')) and 'thread/' in str(c.get('method', ''))])
        self.start()
        with self.assertRaises(ValueError):
            self.w.action('archive', {'threadId': 'n1'})
        self.assertNotIn('thread/archive', [m for m, _ in self.b.calls])

    def test_controls_list_commands_presets_and_skills(self):
        self.b.replies['skills/list'] = {'data': [{'cwd': '/p', 'skills': [{'name': 'doc', 'description': 'long', 'interface': {'shortDescription': 'short'}, 'path': '/s/SKILL.md', 'enabled': True},
                                                                         {'name': 'off', 'description': '', 'path': '/o', 'enabled': False}]}]}
        c = self.w.controls('/p')
        names = [x['name'] for x in c['commands']]
        for n in ('compact', 'status', 'review', 'diff', 'model', 'init', 'mention', 'new'):
            self.assertIn(n, names)
        self.assertEqual([p['label'] for p in c['presets']], ['Read Only', 'Default', 'Full Access'])
        self.assertEqual(c['skills'], [{'name': 'doc', 'description': 'short', 'path': '/s/SKILL.md'}])
        self.b.replies['config/read'] = {'config': {'model': 'gpt-6-luna', 'model_reasoning_effort': 'medium', 'approval_policy': 'on-request', 'sandbox_mode': 'workspace-write'}}
        self.assertEqual(self.w.controls('/p')['config'], {'model': 'gpt-6-luna', 'effort': 'medium', 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write'})


class ChatControlHTTPTests(unittest.TestCase):
    def setUp(self):
        self.b = RecordingBridge({'skills/list': {'data': []}})
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.http.daemon_threads = True
        self.http.workspace = server.Workspace(self.b)
        self.base = 'http://127.0.0.1:' + str(self.http.server_port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path, timeout=3))

    def test_controls_endpoint(self):
        r = self.get('/api/controls?cwd=/tmp')
        self.assertIn('presets', r)
        self.assertEqual(self.b.calls[-2:], [('skills/list', {'cwds': ['/tmp']}), ('config/read', {'cwd': '/tmp'})])

    def test_quick_controls_answer_without_asking_codex(self):
        n = len(self.b.calls)
        r = self.get('/api/controls?quick=1&cwd=/tmp')
        self.assertEqual([p['label'] for p in r['presets']], ['Read Only', 'Default', 'Full Access'])
        self.assertTrue(r['partial'])
        self.assertEqual(self.b.calls[n:], [])

    def test_status_mcp_and_settings_need_a_known_agent(self):
        for path in ('/api/status?id=nope', '/api/mcp?id=nope', '/api/settings?id=nope'):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(self.base + path, timeout=3)
            self.assertEqual(e.exception.code, 400)

    def test_compact_requires_workspace_token(self):
        req = urllib.request.Request(self.base + '/api/compact', data=b'{"threadId":"x"}')
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.b.calls, [])
# ---- end chat controls ----
# approvals (ide-approvals): the reply each request type needs, checked against what reaches Codex
class ReplyValidationTests(unittest.TestCase):
    FAKE = """import sys,json
reqs=[
 {'id':1,'method':'item/commandExecution/requestApproval','params':{'threadId':'a','turnId':'t','itemId':'i1','startedAtMs':1,'kind':'command','command':"/bin/zsh -lc 'curl -sI https://example.com'",'cwd':'/p','reason':'network','availableDecisions':['accept',{'acceptWithExecpolicyAmendment':{'execpolicy_amendment':['curl']}},'cancel']}},
 {'id':2,'method':'item/fileChange/requestApproval','params':{'threadId':'a','turnId':'t','itemId':'i2','startedAtMs':1,'reason':None,'grantRoot':None}},
 {'id':3,'method':'item/tool/requestUserInput','params':{'threadId':'a','turnId':'t','itemId':'i3','isBlocking':True,'autoResolutionMs':None,'questions':[{'id':'name','header':'Name','question':'A or B?','isOther':False,'isSecret':False,'options':[{'label':'A','description':''},{'label':'B','description':''}]},{'id':'why','header':'Why','question':'Why?','isOther':True,'isSecret':False,'options':None}]}},
 {'id':4,'method':'item/permissions/requestApproval','params':{'threadId':'a','turnId':'t','itemId':'i4','environmentId':None,'startedAtMs':1,'cwd':'/p','reason':'net','permissions':{'network':{'enabled':True},'fileSystem':None}}},
 {'id':5,'method':'mcpServer/elicitation/request','params':{'threadId':'a','turnId':None,'serverName':'notes','mode':'form','_meta':None,'message':'Which?','requestedSchema':{'type':'object','properties':{}}}},
 {'id':6,'method':'item/tool/call','params':{'threadId':'a'}},
]
for line in sys.stdin:
 m=json.loads(line)
 if m.get('method')=='initialize':
  print(json.dumps({'id':m['id'],'result':{}}),flush=True)
 elif m.get('method')=='ask':
  for r in reqs: print(json.dumps(r),flush=True)
  print(json.dumps({'id':m['id'],'result':{}}),flush=True)
 elif m.get('method')=='replies':
  print(json.dumps({'id':m['id'],'result':{'got':got}}),flush=True)
 elif 'result' in m:
  got=globals().setdefault('got',{}); got[str(m['id'])]=m['result']
"""

    def setUp(self):
        self.b = server.Bridge([sys.executable, '-u', '-c', 'got={}\n' + self.FAKE])
        self.b.call('ask')
        self.addCleanup(self.close)

    def close(self):
        self.b.close()
        self.b.process.wait(timeout=3)
        for f in (self.b.process.stdin, self.b.process.stdout, self.b.process.stderr):
            f.close()

    def sent(self):
        return self.b.call('replies')['got']

    def test_answers_given_through_the_page_are_kept_between_runs(self):          # walk 2026-11-19: an "allowed once" outlives a reload and a restart
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        w = server.Workspace(self.b); w.answers_path = Path(tmp.name) / 'cache' / 'answers.json'
        w.action('reply', {'id': '1', 'result': {'decision': 'accept'}, 'kept': {'threadId': 'a', 'key': 'item:i1', 't': 100.5, 'said': 'y', 'kind': 'approve', 'verb': 'run', 'cmd': 'curl -sI https://example.com'}})
        w.action('reply', {'id': '2', 'result': {'decision': 'decline'}, 'kept': {'threadId': 'a', 'key': 'item:i2', 't': 101, 'said': 'n', 'kind': 'approve', 'verb': 'change', 'cmd': 'a file change'}})
        w2 = server.Workspace(self.b); w2.answers_path = w.answers_path          # the server started again: the record is read back
        got = {r['key']: r for r in w2.answered() if r['said'] != 'ended'}          # robust: the refused item/tool/call is an 'ended' entry of its own
        self.assertEqual({k: (r['threadId'], r['said'], r['t'], r['cmd']) for k, r in got.items()},
                         {'item:i1': ('a', 'y', 100.5, 'curl -sI https://example.com'), 'item:i2': ('a', 'n', 101, 'a file change')})
        self.assertNotIn('id', got['item:i1'])          # never the request id: a number per connection, handed out again in a later run
        w2.answer_keep({'id': '1', 'result': {}, 'kept': {'threadId': 'a', 'key': 'item:i9', 'said': 'n'}}, {})          # the same id, another request: its own record
        self.assertEqual(sorted(r['key'] for r in w2.answered() if r['said'] != 'ended'), ['item:i1', 'item:i2', 'item:i9'])
        w2.answer_keep({'id': '3', 'result': {}, 'kept': {'threadId': 'a', 'said': 'y'}}, {})          # no identity of its own: not kept
        w2.answer_keep({'id': '4', 'result': {}, 'kept': {'threadId': 'a', 'key': 'item:i4', 'said': 'maybe'}}, {})          # not one of the page's answers: not kept
        self.assertEqual(len([r for r in w2.answered() if r['said'] != 'ended']), 3)

    def test_command_approval_takes_accept_decline_or_cancel_only(self):
        for bad in ({'decision': 'acceptForSession'}, {'decision': 'yes'}, {}, 'accept', None, {'decision': 'decline'}):  # decline is not offered here
            with self.assertRaises(ValueError):
                self.b.reply('1', bad)
        self.b.reply('1', {'decision': 'accept', 'extra': 'dropped'})
        self.assertEqual(self.sent()['1'], {'decision': 'accept'})
        with self.assertRaises(ValueError):
            self.b.reply('1', {'decision': 'accept'})  # already answered

    def test_command_approval_takes_the_rule_codex_proposed(self):          # piece B: "don't ask again" only as Codex offered it
        for bad in ({'decision': {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': ['rm']}}}, {'decision': {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': []}}},
                    {'decision': {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': ['curl']}, 'x': 1}}):
            with self.assertRaises(ValueError, msg=bad):
                self.b.reply('1', bad)
        rule = {'acceptWithExecpolicyAmendment': {'execpolicy_amendment': ['curl']}}
        self.b.reply('1', {'decision': rule, 'message': 'dropped'})
        self.assertEqual(self.sent()['1'], {'decision': rule})

    def test_file_change_takes_accept_for_session_when_no_list_is_sent(self):          # piece B: no availableDecisions: the schema's four
        self.b.reply('2', {'decision': 'acceptForSession'})
        self.assertEqual(self.sent()['2'], {'decision': 'acceptForSession'})

    def test_command_approval_cancel_when_decline_is_not_offered(self):
        self.b.reply('1', {'decision': 'cancel'})
        self.assertEqual(self.sent()['1'], {'decision': 'cancel'})

    def test_file_change_approval_decline(self):
        self.b.reply('2', {'decision': 'decline'})
        self.assertEqual(self.sent()['2'], {'decision': 'decline'})

    def test_question_answers_must_cover_every_question_with_strings(self):
        bad = [{'answers': []}, {'answers': {'name': {'answers': ['A']}}}, {'answers': {'name': {'answers': [1]}, 'why': {'answers': ['x']}}},
               {'answers': {'name': ['A'], 'why': {'answers': ['x']}}}, {'answers': {'name': {'answers': ['A']}, 'why': {'answers': ['x']}, 'other': {'answers': ['y']}}}]
        for r in bad:
            with self.assertRaises(ValueError, msg=r):
                self.b.reply('3', r)
        ok = {'answers': {'name': {'answers': ['B']}, 'why': {'answers': ['shorter']}}}
        self.b.reply('3', ok)
        self.assertEqual(self.sent()['3'], ok)

    def test_additional_access_is_granted_only_as_asked_and_answered(self):          # robust: the person's answer, not a silent no
        with self.assertRaises(ValueError):
            self.b.reply('4', {'decision': 'cancel'})          # not an answer this request takes
        self.b.reply('4', {'decision': 'accept', 'permissions': {'fileSystem': {'write': ['/']}}})          # the page cannot widen it
        self.assertEqual(self.sent()['4'], {'permissions': {'network': {'enabled': True}}, 'scope': 'turn'})

    def test_mcp_elicitation_with_an_empty_form_takes_accept_with_empty_content(self):          # robust: the person's answer
        self.b.reply('5', {'action': 'accept', 'content': {'x': 1}})
        self.assertEqual(self.sent()['5'], {'action': 'accept', 'content': {}, '_meta': None})

    def test_unsupported_request_type_is_refused(self):          # robust: refused at once, never left waiting
        self.assertNotIn('6', self.b.requests)
        with self.assertRaises(ValueError):
            self.b.reply('6', {})
        self.assertEqual([(w['method'], w['why']) for w in self.b.ended], [('item/tool/call', 'unsupported')])
# ---------- providers (harness slice): Claude Code observation and the merge with Codex ----------
from types import SimpleNamespace
from providers.claude_code import ClaudeCodeProvider, encode
from providers.codex import CodexProvider

FIXTURE = Path(__file__).resolve().parent / 'providers' / 'fixtures' / 'claude-code-session.jsonl'
FIXTURE_CWD = '/Users/someone/project'      # the redacted records' cwd


def tokens():
    return SimpleNamespace(unit=server.unit, settle=server.settle, reading=server.reading, compaction=server.compaction, estimate=server.estimate)


def claude_store(root, cwd, name='s1', fixture_cwd=FIXTURE_CWD):
    """A ~/.claude with the fixture as one session whose records say `cwd`."""
    home = root / 'claude'
    folder = home / 'projects' / encode(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    text = FIXTURE.read_text().replace(fixture_cwd, cwd)
    (folder / (name + '.jsonl')).write_text(text)
    return home, folder


class ClaudeCodeProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def provider(self, home):
        return ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')

    def test_fixture_parses_into_the_codex_history_shape(self):
        home, _ = claude_store(self.root, FIXTURE_CWD)
        p = self.provider(home)
        listed = p.list_sessions(FIXTURE_CWD)
        self.assertEqual([(s['id'], s['provider'], s['title'], s['running'], s['parent']) for s in listed], [('claude:s1', 'claude', 'Fixture session', False, None)])
        self.assertEqual(p.list_sessions('/Users/someone/other'), [])
        h = p.read_history('claude:s1')
        self.assertEqual([(e['kind'], e['path']) for e in h['events']], [('run', None), ('read', 'src/app.py'), ('write', 'src/new.py'), ('delegate', None)])
        self.assertEqual([(m['role'], m.get('tool')) for m in h['messages']],
                         [('user', None), ('assistant', None), ('tool', 'Bash'), ('tool', 'Read'), ('tool', 'Write'), ('tool', 'Agent')])
        self.assertTrue(h['messages'][2]['text'].startswith('ls src\n\n'))       # the command, then its result
        self.assertEqual(h['messages'][3]['text'], 'Read src/app.py' + h['messages'][3]['text'][len('Read src/app.py'):])
        # context: input + cache creation + cache read of each response, read once per message id
        self.assertEqual([c['context'] for c in h['contexts']], [80259, 97696, 125877, 128878, 89784])
        self.assertEqual(h['base']['tokens'], 80259 - 100)               # the first reading less the prompt's 400 characters / 4
        self.assertEqual([{k: v for k, v in c.items() if k != 'summary'} for c in h['compacts']], [{'t': h['compacts'][0]['t'], 'from': 131000, 'to': 89784 - 100}])   # memory: the summary's text is MemoryTests'   # compact_boundary's preTokens; after = next reading less the summary
        self.assertEqual(sum(m['tokens'] for m in h['messages'][1:5]), 128878 - 80259)   # growth between readings goes to the items in between (the Agent call
        # comes after the last reading before the compaction, so it is settled at the compaction by its length)

    def test_incremental_tail_and_live_registration(self):
        home, folder = claude_store(self.root, FIXTURE_CWD)
        path = folder / 's1.jsonl'
        lines = path.read_text().splitlines(keepends=True)
        path.write_text(''.join(lines[:5]) + lines[5][:40])                # a partial line: its writer has not finished
        p = self.provider(home)
        self.assertEqual(len(p.read_history('claude:s1')['events']), 1)
        with path.open('a') as f:
            f.write(lines[5][40:] + ''.join(lines[6:]))
        self.assertEqual(len(p.read_history('claude:s1')['events']), 4)
        (home / 'sessions').mkdir()
        (home / 'sessions' / '1.json').write_text(json.dumps({'pid': os.getpid(), 'sessionId': 's1', 'cwd': FIXTURE_CWD, 'status': 'busy', 'name': 'Named in the app'}))
        p._live = (0, {})
        s = p.list_sessions(FIXTURE_CWD)[0]
        self.assertEqual((s['running'], s['title']), (True, 'Named in the app'))
        (home / 'sessions' / '1.json').write_text(json.dumps({'pid': 999999999, 'sessionId': 's1', 'status': 'busy'}))   # its process is gone
        p._live = (0, {})
        self.assertFalse(p.list_sessions(FIXTURE_CWD)[0]['running'])

    def test_subagents_are_linked_to_their_parent(self):
        home, folder = claude_store(self.root, FIXTURE_CWD)
        subs = folder / 's1' / 'subagents'
        subs.mkdir(parents=True)
        (subs / 'agent-a1.jsonl').write_text(FIXTURE.read_text())
        (subs / 'agent-a1.meta.json').write_text(json.dumps({'agentType': 'general-purpose', 'description': 'Look into it', 'toolUseId': 'toolu_x'}))
        listed = {s['id']: s for s in self.provider(home).list_sessions(FIXTURE_CWD)}
        self.assertEqual((listed['claude:s1/a1']['parent'], listed['claude:s1/a1']['title']), ('claude:s1', 'Look into it'))

    def test_a_stopped_turns_own_line_is_not_in_the_chat(self):
        # claude drive: after an interrupt Claude Code writes "No response requested." as a <synthetic> assistant line; it is
        # not the model's and stays out of the chat, while a <synthetic> usage-limit line and real text with the same words stay
        home, folder = claude_store(self.root, FIXTURE_CWD)
        p = self.provider(home)
        before = p.read_history('claude:s1')
        n, model = len(before['messages']), before['model']          # the history is one cached object, updated in place
        def rec(i, model, text):
            return json.dumps({'type': 'assistant', 'uuid': 'u%d' % i, 'timestamp': '2026-11-06T18:23:4%d.000Z' % i, 'cwd': FIXTURE_CWD,
                               'message': {'id': 'm%d' % i, 'model': model, 'role': 'assistant', 'content': [{'type': 'text', 'text': text}],
                                           'usage': {'input_tokens': 0, 'output_tokens': 0}}}) + '\n'
        with (folder / 's1.jsonl').open('a') as f:
            f.write(json.dumps({'type': 'user', 'uuid': 'u0', 'timestamp': '2026-11-06T18:23:40.000Z', 'cwd': FIXTURE_CWD,
                                'message': {'role': 'user', 'content': [{'type': 'text', 'text': '[Request interrupted by user]'}]}}) + '\n')
            f.write(rec(1, '<synthetic>', 'No response requested.'))
            f.write(rec(2, '<synthetic>', "You've hit your session limit"))
            f.write(rec(3, model or 'claude-haiku-5-5', 'No response requested.'))
        h = p.read_history('claude:s1')
        new = [(m['role'], m['text']) for m in h['messages'][n:]]
        self.assertEqual(new, [('user', '[Request interrupted by user]'), ('assistant', "You've hit your session limit"), ('assistant', 'No response requested.')])
        self.assertEqual(h['model'], model or 'claude-haiku-5-5')

    def test_folder_name_is_not_decoded(self):
        # my_project and my-project encode alike; the records' cwd decides which project a session belongs to
        home, _ = claude_store(self.root, '/Users/someone/my_project')
        p = self.provider(home)
        self.assertEqual(len(p.list_sessions('/Users/someone/my_project')), 1)
        self.assertEqual(p.list_sessions('/Users/someone/my-project'), [])


# ---------- continuation: one Claude Code conversation that goes on in a new transcript file (a rewind, --fork-session, /branch) ----------
CONTINUED = Path(__file__).resolve().parent / 'providers' / 'fixtures' / 'claude-code-continued'
CA, CB, CC = 'aaaaaaaa-0000-4000-8000-00000000000a', 'bbbbbbbb-0000-4000-8000-00000000000b', 'cccccccc-0000-4000-8000-00000000000c'


def continued_store(root, cwd, names=(CA, CB, CC)):
    """A ~/.claude whose project folder holds the made-up chain: A; B, a rewind of A; C, a rewind of B (records say `cwd`)."""
    home = root / 'claude'
    folder = home / 'projects' / encode(cwd)
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / (n + '.jsonl')).write_text((CONTINUED / (n + '.jsonl')).read_text().replace('/Users/someone/project', cwd))
    return home, folder


def crec(sid, uid, t, typ='user', text='x', **kw):
    """One made-up record in the fixtures' shape (compact JSON, as Claude Code writes)."""
    r = {'parentUuid': None, 'isSidechain': False, 'type': typ, 'uuid': uid, 'timestamp': '2026-11-02T' + t + '.000Z', 'cwd': FIXTURE_CWD, 'sessionId': sid}
    r['message'] = {'role': 'user', 'content': text} if typ == 'user' else {'model': 'claude-opus-5-5', 'id': 'msg_' + uid[:8], 'role': 'assistant',
                                                                          'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 20000, 'output_tokens': 5}}
    return json.dumps(dict(r, **kw), separators=(',', ':')) + '\n'


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def provider(self, home):
        return ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')

    def rows(self, p):
        return {s['id']: s.get('continues') or None for s in p.list_sessions(FIXTURE_CWD) if not s['parent']}

    def test_a_rewound_chain_is_one_column_with_one_history(self):
        home, folder = continued_store(self.root, FIXTURE_CWD)
        p = self.provider(home)
        listed = p.list_sessions(FIXTURE_CWD)
        self.assertEqual([(s['id'], s['continues']) for s in listed], [('claude:' + CC, ['claude:' + CA, 'claude:' + CB])])
        s = listed[0]
        self.assertEqual(s['title'], 'Planner')                                       # C has no name of its own: B's (A's) carries on
        self.assertEqual(s['started'], server.timestamp('2026-11-02T10:00:00Z'))      # the column keeps A's place on the floor
        h = p.read_history('claude:' + CC)
        texts = [m['text'].split('\n')[0] for m in h['messages']]
        self.assertEqual(texts, ['Start the work on the app', 'Read ' + 'src/app.py', 'Read it.', 'Write the new file', 'Write src/new.py', 'Wrote src/new.py.',
                                 'Write a different file instead', 'Write src/other.py', 'Wrote src/other.py.', 'Carry on from there', 'Carrying on.'])
        self.assertNotIn('A message that is rewound away', texts)          # what came after each join in the older file was rewound away
        self.assertEqual([(e['kind'], e['path']) for e in h['events']], [('read', 'src/app.py'), ('write', 'src/new.py'), ('write', 'src/other.py')])
        ts = [c['t'] for c in h['contexts']]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual([c['context'] for c in h['contexts']], [40000, 42000, 12000, 13000, 14000, 15000, 16000])
        self.assertEqual([c['from'] for c in h['compacts']], [150000])          # A's compaction, drawn once (B and C hold copies of it)
        self.assertEqual((h['start'], h['continues']), (s['started'], 2))
        own = p.read_history('claude:' + CA)          # asked by its own id, the older file is still its whole self
        self.assertIn('A message that is rewound away', [m['text'] for m in own['messages']])
        led = p.session_usage('claude:' + CC)
        self.assertEqual((led.calls, sorted(led.models)), (7, ['claude-opus-5-5']))          # A's four, B's two, C's one: no copied call twice

    def test_a_predecessor_that_went_on_is_a_branch(self):
        home, folder = continued_store(self.root, FIXTURE_CWD)
        with (folder / (CA + '.jsonl')).open('a') as f:
            f.write(crec(CA, '00000099-1111-4111-8111-111111111111', '10:20:00', text='Keep going here as well'))
        p = self.provider(home)
        self.assertEqual(self.rows(p), {'claude:' + CA: None, 'claude:' + CC: ['claude:' + CB]})
        h = p.read_history('claude:' + CC)
        self.assertEqual(h['messages'][0]['text'], 'Write the new file')          # B's own file starts with its copy of A (from A's compaction)

    def test_two_continuations_of_a_stopped_session_keep_their_columns(self):
        home, folder = continued_store(self.root, FIXTURE_CWD)
        a = (folder / (CA + '.jsonl')).read_text().splitlines(keepends=True)
        dd = 'dddddddd-0000-4000-8000-00000000000d'
        copied = [json.dumps(dict(json.loads(x), sessionId=dd), separators=(',', ':')) + '\n' for x in a[5:11]]
        (folder / (dd + '.jsonl')).write_text(json.dumps({'type': 'queue-operation', 'sessionId': dd, 'timestamp': '2026-11-02T10:30:00.000Z'}) + '\n'
                                              + ''.join(copied) + crec(dd, '00000041-1111-4111-8111-111111111111', '10:30:01', text='Another way'))
        p = self.provider(home)
        self.assertEqual(self.rows(p), {'claude:' + CA: None, 'claude:' + CC: ['claude:' + CB], 'claude:' + dd: None})

    def test_a_copy_made_from_this_page_keeps_its_own_column(self):
        # "Start a copy here" (the bridge's send with fork) is a copy the person made on purpose: it never joins its original, and it
        # is no rival to a rewind of that original. Known from the bridge (how 'forked') and, after a restart, from its own records
        # written by the Python Agent SDK (entrypoint 'sdk-py')
        home, folder = continued_store(self.root, FIXTURE_CWD)
        a = (folder / (CA + '.jsonl')).read_text().splitlines(keepends=True)
        dd = 'dddddddd-0000-4000-8000-00000000000d'
        copied = [json.dumps(dict(json.loads(x), sessionId=dd), separators=(',', ':')) + '\n' for x in a[5:11]]
        (folder / (dd + '.jsonl')).write_text(json.dumps({'type': 'queue-operation', 'sessionId': dd, 'timestamp': '2026-11-02T10:30:00.000Z'}) + '\n'
                                              + ''.join(copied) + crec(dd, '00000041-1111-4111-8111-111111111111', '10:30:01', text='A copy from the page', entrypoint='sdk-py'))
        p = self.provider(home)
        p.drive = SimpleNamespace(held={CC: {'how': 'forked', 'from': CB, 'open': True}})          # C: started from this page as a copy of B
        self.assertEqual(self.rows(p), {'claude:' + CB: ['claude:' + CA], 'claude:' + CC: None, 'claude:' + dd: None})
        self.assertEqual(p.read_history('claude:' + CC)['messages'][-1]['text'], 'Carrying on.')          # its own file, as before
        p = self.provider(home)          # after a restart the bridge knows nothing: C (written by the desktop app) joins again, D stays apart
        self.assertEqual(self.rows(p), {'claude:' + CC: ['claude:' + CA, 'claude:' + CB], 'claude:' + dd: None})

    def test_a_branch_command_copy_names_its_source(self):
        # /branch writes forkedFrom {sessionId, messageUuid} on every copied record and no record stamped with its own start
        home, folder = continued_store(self.root, FIXTURE_CWD, names=(CA,))
        ee = 'eeeeeeee-0000-4000-8000-00000000000e'
        a = [json.loads(x) for x in (folder / (CA + '.jsonl')).read_text().splitlines()]
        copied = [dict(r, sessionId=ee, forkedFrom={'sessionId': CA, 'messageUuid': r['uuid']}) for r in a[1:11] if r.get('uuid')]
        (folder / (ee + '.jsonl')).write_text(''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in copied)
                                              + crec(ee, '00000051-1111-4111-8111-111111111111', '10:40:00', text='On the branch'))
        p = self.provider(home)
        self.assertEqual(self.rows(p), {'claude:' + ee: ['claude:' + CA]})
        self.assertEqual(p.read_history('claude:' + ee)['messages'][-1]['text'], 'On the branch')

    def test_the_predecessor_is_searched_for_once(self):
        import providers.claude_code as cc
        home, folder = continued_store(self.root, FIXTURE_CWD)
        calls = []
        real = cc.find_record
        with patch.object(cc, 'find_record', lambda path, uuid: calls.append(Path(path).stem) or real(path, uuid)):
            p = self.provider(home)
            p.list_sessions(FIXTURE_CWD)
            first = len(calls)
            p._scan = (0, None, None)
            p.list_sessions(FIXTURE_CWD)
            self.assertEqual(len(calls), first)          # found once, kept
            orphan = 'ffffffff-0000-4000-8000-00000000000f'          # a copy whose source is not on this machine: searched again only in files new to the folder
            (folder / (orphan + '.jsonl')).write_text(json.dumps({'type': 'queue-operation', 'sessionId': orphan, 'timestamp': '2026-11-02T11:00:00.000Z'}) + '\n'
                                                      + crec(orphan, '00000061-1111-4111-8111-111111111111', '10:00:05') + crec(orphan, '00000062-1111-4111-8111-111111111111', '11:00:01'))
            p._scan, p._folders = (0, None, None), {}
            self.assertEqual(set(self.rows(p)), {'claude:' + CC, 'claude:' + orphan})
            self.assertEqual(calls[first:], [CA])          # only A had started by the orphan's join
            n = len(calls)
            (folder / 'earlier.jsonl').write_text(crec('earlier', '00000071-1111-4111-8111-111111111111', '09:59:00'))
            (folder / 'later.jsonl').write_text(crec('later', '00000072-1111-4111-8111-111111111111', '11:30:00'))
            p._scan, p._folders = (0, None, None), {}
            p.list_sessions(FIXTURE_CWD)
            self.assertEqual(calls[n:], ['earlier'])          # A is not read again; the file started after the join is not a candidate


class ContinuationFloorTests(unittest.TestCase):
    """The floor, the usage panel and the compaction scale see one session for the chain; A's subagent sits under it."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        (self.project / 'src').mkdir(parents=True)
        for f in ('src/app.py', 'src/new.py', 'src/other.py'):
            (self.project / f).write_text('x')
        codex_home = self.root / 'codex'
        codex_home.mkdir()
        self.patch = patch.object(server, 'CODEX_HOME', codex_home)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        home, folder = continued_store(self.root, str(self.project))
        subs = folder / CA / 'subagents'
        subs.mkdir(parents=True)
        (subs / 'agent-k1.jsonl').write_text(crec(CA, '00000081-1111-4111-8111-111111111111', '10:00:30', text='Look', isSidechain=True).replace(FIXTURE_CWD, str(self.project)))
        (subs / 'agent-k1.meta.json').write_text(json.dumps({'agentType': 'general-purpose', 'description': 'Look into it'}))
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.http.daemon_threads = True
        ws = self.http.workspace = server.Workspace(FakeBridge())
        ws._providers = [CodexProvider(ws, codex_home), ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')]
        self.base = 'http://127.0.0.1:' + str(self.http.server_port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path + urllib.parse.quote(str(self.project)), timeout=10))

    def test_floor_usage_and_scale_show_the_chain_once(self):
        D = self.get('/api/canvas?cwd=')
        by = {s['id']: s for s in D['sessions']}
        cid = 'claude:' + CC
        self.assertEqual(set(by), {cid, 'claude:' + CA + '/k1'})
        self.assertEqual((by[cid]['start'], by[cid]['continues'], by['claude:' + CA + '/k1']['parent']), (0, ['claude:' + CA, 'claude:' + CB], cid))
        self.assertEqual(sorted((e['kind'], e['path']) for e in D['events'] if e['session'] == cid and e['kind'] in ('read', 'write')),
                         [('read', 'src/app.py'), ('write', 'src/new.py'), ('write', 'src/other.py')])
        self.assertEqual([e['from'] for e in D['events'] if e['kind'] == 'compact'], [150000])
        self.assertEqual(set(self.get('/api/usage?cwd=')['sessions']), {cid, 'claude:' + CA + '/k1'})
        self.assertEqual(self.get('/api/usage?cwd=')['sessions'][cid]['calls'], 7)
        self.assertEqual(set(self.get('/api/compaction?cwd=')['sessions']), {cid, 'claude:' + CA + '/k1'})
        h = json.load(urllib.request.urlopen(self.base + '/api/history?id=' + urllib.parse.quote(cid), timeout=10))
        self.assertEqual((h['messages'][0]['text'], h['messages'][-1]['text']), ('Start the work on the app', 'Carrying on.'))


class RunningRuleTests(unittest.TestCase):
    """running (2026-11-09): a Claude Code session works while its process is live and its transcript says work is going (a tool call
    without its result, a turn not ended); a parent whose subagents work waits on them; quiet time alone never makes it finished."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'claude'
        self.folder = self.home / 'projects' / encode(FIXTURE_CWD)
        self.folder.mkdir(parents=True)
        self.p = ClaudeCodeProvider(tokens(), home=self.home, app_support=self.root / 'none')
        self.n = 0

    def rec(self, typ, content, **kw):
        self.n += 1
        r = {'type': typ, 'uuid': 'u%d' % self.n, 'timestamp': '2026-11-09T10:00:%02d.000Z' % self.n, 'cwd': FIXTURE_CWD, **kw}
        if typ in ('user', 'assistant'):
            r['message'] = {'role': typ, 'content': content, **({'id': 'm%d' % self.n, 'model': 'claude-haiku-5-5'} if typ == 'assistant' else {}), **kw.pop('message', {})}
        return json.dumps(r) + '\n'

    def ask(self, text):
        return self.rec('user', text)

    def tool(self, tid, name='Bash', inp=None):
        r = json.loads(self.rec('assistant', [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp or {'command': 'sleep 600'}}]))
        r['message']['stop_reason'] = 'tool_use'
        return json.dumps(r) + '\n'

    def result(self, tid, text='done', **kw):
        return self.rec('user', [{'type': 'tool_result', 'tool_use_id': tid, 'content': text}], **kw)

    def answer(self, text='All done.'):
        r = json.loads(self.rec('assistant', [{'type': 'text', 'text': text}]))
        r['message']['stop_reason'] = 'end_turn'
        return json.dumps(r) + '\n'

    def write(self, path, lines, age):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(lines))
        t = time.time() - age
        os.utime(path, (t, t))

    def hold(self, sid, status='idle'):
        (self.home / 'sessions').mkdir(exist_ok=True)
        (self.home / 'sessions' / (str(os.getpid()) + '.json')).write_text(json.dumps({'pid': os.getpid(), 'sessionId': sid, 'cwd': FIXTURE_CWD, 'status': status, 'entrypoint': 'claude-desktop'}))
        self.p._live, self.p._scan = (0, {}), (0, None, None)

    def listed(self):
        self.p._scan = (0, None, None)
        return {s['id']: s for s in self.p.list_sessions(FIXTURE_CWD)}

    def test_an_open_tool_call_older_than_thirty_seconds_is_still_running(self):
        path = self.folder / 'm1.jsonl'
        self.write(path, [self.ask('run the slow suite'), self.tool('t1')], age=300)          # quiet for five minutes, the registration idle
        self.hold('m1')
        self.assertTrue(self.listed()['claude:m1']['running'])
        with path.open('a') as f:
            f.write(self.result('t1') + self.answer())
        self.assertFalse(self.listed()['claude:m1']['running'])          # its answer: returned at once, no grace
        self.hold('nobody')
        self.write(path, [self.ask('run it'), self.tool('t2')], age=5)
        self.assertFalse(self.listed()['claude:m1']['running'])          # no live process holds it: not running, whatever the transcript says

    def test_a_subagent_on_a_long_call_runs_and_its_idle_parent_waits_on_it(self):
        self.write(self.folder / 'm2.jsonl', [self.ask('look into it'), self.tool('tu_x', 'Agent', {'prompt': 'dig', 'run_in_background': True}),
                                              self.result('tu_x', 'Async agent launched', toolUseResult={'status': 'async_launched', 'agentId': 'a1', 'isAsync': True}), self.answer('It is digging.')], age=120)
        sub = self.folder / 'm2' / 'subagents' / 'agent-a1.jsonl'
        self.write(sub, [self.ask('dig'), self.tool('s1')], age=90)          # its file quiet for 90 s: the old rule (30 s) called it finished
        sub.with_name('agent-a1.meta.json').write_text(json.dumps({'description': 'Dig', 'toolUseId': 'tu_x'}))
        self.hold('m2')
        rows = self.listed()
        self.assertEqual((rows['claude:m2']['running'], rows['claude:m2/a1']['running']), (False, True))
        merged = server.waiting_on_agents([{'id': s['id'], 'parent': s['parent'], 'status': 'active' if s['running'] else 'idle'} for s in rows.values()])
        parent = next(s for s in merged if s['id'] == 'claude:m2')
        self.assertEqual((parent['status'], parent['waitingOn']), ('active', 1))          # waits on its agent: not finished
        note = '<task-notification><task-id>a1</task-id><tool-use-id>tu_x</tool-use-id><status>completed</status><summary>Dig</summary></task-notification>'
        with (self.folder / 'm2.jsonl').open('a') as f:
            f.write(self.rec('attachment', None, attachment={'type': 'queued_command', 'commandMode': 'task-notification', 'prompt': note}))
        rows = self.listed()
        self.assertFalse(rows['claude:m2/a1']['running'])          # its parent has its result
        merged = server.waiting_on_agents([{'id': s['id'], 'parent': s['parent'], 'status': 'active' if s['running'] else 'idle'} for s in rows.values()])
        self.assertEqual(next(s for s in merged if s['id'] == 'claude:m2')['status'], 'idle')

    def test_a_compaction_mid_turn_counts_as_running(self):
        path = self.folder / 'm3.jsonl'
        lines = [self.ask('keep going'), self.tool('t1', 'Read', {'file_path': FIXTURE_CWD + '/a.py'}), self.result('t1'),
                 self.rec('system', None, subtype='compact_boundary', compactMetadata={'trigger': 'auto', 'preTokens': 150000})]
        self.write(path, lines, age=20)
        self.hold('m3')
        self.assertTrue(self.listed()['claude:m3']['running'])          # the turn has not ended: compacting, idle for the registration
        os.utime(path, (time.time() - 1000, time.time() - 1000))
        self.assertFalse(self.listed()['claude:m3']['running'])          # past the grace with nothing open: the fallback lets it go
        self.hold('m3', 'busy')
        self.assertTrue(self.listed()['claude:m3']['running'])

    def test_a_copy_keeps_its_original_as_parent_and_says_it_is_a_copy(self):
        rows = [{'id': 'claude:orig', 'parent': None, 'status': 'idle'}, {'id': 'claude:copy', 'parent': 'claude:orig', 'status': 'active', 'copy': True}]
        self.assertEqual(server.waiting_on_agents(rows)[0]['status'], 'idle')          # a copy is not a subagent: its original does not wait on it


class RunningOnChainsTests(unittest.TestCase):
    """running x continuation (merge 2026-11-09): the running rule reads the newest file of a chain; a subagent of an older file is
    held by the newest file's process; a copy made from this page is a branch of the column that draws its original now."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home, self.folder = continued_store(self.root, FIXTURE_CWD)
        self.p = ClaudeCodeProvider(tokens(), home=self.home, app_support=self.root / 'none')

    def hold(self, uuid, status='idle'):
        (self.home / 'sessions').mkdir(exist_ok=True)
        (self.home / 'sessions' / (str(os.getpid()) + '.json')).write_text(json.dumps({'pid': os.getpid(), 'sessionId': uuid, 'cwd': FIXTURE_CWD, 'status': status, 'entrypoint': 'claude-desktop'}))
        self.p._live, self.p._scan = (0, {}), (0, None, None)

    def listed(self):
        self.p._scan = (0, None, None)
        return {s['id']: s for s in self.p.list_sessions(FIXTURE_CWD)}

    def tool(self, sid, uid, t, tid):
        r = json.loads(crec(sid, uid, t, typ='assistant'))
        r['message']['content'], r['message']['stop_reason'] = [{'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': {'command': 'sleep 600'}}], 'tool_use'
        return json.dumps(r) + '\n'

    def test_the_newest_file_of_a_chain_says_whether_the_column_runs(self):
        with (self.folder / (CC + '.jsonl')).open('a') as f:
            f.write(crec(CC, '00000091-1111-4111-8111-111111111111', '10:50:00', text='Run the slow suite') + self.tool(CC, '00000092-1111-4111-8111-111111111111', '10:50:01', 't9'))
        t = time.time() - 600
        os.utime(self.folder / (CC + '.jsonl'), (t, t))          # quiet ten minutes, the registration idle: the open call says it works
        self.hold(CC)
        rows = self.listed()
        self.assertEqual((set(rows), rows['claude:' + CC]['running']), ({'claude:' + CC}, True))
        self.assertTrue(self.p.read_history('claude:' + CC)['turnActive'])
        r = json.loads(crec(CC, '00000094-1111-4111-8111-111111111111', '10:50:09', typ='assistant', text='Done.'))
        r['message']['stop_reason'] = 'end_turn'
        with (self.folder / (CC + '.jsonl')).open('a') as f:
            f.write(crec(CC, '00000093-1111-4111-8111-111111111111', '10:50:08', text=[{'type': 'tool_result', 'tool_use_id': 't9', 'content': 'ok'}]) + json.dumps(r) + '\n')
        self.assertFalse(self.listed()['claude:' + CC]['running'])
        self.assertFalse(self.p.read_history('claude:' + CC)['turnActive'])

    def test_a_subagent_of_an_older_file_is_held_by_the_newest_files_process(self):
        subs = self.folder / CA / 'subagents'
        subs.mkdir(parents=True)
        (subs / 'agent-k1.jsonl').write_text(crec(CA, '00000081-1111-4111-8111-111111111111', '10:00:30', text='Look', isSidechain=True)
                                             + self.tool(CA, '00000082-1111-4111-8111-111111111111', '10:00:31', 'k9'))
        self.hold(CC)          # the app registers the newest id; the older file's id is registered no more
        rows = self.listed()
        self.assertEqual((rows['claude:' + CA + '/k1']['parent'], rows['claude:' + CA + '/k1']['running']), ('claude:' + CC, True))
        self.hold('nobody')
        self.assertFalse(self.listed()['claude:' + CA + '/k1']['running'])

    def test_a_copy_made_here_is_a_branch_of_the_column_that_draws_its_original(self):
        a = (self.folder / (CA + '.jsonl')).read_text().splitlines(keepends=True)
        dd = 'dddddddd-0000-4000-8000-00000000000d'
        copied = [json.dumps(dict(json.loads(x), sessionId=dd), separators=(',', ':')) + '\n' for x in a[5:11]]
        (self.folder / (dd + '.jsonl')).write_text(json.dumps({'type': 'queue-operation', 'sessionId': dd, 'timestamp': '2026-11-02T10:30:00.000Z'}) + '\n'
                                                   + ''.join(copied) + crec(dd, '00000041-1111-4111-8111-111111111111', '10:30:01', text='A copy from the page', entrypoint='sdk-py'))
        rows = self.listed()          # after a restart: the bridge knows nothing, the copy's own records say it was made here
        self.assertEqual(rows['claude:' + dd].get('copyOf'), 'claude:' + CA)
        listed = [{'id': s['id'], 'title': s['title'], 'parent': s['parent'], **({k: s[k] for k in ('continues', 'copyOf') if s.get(k)})} for s in rows.values()]
        cb = SimpleNamespace(available=lambda: {'ready': False}, held={})
        out = {s['id']: s for s in server.Workspace.claude_sessions(SimpleNamespace(claude=cb), listed)}
        self.assertEqual((out['claude:' + dd]['copy'], out['claude:' + dd]['parent'], out['claude:' + dd]['title']), (True, 'claude:' + CC, 'copy of Planner'))
        self.assertFalse(out['claude:' + CC].get('copy'))
        cb.held = {dd: {'how': 'forked', 'from': CB, 'open': True}}          # while this server runs: the bridge's record, the original since joined into C
        out = {s['id']: s for s in server.Workspace.claude_sessions(SimpleNamespace(claude=cb), [dict(s) for s in listed])}
        self.assertEqual((out['claude:' + dd]['parent'], out['claude:' + dd]['title']), ('claude:' + CC, 'copy of Planner'))


class HistoryCacheTests(unittest.TestCase):
    """speed (2026-11-21): a transcript is parsed again only when it changed; an appended one from where the last read stopped; a
    shorter or replaced one in full; the cache keeps a poll's working set however large (one real listing read 357, over the
    300 it held, so every poll parsed everything again)."""
    KEYS = ('messages', 'events', 'contexts', 'compacts', 'reasoning', 'turns', 'errors', 'handbacks', 'asking', 'model', 'title', 'base', 'context')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home, self.folder = claude_store(self.root, FIXTURE_CWD)
        self.path = self.folder / 's1.jsonl'
        self.lines = self.path.read_text().splitlines(keepends=True)

    def counted(self):
        """A provider whose record parser counts the records it is given (a fake around take)."""
        p = ClaudeCodeProvider(tokens(), home=self.home, app_support=self.root / 'none')
        p.taken = 0
        real = p.take

        def take(c, rec):
            p.taken += 1
            return real(c, rec)
        p.take = take
        return p

    def same(self, a, b):
        self.assertEqual({k: a.get(k) for k in self.KEYS}, {k: b.get(k) for k in self.KEYS})

    def test_an_unchanged_transcript_is_not_parsed_again(self):
        p = self.counted()
        p.read_history('claude:s1')
        n = p.taken
        self.assertEqual(n, len(self.lines))
        for _ in range(3):
            p._scan = (0, None, None)
            p.list_sessions(FIXTURE_CWD)
            p.read_history('claude:s1')
        self.assertEqual((p.taken, p.parses), (n, 1))

    def test_an_appended_transcript_is_read_from_where_it_stopped_and_matches_a_full_read(self):
        self.path.write_text(''.join(self.lines[:5]))
        p = self.counted()
        p.read_history('claude:s1')
        self.assertEqual(p.taken, 5)
        with self.path.open('a') as f:
            f.write(''.join(self.lines[5:]))
        h = p.read_history('claude:s1')
        self.assertEqual(p.taken, len(self.lines))          # only the appended records
        self.same(h, self.counted().read_history('claude:s1'))

    def test_a_shorter_or_replaced_transcript_is_read_in_full(self):
        p = self.counted()
        p.read_history('claude:s1')
        self.path.write_text(''.join(self.lines[:4]))          # shorter: rewritten in place
        p.taken = 0
        h = p.read_history('claude:s1')
        self.assertEqual(p.taken, 4)
        self.same(h, self.counted().read_history('claude:s1'))
        other = self.folder / 'next.tmp'          # replaced by another file of the same size (a new inode)
        other.write_text(''.join(self.lines[:4]).replace('"x"', '"y"'))
        os.replace(other, self.path)
        p.taken = 0
        h = p.read_history('claude:s1')
        self.assertEqual(p.taken, 4)
        self.same(h, self.counted().read_history('claude:s1'))

    def subagents(self, n):
        subs = self.folder / 's1' / 'subagents'
        subs.mkdir(parents=True)
        for i in range(n):
            (subs / ('agent-a%03d.jsonl' % i)).write_text(crec('s1', '0000%04d-1111-4111-8111-111111111111' % i, '10:00:00', text='Look %d' % i, isSidechain=True))
        (self.home / 'sessions').mkdir()
        (self.home / 'sessions' / '1.json').write_text(json.dumps({'pid': os.getpid(), 'sessionId': 's1', 'cwd': FIXTURE_CWD, 'status': 'idle'}))

    def test_the_cache_keeps_a_working_set_larger_than_300(self):
        self.subagents(400)          # a registered session: the listing reads every subagent's transcript (running)
        p = self.counted()
        p.list_sessions(FIXTURE_CWD)
        first = p.parses
        self.assertGreaterEqual(first, 401)
        for _ in range(2):
            p._scan = (0, None, None)
            p.list_sessions(FIXTURE_CWD)
        self.assertEqual((p.parses, len(p.cache) >= 401), (first, True))

    def test_past_its_size_the_cache_drops_only_what_the_last_polls_did_not_read(self):
        self.subagents(20)
        p = self.counted()
        p.read_history('claude:s1/a000')          # read once, then not again for two polls
        sub = self.folder / 's1' / 'subagents' / 'agent-a000.jsonl'
        with patch('providers.claude_code.CACHE_BYTES', 1):
            p.list_sessions(FIXTURE_CWD)
            p._scan = (0, None, None)
            p.list_sessions(FIXTURE_CWD)
            n = p.parses
            p._scan = (0, None, None)
            p.list_sessions(FIXTURE_CWD)
            self.assertEqual(p.parses, n)          # the working set stays, over any size
        self.assertIn(str(sub), p.cache)          # the listing reads every subagent here: a000 is in the working set too
        extra = self.folder / 'lone.jsonl'
        extra.write_text(crec('lone', '00009999-1111-4111-8111-111111111111', '10:00:00', text='Alone'))
        with patch('providers.claude_code.CACHE_BYTES', 1):
            p.own_history('claude:lone', extra)
            p._scan = (0, None, None)
            p.list_sessions(FIXTURE_CWD)
            p._scan = (0, None, None)
            p.list_sessions(FIXTURE_CWD)          # two polls without reading lone.jsonl
            p.read_history('claude:s1/a000')
            sub.write_text(sub.read_text() + crec('s1', '00008888-1111-4111-8111-111111111111', '10:00:05', text='More', isSidechain=True))
            p.read_history('claude:s1/a000')          # a parse: the cache is trimmed
        self.assertNotIn(str(extra), p.cache)
        self.assertIn(str(sub), p.cache)


class ProviderMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        (self.project / 'src').mkdir(parents=True)
        (self.project / 'sub' / 'src').mkdir(parents=True)
        for f in ('src/app.py', 'src/new.py', 'sub/src/app.py'):
            (self.project / f).write_text('x')
        codex_home = self.root / 'codex'
        codex_home.mkdir()
        self.patch = patch.object(server, 'CODEX_HOME', codex_home)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        home, _ = claude_store(self.root, str(self.project))
        claude_store(self.root, str(self.project / 'sub'), name='s2')
        bridge = FakeBridge()
        bridge.threads = {'codex1': {'id': 'codex1', 'cwd': str(self.project), 'name': 'Codex work'}}
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.http.daemon_threads = True
        ws = self.http.workspace = server.Workspace(bridge)
        ws._providers = [CodexProvider(ws, codex_home), ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')]
        self.base = 'http://127.0.0.1:' + str(self.http.server_port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path, timeout=5))

    def test_canvas_merges_codex_and_claude_sessions(self):
        D = self.get('/api/canvas?cwd=' + urllib.parse.quote(str(self.project)))
        by = {s['id']: s for s in D['sessions']}
        self.assertEqual({k: (v['provider'], v['home']) for k, v in by.items()}, {'codex1': ('codex', ''), 'claude:s1': ('claude', ''), 'claude:s2': ('claude', 'sub')})
        paths = sorted((e['session'], e['kind'], e['path']) for e in D['events'] if e['kind'] in ('read', 'write'))
        self.assertEqual(paths, [('claude:s1', 'read', 'src/app.py'), ('claude:s1', 'write', 'src/new.py'), ('claude:s2', 'read', 'sub/src/app.py'), ('claude:s2', 'write', 'sub/src/new.py')])
        self.assertEqual([c['to'] for c in D['events'] if c['kind'] == 'compact' and c['session'] == 'claude:s1'], [89784 - 100])
        self.assertEqual(by['claude:s1']['context'], 89784)

    def test_canvas_carries_a_sessions_plan_timed_from_the_floor_origin(self):          # plan progress
        folder = self.root / 'claude' / 'projects' / encode(str(self.project))
        fx = Path(__file__).resolve().parent / 'providers' / 'fixtures' / 'plans' / 'claude-desktop-plan.jsonl'
        (folder / 'plans.jsonl').write_text(fx.read_text().replace(FIXTURE_CWD, str(self.project)))
        D = self.get('/api/canvas?cwd=' + urllib.parse.quote(str(self.project)))
        by = {s['id']: s for s in D['sessions']}
        pl = by['claude:plans']['plan']
        self.assertEqual(len(pl), 4)
        self.assertAlmostEqual(pl[0]['t'] + D['origin'], server.timestamp('2026-11-09T10:00:10.000Z'), places=3)
        self.assertEqual([q['status'] for q in pl[2]['steps']], ['completed', 'completed', 'pending', 'in_progress'])
        self.assertNotIn('plan', by['claude:s1'])          # a session that kept no plan carries none

    # crowding: the page folds finished subagents into their parent, so every session on the canvas carries its parent's id:
    # Codex from the rollout's session_meta.parent_thread_id (also when only the log's tail is read), Claude Code from the subagents/ folder
    def test_canvas_gives_subagents_their_parent_for_both_providers(self):
        folder = self.root / 'claude' / 'projects' / encode(str(self.project))
        subs = folder / 's1' / 'subagents'
        subs.mkdir(parents=True)
        (subs / 'agent-a1.jsonl').write_text(FIXTURE.read_text().replace(FIXTURE_CWD, str(self.project)))
        (subs / 'agent-a1.meta.json').write_text(json.dumps({'agentType': 'general-purpose', 'description': 'Look into it'}))
        meta = lambda tid, parent: json.dumps({'timestamp': '2026-11-06T10:00:00Z', 'type': 'session_meta', 'payload': {'id': tid, 'parent_thread_id': parent, 'timestamp': '2026-11-06T10:00:00Z', 'cwd': str(self.project)}}) + '\n'
        filler = json.dumps({'timestamp': '2026-11-06T10:00:01Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'x' * 1000}]}}) + '\n'
        (self.root / 'main.jsonl').write_text(meta('cmain', None))
        (self.root / 'kid.jsonl').write_text(meta('ckid', 'cmain'))
        (self.root / 'bigkid.jsonl').write_text(meta('cbig', 'cmain') + filler * 4200)          # over 4 MB: only its tail is read
        rows = [{'id': t, 'rollout_path': str(self.root / f), 'created_at': 1, 'updated_at': 2 + i, 'cwd': str(self.project), 'title': t, 'source': 'exec'}
                for i, (t, f) in enumerate([('cmain', 'main.jsonl'), ('ckid', 'kid.jsonl'), ('cbig', 'bigkid.jsonl')])]
        with patch.object(server.Observer, 'rows', lambda self, limit=300: rows):
            D = self.get('/api/canvas?cwd=' + urllib.parse.quote(str(self.project)))
        by = {s['id']: s['parent'] for s in D['sessions']}
        self.assertEqual((by['cmain'], by['ckid'], by['cbig']), (None, 'cmain', 'cmain'))
        self.assertEqual((by['claude:s1'], by['claude:s1/a1']), (None, 'claude:s1'))

    def test_state_history_and_watch_only(self):
        st = self.get('/api/state')
        self.assertEqual([p['name'] for p in st['providers']], ['codex', 'claude'])
        self.assertTrue(st['providers'][1]['found'])
        self.assertFalse(st['providers'][1]['canSend'])
        self.assertEqual({s['id']: s['provider'] for s in st['sessions']}, {'codex1': 'codex', 'claude:s1': 'claude', 'claude:s2': 'claude'})
        h = self.get('/api/history?id=' + urllib.parse.quote('claude:s1'))
        self.assertEqual((h['provider'], len(h['messages']), h['context']), ('claude', 6, 89784))
        token = {'X-Workspace-Token': self.http.workspace.token, 'Content-Type': 'application/json'}
        req = urllib.request.Request(self.base + '/api/send', data=json.dumps({'threadId': 'claude:s1', 'text': 'hi'}).encode(), headers=token)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertIn('watch-only', json.load(e.exception)['error'])

    def test_watch_only_settings_status_and_reasoning(self):
        # the chat's bottom bar and /status card read a Claude Code session's own transcript; no Codex call is made
        q = urllib.parse.quote('claude:s1')
        st = self.get('/api/settings?id=' + q)
        self.assertEqual((st['model'], st['permissionMode'], st['provider'], st['canSend']), ('claude-fable-5-1', 'xxxx', 'claude', False))
        card = self.get('/api/status?id=' + q)
        self.assertEqual((card['settings']['model'], card['context']['tokens'], card['provider']), ('claude-fable-5-1', 89784, 'claude'))
        h = self.get('/api/history?id=' + q)
        self.assertEqual(len(h['turns']), 1)                      # one person's message: one turn start
        self.assertEqual(h['reasoning'], [])                      # the fixture's thinking block carries no text (only a signature)
        self.assertEqual(self.get('/api/state')['logos'], sorted(f.stem for f in (server.ROOT / 'logos').glob('*.svg')))

    def test_thinking_with_text_becomes_reasoning(self):
        home = self.http.workspace._providers[1].home
        path = next((home / 'projects').glob('*/s1.jsonl'))
        rec = {'type': 'assistant', 'timestamp': '2026-11-01T10:09:00.000Z', 'cwd': str(self.project), 'uuid': 'u-think',
               'message': {'id': 'msg_think', 'role': 'assistant', 'model': 'claude-fable-5-1', 'content': [{'type': 'thinking', 'thinking': 'Check the tests first.', 'signature': 'x'}]}}
        with path.open('a') as f:
            f.write(json.dumps(rec) + '\n')
        h = self.get('/api/history?id=' + urllib.parse.quote('claude:s1'))
        self.assertEqual([r['text'] for r in h['reasoning']], ['Check the tests first.'])
        self.assertEqual(h['reasoning'][0]['t'], h['reasoning'][0]['end'])
# ---------- end providers ----------


# ---------- speed: one shared walk of the working tree, status-only polls, tagged polled reads ----------
class UsageRouteTests(unittest.TestCase):
    """GET /api/usage: each floor session's tokens and dollars at API prices, its cache hit rate, and both plans' windows, read
    only from the record files (usage.py)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.root / 'elsewhere').mkdir()
        codex_home = self.root / 'codex'
        day = codex_home / 'sessions' / '2026' / '11' / '07'
        day.mkdir(parents=True)
        (codex_home / 'auth.json').write_text('{"not": "to be read"}')
        self.reset = time.time() + 3600
        rate = {'limit_id': 'codex', 'primary': {'used_percent': 12.0, 'window_minutes': 300, 'resets_at': self.reset},
                'secondary': {'used_percent': 40.0, 'window_minutes': 10080, 'resets_at': self.reset + 86400}, 'plan_type': 'plus'}
        def count(t, last, total):
            return {'timestamp': t, 'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': rate,
                    'info': {'last_token_usage': last, 'total_token_usage': total, 'model_context_window': 272000}}}
        u1 = {'input_tokens': 1000, 'cached_input_tokens': 800, 'cache_write_input_tokens': 0, 'output_tokens': 100, 'reasoning_output_tokens': 20}
        u2 = {'input_tokens': 3000, 'cached_input_tokens': 2900, 'cache_write_input_tokens': 50, 'output_tokens': 200, 'reasoning_output_tokens': 0}
        t1 = {k: u1[k] + 0 for k in u1}
        t2 = {k: u1[k] + u2[k] for k in u1}
        lines = [{'timestamp': '2026-11-07T10:00:00Z', 'type': 'turn_context', 'payload': {'model': 'gpt-6-luna'}},
                 count('2026-11-07T10:00:05Z', u1, t1),
                 count('2026-11-07T10:00:06Z', u1, t1),          # a rate-limit-only update repeats the totals: not a second call
                 count('2026-11-07T10:01:00Z', u2, t2)]
        self.rollout = day / 'rollout-2026-11-07T10-00-00-codex1.jsonl'
        self.rollout.write_text(''.join(json.dumps(x) + '\n' for x in lines))
        self.patch = patch.object(server, 'CODEX_HOME', codex_home)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        home, folder = claude_store(self.root, str(self.project))
        # a plan reading: the desktop app's usage tool answered an agent, and its result is in that agent's transcript
        # its reset times are taken from now: a window whose reset has passed no longer shows its old figure (see the test below)
        iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))
        reading = {'plan': {'plan': 'Pro', 'windows': [{'label': 'Weekly · all models', 'percentUsed': 42, 'resetsAt': iso(time.time() + 3 * 86400)},
                                                       {'label': 'Weekly · Sonnet only', 'percentUsed': 30, 'resetsAt': iso(time.time() - 86400)}]}}
        with open(folder / 's1.jsonl', 'a') as f:
            f.write(json.dumps({'type': 'user', 'timestamp': '2026-11-01T10:11:00.000Z', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_x', 'content': [{'type': 'text', 'text': json.dumps(reading)}]}]}}) + '\n')
        (home / 'sessions').mkdir(exist_ok=True)
        (home / 'sessions' / 'abc.key').write_text('not to be read')
        bridge = FakeBridge()
        bridge.threads = {'codex1': {'id': 'codex1', 'cwd': str(self.project), 'name': 'Codex work'}}
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.http.daemon_threads = True
        ws = self.http.workspace = server.Workspace(bridge)
        ws._providers = [CodexProvider(ws, codex_home), ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')]
        self.ws = ws
        self.base = 'http://127.0.0.1:' + str(self.http.server_port)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        row = lambda obs, tid: {'id': tid, 'rollout_path': str(self.rollout)} if tid == 'codex1' else None
        self.row = patch.object(server.Observer, 'row', row)
        self.row.start()
        self.addCleanup(self.row.stop)

    def get(self, cwd):
        return json.load(urllib.request.urlopen(self.base + '/api/usage?cwd=' + urllib.parse.quote(str(cwd)), timeout=10))

    def test_codex_session_counts_each_call_once_at_list_prices(self):
        s = self.get(self.project)['sessions']['codex1']
        self.assertEqual(s['calls'], 2)
        self.assertEqual(s['tokens'], {'fresh': 250, 'read': 3700, 'write': 50, 'out': 300, 'reasoning': 20})
        self.assertAlmostEqual(s['total'], (250 * 0.10 + 3700 * 0.01 + 50 * 0.125 + 300 * 0.50) / 1e6, places=5)
        self.assertAlmostEqual(s['hit'], 3700 / 4000)
        self.assertEqual(s['provider'], 'codex')

    def test_claude_session_counts_each_response_once(self):
        s = self.get(self.project)['sessions']['claude:s1']
        self.assertEqual(s['calls'], 5)          # six assistant records, two of them one response
        self.assertEqual(s['tokens'], {'fresh': 70, 'read': 386589, 'write': 135835, 'out': 8510, 'reasoning': 4204})
        # Fable 5.1: $10 input, $0.25 cache read, $20 one-hour cache write, $50 output per million tokens
        self.assertAlmostEqual(s['total'], (70 * 10 + 386589 * 0.25 + 135835 * 20 + 8510 * 50) / 1e6, places=4)
        self.assertAlmostEqual(s['hit'], 386589 / (70 + 386589 + 135835))

    def test_plan_windows_from_both_harnesses(self):
        P = {p['name']: p for p in self.get(self.project)['providers']}
        self.assertEqual([p['name'] for p in self.get(self.project)['providers']], ['codex', 'claude'])          # the server's order
        cx, cl = P['codex']['plan'], P['claude']['plan']
        self.assertEqual((cx['plan'], P['codex']['label']), ('plus', 'Codex'))
        self.assertEqual([(w['label'], w['percent'], w['resetsAt']) for w in cx['windows']], [('5-hour', 12.0, self.reset), ('weekly', 40.0, self.reset + 86400)])
        # the newest usage-tool reading: no 5-hour figure in it (None), its all-models window as "weekly"
        self.assertEqual((cl['plan'], P['claude']['label']), ('Pro', 'Claude Code'))
        self.assertEqual([(w['label'], w['percent']) for w in cl['windows']], [('5-hour', None), ('weekly', 42), ('weekly, Sonnet only', None)])
        r = cl['detail']['readings'][-1]
        self.assertEqual((r['plan'], r['windows'][0]['label'], r['windows'][0]['percent']), ('Pro', 'Weekly · all models', 42))

    def test_a_claude_window_past_its_reset_shows_no_figure(self):
        # a real user whose last reading is from before the weekly reset: last week's 42% is no longer their use, so the panel shows
        # no figure (unknown) until a newer reading arrives, and keeps the reset time the reading named
        with patch.object(time, 'time', lambda real=time.time: real() + 4 * 86400):
            cl = {p['name']: p for p in self.get(self.project)['providers']}['claude']['plan']
        self.assertEqual([(w['label'], w['percent']) for w in cl['windows']], [('5-hour', None), ('weekly', None), ('weekly, Sonnet only', None)])
        self.assertIsNotNone(cl['windows'][1]['resetsAt'])

    def test_a_third_provider_is_asked_like_the_others(self):
        from providers.fake import FakeProvider
        fake = FakeProvider(self.project, now=time.time())
        self.ws._providers.append(fake)
        D = self.get(self.project)
        self.assertEqual([p['name'] for p in D['providers']], ['codex', 'claude', 'fake'])
        P = D['providers'][2]
        self.assertEqual((P['label'], P['plan']['plan']), ('Test harness', 'test'))
        self.assertEqual([(w['label'], w['percent']) for w in P['plan']['windows']], [('daily', 30), ('monthly', 8)])
        s = D['sessions']['fake:one']
        self.assertEqual((s['provider'], s['calls']), ('fake', 3))
        # priced from the one table, by model (gpt-6-luna: $0.10 input, $0.01 cache read, $0.50 output per million)
        self.assertAlmostEqual(s['total'], (7000 * 0.10 + 170000 * 0.01 + 4600 * 0.50) / 1e6, places=5)
        self.assertIn('codex1', D['sessions'])          # the others unchanged
        self.assertEqual(self.get(self.root / 'elsewhere')['sessions'], {})

    def test_a_provider_with_no_usage_is_listed_without_plan_or_spend(self):
        from providers.base import Provider
        self_project = self.project
        class Quiet(Provider):
            name, label = 'quiet', 'Quiet harness'
            def owns(self, sid):
                return str(sid).startswith('quiet:')
            def list_sessions(self, project_root=None):
                return [{'id': 'quiet:1', 'provider': 'quiet', 'title': 'q', 'cwd': str(self_project), 'started': 1, 'updated': 2, 'running': False, 'parent': None}]
        self.ws._providers.append(Quiet())
        D = self.get(self.project)
        self.assertEqual(D['providers'][-1], {'name': 'quiet', 'label': 'Quiet harness', 'plan': None, 'state': None,
                                              'note': "Quiet harness doesn't report its plan use to other apps"})          # why, not a dash
        self.assertNotIn('quiet:1', D['sessions'])
        self.assertIn('claude:s1', D['sessions'])

    def test_no_folder_gives_the_plan_blocks_alone(self):
        # an empty or missing cwd (the main line answered {"error": "'cwd'"}): the plan blocks, no floor spend
        for q in ('/api/usage?cwd=', '/api/usage', '/api/usage?cwd=%20'):
            D = json.load(urllib.request.urlopen(self.base + q, timeout=10))
            self.assertNotIn('error', D)
            self.assertEqual(D['sessions'], {})
            self.assertIsInstance(D['providers'], list)

    def test_other_folder_has_no_sessions(self):
        self.assertEqual(self.get(self.root / 'elsewhere')['sessions'], {})

    def test_reads_only_record_files_never_credentials(self):
        opened, real = [], open
        def spy(file, *a, **k):
            opened.append(str(file))
            return real(file, *a, **k)
        with patch('builtins.open', spy):
            self.get(self.project)
        mine = [p for p in opened if p.startswith(str(self.root))]
        self.assertTrue(mine)
        for p in mine:
            self.assertTrue('/sessions/2026/' in p or '/projects/' in p, p)
            self.assertNotIn('auth.json', p)
            self.assertFalse(p.endswith('.key'), p)


class CompactionPointTests(unittest.TestCase):
    """Where each session compacts (the columns' compaction scale): each provider's rule (compaction_point), and the canvas and
    GET /api/compaction carrying it (compact_points.py)."""
    setUp = UsageRouteTests.setUp

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path, timeout=10))

    def models(self, home):
        (Path(home) / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'gpt-6-luna', 'context_window': 272000, 'effective_context_window_percent': 95},
                                                                             {'slug': 'gpt-6.1-sol', 'context_window': 500000, 'effective_context_window_percent': 95}]}))

    def test_codex_compacts_at_90_percent_of_the_model_window(self):
        from providers.codex import codex_point
        home = self.root / 'codex-rule'
        home.mkdir()
        self.models(home)
        self.assertEqual((codex_point('gpt-6-luna', None, home)['threshold'], codex_point('gpt-6.1-sol', None, home)['threshold']), (244800, 450000))
        self.assertFalse(codex_point('gpt-6-luna', None, home)['estimate'])
        # a model the list lacks: from the window its rollout reported (95% of the model window); with neither, a stand-in
        self.assertEqual((codex_point('gpt-new', 258400, home)['threshold'], codex_point('gpt-new', 258400, home)['estimate']), (244800, False))
        self.assertEqual((codex_point(None, None, home)['threshold'], codex_point(None, None, home)['estimate']), (244800, True))
        # config.toml lowers it, never raises it; nothing else in config.toml is kept
        (home / 'config.toml').write_text('model_auto_compact_token_limit = 200000\nsecret_thing = "not kept"\n')
        r = codex_point('gpt-6-luna', None, home)
        self.assertEqual((r['threshold'], r['rule']), (200000, 'model_auto_compact_token_limit in config.toml'))
        (home / 'config.toml').write_text('model_auto_compact_token_limit = 300000\nmodel_context_window = 400000\n')
        time.sleep(0.01)
        self.assertEqual(codex_point('gpt-6.1-sol', None, home)['threshold'], 300000)          # 90% of 400k = 360k, lowered to 300k
        self.assertEqual(codex_point('gpt-6-luna', None, home)['threshold'], 244800)           # 300k is above its own point

    def test_claude_code_compacts_33k_below_its_auto_compact_window(self):
        from providers.claude_code import claude_point
        s = {'autoCompactWindow': 300000, 'modelSettings': {'claude-opus-5-5': {'autoCompactWindow': 400000}}}
        self.assertEqual(claude_point('claude-opus-5-5', s)['threshold'], 367000)          # the per-model window (what /autocompact sets)
        self.assertEqual(claude_point('claude-fable-5-1', s)['threshold'], 267000)         # else the global one
        self.assertEqual(claude_point('claude-opus-4-5', s)['threshold'], 167000)          # never above a 200k model's own window
        self.assertEqual(claude_point('claude-fable-5-1', {})['threshold'], 967000)        # no setting: a 1M model at about 967k (docs)
        self.assertEqual(claude_point('claude-opus-5-5[1m]', s)['threshold'], 367000)
        self.assertFalse(claude_point('claude-opus-5-5', s)['estimate'])
        for model in (None, 'claude-new-9'):          # no model, or one this page does not know: a stand-in, marked
            r = claude_point(model, s)
            self.assertEqual((r['threshold'], r['estimate']), (167000, True))

    def test_canvas_and_route_carry_each_sessions_point(self):
        self.models(server.CODEX_HOME)
        opened, real = [], open
        def spy(file, *a, **k):
            opened.append(str(file))
            return real(file, *a, **k)
        with patch('builtins.open', spy), patch.object(Path, 'read_text', lambda p, *a, **k: (opened.append(str(p)), real(p, *a, **k).read())[1]):
            D = self.get('/api/compaction?cwd=' + urllib.parse.quote(str(self.project)))
        P = D['sessions']
        self.assertEqual((P['codex1']['provider'], P['codex1']['model'], P['codex1']['threshold'], P['codex1']['estimate']), ('codex', 'gpt-6-luna', 244800, False))
        self.assertEqual((P['claude:s1']['provider'], P['claude:s1']['model'], P['claude:s1']['threshold']), ('claude', 'claude-fable-5-1', 967000))
        self.assertFalse(any('auth.json' in f or f.endswith('.key') for f in opened), opened)
        cv = self.get('/api/canvas?cwd=' + urllib.parse.quote(str(self.project)))
        by = {s['id']: s['compaction'] for s in cv['sessions']}
        self.assertEqual((by['codex1']['threshold'], by['claude:s1']['threshold']), (244800, 967000))
        self.assertEqual(self.get('/api/compaction?cwd=' + urllib.parse.quote(str(self.root / 'elsewhere')))['sessions'], {})

    def test_the_fake_harness_point_and_no_rule_by_default(self):
        from providers.fake import FakeProvider, POINT
        fake = FakeProvider(self.project, now=time.time())
        self.ws._providers.append(fake)
        P = self.get('/api/compaction?cwd=' + urllib.parse.quote(str(self.project)))['sessions']
        self.assertEqual((P['fake:one']['threshold'], fake.read_history('fake:one')['context']), (POINT, 97000))
        from providers.base import Provider
        self.assertIsNone(Provider().compaction_point('x'))


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / 'repo'
        (self.repo / 'app' / 'src').mkdir(parents=True)
        git = lambda *a: subprocess.run(['git', '-C', str(self.repo), *a], check=True, capture_output=True)
        git('init', '-q')
        for f in ('top.txt', 'old.txt', 'app/src/kept.py', 'app/src/edit.py', 'app/gone.py'):
            (self.repo / f).write_text(f)
        (self.repo / '.gitignore').write_text('*.log\n')
        git('add', '-A')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'one')
        (self.repo / 'app/src/edit.py').write_text('changed')
        (self.repo / 'app/new dir').mkdir()
        (self.repo / 'app/new dir/fresh file.py').write_text('x')
        (self.repo / 'app/noise.log').write_text('x')
        (self.repo / 'app/gone.py').unlink()
        git('mv', 'old.txt', 'app/moved.txt')
        server._scans.clear()
        server._untracked.clear()

    def old_files(self, cwd):
        out = subprocess.run(['git', '-C', str(cwd), 'ls-files', '-c', '-o', '--exclude-standard', '-z'], capture_output=True, text=True).stdout
        return sorted(set(p for p in out.split('\0') if p))

    def test_file_list_matches_git_ls_files_at_the_root_and_in_a_subfolder(self):
        for cwd in (self.repo, self.repo / 'app'):
            server._scans.clear()
            server._untracked.clear()
            self.assertEqual(server.files_in(str(cwd))['files'], self.old_files(cwd))

    def test_status_names_changed_untracked_and_moved_files_relative_to_the_folder(self):
        st = server.git_scan(self.repo / 'app')['status']
        names = {l[3:].split(' -> ')[-1] for l in st.splitlines()}
        self.assertEqual(names, {'src/edit.py', 'new dir/fresh file.py', 'gone.py', 'moved.txt'})
        self.assertNotIn('noise.log', st)
        server._scans.clear()
        server._untracked.clear()
        top = server.git_scan(self.repo)['status'].splitlines()          # a rename inside the folder reads as git's own "R  old -> new"
        self.assertIn('R  old.txt -> app/moved.txt', top)

    def next_poll(self, cwd=None, writes=()):          # the page's next poll, SCAN_TTL later
        k = str(cwd or self.repo)
        if k in server._scans:
            server._scans[k] = (time.monotonic() - server.SCAN_TTL - 1,) + server._scans[k][1:]
        return server.files_in(k, writes)['files']

    def test_scan_is_shared_for_a_few_seconds(self):
        calls = []
        real = server.run_git
        with patch.object(server, 'run_git', lambda *a: calls.append(a) or real(*a)):
            server.git_scan(self.repo)
            server.git_scan(self.repo)
            self.assertEqual([c[1] for c in calls], ['rev-parse', 'ls-files', 'status', 'rev-parse', 'ls-files'])          # once
            self.assertEqual(calls[-1][1:3], ('ls-files', '-o'))
            self.next_poll()
            self.assertEqual([c[1] for c in calls[5:]], ['rev-parse', 'ls-files', 'status'])          # nothing moved: the tracked part only
            k = str(self.repo)
            server._untracked[k] = (time.monotonic() - server.UNTRACKED_TTL - 1,) + server._untracked[k][1:]
            self.next_poll()
            self.assertEqual(calls[-1][1:3], ('ls-files', '-o'))          # at the latest every UNTRACKED_TTL seconds

    def test_a_new_moved_or_removed_file_shows_on_the_next_poll_without_an_agent(self):
        k = str(self.repo)
        server.files_in(k)
        (self.repo / 'app/src/brand new.py').write_text('x')          # in a folder that holds listed files
        self.assertIn('app/src/brand new.py', self.next_poll())
        (self.repo / 'app/new dir/fresh file.py').rename(self.repo / 'app/new dir/renamed.py')
        self.assertEqual(self.next_poll(), self.old_files(self.repo))
        (self.repo / 'app/empty').mkdir()
        self.next_poll()
        (self.repo / 'app/empty/first.py').write_text('x')          # a new file in an empty folder
        self.assertIn('app/empty/first.py', self.next_poll())
        (self.repo / 'app/src/brand new.py').unlink()
        self.assertNotIn('app/src/brand new.py', self.next_poll())
        (self.repo / '.gitignore').write_text('*.tmp\n')          # an ignore file changed: noise.log is no longer ignored
        self.assertIn('app/noise.log', self.next_poll())
        self.assertEqual(self.next_poll(), self.old_files(self.repo))
        self.assertIn('?? app/noise.log', server.git_scan(self.repo)['status'].splitlines())

    def test_an_agents_new_file_deep_in_a_folder_of_ignored_files_shows_on_the_next_poll(self):
        (self.repo / 'app/cache/inner').mkdir(parents=True)
        (self.repo / 'app/cache/x.log').write_text('x')          # app/cache holds only ignored files
        k = str(self.repo)
        server.files_in(k)
        (self.repo / 'app/cache/inner/made.py').write_text('x')          # two levels below any listed file: no folder time catches it
        self.assertNotIn('app/cache/inner/made.py', self.next_poll())
        old = time.time() - 3600          # a write from before the last search, or of a file already listed, starts no search
        self.assertNotIn('app/cache/inner/made.py', self.next_poll(writes=[(old, 'app/cache/inner/made.py'), (time.time() + 1, 'top.txt')]))
        now = self.next_poll(writes=[(time.time() + 1, 'app/cache/inner/made.py')])
        self.assertIn('app/cache/inner/made.py', now)
        self.assertEqual(now, self.old_files(self.repo))

    def test_history_sends_only_what_changed_since_the_version_the_page_has(self):
        server._hist_sent.clear()
        msgs = [{'role': 'user', 'text': 'a', 't': 1}, {'role': 'tool', 'text': 'run', 't': 2}, {'role': 'assistant', 'text': 'b', 't': 3}]
        first = server.history_since('s1', {'messages': [dict(m) for m in msgs]})
        self.assertEqual(len(first['messages']), 3)
        self.assertNotIn('from', first)
        grown = msgs + [{'role': 'assistant', 'text': 'c', 't': 4}]
        r = server.history_since('s1', {'messages': [dict(m) for m in grown]}, first['version'])
        self.assertEqual((r['from'], r['messages']), (3, grown[3:]))
        edited = [dict(m) for m in grown]
        edited[1]['text'] = 'run\noutput'          # an earlier message filled in later: sent again from there
        r2 = server.history_since('s1', {'messages': edited}, r['version'])
        self.assertEqual((r2['from'], r2['messages']), (1, edited[1:]))
        self.assertEqual(server.history_since('s1', {'messages': edited}, 'unknown')['messages'], edited)
        same = server.history_since('s1', {'messages': edited}, r2['version'])
        self.assertEqual((same['from'], same['messages'], same['version']), (4, [], r2['version']))

    def test_polled_reads_answer_304_when_unchanged(self):
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        http.workspace = server.Workspace(FakeBridge())
        http.workspace._providers = []
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        base = 'http://127.0.0.1:%d' % http.server_port
        with patch.object(server.Workspace, 'all_sessions', lambda self, ready=True: []):
            for path in ('/api/state', '/api/state?lite=1', '/api/canvas?cwd=' + urllib.parse.quote(str(self.repo))):
                r = urllib.request.urlopen(base + path, timeout=5)
                tag, body = r.headers['ETag'], json.load(r)
                self.assertTrue(tag)
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(urllib.request.Request(base + path, headers={'If-None-Match': tag}), timeout=5)
                self.assertEqual(e.exception.code, 304)
                r2 = urllib.request.urlopen(urllib.request.Request(base + path, headers={'If-None-Match': '"other"'}), timeout=5)
                self.assertEqual(r2.status, 200)
            st = json.load(urllib.request.urlopen(base + '/api/diff?status=1&cwd=' + urllib.parse.quote(str(self.repo)), timeout=5))
            self.assertEqual(set(st), {'status'})
            self.assertIn('app/src/edit.py', st['status'])
            plain = Path(tempfile.mkdtemp())          # a folder outside Git: the poll answers with nothing to tint, not a 400
            self.addCleanup(shutil.rmtree, plain, True)
            st = json.load(urllib.request.urlopen(base + '/api/diff?status=1&cwd=' + urllib.parse.quote(str(plain)), timeout=5))
            self.assertEqual(st, {'status': '', 'repo': False})

    def test_long_titles_are_cut(self):
        w = server.Workspace(FakeBridge())
        w._providers = [None]          # Codex only: the other providers are not read
        with patch.object(server.Workspace, 'sessions', lambda self: [{'id': 'a', 'title': 'x' * 5000}, {'id': 'b', 'title': 'short'}]):
            titles = [s['title'] for s in w.all_sessions()]
        self.assertEqual(len(titles[0]), server.TITLE_MAX)
        self.assertTrue(titles[0].endswith('…'))
        self.assertEqual(titles[1], 'short')

# ---------- git and memory ----------
def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='T', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='T', GIT_COMMITTER_EMAIL='t@t')
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, env=env).stdout.decode()


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        for i in range(3):
            (self.repo / 'a.txt').write_text(str(i))
            git(self.repo, 'add', '.')
            git(self.repo, 'commit', '-q', '-m', 'main ' + str(i))
        git(self.repo, 'checkout', '-q', '-b', 'feat')
        for i in range(2):
            (self.repo / ('f%d.txt' % i)).write_text('x')
            git(self.repo, 'add', '.')
            git(self.repo, 'commit', '-q', '-m', 'feat ' + str(i))
        git(self.repo, 'checkout', '-q', 'main')
        git(self.repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat'", 'feat')
        git(self.repo, 'branch', 'side')
        self.wt = self.root / 'wt'
        git(self.repo, 'worktree', 'add', '-b', 'work', str(self.wt))
        (self.wt / 'w.txt').write_text('w')
        git(self.wt, 'add', '.')
        git(self.wt, 'commit', '-q', '-m', 'work 0')
        git(self.repo, 'checkout', '-q', 'side')
        server.GIT_CACHE.clear()

    def test_merged_name_reads_the_subject(self):
        self.assertEqual(server.merged_name("Merge branch 'ide-gitmem'"), 'ide-gitmem')
        self.assertEqual(server.merged_name('Merge borrowed suite adapters (branch suite-adapters-2026-11-01)'), 'suite-adapters-2026-11-01')
        self.assertEqual(server.merged_name('Merge ide-chat-log: the chat log'), 'ide-chat-log')
        self.assertIsNone(server.merged_name('Fix the floor'))

    def test_branches_worktrees_counts_and_merges(self):
        d = server.git_data(str(self.repo))
        self.assertEqual((d['current'], d['base'], d['root']), ('side', 'main', str(self.repo)))
        self.assertEqual(d['shown'][0], 'main')          # the default branch first
        self.assertIn('side', d['shown'])          # this floor's branch always shows
        times = {b['name']: b['time'] for b in d['branches']}
        lanes = d['shown'][1:]
        self.assertEqual(lanes, sorted(lanes, key=lambda n: -times[n]))          # newest first: it stands closest to the floor
        self.assertLessEqual(len([n for n in lanes if n != 'side']), 3)
        by = {b['name']: b for b in d['branches']}
        self.assertEqual((by['work']['ahead'], by['work']['behind'], by['work']['files']), (1, 0, 1))
        self.assertEqual(by['work']['worktree'], str(self.wt))
        self.assertEqual(by['work']['commits'][0]['subject'], 'work 0')
        self.assertEqual((by['side']['ahead'], by['side']['commits']), (0, []))
        self.assertEqual(by['main']['count'], 4)                    # first parent only: the merge is one step
        m = by['main']['commits'][0]
        self.assertEqual((m['parents'], m['merged'], m['mergedCount'], [c['subject'] for c in m['mergedCommits']]), (2, 'feat', 2, ['feat 1', 'feat 0']))
        self.assertIn(str(self.wt), [w['path'] for w in d['worktrees']])
        self.assertIs(server.git_data(str(self.repo)), d)           # cached
        git(self.wt, 'commit', '-q', '--allow-empty', '-m', 'work 1')
        server.git_invalidate(str(self.repo), server.git_status_sig(str(self.repo)))        # the Git poll sees the branch move
        self.assertEqual({b['name']: b for b in server.git_data(str(self.repo))['branches']}['work']['ahead'], 2)

    def test_lanes_are_the_three_newest_and_this_floor(self):
        repo = self.root / 'many'
        repo.mkdir()
        def g(*args, when=None):
            env = dict(os.environ, GIT_AUTHOR_NAME='T', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='T', GIT_COMMITTER_EMAIL='t@t')
            if when:
                env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=env)
        g('init', '-q'); g('symbolic-ref', 'HEAD', 'refs/heads/main')
        g('commit', '-q', '--allow-empty', '-m', 'root', when='2026-11-01T10:00:00')
        for i, n in enumerate(['old', 'b1', 'b2', 'b3', 'b4']):          # old is oldest, b4 newest
            g('checkout', '-q', '-b', n, 'main')
            g('commit', '-q', '--allow-empty', '-m', n, when='2026-11-0%dT10:00:00' % (i + 2))
        g('checkout', '-q', 'b4')
        d = server.git_data(str(repo))
        self.assertEqual(d['shown'], ['main', 'b4', 'b3', 'b2'])
        self.assertEqual(d['more'], 2)
        g('checkout', '-q', 'old')          # this floor's branch is old: it shows in addition, last by time
        server.GIT_CACHE.clear()
        d = server.git_data(str(repo))
        self.assertEqual((d['shown'], d['more']), (['main', 'b4', 'b3', 'b2', 'old'], 1))

    def test_tree_of_a_branch_and_folders_outside_git(self):
        self.assertEqual(server.git_tree(str(self.repo), 'feat')['files'], ['a.txt', 'f0.txt', 'f1.txt'])
        with self.assertRaises(ValueError):
            server.git_tree(str(self.repo), 'nope')
        plain = self.root / 'plain'
        plain.mkdir()
        self.assertFalse(server.git_repo(str(plain)))
        self.assertTrue(server.git_repo(str(self.wt)))


class NestedRepoTests(unittest.TestCase):
    """floor (2026-11-21): a folder that is not a repository but holds repositories and a folder of their linked worktrees."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'project'
        self.root.mkdir()
        (self.root / 'notes.md').write_text('n')
        for name in ('alpha', 'beta'):
            r = self.root / name
            r.mkdir()
            git(r, 'init', '-q')
            git(r, 'symbolic-ref', 'HEAD', 'refs/heads/main')
            (r / '.gitignore').write_text('build/\n')
            (r / 'src').mkdir()
            for i in range(3):
                (r / 'src' / ('f%d.py' % i)).write_text(str(i))
            git(r, 'add', '.')
            git(r, 'commit', '-q', '-m', 'start ' + name)
            (r / 'build').mkdir()
            (r / 'build' / 'out.bin').write_text('ignored')
        (self.root / 'worktrees').mkdir()
        for i in range(3):
            git(self.root / 'alpha', 'worktree', 'add', '-b', 'task-%d' % i, str(self.root / 'worktrees' / ('alpha-%d' % i)))
        git(self.root / 'beta', 'worktree', 'add', '-b', 'side', str(self.root / 'worktrees' / 'beta-0'))
        self.away = Path(self.tmp.name).resolve() / 'elsewhere'          # a repository outside the folder whose worktree is inside it
        self.away.mkdir()
        git(self.away, 'init', '-q')
        git(self.away, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(self.away, 'commit', '-q', '--allow-empty', '-m', 'away')
        git(self.away, 'worktree', 'add', '-b', 'visit', str(self.root / 'worktrees' / 'away-0'))
        server.GIT_CACHE.clear()

    def test_walk_lists_repositories_by_git_and_leaves_worktrees_out(self):
        files = server.files_in(str(self.root))['files']
        self.assertIn('notes.md', files)
        self.assertIn('alpha/src/f0.py', files)
        self.assertIn('alpha/.gitignore', files)          # Git's own listing, as in a repository project
        self.assertNotIn('alpha/build/out.bin', files)          # ignored by the repository
        self.assertFalse([p for p in files if p.startswith('worktrees/')])          # linked worktrees are lanes, not folders

    def test_cap_is_shared_between_top_level_entries(self):
        big = self.root / 'alpha' / 'data'
        big.mkdir()
        for i in range(40):
            (big / ('d%02d.csv' % i)).write_text('x')
        with patch.object(server, 'FILES_CAP', 20):
            out = server.files_in(str(self.root))
        files = out['files']
        self.assertTrue(out['truncated'])
        self.assertEqual(len(files), 20)
        self.assertIn('notes.md', files)
        self.assertEqual(len([p for p in files if p.startswith('beta/')]), 4)          # the small repository keeps all it has
        self.assertEqual(len([p for p in files if p.startswith('alpha/')]), 15)          # the large one gets what is left
        self.assertEqual(server.share_cap({'a': [1] * 50, 'b': [1] * 3, '': [1]}, 20), {'': 1, 'b': 3, 'a': 16})

    def test_nested_repos_inside_first_then_the_owners_of_worktrees(self):
        tops = server.nested_repos(str(self.root))
        self.assertEqual(tops, [str(self.root / 'alpha'), str(self.root / 'beta'), str(self.away)])
        self.assertEqual(server.nested_repos(str(self.root / 'worktrees' / 'alpha-0' / 'src')), [])

    def test_git_route_over_several_repositories(self):
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        http.workspace = server.Workspace(FakeBridge())
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        if True:
            r = json.load(urllib.request.urlopen('http://127.0.0.1:%d/api/git?cwd=%s' % (http.server_port, urllib.parse.quote(str(self.root))), timeout=10))
        self.assertIs(r['repo'], False)
        self.assertEqual([(x['name'], x['rel']) for x in r['repos']], [('alpha', 'alpha'), ('beta', 'beta'), ('elsewhere', None)])
        beta = r['repos'][1]
        self.assertEqual((beta['base'], beta['current']), ('main', 'main'))
        self.assertIn('side', beta['shown'])
        self.assertEqual({b['name']: b['worktree'] for b in beta['branches']}['side'], str(self.root / 'worktrees' / 'beta-0'))
        plain = Path(self.tmp.name) / 'plain'
        plain.mkdir()
        r = json.load(urllib.request.urlopen('http://127.0.0.1:%d/api/git?cwd=%s' % (http.server_port, urllib.parse.quote(str(plain))), timeout=10))
        self.assertEqual(r, {'repo': False})


class FloorGrowsTests(unittest.TestCase):
    """floor grows: which files the floor's history created, and when (server.births, the one place the rule lives)"""
    HAS_BT = hasattr(os.stat('.'), 'st_birthtime')          # macOS records a file's creation; elsewhere only write events tell

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / 'repo'
        self.repo.mkdir()
        self.git = lambda *a, **env: subprocess.run(['git', '-C', str(self.repo), *a], check=True, capture_output=True, env={**os.environ, **env})
        self.git('init', '-q')
        for f in ('old.txt', 'kept.txt'):
            (self.repo / f).write_text(f)
        (self.repo / 'pre.txt').write_text('on disk, untracked, before the run')
        self.git('add', 'old.txt', 'kept.txt')
        past = str(int(time.time()) - 100)
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'one', GIT_AUTHOR_DATE=past + ' +0000', GIT_COMMITTER_DATE=past + ' +0000')
        time.sleep(1.1)
        self.origin = time.time()          # the floor's first moment: the first session's start
        (self.repo / 'app').mkdir()
        (self.repo / 'app' / 'new.py').write_text('made by an agent, untracked')
        (self.repo / 'shell.txt').write_text('cat > shell.txt, no write event')
        (self.repo / 'added.txt').write_text('made, then committed by the run')
        self.git('add', 'added.txt')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'two')
        server._scans.clear()
        server._untracked.clear()
        server._before_trees.clear()

    def born(self, writes, cwd=None):
        cwd = cwd or self.repo
        return server.births(str(cwd), self.origin, writes, server.files_in(str(cwd))['files'])

    def test_files_in_git_before_the_origin_are_always_there_even_when_written(self):
        b = self.born([(3.0, 'old.txt'), (4.0, 'kept.txt')])
        self.assertNotIn('old.txt', b)
        self.assertNotIn('kept.txt', b)

    def test_new_files_are_born_at_their_first_write_or_their_creation_on_disk(self):
        b = self.born([(40.0, 'app/new.py'), (60.0, 'app/new.py')])
        if self.HAS_BT:          # created a moment after the origin, before the write event: the creation wins
            self.assertLess(b['app/new.py'], 5)
            self.assertGreaterEqual(b['app/new.py'], 0)
            self.assertIn('shell.txt', b)          # a shell command's file: no write event, its creation time
            self.assertIn('added.txt', b)          # committed after the origin: new
            self.assertNotIn('pre.txt', b)          # untracked but on disk before the origin
        else:
            self.assertEqual(b['app/new.py'], 40.0)
            self.assertNotIn('shell.txt', b)          # nothing known: always shown
        b2 = self.born([(0.0, 'added.txt')])
        self.assertEqual(b2['added.txt'], 0.0)          # the earliest of the two

    def test_a_repository_whose_first_commit_came_later_has_every_file_new(self):
        b = server.births(str(self.repo), self.origin - 1000, [(5.0, 'old.txt')], server.files_in(str(self.repo))['files'])
        self.assertIn('old.txt', b)
        self.assertLessEqual(b['old.txt'], 5.0)

    def test_a_subfolder_floor_reads_paths_relative_to_it(self):
        b = self.born([(7.0, 'new.py')], self.repo / 'app')
        self.assertIn('new.py', b)

    def test_the_tree_before_the_origin_is_read_once_per_commit(self):
        calls = []
        real = server.run_git
        with patch.object(server, 'run_git', lambda *a: calls.append(a[1]) or real(*a)):
            self.born([])
            self.born([])
        self.assertEqual(calls.count('ls-tree'), 1)
        self.assertEqual(calls.count('rev-list'), 1)          # within SCAN_TTL the base commit is kept too

    def test_outside_git_only_the_disk_can_say_a_file_is_new(self):
        plain = Path(self.tmp.name).resolve() / 'plain'
        plain.mkdir()
        (plain / 'before.txt').write_text('x')
        time.sleep(1.1)
        origin = time.time()
        (plain / 'after.txt').write_text('x')
        b = server.births(str(plain), origin, [(2.0, 'before.txt'), (3.0, 'after.txt')], server.files_in(str(plain))['files'])
        self.assertNotIn('before.txt', b)
        if self.HAS_BT:
            self.assertIn('after.txt', b)
        else:
            self.assertEqual(b, {})


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_summary_first_line_skips_the_preface_and_headings(self):
        text = ('This session is being continued from a previous conversation.\n\nSummary:\n1. Primary Request and Intent:\n'
                '   The owner wants the floor wired to git.\n2. Key Concepts:')
        self.assertEqual(server.summary_first(text), 'The owner wants the floor wired to git.')
        self.assertIsNone(server.summary_first(None))
        self.assertEqual(server.summary_first('**Summary**\n\nThe tree folds finished subagents.'), 'The tree folds finished subagents.')
        self.assertEqual(server.summary_first('## Summary\nThe tree folds.'), 'The tree folds.')

    def test_notes_are_written_then_read_back_and_named_outputs_kept_apart(self):
        log = {'events': [{'kind': 'write', 'path': 'notes/plan.md', 'at': 1}, {'kind': 'read', 'path': 'notes/plan.md', 'at': 5},
                          {'kind': 'read', 'path': 'old.md', 'at': 2}, {'kind': 'write', 'path': 'old.md', 'at': 3},
                          {'kind': 'write', 'path': 'AGENTS.md', 'at': 1}, {'kind': 'read', 'path': 'AGENTS.md', 'at': 2},
                          {'kind': 'write', 'path': 'out/report.md', 'at': 4}, {'kind': 'read', 'path': 'out/report.md', 'at': 6},
                          {'kind': 'write', 'path': 'x.py', 'at': 1}, {'kind': 'read', 'path': 'x.py', 'at': 2}],
               'messages': [{'role': 'assistant', 'text': 'Done: see report.md.'}]}
        notes, outputs = server.notes_of(log)
        self.assertEqual([(n['path'], n['writes'], n['reads']) for n in notes], [('notes/plan.md', [1], [5])])
        self.assertEqual([n['path'] for n in outputs], ['out/report.md'])

    def test_codex_instruction_chain(self):
        home, repo = self.root / 'codex', self.root / 'repo'
        (repo / 'sub' / 'deep').mkdir(parents=True)
        home.mkdir()
        git(repo, 'init', '-q')
        (home / 'AGENTS.md').write_text('global\n')
        (repo / 'AGENTS.md').write_text('root\n')
        (repo / 'sub' / 'AGENTS.md').write_text('sub\n')
        (repo / 'sub' / 'AGENTS.override.md').write_text('override\n')
        got = server.codex_instructions(repo / 'sub' / 'deep', home)
        self.assertEqual([(Path(e['path']).relative_to(self.root).as_posix(), e['how']) for e in got],
                         [('codex/AGENTS.md', 'global'), ('repo/AGENTS.md', 'project'), ('repo/sub/AGENTS.override.md', 'project')])

    def test_claude_instruction_files(self):
        home, proj = self.root / 'claude', self.root / 'proj'
        (proj / '.claude' / 'rules').mkdir(parents=True)
        home.mkdir()
        (home / 'CLAUDE.md').write_text('g\n')
        (proj / 'CLAUDE.md').write_text('p\n')
        (proj / '.claude' / 'rules' / 'style.md').write_text('r\n')
        (proj / '.claude' / 'settings.json').write_text('{}')
        mem = home / 'projects' / encode(str(proj)) / 'memory'
        mem.mkdir(parents=True)
        (mem / 'MEMORY.md').write_text('- a\n- b\n')
        got = [(Path(e['path']).name, e['how']) for e in server.claude_instructions(str(proj), home)]
        self.assertEqual(got, [('CLAUDE.md', 'global'), ('CLAUDE.md', 'project'), ('style.md', 'rules'), ('MEMORY.md', 'memory'), ('settings.json', 'settings')])

    def test_claude_compaction_keeps_its_summary(self):
        # the fixture's compact_boundary is followed by its isCompactSummary record (text redacted to x's): the compaction carries it
        home, folder = claude_store(self.root, FIXTURE_CWD)
        h = ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none').read_history('claude:s1')
        summary = h['compacts'][0]['summary']
        self.assertTrue(summary and set(summary) == {'x'})
        self.assertEqual(server.history_compacts(h)[0]['first'], summary[:160])

# ---------- end git and memory ----------

# ---------- connectors ----------
class ConnectorTests(unittest.TestCase):
    """MCP calls in both harnesses' histories, and /api/connectors (configured, live, seen in sessions)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        (self.project / 'src').mkdir(parents=True)
        for f in ('src/app.py', 'src/new.py'):
            (self.project / f).write_text('x')
        (self.project / '.mcp.json').write_text(json.dumps({'mcpServers': {'local_db': {'command': 'db-mcp'}}}))
        self.codex_home = self.root / 'codex'
        self.codex_home.mkdir()
        (self.codex_home / 'config.toml').write_text('[mcp_servers.node_repl]\ncommand = "/bin/node_repl"\nargs = []\n\n[mcp_servers.far_away]\nurl = "https://example.invalid/mcp"\n')
        claude_json = self.root / 'claude.json'
        claude_json.write_text(json.dumps({'mcpServers': {'user_tool': {'command': 'ut'}}, 'projects': {str(self.project): {'mcpServers': {'proj_tool': {'url': 'http://127.0.0.1:9/mcp'}}}, '/elsewhere': {'mcpServers': {'other': {}}}}}))
        self.lists = server.McpLists(runner=lambda h, cwd, timeout: (None, 'absent'))          # connectors list: never the real CLIs in tests
        for p in (patch.object(server, 'CODEX_HOME', self.codex_home), patch.object(server, 'CLAUDE_JSON', claude_json), patch.object(server, 'MCP_LISTS', self.lists), patch.object(server, 'MOCK_ON', None)):
            p.start()
            self.addCleanup(p.stop)
        self.home, folder = claude_store(self.root, str(self.project))
        recs = [{'type': 'assistant', 'timestamp': '2026-11-01T10:09:00.000Z', 'cwd': str(self.project), 'uuid': 'u-m1',
                 'message': {'id': 'msg_m1', 'role': 'assistant', 'model': 'claude-fable-5-1', 'content': [{'type': 'tool_use', 'id': 'toolu_m1', 'name': 'mcp__claude_ai_Google_Drive__search_files', 'input': {'query': 'plan'}}]}},
                {'type': 'user', 'timestamp': '2026-11-01T10:09:04.000Z', 'cwd': str(self.project), 'uuid': 'u-m2',
                 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_m1', 'content': 'two files'}]}}]
        with (folder / 's1.jsonl').open('a') as f:
            f.write(''.join(json.dumps(r) + '\n' for r in recs))

    def rollout(self):
        path = self.root / 'rollout.jsonl'
        lines = [{'timestamp': '2026-11-01T10:00:00.000Z', 'type': 'session_meta', 'payload': {'timestamp': '2026-11-01T10:00:00.000Z', 'cwd': str(self.project)}},
                 {'timestamp': '2026-11-01T10:00:01.000Z', 'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'js', 'namespace': 'mcp__cua_repl', 'arguments': '{"code":"1"}', 'call_id': 'call_a'}},
                 {'timestamp': '2026-11-01T10:00:03.000Z', 'type': 'event_msg', 'payload': {'type': 'item_completed', 'started_at_ms': 1793527201000, 'completed_at_ms': 1793527203000,
                  'item': {'type': 'McpToolCall', 'id': 'call_a', 'server': 'cua_repl', 'tool': 'js', 'arguments': {'code': '1'}, 'status': 'completed'}}},
                 {'timestamp': '2026-11-01T10:00:05.000Z', 'type': 'event_msg', 'payload': {'type': 'item_completed', 'started_at_ms': 1793527204000, 'completed_at_ms': 1793527205000,
                  'item': {'type': 'McpToolCall', 'id': 'call_b', 'server': 'codex_app', 'tool': 'list_threads', 'arguments': {}, 'status': 'completed'}}}]
        path.write_text(''.join(json.dumps(r) + '\n' for r in lines))
        return {'id': 'codex1', 'rollout_path': str(path), 'created_at': 1793527200, 'updated_at': 1793527205, 'cwd': str(self.project)}

    def test_codex_mcp_calls_become_mcp_events_with_their_end(self):
        log = server.Observer().read_log(self.rollout())
        mcp = [(e['connector'], e['tool'], e['item'], e['end'] - e['at']) for e in log['events'] if e['kind'] == 'mcp']
        self.assertEqual(mcp, [('cua_repl', 'js', 'call_a', 2.0), ('codex_app', 'list_threads', 'call_b', 1.0)])   # the second has no function_call: its event comes from item_completed
        tools = [m for m in log['messages'] if m['role'] == 'tool']
        self.assertEqual([(m['text'], m['connector'], m['tool']) for m in tools], [('cua_repl.js', 'cua_repl', 'js'), ('codex_app.list_threads', 'codex_app', 'list_threads')])

    def test_claude_mcp_tool_use_maps_to_server_and_tool(self):
        p = ClaudeCodeProvider(tokens(), home=self.home, app_support=self.root / 'none')
        h = p.read_history('claude:s1')
        e = [e for e in h['events'] if e['kind'] == 'mcp']
        self.assertEqual([(x['connector'], x['tool'], x['end'] - x['at']) for x in e], [('claude_ai_Google_Drive', 'search_files', 4.0)])
        m = [m for m in h['messages'] if m.get('connector')]
        self.assertTrue(m[0]['text'].startswith('claude_ai_Google_Drive.search_files'))
        self.assertEqual(server.connector_id('claude_ai_Google_Drive'), 'claude_ai_google_drive')

    def test_connectors_endpoint_lists_configured_live_and_seen(self):
        class LiveBridge(FakeBridge):
            mcp = {'*': {'node_repl': {'status': 'ready'}}}
            def call(self, method, params, timeout=45):
                assert method == 'mcpServerStatus/list', method
                return {'data': [{'name': 'node_repl', 'tools': {}}]}
        bridge = LiveBridge()
        row = self.rollout()
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        ws = http.workspace = server.Workspace(bridge)
        ws._providers = [CodexProvider(ws, self.codex_home), ClaudeCodeProvider(tokens(), home=self.home, app_support=self.root / 'none')]
        ws.observer.rows = lambda limit=300: [row]
        ws.observer.sessions = lambda b: [{'id': 'codex1', 'title': 'Codex work', 'cwd': str(self.project), 'projectId': None, 'source': 'cli', 'model': None, 'updatedAt': row['updated_at'],
                                          'createdAt': row['created_at'], 'parent': None, 'status': 'idle', 'managed': False, 'external': False, 'turnId': None, 'preview': ''}]
        ws.observer.cache['codex1'] = ws.observer.read_log(row)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        base = 'http://127.0.0.1:' + str(http.server_port)
        r = json.load(urllib.request.urlopen(base + '/api/connectors?cwd=' + urllib.parse.quote(str(self.project)), timeout=5))
        by = {c['id']: c for c in r['connectors']}
        self.assertEqual(r['harnesses'], ['claude', 'codex'])
        self.assertEqual(set(by), {'node_repl', 'far_away', 'user_tool', 'proj_tool', 'local_db', 'cua_repl', 'codex_app', 'claude_ai_google_drive'})   # not /elsewhere's
        self.assertEqual(sorted((s['harness'], s['source']) for s in by['node_repl']['sources']), [('codex', 'file'), ('codex', 'live')])
        self.assertEqual((by['node_repl']['status'], by['node_repl']['kind'], by['node_repl']['calls'], by['node_repl']['shown']), ('ready', 'stdio', 0, True))
        self.assertEqual(by['far_away']['kind'], 'http')
        self.assertNotIn('example.invalid', json.dumps(r))          # connectors list: no command or URL leaves the server
        self.assertNotIn('/bin/node_repl', json.dumps(r))
        self.assertEqual((by['node_repl']['builtin'], by['node_repl']['per']['codex']), (True, {'from': 'built into the Codex app', 'status': 'connected'}))
        self.assertEqual(by['local_db']['per']['claude']['from'], "this project's .mcp.json")
        self.assertEqual(by['user_tool']['per']['claude']['from'], 'user config')
        self.assertEqual([s['source'] for s in by['local_db']['sources']], ['file'])
        self.assertEqual((by['cua_repl']['calls'], by['cua_repl']['tools'], by['cua_repl']['harnesses']), (1, {'js': {'codex1': 1}}, ['codex']))
        self.assertEqual((by['claude_ai_google_drive']['name'], by['claude_ai_google_drive']['sources'][0]['source'], by['claude_ai_google_drive']['tools']),
                         ('claude ai Google Drive', 'seen-in-session', {'search_files': {'claude:s1': 1}}))
        self.assertEqual(by['codex_app']['logo'], None)
        D = json.load(urllib.request.urlopen(base + '/api/canvas?cwd=' + urllib.parse.quote(str(self.project)), timeout=5))
        mcp = sorted((e['session'], e['connector'], e['tool'], e['end'] - e['t']) for e in D['events'] if e['kind'] == 'mcp')
        self.assertEqual(mcp, [('claude:s1', 'claude_ai_google_drive', 'search_files', 4.0), ('codex1', 'codex_app', 'list_threads', 1.0), ('codex1', 'cua_repl', 'js', 2.0)])

class ConnectorListTests(unittest.TestCase):
    """connectors list: the harnesses' own `mcp list` answers, parsed to names, kinds and status only, cached, never blocking."""
    CLAUDE_OUT = ('Checking MCP server health…\n\n'
                  'claude.ai Claude Docs: https://api.anthropic.com/v1/pages/mcp - ✔ Connected\n'
                  'claude.ai Google Drive: https://drivemcp.googleapis.com/mcp/v1?token=SECRET123 - ✔ Connected\n'
                  'local_db: db-mcp --password hunter2 - ✗ Failed to connect\n'
                  'linear: https://mcp.linear.app/sse - ⚠ Needs authentication\n')
    CODEX_OUT = json.dumps([
        {'name': 'codex_app', 'enabled': False, 'transport': {'type': 'stdio', 'command': '/x/launch', 'args': ['./server.mjs'], 'env': {'API_KEY': 'sk-SECRET'}}, 'auth_status': 'unsupported'},
        {'name': 'node_repl', 'enabled': True, 'transport': {'type': 'stdio', 'command': '/x/node_repl', 'env': {'TOKEN': 'tok-SECRET'}}, 'auth_status': 'unsupported'},
        {'name': 'notion', 'enabled': True, 'transport': {'type': 'streamable_http', 'url': 'https://mcp.notion.com/mcp', 'bearer_token_env_var': 'NOTION_TOKEN'}, 'auth_status': 'not_logged_in'}])

    def test_claude_list_parses_names_and_status_and_drops_targets(self):
        rows = server.parse_claude_mcp_list(self.CLAUDE_OUT)
        self.assertEqual([(r['name'], r['account'], r['kind'], r['status']) for r in rows],
                         [('Claude Docs', True, 'http', 'connected'), ('Google Drive', True, 'http', 'connected'), ('local_db', False, 'stdio', 'failed'), ('linear', False, 'http', 'needs auth')])
        txt = json.dumps(rows)
        for secret in ('SECRET123', 'hunter2', 'googleapis', 'db-mcp'):
            self.assertNotIn(secret, txt)
        self.assertEqual(server.parse_claude_mcp_list('No MCP servers configured.'), [])

    def test_codex_list_keeps_no_command_env_or_url(self):
        rows = server.parse_codex_mcp_list(self.CODEX_OUT)
        self.assertEqual([(r['name'], r['kind'], r['status']) for r in rows], [('codex_app', 'stdio', 'disabled'), ('node_repl', 'stdio', 'unknown'), ('notion', 'streamable_http', 'needs auth')])
        txt = json.dumps(rows)
        for secret in ('sk-SECRET', 'tok-SECRET', 'NOTION_TOKEN', '/x/', 'notion.com'):
            self.assertNotIn(secret, txt)
        self.assertIsNone(server.parse_codex_mcp_list('not json'))

    def test_builtin_servers_are_told_apart(self):
        self.assertEqual([n for n in ('node_repl', 'codex_apps', 'cua_repl', 'ccd_session', 'Claude_Browser', 'computer-use', 'claude_ai_Google_Drive', 'github', 'linear') if server.builtin_server(n)],
                         ['node_repl', 'codex_apps', 'cua_repl', 'ccd_session', 'Claude_Browser', 'computer-use'])

    def test_get_never_blocks_and_caches(self):
        gate, calls = threading.Event(), []
        def slow(h, cwd, timeout):
            calls.append(h); gate.wait(5); return [{'name': 'x', 'status': 'connected'}], 'ok'
        lists = server.McpLists(runner=slow, ttl=300)
        t0 = time.time()
        self.assertEqual(lists.get('claude', '/p')['state'], 'pending')
        self.assertLess(time.time() - t0, 0.5)
        self.assertEqual(lists.get('claude', '/p')['state'], 'pending')          # one run at a time
        gate.set()
        for _ in range(50):
            if lists.get('claude', '/p')['state'] == 'ok':
                break
            time.sleep(0.05)
        got = lists.get('claude', '/p')
        self.assertEqual((got['state'], got['rows']), ('ok', [{'name': 'x', 'status': 'connected'}]))
        self.assertEqual(calls, ['claude'])          # cached: no second run inside the ttl

    def test_timeout_path_reports_timeout_and_keeps_old_rows(self):
        lists = server.McpLists(timeout=0.2)
        with patch.object(server, 'claude_binary', lambda: sys.executable), patch.object(server.subprocess, 'run', side_effect=subprocess.TimeoutExpired('claude', 0.2)):
            self.assertEqual(lists.run('claude', '/tmp', 0.2), (None, 'timeout'))
        with patch.object(server, 'claude_binary', lambda: None):
            self.assertEqual(lists.run('claude', '/tmp', 0.2), (None, 'absent'))
        done = subprocess.CompletedProcess(['claude'], 0, stdout='Checking MCP server health…\n\nNo MCP servers configured.\n', stderr='')
        with patch.object(server, 'claude_binary', lambda: sys.executable), patch.object(server.subprocess, 'run', return_value=done):
            self.assertEqual(lists.run('claude', '/tmp', 0.2), (None, 'empty'))          # an empty answer keeps the last rows (see _refresh)
        n = [0]
        def flaky(h, cwd, timeout):
            n[0] += 1
            return ([{'name': 'a', 'status': 'connected'}], 'ok') if n[0] == 1 else (None, 'timeout')
        lists = server.McpLists(runner=flaky, ttl=0)
        lists._refresh(('claude', '/p'))
        lists._refresh(('claude', '/p'))
        c = lists.cache[('claude', '/p')]
        self.assertEqual((c['state'], c['rows']), ('timeout', [{'name': 'a', 'status': 'connected'}]))

    def test_endpoint_shows_account_connectors_merged_with_their_session_twin(self):
        t = ConnectorTests('test_connectors_endpoint_lists_configured_live_and_seen')
        t.setUp(); self.addCleanup(t.doCleanups)
        rows = {'claude': (server.parse_claude_mcp_list(self.CLAUDE_OUT), 'ok'), 'codex': (server.parse_codex_mcp_list(self.CODEX_OUT), 'ok')}
        lists = server.McpLists(runner=lambda h, cwd, timeout: rows[h])
        for h in ('claude', 'codex'):
            lists._refresh((h, str(t.project)))
        names = t.root / 'names.json'
        names.write_text(json.dumps({'claude_ai_Google_Drive': {'name': 'Google Drive', 'logo': 'google_drive'}}))
        with patch.object(server, 'MCP_LISTS', lists), patch.object(server, 'CONNECTOR_NAMES', names):
            class Off(FakeBridge):
                ready = False
            ws = server.Workspace(Off())
            ws._providers = [ClaudeCodeProvider(tokens(), home=t.home, app_support=t.root / 'none')]
            r = ws.connectors(str(t.project))
        by = {c['id']: c for c in r['connectors']}
        g = by['claude_ai_google_drive']          # the session's entry took the list's word
        self.assertEqual((g['name'], g['per']['claude'], g['logo']), ('Google Drive', {'from': 'claude.ai account', 'status': 'connected'}, 'google_drive'))
        self.assertEqual([c['id'] for c in r['connectors'] if c['name'] == 'Google Drive'], ['claude_ai_google_drive'])
        self.assertEqual(by['claude_ai_claude_docs']['per']['claude'], {'from': 'claude.ai account', 'status': 'connected'})
        self.assertEqual(by['local_db']['per']['claude'], {'from': "this project's .mcp.json", 'status': 'failed'})
        self.assertEqual(by['codex_app']['per']['codex'], {'from': 'built into the Codex app', 'status': 'disabled'})
        self.assertEqual(r['lists'], {'claude': {'state': 'ok', 'at': r['lists']['claude']['at']}, 'codex': {'state': 'ok', 'at': r['lists']['codex']['at']}})
        txt = json.dumps(r)
        for secret in ('SECRET', 'hunter2', 'NOTION_TOKEN', 'googleapis'):
            self.assertNotIn(secret, txt)
        with patch.object(server, 'MCP_LISTS', lists), patch.object(server, 'MOCK_ON', 'junior'):          # the mock never shows the person's lists
            r2 = ws.connectors(str(t.project))
        self.assertEqual(r2['lists'], {})
# ---------- end connectors ----------


# ---------- mock: connectors.json names and the junior SWE scenario (AGENT_IDE_MOCK=junior) ----------
import mock

DRIVE, SLACK, LINEAR = 'dddd0000-0000-4000-8000-000000000001', '3f1c2a9e-5b7d-4e2a-9c61-0d8e4b7a2f15', 'a7e4c0d2-81f3-4b6e-9d25-6c0b1e9f3a48'


class ConnectorNameTests(unittest.TestCase):
    """connectors.json gives a server id a display name and a logo; an id with no entry keeps its own name."""
    setUp, rollout = ConnectorTests.setUp, ConnectorTests.rollout

    def test_names_file_skips_notes_and_survives_a_bad_file(self):
        f = self.root / 'names.json'
        f.write_text(json.dumps({'_about': 'x', 'github': {'name': 'GitHub', 'logo': 'github'}, 'odd': 'not a dict'}))
        self.assertEqual(server.connector_names(f), {'github': {'name': 'GitHub', 'logo': 'github'}})
        f.write_text('{not json')
        self.assertEqual(server.connector_names(f), {})
        self.assertEqual(server.connector_names(self.root / 'absent.json'), {})

    def test_connectors_endpoint_uses_names_and_logos(self):
        names = self.root / 'names.json'
        names.write_text(json.dumps({'claude_ai_Google_Drive': {'name': 'Google Drive', 'logo': 'google_drive'}, 'cua_repl': {'name': 'Computer use', 'logo': 'no_such_file'}}))
        folder = next((self.home / 'projects').iterdir())
        with (folder / 's1.jsonl').open('a') as f:      # a claude.ai connector recorded only by its UUID, with no entry
            f.write(json.dumps({'type': 'assistant', 'timestamp': '2026-11-01T10:10:00.000Z', 'cwd': str(self.project), 'uuid': 'u-m3',
                                'message': {'id': 'msg_m3', 'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_m3', 'name': 'mcp__0a1b2c3d-1111-4222-8333-444455556666__ping', 'input': {}}]}}) + '\n')
        row = self.rollout()
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        class QuietBridge(FakeBridge):
            def call(self, method, params, timeout=45):
                return {'data': []}
        ws = http.workspace = server.Workspace(QuietBridge())
        ws._providers = [CodexProvider(ws, self.codex_home), ClaudeCodeProvider(tokens(), home=self.home, app_support=self.root / 'none')]
        ws.observer.sessions = lambda b: [{'id': 'codex1', 'title': 'Codex work', 'cwd': str(self.project), 'projectId': None, 'source': 'cli', 'model': None, 'updatedAt': row['updated_at'],
                                          'createdAt': row['created_at'], 'parent': None, 'status': 'idle', 'managed': False, 'external': False, 'turnId': None, 'preview': ''}]
        ws.observer.cache['codex1'] = ws.observer.read_log(row)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        with patch.object(server, 'CONNECTOR_NAMES', names), patch.object(server, 'OWN_CONNECTOR_NAMES', self.root / 'no-own.json'):
            r = json.load(urllib.request.urlopen('http://127.0.0.1:' + str(http.server_port) + '/api/connectors?cwd=' + urllib.parse.quote(str(self.project)), timeout=5))
        by = {c['id']: c for c in r['connectors']}
        self.assertEqual((by['claude_ai_google_drive']['name'], by['claude_ai_google_drive']['logo']), ('Google Drive', 'google_drive'))
        self.assertEqual((by['cua_repl']['name'], by['cua_repl']['logo']), ('Computer use', None))      # a logo with no file is not offered
        self.assertEqual((by['0a1b2c3d-1111-4222-8333-444455556666']['name'], by['0a1b2c3d-1111-4222-8333-444455556666']['logo']), ('connector 0a1b2c3d', None))
        self.assertEqual(by['codex_app']['name'], 'codex app')

    def test_shipped_names_file_names_the_four_mock_connectors(self):
        named = server.connector_names(own=self.root / 'no-own.json')
        self.assertEqual({k: v['name'] for k, v in named.items()}, {DRIVE: 'Google Drive', SLACK: 'Slack', LINEAR: 'Linear', 'github': 'GitHub'})
        for v in named.values():
            self.assertTrue((server.ROOT / 'logos' / (v['logo'] + '.svg')).is_file())
        self.assertTrue(all(k == 'github' or k.startswith(('dddd0000-', '3f1c2a9e-', 'a7e4c0d2-')) for k in named), 'only the mock\'s invented ids ship')

    def test_own_names_file_in_the_data_folder_is_read_over_the_shipped_one(self):
        # a claude.ai connector's id is the person's own: they name it in connectors.json in the app data folder
        data = self.root / 'data'
        (data / 'cache').mkdir(parents=True)
        (data / 'connectors.json').write_text(json.dumps({'_about': 'mine', '0a1b2c3d-1111-4222-8333-444455556666': {'name': 'My Drive', 'logo': 'google_drive'}, 'github': {'name': 'GitHub (work)', 'logo': 'github'}}))
        with patch.dict(os.environ, {'AGENT_IDE_CACHE_DIR': str(data / 'cache')}):
            named = server.connector_names()
        self.assertEqual(named['0a1b2c3d-1111-4222-8333-444455556666']['name'], 'My Drive')
        self.assertEqual(named['github']['name'], 'GitHub (work)', 'the person\'s word wins')
        self.assertEqual(named[SLACK]['name'], 'Slack', 'the shipped names stay')
        with patch.dict(os.environ, {'AGENT_IDE_CACHE_DIR': str(self.root / 'none' / 'cache')}):
            self.assertEqual(set(server.connector_names()), {DRIVE, SLACK, LINEAR, 'github'}, 'no own file: the shipped names alone')


class MockScenarioTests(unittest.TestCase):
    """AGENT_IDE_MOCK=junior: the fixture laid out in the real stores' shapes, read by the ordinary providers."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.now = time.time()
        cls.m = mock.setup('junior', now=cls.now, run=Path(cls.tmp.name).resolve() / 'run')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_project_is_a_git_repository_with_two_commits_and_a_feature_branch(self):
        p = self.m['project']
        git = lambda *a: subprocess.run(['git', '-C', str(p), *a], capture_output=True, text=True, check=True).stdout
        self.assertEqual(p.name, 'receipts-api')
        self.assertEqual(git('rev-parse', '--abbrev-ref', 'HEAD').strip(), 'fix/rcp-142-rounding')
        self.assertEqual(git('rev-list', '--count', 'main').strip(), '2')
        self.assertEqual(git('rev-list', '--count', 'HEAD').strip(), '3')
        self.assertEqual(git('status', '--short').strip(), '?? docs/notes/')
        self.assertIn('ROUND_HALF_EVEN', (p / 'src/receipts/totals.py').read_text())
        self.assertIn('ROUND_HALF_UP', git('show', 'main:src/receipts/totals.py'))

    def test_claude_code_provider_reads_the_session_and_its_subagent(self):
        cp = ClaudeCodeProvider(tokens(), home=self.m['claude_home'], app_support=Path(self.tmp.name) / 'none')
        ss = cp.list_sessions(str(self.m['project']))
        self.assertEqual(sorted(s['title'] for s in ss), ['Find every place money is rounded', 'RCP-142: receipt totals a cent off'])
        main = next(s for s in ss if not s['parent'])
        self.assertAlmostEqual(main['updated'], self.now - mock.LEAD, delta=2)      # the afternoon ends a minute before the start
        self.assertAlmostEqual(main['updated'] - main['started'], 2400, delta=2)      # forty minutes
        h = cp.read_history(main['id'])
        calls = [(e['connector'], e['tool']) for e in h['events'] if e['kind'] == 'mcp']
        self.assertEqual(calls, [(LINEAR, 'get_issue'), (SLACK, 'search_messages'), (SLACK, 'read_thread'), (DRIVE, 'search_files'), (DRIVE, 'read_file_content'),
                                 (SLACK, 'send_message'), (DRIVE, 'read_file_content'), (LINEAR, 'add_comment')])
        self.assertTrue(all(e.get('end') for e in h['events'] if e['kind'] == 'mcp'))
        self.assertIn(('write', 'docs/notes/rcp-142-rounding.md'), [(e['kind'], e['path']) for e in h['events']])
        self.assertEqual([e['kind'] for e in h['events']].count('run'), 2)

    def test_codex_observer_reads_the_rollout_that_opens_the_pull_request(self):
        with patch.object(server, 'CODEX_HOME', self.m['codex_home']):
            rows = server.Observer().rows()
        self.assertEqual(sorted((r['title'], r['cwd']) for r in rows), [('Open the PR for RCP-142', str(self.m['project'])), ('Second opinion on the RCP-142 fix', str(self.m['project']))])
        log = server.Observer().read_log(next(r for r in rows if r['title'] == 'Open the PR for RCP-142'))
        self.assertEqual([(e['connector'], e['tool']) for e in log['events'] if e['kind'] == 'mcp'], [('github', 'get_file'), ('github', 'create_pull_request')])
        self.assertIn(('read', 'src/receipts/totals.py'), [(e['kind'], e['path']) for e in log['events']])

    def test_a_send_to_a_finished_codex_fixture_thread_is_recorded_by_the_mock(self):
        tid = '019a5c3d-f100-7c11-9a21-4b3c2d1e0f60'          # "Second opinion on the RCP-142 fix": its app-server (empty home) never knew it
        bridge = FakeBridge()
        ws = server.Workspace(bridge)
        with patch.object(server, 'CODEX_HOME', self.m['codex_home']), patch.object(server, 'MOCK', self.m):
            r = ws.action('send', {'threadId': tid, 'text': 'Two comments:\n1. keep the docstring'})
            row = server.Observer().row(tid)
            log = server.Observer().read_log(row)
        self.assertEqual((r['turn']['status'], r['mock']), ('completed', True))
        # FakeBridge.call raises on any call: the send never reached the app-server (no thread/resume, no turn/start)
        self.assertEqual([m['text'] for m in log['messages'] if m['role'] == 'user'][-1], 'Two comments:\n1. keep the docstring')
        self.assertFalse(log['turnActive'])
        with patch.object(server, 'CODEX_HOME', self.m['codex_home']), patch.object(server, 'MOCK', self.m), self.assertRaisesRegex(ValueError, 'Write a message first'):
            ws.action('send', {'threadId': tid, 'text': '  '})

    def test_a_stop_of_a_running_codex_fixture_thread_is_recorded_by_the_mock(self):
        tid = '019a5c3d-f100-7c11-9a21-4b3c2d1e0f60'
        bridge = FakeBridge()
        bridge.active = {tid: 'turn-running'}          # as mock_live lays the attention scenario's running turns
        ws = server.Workspace(bridge)
        with patch.object(server, 'CODEX_HOME', self.m['codex_home']), patch.object(server, 'MOCK', self.m):
            self.assertTrue(ws.action('stop', {'threadId': tid})['mock'])          # FakeBridge.call raises: no turn/interrupt reached the app-server
            rollout = server.Observer().row(tid)['rollout_path']
            with self.assertRaisesRegex(ValueError, 'not running'):
                ws.action('stop', {'threadId': tid})
        last = json.loads(Path(rollout).read_text().splitlines()[-1])
        self.assertEqual((last['payload']['type'], last['payload']['turn_id']), ('turn_aborted', 'turn-running'))
        self.assertNotIn(tid, bridge.active)

    def drop_thread(self, tid):
        db = sqlite3.connect(Path(self.m['codex_home']) / 'state_5.sqlite')
        try:
            row = db.execute('SELECT rollout_path FROM threads WHERE id = ?', (tid,)).fetchone()
            db.execute('DELETE FROM threads WHERE id = ?', (tid,))
            db.commit()
        finally:
            db.close()
        if row:
            Path(row[0]).unlink(missing_ok=True)

    def test_a_fork_of_a_codex_fixture_thread_is_made_by_the_mock(self):
        tid = '019a5c3d-f100-7c11-9a21-4b3c2d1e0f60'
        ws = server.Workspace(FakeBridge())
        with patch.object(server, 'CODEX_HOME', self.m['codex_home']), patch.object(server, 'MOCK', self.m):
            r = ws.action('fork', {'threadId': tid})
            new = r['thread']['id']
            self.addCleanup(self.drop_thread, new)          # the scenario is shared by the class: the copy goes again
            row, old = server.Observer().row(new), server.Observer().row(tid)
            log = server.Observer().read_log(row)
        self.assertNotEqual(new, tid)
        self.assertNotEqual(row['rollout_path'], old['rollout_path'])
        self.assertEqual(row['cwd'], old['cwd'])
        self.assertTrue([m for m in log['messages'] if m['role'] == 'user'])          # the copy carries the original's conversation

    def test_mock_switch_points_only_the_paths(self):
        saved = (server.CODEX_HOME, server.CLAUDE_JSON, server.DEFAULT_PROJECT, server.MOCK, server.MOCK_ON)
        self.addCleanup(lambda: setattr(server, 'CODEX_HOME', saved[0]) or setattr(server, 'CLAUDE_JSON', saved[1]) or setattr(server, 'DEFAULT_PROJECT', saved[2])
                        or setattr(server, 'MOCK', saved[3]) or setattr(server, 'MOCK_ON', saved[4]))          # mock_switch sets MOCK and MOCK_ON too: left set, a later class's sends went to this class's deleted mock
        with patch.dict(os.environ, {'AGENT_IDE_MOCK': ''}), patch.object(mock, 'setup', lambda name: self.m):
            self.assertIsNone(server.mock_switch())      # unset: nothing changes
            self.assertEqual(server.CODEX_HOME, saved[0])
            m = server.mock_switch('junior')
            self.assertEqual((server.CODEX_HOME, server.CLAUDE_JSON, server.DEFAULT_PROJECT), (m['codex_home'], m['claude_json'], m['project']))
            self.assertEqual(os.environ['CLAUDE_CONFIG_DIR'], str(m['claude_home']))
            self.assertEqual(os.environ['CODEX_HOME'], str(m['codex_runtime']))      # the app-server's own, empty home
        self.assertEqual(server.claude_config_servers(str(m['project']), m['claude_json']), [])

    def test_unknown_scenario_says_which_exist(self):
        with self.assertRaises(SystemExit) as e:
            mock.setup('senior', run=Path(self.tmp.name) / 'x')
        self.assertIn('junior', str(e.exception))
# ---------- end mock ----------


# ---------- attention: errors, hand-backs and last answers the server reports per session (the page keeps what was read) ----------
P_ID, C1_ID, C2_ID = '019a5c40-d004-7a04-8c04-3e5b7d9f1a04', '019a5c40-d104-7a05-8c05-3e5b7d9f1a05', '019a5c40-d204-7a06-8c06-3e5b7d9f1a06'


def claude_rec(typ, t, **kw):
    r = {'type': typ, 'uuid': 'u-' + t, 'timestamp': '2026-11-05T14:' + t + '.000Z', 'cwd': FIXTURE_CWD, 'sessionId': 'p1'}
    r.update(kw)
    return json.dumps(r) + '\n'


def agent_call(t, tid, name='Agent'):
    return claude_rec('assistant', t, message={'id': 'm-' + t, 'role': 'assistant', 'model': 'claude-fable-5-1', 'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': {'description': 'look', 'prompt': 'look'}}],
                                               'usage': {'input_tokens': 1, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': 1}})


def tool_result(t, tid, text, tur=None):
    return claude_rec('user', t, message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': [{'type': 'text', 'text': text}]}]}, toolUseResult=tur or {})


class ChatTagsTests(unittest.TestCase):
    """chat tags (2026-11-21): the blocks Claude Code writes as the person's message are readable notices, never "you"."""
    NOTE = ('<task-notification>\n<task-id>aad21e</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n<output-file>/private/tmp/x/tasks/aad21e.output</output-file>\n'
            '<status>completed</status>\n<summary>Agent "VM probes" is waiting on its own background work</summary>\n<note>This agent stopped with background work</note>\n'
            '<result>This agent has not reported yet: & <b>not html</b></result>\n<usage><subagent_tokens>236961</subagent_tokens><tool_uses>89</tool_uses><duration_ms>754893</duration_ms></usage>\n</task-notification>')

    def test_blocks_become_notices_and_slash_commands_stay_yours(self):
        from providers.claude_code import chat_tags
        n = chat_tags(self.NOTE)
        self.assertEqual(n['role'], 'note')
        self.assertEqual(n['text'], 'background task · completed: Agent "VM probes" is waiting on its own background work')
        self.assertEqual(n['rows'], [{'h': 'background task · completed'}, {'v': 'Agent "VM probes" is waiting on its own background work'},
                                     {'k': 'note', 'v': 'This agent stopped with background work'}, {'k': 'result', 'v': 'This agent has not reported yet: & <b>not html</b>'},
                                     {'v': '237k tokens · 89 tool uses · 12 min'}])
        self.assertEqual(chat_tags('<command-message>review</command-message>\n<command-name>/review</command-name>\n<command-args>high src</command-args>'), {'role': 'user', 'text': '/review high src'})
        self.assertEqual(chat_tags('<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>'), {'role': 'user', 'text': '/clear'})
        self.assertEqual(chat_tags('<bash-input>ls -la</bash-input>'), {'role': 'user', 'text': '!ls -la'})
        out = chat_tags('<local-command-stdout>\x1b[1mCompacted\x1b[22m</local-command-stdout>')
        self.assertEqual((out['text'], out['rows']), ('command output: Compacted', [{'h': 'command output'}, {'v': 'Compacted'}]))
        self.assertEqual(chat_tags('<bash-stdout> M a\n?? b\n</bash-stdout><bash-stderr></bash-stderr>')['rows'], [{'h': 'shell output'}, {'v': ' M a\n?? b'}])
        self.assertEqual(chat_tags('<some_new-block><first-field>one</first-field><second>two</second></some_new-block>')['rows'],
                         [{'h': 'some new block'}, {'k': 'first field', 'v': 'one'}, {'k': 'second', 'v': 'two'}])
        self.assertIsNone(chat_tags('please fix <div> in the page'))
        self.assertIsNone(chat_tags('<div>x</div> why does this render wrong?'))          # the person's own words that open with a tag

    def test_history_keeps_notices_out_of_your_messages(self):
        user = lambda t, text: claude_rec('user', t, message={'role': 'user', 'content': text})
        said = lambda t, text: claude_rec('assistant', t, message={'id': 'm-' + t, 'role': 'assistant', 'model': 'claude-fable-5-1', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}],
                                                                   'usage': {'input_tokens': 1, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': 1}})
        home = self.root / 'claude'
        folder = home / 'projects' / encode(FIXTURE_CWD)
        folder.mkdir(parents=True)
        (folder / 'p1.jsonl').write_text(user('00:00', 'look at the tests') + said('00:10', 'Started a background check.')
                                         + user('00:20', [{'type': 'text', 'text': 'and the docs'}, {'type': 'text', 'text': '<system-reminder>context</system-reminder>'}])
                                         + user('00:30', '<command-name>/status</command-name><command-args></command-args>')
                                         + user('00:40', ChatTagsTests.NOTE))
        p = ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')
        ms = p.read_history('claude:p1')['messages']
        self.assertEqual([(m['role'], m['text']) for m in ms], [('user', 'look at the tests'), ('assistant', 'Started a background check.'), ('user', 'and the docs'),
                                                                ('user', '/status'), ('note', 'background task · completed: Agent "VM probes" is waiting on its own background work')])
        last = next(m for m in reversed(ms) if m.get('role') in ('assistant', 'user'))          # what attention_of reads as the last word: never the notice
        self.assertEqual(last['text'], '/status')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_observer_keeps_failed_turns_and_subagent_completions(self):
        rec = lambda ts, p: json.dumps({'timestamp': '2026-11-05T14:' + ts + '.000Z', 'type': 'event_msg', 'payload': p}) + '\n'
        path = self.root / 'rollout.jsonl'
        path.write_text(rec('00:00', {'type': 'task_started', 'turn_id': 't1'})
                        + rec('00:05', {'type': 'task_complete', 'turn_id': 't1', 'last_agent_message': None, 'error': {'message': 'model not supported', 'codex_error_info': 'model_not_found'}})
                        + rec('01:00', {'type': 'task_started', 'turn_id': 't2'})
                        + rec('01:10', {'type': 'item_completed', 'item': {'type': 'SubAgentActivity', 'kind': 'started', 'agent_thread_id': 'kid'}})
                        + rec('02:00', {'type': 'item_completed', 'item': {'type': 'SubAgentActivity', 'kind': 'completed', 'agent_thread_id': 'kid'}})
                        + rec('02:30', {'type': 'task_complete', 'turn_id': 't2', 'last_agent_message': 'done'}))
        log = server.Observer().read_log({'id': 'p', 'rollout_path': str(path), 'created_at': 1, 'updated_at': 2, 'cwd': str(self.root)})
        self.assertEqual([(e['text'], e['code'], e['turn']) for e in log['errors']], [('model not supported', 'model_not_found', 't1')])      # a finished turn is no error
        self.assertEqual(list(log['subdone']), ['kid'])
        self.assertEqual(log['subdone']['kid'], server.timestamp('2026-11-05T14:02:00.000Z'))      # the completion, not the start

    def test_stream_errors_that_end_a_turn_are_kept_once_per_turn(self):
        b = SimpleNamespace(failures={})
        server.failed(b, 'error', {'threadId': 'a', 'turnId': 't1', 'willRetry': True, 'error': {'message': 'reconnecting'}})      # retried: not a failure
        server.failed(b, 'error', {'threadId': 'a', 'turnId': 't1', 'willRetry': False, 'error': {'message': 'usage limit', 'codexErrorInfo': 'usageLimitExceeded'}})
        server.failed(b, 'turn/completed', {'threadId': 'a', 'turn': {'id': 't1', 'status': 'failed', 'error': {'message': 'usage limit'}}})      # the same turn again
        server.failed(b, 'turn/completed', {'threadId': 'a', 'turn': {'id': 't2', 'status': 'completed', 'error': None}})
        server.failed(b, 'turn/completed', {'threadId': 'b', 'turn': {'id': 't9', 'error': {'message': 'stream disconnected'}}})
        self.assertEqual([(e['text'], e['code'], e['turn']) for e in b.failures['a']], [('usage limit', 'usageLimitExceeded', 't1')])
        self.assertEqual([e['text'] for e in b.failures['b']], ['stream disconnected'])
        for i in range(12):
            server.failed(b, 'error', {'threadId': 'c', 'turnId': 'x%d' % i, 'willRetry': False, 'error': {'message': str(i)}})
        self.assertEqual([e['text'] for e in b.failures['c']], [str(i) for i in range(2, 12)])      # the last ten

    def test_first_line_drops_markdown_marks(self):
        self.assertEqual(server.first_line('\n## **Done**: `totals.py` fixed\nmore'), 'Done: totals.py fixed')
        self.assertEqual(server.first_line('- item one'), 'item one')
        self.assertEqual(server.first_line('x' * 300, 10), 'x' * 9 + '…')

    def claude(self, parent_lines, sub_meta=None, agent='a1'):
        home = self.root / 'claude'
        folder = home / 'projects' / encode(FIXTURE_CWD)
        (folder / 'p1' / 'subagents').mkdir(parents=True, exist_ok=True)
        (folder / 'p1.jsonl').write_text(''.join(parent_lines))
        (folder / 'p1' / 'subagents' / ('agent-' + agent + '.jsonl')).write_text(claude_rec('user', '00:01', isSidechain=True, agentId=agent, message={'role': 'user', 'content': 'look'}))
        (folder / 'p1' / 'subagents' / ('agent-' + agent + '.meta.json')).write_text(json.dumps(sub_meta or {'description': 'look', 'toolUseId': 'tu1'}))
        return ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')

    def test_claude_code_hand_back_from_the_agent_tool_result(self):
        p = self.claude([agent_call('00:00', 'tu1'), tool_result('03:00', 'tu1', 'Found two places.')])
        hb = p.handback('claude:p1/a1')
        self.assertEqual((hb['t'], hb['text']), (server.timestamp('2026-11-05T14:03:00.000Z'), 'Found two places.'))
        self.assertIsNone(p.handback('claude:p1'))      # a main session hands nothing back

    def test_claude_code_background_agent_hands_back_by_notification_not_by_its_launch(self):
        launched = {'status': 'async_launched', 'agentId': 'a1'}
        note = '<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>tu1</tool-use-id>\n<status>completed</status>\n<summary>done</summary>\n</task-notification>'
        lines = [agent_call('00:00', 'tu1'), tool_result('00:01', 'tu1', 'Async agent launched', launched)]
        self.assertIsNone(self.claude(lines).handback('claude:p1/a1'))      # the launch's tool result is not the result
        lines.append(claude_rec('attachment', '04:00', attachment={'type': 'queued_command', 'commandMode': 'task-notification', 'prompt': note}))
        hb = self.claude(lines).handback('claude:p1/a1')
        self.assertEqual(hb['t'], server.timestamp('2026-11-05T14:04:00.000Z'))
        running = note.replace('completed', 'running')
        lines2 = [agent_call('00:00', 'tu1'), tool_result('00:01', 'tu1', 'Async agent launched', launched),
                  claude_rec('attachment', '04:00', attachment={'type': 'queued_command', 'commandMode': 'task-notification', 'prompt': running})]
        self.assertIsNone(self.claude(lines2).handback('claude:p1/a1'))      # a notification of a still-running task hands nothing back

    def test_claude_code_report_arriving_with_origin_handback(self):
        lines = [agent_call('00:00', 'tu1'), tool_result('00:01', 'tu1', 'Async agent launched', {'status': 'async_launched'}),
                 claude_rec('user', '05:00', origin={'handback': True, 'from': 'a1', 'handbackReportText': 'Report: three places.'}, message={'role': 'user', 'content': 'Report: three places.'})]
        hb = self.claude(lines).handback('claude:p1/a1')
        self.assertEqual((hb['t'], hb['text']), (server.timestamp('2026-11-05T14:05:00.000Z'), 'Report: three places.'))

    def ask_call(self, t, tid, name='AskUserQuestion', inp=None):
        inp = inp or {'questions': [{'header': 'Release', 'question': 'Ship 1.4.2 now?', 'multiSelect': False, 'options': [{'label': 'Yes', 'description': 'today'}, {'label': 'No', 'description': 'later'}]}]}
        return claude_rec('assistant', t, message={'id': 'm-' + t, 'role': 'assistant', 'model': 'claude-fable-5-1', 'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}],
                                                   'usage': {'input_tokens': 1, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'output_tokens': 1}})

    def test_claude_code_question_waits_on_you_until_its_tool_result(self):
        said = claude_rec('assistant', '00:30', message={'id': 'm-said', 'role': 'assistant', 'model': 'claude-fable-5-1', 'content': [{'type': 'text', 'text': 'One decision is yours.'}], 'usage': {'input_tokens': 1, 'output_tokens': 1}})
        lines = [claude_rec('user', '00:00', message={'role': 'user', 'content': 'plan it'}), said, self.ask_call('01:00', 'tq1')]
        p = self.claude(lines)
        reqs = p.ask_requests('claude:p1')
        self.assertEqual([(r['id'], r['method'], r['params']['threadId'], r['params']['watchOnly']) for r in reqs], [('claude-ask:tq1', 'item/tool/requestUserInput', 'claude:p1', True)])
        q = reqs[0]['params']['questions'][0]
        self.assertEqual((q['question'], [o['label'] for o in q['options']]), ('Ship 1.4.2 now?', ['Yes', 'No']))
        self.assertEqual(reqs[0]['params']['startedAtMs'], int(server.timestamp('2026-11-05T14:01:00.000Z') * 1000))
        self.assertEqual(p.ask_requests('claude:p1/a1'), [])          # a subagent asks nothing
        p = self.claude(lines + [tool_result('02:00', 'tq1', 'The user answered: "Ship 1.4.2 now?"="Yes".')])
        self.assertEqual(p.ask_requests('claude:p1'), [])          # answered: gone

    def test_claude_code_plan_approval_is_a_question_and_a_typed_message_ends_any(self):
        lines = [self.ask_call('01:00', 'tp1', 'ExitPlanMode', {'plan': '# Release plan\n\n1. Bump the version'})]
        q = self.claude(lines).ask_requests('claude:p1')[0]['params']['questions'][0]
        self.assertEqual(q['question'], 'Approve its plan: Release plan?')
        lines.append(claude_rec('user', '02:00', message={'role': 'user', 'content': 'never mind, stop'}))
        self.assertEqual(self.claude(lines).ask_requests('claude:p1'), [])

    def test_claude_code_question_needs_a_live_process_where_the_store_registers_them(self):
        p = self.claude([self.ask_call('01:00', 'tq1')])
        (p.home / 'sessions').mkdir()
        p._live = (0, {})
        self.assertEqual(p.ask_requests('claude:p1'), [])          # registrations kept, none for p1: its process exited, nobody waits
        p._live = (time.time() + 60, {'p1': {'status': 'idle', 'pid': 1}})
        self.assertEqual(len(p.ask_requests('claude:p1')), 1)

    def test_claude_code_api_error_is_kept_as_an_error(self):
        err = claude_rec('assistant', '06:00', isApiErrorMessage=True, error='server_error',
                         message={'id': 'e1', 'role': 'assistant', 'model': '<synthetic>', 'content': [{'type': 'text', 'text': "API Error: Can't reach the API server"}], 'usage': {'input_tokens': 0, 'output_tokens': 0}})
        p = self.claude([claude_rec('user', '05:59', message={'role': 'user', 'content': 'summarise'}), err])
        h = p.read_history('claude:p1')
        self.assertEqual([(e['text'], e['code']) for e in h['errors']], [("API Error: Can't reach the API server", 'server_error')])
        self.assertNotIn("API Error: Can't reach the API server", [m['text'] for m in h['messages']])      # an error, not the agent's answer

    def test_claude_code_api_errors_it_retried_itself_are_no_error(self):
        retry = lambda t, n: claude_rec('system', t, subtype='api_error', level='error', error={'status': 529}, retryAttempt=n, maxRetries=10)
        answer = claude_rec('assistant', '06:30', message={'id': 'm1', 'role': 'assistant', 'model': 'claude-fable-5-1', 'content': [{'type': 'text', 'text': 'Done.'}], 'stop_reason': 'end_turn',
                                                           'usage': {'input_tokens': 1, 'output_tokens': 1}})
        h = self.claude([claude_rec('user', '05:59', message={'role': 'user', 'content': 'summarise'}), retry('06:00', 1), retry('06:05', 2), answer]).read_history('claude:p1')
        self.assertEqual(h['errors'], [])


class AttentionMockTests(unittest.TestCase):
    """AGENT_IDE_MOCK=attention: the canvas carries each session's last answer, failed turns and hand-back; the mock's
    requests wait on you and are answered by the mock itself."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.now = time.time()
        cls.m = mock.setup('attention', now=cls.now, run=Path(cls.tmp.name).resolve() / 'run')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def canvas(self, bridge):
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        ws = http.workspace = server.Workspace(bridge)
        ws._providers = [CodexProvider(ws, self.m['codex_home']), ClaudeCodeProvider(tokens(), home=self.m['claude_home'], app_support=Path(self.tmp.name) / 'none')]
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        with patch.object(server, 'CODEX_HOME', self.m['codex_home']):
            D = json.load(urllib.request.urlopen('http://127.0.0.1:%d/api/canvas?cwd=%s' % (http.server_port, urllib.parse.quote(str(self.m['project']))), timeout=10))
        return {s['title']: s for s in D['sessions']}, ws

    def test_canvas_carries_errors_hand_backs_and_last_answers(self):
        bridge = FakeBridge()
        bridge.failures = {P_ID: [{'t': self.now - 30, 'text': 'stream disconnected before completion', 'code': None, 'turn': 'live-turn'}]}      # one the stream reported
        by, _ = self.canvas(bridge)
        review = by['Review the receipts module']
        self.assertEqual([e['text'] for e in review['errors']], ["The 'haiku' model is not supported when using Codex with a ChatGPT account.",
                                                                 "You've hit your usage limit. Try again at 3:05 PM.", 'stream disconnected before completion'])
        self.assertTrue(all(review['errors'][i]['at'] <= review['errors'][i + 1]['at'] for i in range(2)))
        self.assertIsNone(review['handback'])
        for kid in ('Check totals.py rounding', 'Check currency.py conversions'):
            self.assertEqual(by[kid]['parent'], P_ID)
            self.assertTrue(by[kid]['handback'] and by[kid]['handback']['text'])          # the parent's SubAgentActivity completed; the child's last answer
        self.assertLess(by['Check totals.py rounding']['handback']['at'], by['Check currency.py conversions']['handback']['at'])
        self.assertEqual([e['later'] for e in review['errors']], [True, True, False])      # turns 2 and 3 came after the first two; nothing after the stream's
        claude_err = by['Summarise the support thread']
        self.assertEqual([(e['text'], e['later']) for e in claude_err['errors']], [("API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)", False)])
        resumed, left = by['Collect the support replies'], by['Check the ticket history']          # its subagents: one resumed after its failure, one left
        self.assertEqual((resumed['parent'], [e['later'] for e in resumed['errors']]), (claude_err['id'], [True]))          # its two retries are no error; the given-up one is followed by work
        self.assertEqual((left['parent'], [e['later'] for e in left['errors']]), (claude_err['id'], [False]))
        self.assertEqual([e['later'] for e in by['Draft the reply to support']['errors']], [False])
        sub = by['Find every place money is rounded']          # the junior afternoon's Claude Code subagent: its Agent call's result
        self.assertTrue(sub['handback']['at'] > 0 and sub['handback']['text'])
        pr = by['Open the PR for RCP-142']
        self.assertTrue(pr['say']['text'].startswith('Committed the fix'))
        self.assertEqual(pr['errors'], [])
        self.assertIsNone(by['Run the API tests in a clean venv']['say'])      # its last message is a tool call, not an answer

    def test_hand_back_carries_the_report_the_parent_received_whole(self):
        by, ws = self.canvas(FakeBridge())
        sub = by['Find every place money is rounded']          # a Claude Code subagent: its parent's record holds the report
        got = ws._providers[1].handback(sub['id'])['text']
        self.assertGreater(len(got), 200)          # longer than the floor's first line
        self.assertEqual(sub['handback']['full'], got)          # whole, as the parent received it
        self.assertEqual(sub['handback']['text'], server.first_line(got))          # the floor's short form stays as it was

    def test_child_read_before_its_parent_still_gets_its_hand_back(self):
        _, ws = self.canvas(FakeBridge())
        ws.observer.cache.clear()
        with patch.object(server, 'CODEX_HOME', self.m['codex_home']):
            kid = next(s for s in ws.all_sessions() if s['id'] == C1_ID)
            at = ws.attention_of(kid, {'messages': []})
        self.assertTrue(at['handback'] and at['handback']['at'])
        self.assertEqual(at['handback']['text'], 'finished')      # no answer of its own in what was passed

    def test_mock_requests_wait_and_are_answered_by_the_mock(self):
        live = self.m['live']
        self.assertEqual(sorted(q['method'] for q in live['requests']), ['item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/tool/requestUserInput'])
        self.assertNotIn('{{PROJECT}}', json.dumps(live))
        code = "import sys,json\nfor line in sys.stdin:\n m=json.loads(line)\n if m.get('method')=='initialize': print(json.dumps({'id':m['id'],'result':{}}),flush=True)\n elif 'id' in m and 'method' not in m: print(json.dumps({'method':'unexpected/reply'}),flush=True)\n"
        b = server.Bridge([sys.executable, '-u', '-c', code])          # an app-server that answers only initialize, and says so if a reply reaches it
        try:
            b.mock_live(live)
            self.assertEqual(set(b.requests), {'9101', '9102', '9103'})
            self.assertEqual(b.active[P_ID], live['active'][P_ID])
            seq = b.event_seq
            b.reply('9101', {'decision': 'accept'})
            self.assertNotIn('9101', b.requests)
            self.assertEqual(b.events[-1], (seq + 1, {'method': 'serverRequest/resolved', 'params': {'threadId': live['requests'][0]['params']['threadId'], 'requestId': 9101}}))
            with self.assertRaises(ValueError):
                b.reply('9101', {'decision': 'accept'})      # once
            time.sleep(0.3)
            self.assertNotIn('unexpected/reply', [m.get('method') for _, m in b.events])      # nothing went to the app-server
        finally:
            b.close()
            b.process.wait(timeout=3)
            for f in (b.process.stdin, b.process.stdout, b.process.stderr):
                f.close()

    def test_claude_code_question_waits_until_answered_in_claude_code(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        m = mock.setup('attention', now=self.now, run=Path(tmp.name).resolve() / 'run')          # its own copy: the mock's events below change it
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.daemon_threads = True
        ws = http.workspace = server.Workspace(FakeBridge())
        ws._providers = [CodexProvider(ws, m['codex_home']), ClaudeCodeProvider(tokens(), home=m['claude_home'], app_support=Path(tmp.name) / 'none')]
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        url = 'http://127.0.0.1:%d/api/canvas?cwd=%s' % (http.server_port, urllib.parse.quote(str(m['project'])))
        def canvas():
            with patch.object(server, 'CODEX_HOME', m['codex_home']):
                return {s['title']: s for s in json.load(urllib.request.urlopen(url, timeout=10))['sessions']}
        def asks():
            with patch.object(server, 'CODEX_HOME', m['codex_home']):
                return [r for r in ws.snapshot()['requests'] if str(r['id']).startswith('claude-ask:')]
        plan = canvas()['Plan the RCP-142 release']
        self.assertEqual((plan['state'], plan['end'], plan['say']), ('running', None, None))          # stopped on a question: a turn not ended, no unread answer
        reqs = asks()
        self.assertEqual([r['params']['questions'][0]['question'] for r in reqs], ['Ship the rounding fix as 1.4.2 now, or hold it for 1.5.0 with the currency work?'])
        with self.assertRaises(ValueError):
            ws.action('reply', {'id': reqs[0]['id'], 'result': {'answers': {}}})          # watch only: never answered from here
        kid = canvas()['Check the release notes']
        self.assertIsNone(kid['handback'])
        mock.act(m, 'handback')
        self.assertTrue(canvas()['Check the release notes']['handback']['at'] > self.now - 5)          # the notification's time: now
        mock.act(m, 'answer')
        self.assertEqual(asks(), [])
        plan = canvas()['Plan the RCP-142 release']
        self.assertEqual(plan['state'], 'done')
        self.assertTrue(plan['say']['text'].startswith('Release plan: ship the rounding fix alone'))
        self.assertEqual(mock.act(m, 'answer'), {'done': 'answer', 'again': True})          # once

    def test_snapshot_names_the_mock(self):
        with patch.object(server, 'MOCK_ON', 'attention'):
            ws = server.Workspace(FakeBridge())
            ws._providers = [CodexProvider(ws, self.m['codex_home'])]
            with patch.object(server, 'CODEX_HOME', self.m['codex_home']):
                self.assertEqual(ws.snapshot()['mock'], 'attention')
# ---------- end attention ----------


# ---------- claude drive: the Claude Code bridge with its worker faked (no Claude Code process, no model call) ----------
import claude_bridge
from providers.claude_code import encode as claude_code_encode

FAKE_WORKER = r"""
import json, sys
def out(m):
    sys.stdout.write(json.dumps(m) + '\n'); sys.stdout.flush()
log = open(sys.argv[1], 'a')
out({'method': 'ready', 'params': {}})
for line in sys.stdin:
    m = json.loads(line); p = m.get('params') or {}; k = p.get('key')
    log.write(json.dumps(m) + '\n'); log.flush()
    meth = m['method']
    if meth == 'models':
        out({'id': m['id'], 'result': {'models': [{'value': 'default', 'resolvedModel': 'claude-opus-5-5', 'displayName': 'Default', 'supportedEffortLevels': ['low', 'high']},
                                                  {'value': 'haiku', 'resolvedModel': 'claude-haiku-5-5', 'displayName': 'Haiku 5.5', 'description': 'Fastest'}]}})
    elif meth == 'open':
        out({'id': m['id'], 'result': {}})
        out({'method': 'message', 'params': {'key': k, 'message': {'_type': 'SystemMessage', 'subtype': 'init', 'data': {'model': 'claude-haiku-5-5', 'permissionMode': p.get('permissionMode')}}}})
    elif meth == 'query':
        out({'id': m['id'], 'result': {}})
        t = p['text']
        if 'write' in t:
            out({'method': 'permission', 'params': {'key': k, 'requestId': 'r-write', 'toolName': 'Write', 'toolUseId': 'tu1', 'input': {'file_path': '/proj/notes.txt', 'content': 'a\nb\n'}}})
        elif 'ask' in t:
            out({'method': 'permission', 'params': {'key': k, 'requestId': 'r-ask', 'toolName': 'AskUserQuestion', 'input': {'questions': [{'question': 'Which name?', 'header': 'Name', 'options': [{'label': 'A'}, {'label': 'B'}], 'multiSelect': False}]}}})
        else:
            out({'method': 'message', 'params': {'key': k, 'message': {'_type': 'ResultMessage', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 0.001}}})
    elif meth == 'permission':
        out({'id': m['id'], 'result': {}})
        out({'method': 'message', 'params': {'key': k, 'message': {'_type': 'ResultMessage', 'subtype': 'success', 'is_error': False, 'total_cost_usd': 0.002}}})
    elif meth == 'interrupt':
        out({'id': m['id'], 'result': {}})
        out({'method': 'message', 'params': {'key': k, 'message': {'_type': 'ResultMessage', 'subtype': 'error_during_execution', 'is_error': True, 'result': 'interrupted'}}})
    elif meth == 'close':
        out({'id': m['id'], 'result': {}})
        out({'method': 'closed', 'params': {'key': k}})
    else:
        out({'id': m['id'], 'result': {}})
"""


class FakeLive:
    """providers/claude_code.py's live(): the sessions other processes hold."""
    def __init__(self):
        self.regs = {}

    def live(self, fresh=False):
        return dict(self.regs)


class ClaudeDriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'worker.py').write_text(FAKE_WORKER)
        self.calls = self.root / 'calls.jsonl'
        self.events, self.opened = [], []
        self.live = FakeLive()
        self.cb = claude_bridge.ClaudeBridge(publish=self.events.append, provider=self.live, command=[sys.executable, '-I', str(self.root / 'worker.py'), str(self.calls)],
                                             cli='/fake/claude', environ={'PATH': os.environ.get('PATH', ''), 'ANTHROPIC_API_KEY': 'not-a-real-key'}, opener=self.opened.append)
        self.addCleanup(self.cb.close)
        self.project = self.root / 'proj'
        self.project.mkdir()

    def sent(self, method=None):
        rows = [json.loads(l) for l in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return [r for r in rows if method is None or r['method'] == method]

    def wait(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def test_requests_in_the_pages_shapes(self):
        w = claude_bridge.as_request({'key': 'k', 'requestId': 'r1', 'toolName': 'Write', 'toolUseId': 't', 'input': {'file_path': '/p/a/b.txt', 'content': 'x\ny'}}, '/p')
        self.assertEqual((w['method'], w['params']['threadId'], w['params']['claude']['summary']), ('item/fileChange/requestApproval', 'claude:k', 'write a/b.txt (2 lines)'))
        b = claude_bridge.as_request({'key': 'k', 'requestId': 'r2', 'toolName': 'Bash', 'input': {'command': 'ls -la', 'description': 'List'}}, '/p')
        self.assertEqual((b['method'], b['params']['command'], b['params']['reason']), ('item/commandExecution/requestApproval', 'ls -la', 'List'))
        q = claude_bridge.as_request({'key': 'k', 'requestId': 'r3', 'toolName': 'AskUserQuestion', 'input': {'questions': [{'question': 'Pick?', 'options': [{'label': 'A', 'description': 'first'}]}]}}, '/p')
        self.assertEqual((q['method'], q['params']['questions'][0]['id'], q['params']['questions'][0]['options'][0]['label']), ('item/tool/requestUserInput', 'q0', 'A'))
        o = claude_bridge.as_request({'key': 'k', 'requestId': 'r4', 'toolName': 'WebFetch', 'input': {'url': 'https://example.com'}}, '/p')
        self.assertEqual((o['method'], o['params']['command']), ('item/claude/requestApproval', 'WebFetch https://example.com'))
        self.assertRegex(o['method'], 'requestApproval$')          # the page's generic approve card
        e = claude_bridge.as_request({'key': 'k', 'requestId': 'r5', 'toolName': 'Edit', 'input': {'file_path': '/p/x.py', 'old_string': 'a', 'new_string': 'b\nc'}}, '/p')
        self.assertEqual(e['params']['claude']['summary'], 'edit x.py (+2 −1)')

    def test_answers_back_to_claude_code(self):
        q = claude_bridge.as_request({'key': 'k', 'requestId': 'r3', 'toolName': 'AskUserQuestion', 'input': {'questions': [{'question': 'Pick?', 'options': []}]}}, '/p')
        q['params']['original'] = {'questions': [{'question': 'Pick?', 'options': []}]}
        a = claude_bridge.answer_of(q, {'answers': {'q0': {'answers': ['B']}}})
        self.assertEqual(a, {'allow': True, 'updatedInput': {'questions': [{'question': 'Pick?', 'options': []}], 'answers': {'Pick?': 'B'}}})
        with self.assertRaises(ValueError):
            claude_bridge.answer_of(q, {'answers': {}})
        with self.assertRaises(ValueError):
            claude_bridge.answer_of(q, {'answers': {'zz': {'answers': ['B']}}})
        w = claude_bridge.as_request({'key': 'k', 'requestId': 'r1', 'toolName': 'Write', 'input': {}}, '/p')
        self.assertEqual(claude_bridge.answer_of(w, {'decision': 'accept'}), {'allow': True})
        self.assertEqual(claude_bridge.answer_of(w, {'decision': 'decline'})['allow'], False)
        self.assertTrue(claude_bridge.answer_of(w, {'decision': 'cancel'})['interrupt'])
        with self.assertRaises(ValueError):
            claude_bridge.answer_of(w, {'decision': 'acceptForSession'})          # piece B: Claude Code suggested nothing here, so no "don't ask again"

    def test_dont_ask_again_and_a_reason_back_to_claude_code(self):          # piece B
        sug = [{'type': 'addRules', 'rules': [{'toolName': 'Bash', 'ruleContent': 'npm test:*'}], 'behavior': 'allow', 'destination': 'localSettings'}]
        b = claude_bridge.as_request({'key': 'k', 'requestId': 'r2', 'toolName': 'Bash', 'input': {'command': 'npm test', 'description': 'Run'}, 'suggestions': sug}, '/p')
        self.assertEqual(b['params']['availableDecisions'], ['accept', 'acceptForSession', 'decline', 'cancel'])
        self.assertEqual(claude_bridge.answer_of(b, {'decision': 'acceptForSession'}), {'allow': True, 'updatedPermissions': sug})
        no = claude_bridge.answer_of(b, {'decision': 'decline', 'message': '  use the   fast suite '})
        self.assertEqual((no['allow'], no['interrupt']), (False, False))
        self.assertTrue(no['message'].endswith('They said: use the fast suite'))
        e = claude_bridge.as_request({'key': 'k', 'requestId': 'r5', 'toolName': 'Edit', 'input': {'file_path': '/p/x.py', 'old_string': 'a', 'new_string': 'b\nc'}}, '/p')
        self.assertEqual(e['params']['changes'], [{'path': 'x.py', 'kind': {'type': 'update'}, 'diff': '-a\n+b\n+c', 'more': 0}])
        self.assertNotIn('acceptForSession', e['params']['availableDecisions'])
        big = claude_bridge.as_request({'key': 'k', 'requestId': 'r6', 'toolName': 'Write', 'input': {'file_path': '/p/nope/new.txt', 'content': '\n'.join(map(str, range(claude_bridge.CHANGE_LINES_MAX + 7)))}}, '/p')
        ch = big['params']['changes'][0]
        self.assertEqual((ch['kind'], ch['more'], len(ch['diff'].split('\n'))), ({'type': 'add'}, 7, claude_bridge.CHANGE_LINES_MAX))

    def test_a_queued_message_runs_its_own_turn(self):
        # a message sent mid-turn is queued; when the turn's result arrives the queued message starts the next turn, and the
        # session must read as running again until that turn's own result (the <synthetic> line after a stop does not count)
        sid = self.cb.start(str(self.project))['thread']['id']
        key = sid[len('claude:'):]
        h = self.cb.held[key]
        msg = lambda m: self.cb.take('message', {'key': key, 'message': m})
        h['active'] = True
        self.assertEqual(self.cb.send(sid, 'second')['how'], 'queued')
        self.assertEqual(h['queued'], 1)
        self.assertTrue(self.wait(lambda: not h['active']))          # the fake worker answers each query with a result
        h['queued'] = 1          # as if the first turn's result came before the fake's
        msg({'_type': 'AssistantMessage', 'model': 'claude-haiku-5-5', 'content': []})
        self.assertEqual((h['active'], h['queued']), (True, 0))
        self.assertTrue(any(e.get('method') == 'claude/turn' and e['params'].get('started') for e in self.events))
        msg({'_type': 'ResultMessage', 'subtype': 'success', 'is_error': False})
        self.assertFalse(h['active'])
        msg({'_type': 'AssistantMessage', 'model': 'claude-haiku-5-5', 'content': []})          # nothing queued: stays idle
        self.assertFalse(h['active'])
        h['queued'] = 1
        msg({'_type': 'AssistantMessage', 'model': '<synthetic>', 'content': []})
        self.assertFalse(h['active'])
        self.cb.send(sid, 'idle send')
        self.assertEqual(h['queued'], 0)

    def test_api_key_route_is_off_unless_switched_on(self):
        env = claude_bridge.worker_env({'PATH': '/bin', 'ANTHROPIC_API_KEY': 'x', 'CLAUDE_CODE_OAUTH_TOKEN': 'y', 'CLAUDECODE': '1'})
        self.assertEqual(env, {'PATH': '/bin'})
        env = claude_bridge.worker_env({'PATH': '/bin', 'ANTHROPIC_API_KEY': 'x', 'AGENT_IDE_CLAUDE_API_KEY': 'pass'})
        self.assertEqual(env['ANTHROPIC_API_KEY'], 'x')
        with patch('importlib.util.find_spec', return_value=None):          # the tests' venv path does not exist, nor the SDK here
            self.assertFalse(claude_bridge.ClaudeBridge(command=None).available()['ready'])
            self.assertIn('claude_bridge_setup.sh', claude_bridge.ClaudeBridge(command=None).available()['reason'])

    def test_worker_python_is_the_venv_else_this_python_with_the_sdk(self):
        # desktop app (2026-11-21): the server runs under `uv run` with the SDK installed beside it, so no separate venv is needed
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(claude_bridge.worker_python(venv=Path(d) / 'venv', here=False))
            self.assertEqual(claude_bridge.worker_python(venv=Path(d) / 'venv', here=True), sys.executable)
            (Path(d) / 'venv' / 'bin').mkdir(parents=True)
            (Path(d) / 'venv' / 'bin' / 'python').write_text('')
            self.assertEqual(claude_bridge.worker_python(venv=Path(d) / 'venv', here=True), str(Path(d) / 'venv' / 'bin' / 'python'))

    def test_start_send_approve_and_turn_end(self):
        r = self.cb.start(str(self.project), model='haiku', mode='default')
        sid = r['thread']['id']
        key = sid[len('claude:'):]
        opened = self.sent('open')[0]['params']
        self.assertEqual((opened['sessionId'], opened['cwd'], opened['model'], opened['cli'], opened['permissionMode']), (key, str(self.project), 'haiku', '/fake/claude', 'default'))
        self.assertNotIn('resume', opened)
        self.assertEqual(self.cb.send(sid, 'please write it')['how'], 'sent')
        self.assertTrue(self.cb.is_active(sid))
        self.assertTrue(self.wait(lambda: 'r-write' in self.cb.requests))
        req = self.cb.requests['r-write']
        self.assertEqual((req['method'], req['params']['threadId'], req['params']['claude']['summary']), ('item/fileChange/requestApproval', sid, 'write /proj/notes.txt (2 lines)'))
        self.assertIn(req, self.events)          # published with its id: the page refreshes and shows the card
        self.cb.reply('r-write', {'decision': 'accept'})
        self.assertEqual(self.sent('permission')[0]['params'], {'key': key, 'requestId': 'r-write', 'allow': True})
        self.assertNotIn('r-write', self.cb.requests)
        self.assertTrue(any(e.get('method') == 'serverRequest/resolved' and e['params']['requestId'] == 'r-write' for e in self.events))
        self.assertTrue(self.wait(lambda: not self.cb.is_active(sid)))
        self.assertTrue(any(e.get('method') == 'claude/turn' for e in self.events))
        self.assertEqual(self.cb.settings(sid)['permissionMode'], 'default')
        with self.assertRaises(ValueError):
            self.cb.reply('r-write', {'decision': 'accept'})          # already resolved

    def test_question_answer_goes_back_in_the_tools_input(self):
        sid = self.cb.start(str(self.project))['thread']['id']
        self.cb.send(sid, 'ask me')
        self.assertTrue(self.wait(lambda: 'r-ask' in self.cb.requests))
        self.cb.reply('r-ask', {'answers': {'q0': {'answers': ['B']}}})
        p = self.sent('permission')[0]['params']
        self.assertEqual(p['updatedInput']['answers'], {'Which name?': 'B'})
        self.assertEqual(p['updatedInput']['questions'][0]['question'], 'Which name?')

    def test_send_resumes_an_idle_session_and_forks_a_held_one(self):
        key = '11111111-2222-3333-4444-555555555555'
        r = self.cb.send('claude:' + key, 'hello', cwd=str(self.project))
        self.assertEqual((r['how'], r['threadId']), ('resumed', 'claude:' + key))
        o = self.sent('open')[-1]['params']
        self.assertEqual((o['resume'], o['fork'], 'sessionId' in o), (key, False, False))
        other = '99999999-2222-3333-4444-555555555555'
        self.live.regs[other] = {'status': 'idle', 'pid': 4242, 'entrypoint': 'claude-desktop'}          # the desktop app holds it, even idle
        r = self.cb.send('claude:' + other, 'hello', cwd=str(self.project), fork=True, session_model='claude-haiku-5-5')
        self.assertEqual(r['how'], 'forked')
        self.assertEqual(self.sent('open')[-1]['params']['model'], 'claude-haiku-5-5')          # the copy runs on the session's own model
        self.assertNotEqual(r['threadId'], 'claude:' + other)
        o = self.sent('open')[-1]['params']
        self.assertEqual((o['resume'], o['fork'], o['sessionId']), (other, True, r['threadId'][len('claude:'):]))
        self.assertTrue(self.wait(lambda: not self.cb.is_active(r['threadId'])))
        self.assertEqual(self.cb.send(r['threadId'], 'again')['how'], 'sent')          # its own client is still open: no resume
        with self.assertRaises(ValueError):
            self.cb.send('claude:' + key + '/agent1', 'hi', cwd=str(self.project))          # subagents are not messaged

    def test_a_held_session_asks_before_forking(self):
        self.cb.models(str(self.project))
        other = '99999999-2222-3333-4444-555555555555'
        self.live.regs[other] = {'status': 'idle', 'pid': 4242, 'entrypoint': 'claude-desktop'}
        before = len(self.sent('open'))
        r = self.cb.send('claude:' + other, 'test test', cwd=str(self.project), session_model='claude-haiku-5-5')
        self.assertEqual(r, {'threadId': 'claude:' + other, 'how': 'choice', 'heldBy': 'app', 'model': 'Haiku 5.5', 'desktopUrl': 'claude://resume?session=' + other})
        self.assertEqual((len(self.sent('open')), self.sent('query'), self.cb.held, self.opened), (before, [], {}, []))          # nothing started, sent or opened
        self.live.regs[other]['entrypoint'] = 'sdk-cli'          # another program holds it: no app link offered
        r = self.cb.send('claude:' + other, 'test test', cwd=str(self.project), model='haiku', fork=False)
        self.assertEqual((r['how'], r['heldBy'], r['model'], r['desktopUrl']), ('choice', 'process', 'Haiku 5.5', None))
        self.assertEqual(self.cb.holder({'entrypoint': 'cli'}), 'terminal')
        self.assertEqual((claude_bridge.ClaudeBridge(command=None).model_name('claude-opus-5-5'), self.cb.model_name('sonnet'), self.cb.model_name(None)), ('Opus 5.5', 'sonnet', None))          # before the model list is read
        with self.assertRaises(ValueError):
            self.cb.to_desktop('claude:' + other)          # the app refuses to import a session another process runs
        self.live.regs[other]['entrypoint'] = 'claude-desktop'
        self.assertEqual(self.cb.to_desktop('claude:' + other)['url'], 'claude://resume?session=' + other)          # held by the app: the link switches to it
        self.assertEqual(self.opened, ['claude://resume?session=' + other])
        self.assertEqual(len(self.sent('open')), before)
        r = self.cb.send('claude:' + other, 'test test', cwd=str(self.project), fork=True)
        self.assertEqual(r['how'], 'forked')
        self.assertEqual(r['from'], 'claude:' + other)
        self.assertEqual(self.sent('query')[-1]['params']['text'], 'test test')

    def test_stop_and_desktop_handover(self):
        sid = self.cb.start(str(self.project))['thread']['id']
        with self.assertRaises(ValueError):
            self.cb.stop(sid)          # not running
        self.cb.send(sid, 'ask me')
        self.assertTrue(self.wait(lambda: 'r-ask' in self.cb.requests))
        with self.assertRaises(ValueError):
            self.cb.to_desktop(sid)          # mid-turn
        self.cb.stop(sid)
        self.assertEqual(len(self.sent('interrupt')), 1)
        self.assertTrue(self.wait(lambda: not self.cb.is_active(sid)))
        r = self.cb.to_desktop(sid)
        self.assertEqual(self.opened, ['claude://resume?session=' + sid[len('claude:'):]])
        self.assertEqual(r['url'], self.opened[0])
        self.assertEqual(len(self.sent('close')), 1)
        self.assertTrue(self.wait(lambda: not self.cb.requests))          # the closed client's open request is withdrawn

    def test_plan_toggle_starts_in_plan_mode_and_approval_moves_on(self):
        # plan toggle: plan on starts the session in permission mode "plan"; approving its plan (ExitPlanMode) from the page
        # leaves plan mode for the approval mode chosen at start
        sid = self.cb.start(str(self.project), mode='acceptEdits', plan=True)['thread']['id']
        key = sid[len('claude:'):]
        self.assertEqual(self.sent('open')[-1]['params']['permissionMode'], 'plan')
        self.assertEqual((self.cb.settings(sid)['plan'], self.cb.settings(sid)['permissionMode']), (True, 'plan'))
        self.cb.take('permission', {'key': key, 'requestId': 'r-plan', 'toolName': 'ExitPlanMode', 'toolUseId': 'tu-plan', 'input': {'plan': '# Plan\nwrite notes'}})
        req = self.cb.requests['r-plan']
        self.assertEqual((req['method'], req['params']['claude']['summary']), ('item/claude/requestApproval', 'leave plan mode: # Plan'))          # the page's approve card
        self.cb.reply('r-plan', {'decision': 'accept'})
        self.assertEqual(self.sent('permission')[-1]['params'], {'key': key, 'requestId': 'r-plan', 'allow': True, 'updatedPermissions': [{'type': 'setMode', 'mode': 'acceptEdits', 'destination': 'session'}]})
        self.assertEqual(self.cb.settings(sid)['permissionMode'], 'acceptEdits')
        off = self.cb.start(str(self.project), mode='default')['thread']['id']          # plan off: as before
        self.assertEqual((self.sent('open')[-1]['params']['permissionMode'], self.cb.settings(off)['plan']), ('default', False))
        k2 = self.cb.start(str(self.project), mode='plan', plan=True)['thread']['id'][len('claude:'):]          # "Plan" chosen as the mode too: Ask first after
        self.assertEqual(self.cb.held[k2]['afterPlan'], 'default')
        self.cb.take('permission', {'key': k2, 'requestId': 'r-no', 'toolName': 'ExitPlanMode', 'input': {'plan': 'p'}})
        self.cb.reply('r-no', {'decision': 'decline'})
        self.assertNotIn('updatedPermissions', self.sent('permission')[-1]['params'])          # a declined plan stays in plan mode
        self.assertEqual(self.cb.held[k2]['permissionMode'], 'plan')

    def test_workspace_sends_the_plan_toggle_to_claude_code(self):
        w = server.Workspace(FakeBridge())
        w._claude = self.cb
        w._providers = [CodexProvider(w, self.root / 'codex')]
        sid = w.action('start', {'harness': 'claude', 'cwd': str(self.project)})['thread']['id']
        self.assertEqual((self.sent('open')[-1]['params']['permissionMode'], w.plan_known(sid)), ('default', False))          # Claude Code's default is off
        sid = w.action('start', {'harness': 'claude', 'cwd': str(self.project), 'approvalPolicy': 'acceptEdits', 'plan': True})['thread']['id']
        self.assertEqual((self.sent('open')[-1]['params']['permissionMode'], w.plan_known(sid), self.cb.held[sid[len('claude:'):]]['afterPlan']), ('plan', True, 'acceptEdits'))
        with self.assertRaises(ValueError):
            w.action('start', {'harness': 'claude', 'cwd': str(self.project), 'plan': 1})

    def test_a_driven_sessions_plan_approval_is_not_shown_twice(self):
        # the transcript of a session this server drives also reads as stopped on ExitPlanMode: only the answerable request shows
        w = server.Workspace(FakeBridge())
        w._claude = self.cb
        key = self.cb.start(str(self.project), plan=True)['thread']['id'][len('claude:'):]
        ask = lambda sid: [{'id': 'claude-ask:tu', 'method': 'item/tool/requestUserInput', 'params': {'threadId': sid, 'itemId': 'tu', 'watchOnly': True}}]
        prov = SimpleNamespace(name='claude', paths={}, cache={}, live=lambda: {key: {}, 'other': {}}, read_history=lambda sid: {'asking': True},
                               list_sessions=lambda: [{'id': 'claude:' + key}, {'id': 'claude:other'}], ask_requests=lambda sid, hist: ask(sid))
        w._providers = [CodexProvider(w, self.root / 'codex'), prov]
        self.assertEqual([r['params']['threadId'] for r in w.watch_requests()], ['claude:other'])          # a watched session still asks

    def test_workspace_routes_claude_ops_and_never_calls_codex(self):
        w = server.Workspace(FakeBridge())          # FakeBridge.call raises on any Codex call
        home = self.root / 'claude-home'
        (home / 'projects').mkdir(parents=True)
        w._providers = [CodexProvider(w, self.root / 'codex'), ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')]
        w._claude = self.cb
        w._providers[1].drive = self.cb
        self.cb.provider = w._providers[1]
        with patch.object(server.Observer, 'rows', lambda self, limit=300: []):
            r = w.action('start', {'harness': 'claude', 'cwd': str(self.project), 'model': 'haiku', 'approvalPolicy': 'default', 'sandbox': None})
            sid = r['thread']['id']
            row = next(s for s in w.all_sessions() if s['id'] == sid)          # listed before its transcript exists
            self.assertEqual((row['provider'], row['status'], row['drivable'], row['external'], row['cwd']), ('claude', 'idle', True, False, str(self.project.resolve())))
            self.assertEqual(w.action('send', {'threadId': sid, 'text': 'hi there'})['how'], 'sent')
            self.assertTrue(self.wait(lambda: not self.cb.is_active(sid)))
            st = w.watch_settings(sid)
            self.assertEqual((st['approvalPolicy'], st['sandbox'], st['canSend']), ('default', None, True))
            with self.assertRaises(ValueError):
                w.action('archive', {'threadId': sid})
            with self.assertRaises(ValueError):
                w.action('start', {'harness': 'opencode', 'cwd': str(self.project)})
            self.cb.send(sid, 'please write')
            self.assertTrue(self.wait(lambda: 'r-write' in self.cb.requests))
            self.assertIn('r-write', [q['id'] for q in w.snapshot()['requests']])
            w.action('reply', {'id': 'r-write', 'result': {'decision': 'decline'}})
            self.assertEqual(self.sent('permission')[-1]['params']['allow'], False)
            held = '77777777-2222-3333-4444-555555555555'          # a transcript the desktop app holds open
            pdir = home / 'projects' / claude_code_encode(str(self.project.resolve()))
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / (held + '.jsonl')).write_text(json.dumps({'type': 'user', 'sessionId': held, 'cwd': str(self.project.resolve()), 'timestamp': '2026-11-06T10:00:00Z', 'uuid': 'u1',
                                                              'message': {'role': 'user', 'content': 'hello'}}) + '\n' + json.dumps(
                {'type': 'assistant', 'sessionId': held, 'cwd': str(self.project.resolve()), 'timestamp': '2026-11-06T10:00:05Z', 'uuid': 'u2', 'parentUuid': 'u1',
                 'message': {'id': 'msg1', 'role': 'assistant', 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'hi'}]}}) + '\n')
            (home / 'sessions').mkdir(exist_ok=True)
            (home / 'sessions' / (str(os.getpid()) + '.json')).write_text(json.dumps({'pid': os.getpid(), 'sessionId': held, 'cwd': str(self.project.resolve()), 'status': 'idle', 'entrypoint': 'claude-desktop'}))
            w._providers[1]._scan, w._providers[1]._live = (0, None, []), (0, {})          # past the provider's 2 s listing cache
            row = next(s for s in w.all_sessions() if s['id'] == 'claude:' + held)
            self.assertEqual((row['heldElsewhere'], row['heldBy'], row['drivable']), (True, 'app', True))          # the page's quiet note
            opens = len(self.sent('open'))
            r = w.action('send', {'threadId': 'claude:' + held, 'text': 'test test'})
            self.assertEqual((r['how'], r['heldBy'], r['desktopUrl']), ('choice', 'app', 'claude://resume?session=' + held))
            self.assertEqual(len(self.sent('open')), opens)          # no fork without the page's choice
            r = w.action('send', {'threadId': 'claude:' + held, 'text': 'test test', 'fork': 'yes'})
            self.assertEqual(r['how'], 'choice')          # only fork: true forks
            r = w.action('send', {'threadId': 'claude:' + held, 'text': 'test test', 'fork': True})
            self.assertEqual(r['how'], 'forked')
            o = self.sent('open')[-1]['params']
            self.assertEqual((o['resume'], o['fork'], o['model']), (held, True, 'claude-opus-5-5'))          # the copy runs on the session's own model
            copy = next(s for s in w.all_sessions() if s['id'] == r['threadId'])          # a copy: a branch of its original, named as a copy
            self.assertEqual((copy['parent'], copy['copy'], copy['title'].startswith('copy of ')), ('claude:' + held, True, True))
        self.assertEqual(self.cb.models(str(self.project))['data'][1]['id'], 'haiku')
        self.assertEqual(self.cb.alias('claude-haiku-5-5'), 'haiku')
# ---------- end claude drive ----------

# ---------- plan progress: the plan each agent keeps, from each harness's records (providers/plans.py) ----------
from providers import plans as plans_mod

PLANS = Path(__file__).resolve().parent / 'providers' / 'fixtures' / 'plans'


def plan_rows(log):
    """[(seconds after the first state, [(text, status)])]"""
    st = log.get('plan') or []
    t0 = st[0]['t'] if st else 0
    return [(round(x['t'] - t0), [(q['text'], q['status']) for q in x['steps']]) for x in st]


class PlanProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def claude(self, fixture):
        folder = self.root / 'claude' / 'projects' / encode(FIXTURE_CWD)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'plans.jsonl').write_text((PLANS / fixture).read_text())
        p = ClaudeCodeProvider(tokens(), home=self.root / 'claude', app_support=self.root / 'none')
        p.list_sessions(FIXTURE_CWD)
        return p.read_history('claude:plans')

    def test_codex_update_plan_calls_in_the_rollout(self):
        path = self.root / 'rollout.jsonl'
        path.write_text((PLANS / 'codex-update-plan.jsonl').read_text())
        log = server.Observer().read_log({'id': 'p1', 'rollout_path': str(path), 'created_at': 1, 'updated_at': 2, 'cwd': FIXTURE_CWD})
        # one state per call that changed the list (the repeated call at 41 s adds none), the other function call ignored, timed by its record
        self.assertEqual(plan_rows(log), [
            (0, [('Read the code', 'in_progress'), ('Write the fix', 'pending'), ('Run the tests', 'pending')]),
            (35, [('Read the code', 'completed'), ('Write the fix', 'in_progress'), ('Run the tests', 'pending')]),
            (85, [('Read the code', 'completed'), ('Write the fix', 'completed'), ('Run the tests', 'completed')])])
        self.assertEqual(log['plan'][0]['t'], server.timestamp('2026-11-09T10:00:05.000Z'))

    def codex_code_mode(self, lines=None):
        path = self.root / 'code-mode.jsonl'
        text = (PLANS / 'codex-code-mode.jsonl').read_text().splitlines(keepends=True)
        path.write_text(''.join(text[:lines] if lines else text))
        return path, text

    def test_codex_code_mode_plan_calls_inside_the_exec_script(self):
        # a real run, trimmed (codex-cli 0.167, gpt-6-luna, update_plan turned on, 2026-11-09): its three update_plan calls stand
        # inside the JavaScript of two `exec` calls; the second script's last call comes after its commands, so it counts at
        # the script's output record (0.19 s later), not at the call
        path, _ = self.codex_code_mode()
        log = server.Observer().read_log({'id': 'p2', 'rollout_path': str(path), 'created_at': 1, 'updated_at': 2, 'cwd': FIXTURE_CWD})
        steps = ['Inspect the workspace and any repository instructions', 'Implement a small file word-count CLI and tests', 'Run the tests and report the result']
        self.assertEqual([[(q['text'], q['status']) for q in s['steps']] for s in log['plan']], [
            list(zip(steps, ['in_progress', 'pending', 'pending'])),
            list(zip(steps, ['completed', 'in_progress', 'pending'])),
            list(zip(steps, ['completed', 'completed', 'completed']))])
        self.assertEqual([s['t'] for s in log['plan']], [server.timestamp('2026-11-10T03:10:03.408Z'), server.timestamp('2026-11-10T03:10:34.180Z'),
                                                         server.timestamp('2026-11-10T03:10:34.370Z')])

    def test_codex_code_mode_plan_read_as_the_rollout_grows(self):
        # live: the second script written but its output not yet: the step it is on shows, the finished list waits for the output
        path, text = self.codex_code_mode(lines=7)
        obs = server.Observer()
        row = {'id': 'p3', 'rollout_path': str(path), 'created_at': 1, 'updated_at': 2, 'cwd': FIXTURE_CWD}
        self.assertEqual([[q['status'] for q in s['steps']] for s in obs.read_log(row)['plan']],
                         [['in_progress', 'pending', 'pending'], ['completed', 'in_progress', 'pending']])
        with path.open('a') as f:
            f.write(''.join(text[7:]))
        self.assertEqual([[q['status'] for q in s['steps']] for s in obs.read_log(row)['plan']][-1], ['completed', 'completed', 'completed'])

    def test_code_mode_reads_only_literal_plan_calls_outside_strings_and_comments(self):
        src = ('const a = "tools.update_plan({plan:[]})"; // tools.update_plan({plan:[{step:"no"}]})\n'
               '/* tools.update_plan({plan:[]}) */ await tools.update_plan({plan});\n'
               'await tools.update_plan({plan:[{step:`a${b}`}]});\n'
               "text(await tools['update_plan']({plan:[{step:'Say \\'hi\\' \\u2014 twice', status:'pending',},],}));\n"
               'await tools.exec_command({cmd:"ls"}); await tools.update_plan({explanation:"x", plan:[{step:"After", status:"completed"}]});')
        self.assertEqual(plans_mod.code_mode_plans(src), [({'plan': [{'step': "Say 'hi' — twice", 'status': 'pending'}]}, False),
                                                          ({'explanation': 'x', 'plan': [{'step': 'After', 'status': 'completed'}]}, True)])
        c = {}
        self.assertFalse(plans_mod.codex_call(c, 5, 'exec', 'await tools.exec_command({cmd:"ls"});', 'k1'))          # no plan in the script
        self.assertTrue(plans_mod.codex_call(c, 5, 'exec', src, 'k2'))
        self.assertEqual(plan_rows(c), [(0, [("Say 'hi' — twice", 'pending')])])
        plans_mod.codex_output(c, 9, 'k2')
        self.assertEqual(plan_rows(c), [(0, [("Say 'hi' — twice", 'pending')]), (4, [('After', 'completed')])])

    def test_codex_live_notification_has_the_same_shape(self):
        c = {}
        plans_mod.codex_live(c, 10, {'threadId': 'x', 'turnId': 'y', 'explanation': None, 'plan': [{'step': 'A', 'status': 'inProgress'}, {'step': 'B', 'status': 'pending'}]})
        self.assertEqual(plan_rows(c), [(0, [('A', 'in_progress'), ('B', 'pending')])])

    def test_claude_todowrite_whole_list_each_call(self):
        self.assertEqual(plan_rows(self.claude('claude-todowrite.jsonl')), [
            (0, [('Find the bug', 'in_progress'), ('Fix it', 'pending')]),
            (20, [('Find the bug', 'completed'), ('Fix it', 'in_progress'), ('Add a test', 'pending')])])

    def test_claude_task_tools_paired_with_their_results(self):
        rows = plan_rows(self.claude('claude-tasks.jsonl'))
        self.assertEqual(rows[-1], (41, [('Survey', 'completed'), ('Build it', 'in_progress')]))          # task 3 deleted; ids from the results
        self.assertEqual(rows[3], (10, [('Survey', 'in_progress'), ('Build', 'pending'), ('Drop me', 'pending')]))

    def test_desktop_plan_items_advance_done_start_and_wrap_up(self):
        self.assertEqual(plan_rows(self.claude('claude-desktop-plan.jsonl')), [
            (0, [('Look around', 'in_progress'), ('Change it', 'pending'), ('Check it', 'pending'), ('Hand back', 'pending')]),
            (10, [('Look around', 'completed'), ('Change it', 'in_progress'), ('Check it', 'pending'), ('Hand back', 'pending')]),
            (20, [('Look around', 'completed'), ('Change it', 'completed'), ('Check it', 'pending'), ('Hand back', 'in_progress')]),
            (30, [('Look around', 'completed'), ('Change it', 'completed'), ('Check it', 'completed'), ('Hand back', 'completed')])])

    def test_a_refused_plan_call_changes_nothing(self):
        # a hook refused the desktop plan call and the wrap-up (as for a helper agent): the TodoWrite list stands, unfinished
        self.assertEqual(plan_rows(self.claude('claude-refused.jsonl')), [(0, [('Look around', 'in_progress'), ('Hand back', 'pending')])])

    def test_incremental_read_keeps_the_plan_between_tails(self):
        folder = self.root / 'claude' / 'projects' / encode(FIXTURE_CWD)
        folder.mkdir(parents=True)
        lines = (PLANS / 'claude-desktop-plan.jsonl').read_text().splitlines(keepends=True)
        path = folder / 'plans.jsonl'
        path.write_text(''.join(lines[:3]))
        p = ClaudeCodeProvider(tokens(), home=self.root / 'claude', app_support=self.root / 'none')
        p.list_sessions(FIXTURE_CWD)
        self.assertEqual(len(p.read_history('claude:plans')['plan']), 1)
        with path.open('a') as f:
            f.write(''.join(lines[3:]))
        self.assertEqual(len(p.read_history('claude:plans')['plan']), 4)          # advance and wrap_up need the list kept from the first read

    def test_canvas_carries_each_sessions_plan_from_the_floor_origin(self):
        self.assertEqual(plans_mod.relative([{'t': 105.0, 'steps': []}], 100.0), [{'t': 5.0, 'steps': []}])
        self.assertEqual(plans_mod.relative(None, 100.0), [])


class KeptBetweenRunsTests(unittest.TestCase):
    """speed (3rd pass): providers/keep.py: a provider given the last run's kept state lists, joins and counts exactly what a new
    one reading everything does, including what was written while no server ran; a file replaced since is read again."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def provider(self, home):
        return ClaudeCodeProvider(tokens(), home=home, app_support=self.root / 'none')

    def seen(self, p):
        listed = sorted(p.list_sessions(FIXTURE_CWD), key=lambda s: s['id'])
        return json.dumps([listed, {s['id']: p.session_usage(s['id']).view() for s in listed}, p.plan_usage()], sort_keys=True, default=str)

    def test_a_kept_state_reads_as_a_fresh_one(self):
        from providers import keep
        home, folder = continued_store(self.root, FIXTURE_CWD)
        first = self.provider(home)
        self.seen(first)
        kept = json.loads(json.dumps(keep.dump(first)))
        self.assertTrue(kept['links'] and kept['heads'] and kept['transcripts'])
        with (folder / (CC + '.jsonl')).open('a') as f:          # written while no server ran
            f.write(crec(CC, '00000081-1111-4111-8111-111111111111', '12:00:00', typ='assistant', text='Later'))
        again = self.provider(home)
        self.assertTrue(keep.load(again, kept))
        self.assertEqual(self.seen(again), self.seen(self.provider(home)))
        (folder / (CA + '.jsonl')).unlink()          # replaced: its kept entries are not used
        (folder / (CA + '.jsonl')).write_text((CONTINUED / (CA + '.jsonl')).read_text().replace('/Users/someone/project', FIXTURE_CWD))
        third = self.provider(home)
        keep.load(third, kept)
        self.assertNotIn(str(folder / (CA + '.jsonl')), third.transcripts.files)
        self.assertEqual(self.seen(third), self.seen(self.provider(home)))
        self.assertFalse(keep.load(self.provider(home), dict(kept, version='other code')))

    def test_the_cache_lives_in_the_users_app_data_folder(self):
        """packaging (2026-11-09): never next to the code; AGENT_IDE_CACHE_DIR moves it; the standard folder on each system."""
        from providers import keep
        h = self.root / 'home'
        self.assertEqual(keep.cache_dir({}, 'darwin', h), h / 'Library' / 'Application Support' / 'agent-ide' / 'cache')
        self.assertEqual(keep.cache_dir({}, 'linux', h), h / '.config' / 'agent-ide' / 'cache')
        self.assertEqual(keep.cache_dir({'XDG_CONFIG_HOME': '/x/conf'}, 'linux', h), Path('/x/conf/agent-ide/cache'))
        self.assertEqual(keep.cache_dir({'APPDATA': '/x/Roaming'}, 'win32', h), Path('/x/Roaming/agent-ide/cache'))
        self.assertEqual(keep.cache_dir({}, 'win32', h), h / 'AppData' / 'Roaming' / 'agent-ide' / 'cache')
        self.assertEqual(keep.cache_dir({'AGENT_IDE_CACHE_DIR': '/x/packaged/cache'}, 'darwin', h), Path('/x/packaged/cache'))
        self.assertEqual(keep.cache_dir({'AGENT_IDE_CACHE_DIR': '~/c'}), Path.home() / 'c')
        for plat in ('darwin', 'linux', 'win32'):
            self.assertNotIn(server.ROOT, keep.cache_dir({}, plat, h).parents)

    def test_the_server_reads_and_writes_only_the_users_folder(self):
        """The folder is made when missing; the old in-repo .cache is neither read nor removed; a save lands in the new folder."""
        from providers import keep
        home, _ = continued_store(self.root, FIXTURE_CWD)
        p = self.provider(home)
        p.name = 'claude'
        repo = self.root / 'repo'
        old = repo / '.cache' / ('claude-' + hashlib.blake2b(str(p.home).encode(), digest_size=6).hexdigest() + '.json')
        old.parent.mkdir(parents=True)
        old.write_text('{"version": "kept from before"}')
        folder = self.root / 'app data' / 'agent-ide' / 'cache'
        read = []
        with patch.dict(os.environ, {'AGENT_IDE_CACHE_DIR': str(folder)}), patch.object(server, 'ROOT', repo), \
                patch.object(keep, 'read', side_effect=lambda q: read.append(Path(q))), patch('threading.Thread'), patch('atexit.register'):
            path = server.keep_between_runs(type('W', (), {'providers': [p]})())
        self.assertTrue(folder.is_dir())
        self.assertEqual((path.parent, read), (folder, [path]))
        self.assertEqual(old.read_text(), '{"version": "kept from before"}')
        self.seen(p)
        keep.save(p, path)
        self.assertEqual([f.name for f in folder.iterdir()], [old.name])
        self.assertTrue(keep.load(self.provider(home), keep.read(path)))


import edits          # reading what an agent changed


class EditsTests(unittest.TestCase):
    """GET /api/edits (edits.py): an agent's own recorded edits, read from its record file on request."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name).resolve() / 'proj'
        self.cwd.mkdir()
        self.n = 0

    def claude(self, calls):
        """A Claude Code transcript: calls [(tool, input, result dict or None, is_error)], one a second."""
        out = []
        for i, (name, inp, tur, err) in enumerate(calls):
            ts = '2026-11-05T14:%02d:%02d.000Z' % (i // 60, i % 60)
            out.append({'type': 'assistant', 'timestamp': ts, 'cwd': str(self.cwd), 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tu%d' % i, 'name': name, 'input': inp}]}})
            if tur is not None or err:
                r = {'type': 'user', 'timestamp': ts, 'cwd': str(self.cwd), 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'tu%d' % i, 'content': 'x', **({'is_error': True} if err else {})}]}}
                if tur is not None:
                    r['toolUseResult'] = tur
                out.append(r)
        p = Path(self.tmp.name) / ('t%d.jsonl' % self.n)
        self.n += 1
        p.write_text(''.join(json.dumps(r) + '\n' for r in out))
        return edits.answer(*edits.claude_edits([p]))

    def f(self, name):
        return str(self.cwd / name)

    def test_numbered_edit_from_structured_patch(self):
        patch_ = [{'oldStart': 10, 'oldLines': 3, 'newStart': 10, 'newLines': 3, 'lines': [' a', '-b', '+c', ' d']}]
        a = self.claude([('Edit', {'file_path': self.f('x.py'), 'old_string': 'b', 'new_string': 'c'}, {'structuredPatch': patch_}, False)])
        self.assertEqual(a['files'], [{'path': 'x.py', 'more': 0, 'edits': [{'t': a['files'][0]['edits'][0]['t'], 'kind': 'update', 'numbered': True,
                                                                             'hunks': [{'oldStart': 10, 'newStart': 10, 'lines': [' a', '-b', '+c', ' d']}]}]}])
        self.assertEqual(a['cut'], {'files': 0, 'lines': 0})

    def test_unnumbered_edit_rebuilt_from_its_strings(self):
        a = self.claude([('Edit', {'file_path': self.f('x.py'), 'old_string': 'one\ntwo', 'new_string': 'three'}, [{'type': 'text', 'text': 'updated'}], False)])
        e = a['files'][0]['edits'][0]
        self.assertEqual((e['kind'], e['numbered'], e['hunks']), ('update', False, [{'lines': ['-one', '-two', '+three']}]))

    def test_error_edit_left_out_of_the_answer_and_the_count(self):
        calls = [('Edit', {'file_path': self.f('bad.py'), 'old_string': 'a', 'new_string': 'b'}, None, True),
                 ('Edit', {'file_path': self.f('good.py'), 'old_string': 'a', 'new_string': 'b'}, {}, False)]
        a = self.claude(calls)
        self.assertEqual([x['path'] for x in a['files']], ['good.py'])
        uid = '11111111-2222-4333-8444-555555555555'          # the count ("N files changed" is the paths among the write events): the same transcript, read by the provider
        dst = Path(self.tmp.name) / 'home' / 'projects' / 'p' / (uid + '.jsonl')
        dst.parent.mkdir(parents=True)
        shutil.copyfile(Path(self.tmp.name) / 't0.jsonl', dst)
        cp = ClaudeCodeProvider(tokens(), home=Path(self.tmp.name) / 'home', app_support=Path(self.tmp.name) / 'none')
        h = cp.read_history('claude:' + uid)
        self.assertEqual(sorted({e['path'] for e in h['events'] if e['kind'] == 'write'}), ['good.py'])

    def test_result_in_a_later_continuation_file_than_its_call(self):
        ts = '2026-11-05T14:00:00.000Z'
        use = lambda i, f: {'type': 'assistant', 'timestamp': ts, 'cwd': str(self.cwd), 'message': {'content': [
            {'type': 'tool_use', 'id': 'tu%d' % i, 'name': 'Edit', 'input': {'file_path': self.f(f), 'old_string': 'a', 'new_string': 'b'}}]}}
        res = lambda i, **k: {'type': 'user', 'timestamp': ts, 'cwd': str(self.cwd), 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 'tu%d' % i, 'content': 'x', **({'is_error': True} if k.get('err') else {})}]}, **({'toolUseResult': k['tur']} if 'tur' in k else {})}
        old, new = Path(self.tmp.name) / 'old.jsonl', Path(self.tmp.name) / 'new.jsonl'
        old.write_text(json.dumps(use(0, 'refused.py')) + '\n' + json.dumps(use(1, 'patched.py')) + '\n')
        new.write_text(json.dumps(res(0, err=True)) + '\n' + json.dumps(res(1, tur={'structuredPatch': [{'oldStart': 4, 'newStart': 4, 'lines': ['-a', '+b']}]})) + '\n')
        a = edits.answer(*edits.claude_edits([old, new]))
        self.assertEqual([(f['path'], [(e['numbered'], e['hunks']) for e in f['edits']]) for f in a['files']],
                         [('patched.py', [(True, [{'oldStart': 4, 'newStart': 4, 'lines': ['-a', '+b']}])])])

    def test_new_file_from_a_write_that_creates(self):
        a = self.claude([('Write', {'file_path': self.f('new.md'), 'content': '# T\nbody\n'}, {'type': 'create', 'content': '# T\nbody\n', 'structuredPatch': []}, False)])
        e = a['files'][0]['edits'][0]
        self.assertEqual((e['kind'], e['numbered'], e['hunks']), ('add', True, [{'oldStart': 0, 'newStart': 1, 'lines': ['+# T', '+body']}]))

    def test_several_edits_to_one_file_kept_apart_in_time_order(self):
        calls = [('Edit', {'file_path': self.f('a.py'), 'old_string': '1', 'new_string': '2'}, {}, False),
                 ('Edit', {'file_path': self.f('b.py'), 'old_string': '1', 'new_string': '2'}, {}, False),
                 ('Edit', {'file_path': self.f('a.py'), 'old_string': '3', 'new_string': '4'}, {}, False)]
        a = self.claude(calls)
        self.assertEqual([x['path'] for x in a['files']], ['a.py', 'b.py'])
        es = a['files'][0]['edits']
        self.assertEqual([e['hunks'][0]['lines'] for e in es], [['-1', '+2'], ['-3', '+4']])
        self.assertLess(es[0]['t'], es[1]['t'])

    def test_cut_per_file(self):
        big = '\n'.join('l%d' % i for i in range(edits.CHANGE_LINES_MAX + 50))
        a = self.claude([('Write', {'file_path': self.f('big.txt'), 'content': big}, {'type': 'create', 'content': big}, False)])
        self.assertEqual(sum(len(h['lines']) for e in a['files'][0]['edits'] for h in e['hunks']), edits.CHANGE_LINES_MAX)
        self.assertEqual((a['files'][0]['more'], a['cut']), (50, {'files': 0, 'lines': 50}))

    def test_cut_per_agent(self):
        e = lambda p, n: {'t': 1, 'path': p, 'kind': 'update', 'numbered': False, 'hunks': [{'lines': ['+x'] * n}]}
        a = edits.answer([e('a', 8), e('b', 8), e('c', 5)], 0, file_max=10, agent_max=12)
        self.assertEqual([(f['path'], f['more']) for f in a['files']], [('a', 0), ('b', 4)])
        self.assertEqual(a['cut'], {'files': 1, 'lines': 9})
        self.assertEqual(edits.AGENT_LINES_MAX, 3000)

    def test_codex_file_changes_failed_left_out_and_commands_counted(self):
        def done(item, ms):
            return {'timestamp': '2026-11-05T14:00:00.000Z', 'type': 'event_msg', 'payload': {'type': 'item_completed', 'completed_at_ms': ms, 'item': item}}
        recs = [done({'type': 'FileChange', 'id': 'f1', 'status': 'completed', 'changes': {self.f('x.py'): {'type': 'update', 'unified_diff': '@@ -3,2 +3,2 @@\n a\n-b\n+c\n'}}}, 1000),
                done({'type': 'FileChange', 'id': 'f2', 'status': 'failed', 'changes': {self.f('y.py'): {'type': 'update', 'unified_diff': '@@ -1 +1 @@\n-a\n+b\n'}}}, 2000),
                done({'type': 'FileChange', 'id': 'f3', 'status': 'completed', 'changes': [{'path': self.f('n.md'), 'kind': {'type': 'add'}, 'diff': 'hi\nthere\n'}]}, 3000),
                done({'type': 'CommandExecution', 'id': 'c1', 'parsed_cmd': [{'type': 'read', 'cmd': 'cat x'}]}, 4000),
                done({'type': 'CommandExecution', 'id': 'c2', 'parsed_cmd': [{'type': 'unknown', 'cmd': "sed -i '' s/a/b/ x.py"}]}, 5000)]
        p = Path(self.tmp.name) / 'rollout.jsonl'
        p.write_text(''.join(json.dumps(r) + '\n' for r in recs))
        a = edits.answer(*edits.codex_edits(p, str(self.cwd)))
        self.assertEqual([(f['path'], [(e['kind'], e['numbered'], e['hunks']) for e in f['edits']]) for f in a['files']],
                         [('x.py', [('update', True, [{'oldStart': 3, 'newStart': 3, 'lines': [' a', '-b', '+c']}])]),
                          ('n.md', [('add', True, [{'oldStart': 0, 'newStart': 1, 'lines': ['+hi', '+there']}])])])
        self.assertEqual(a['commands'], 1)

    def test_codex_apply_patch_calls_read_when_no_file_change_covers_them(self):
        ts = '2026-11-05T14:00:%02d.000Z'
        call = lambda i, name, text, args=False: {'timestamp': ts % i, 'type': 'response_item', 'payload': dict(
            {'type': 'function_call', 'name': name, 'arguments': json.dumps({'input': text})} if args else {'type': 'custom_tool_call', 'name': name, 'input': text}, call_id='p%d' % i)}
        only = ('*** Begin Patch\n*** Update File: a.py\n@@ def f():\n     x = 1\n-    y = 2\n+    y = 3\n@@\n-z\n+w\n'
                '*** Add File: new.md\n+# New\n+body\n*** End Patch')
        both = '*** Begin Patch\n*** Update File: b.py\n@@\n-1\n+2\n*** End Patch'
        fc = {'timestamp': ts % 3, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'completed_at_ms': None, 'item': {
            'type': 'FileChange', 'id': 'fc1', 'status': 'completed', 'changes': {self.f('b.py'): {'type': 'update', 'unified_diff': '@@ -5 +5 @@\n-1\n+2\n'}}}}}
        p = Path(self.tmp.name) / 'rollout-patch.jsonl'
        p.write_text(''.join(json.dumps(r) + '\n' for r in [call(1, 'apply_patch', only), call(2, 'apply_patch', both, args=True), fc]))
        a = edits.answer(*edits.codex_edits(p, str(self.cwd)))
        self.assertEqual([(f['path'], [(e['kind'], e['numbered'], e['hunks']) for e in f['edits']]) for f in a['files']],
                         [('a.py', [('update', False, [{'lines': ['     x = 1', '-    y = 2', '+    y = 3']}, {'lines': ['-z', '+w']}])]),
                          ('new.md', [('add', False, [{'lines': ['+# New', '+body']}])]),
                          ('b.py', [('update', True, [{'oldStart': 5, 'newStart': 5, 'lines': ['-1', '+2']}])])])          # b.py once: its FileChange, not the call too

    def test_unknown_agent(self):
        app = SimpleNamespace(providers=['codex'], provider_of=lambda sid: 'codex', session=lambda sid: None)
        with self.assertRaisesRegex(ValueError, 'Unknown agent'):
            edits.of_agent(app, 'nope')
        cp = ClaudeCodeProvider(tokens(), home=Path(self.tmp.name) / 'home', app_support=Path(self.tmp.name) / 'none')
        app = SimpleNamespace(providers=['codex', cp], provider_of=lambda sid: cp, session=lambda sid: None)
        with self.assertRaisesRegex(ValueError, 'Unknown agent'):
            edits.of_agent(app, 'claude:00000000-0000-0000-0000-000000000000')


if __name__ == '__main__':
    unittest.main()
