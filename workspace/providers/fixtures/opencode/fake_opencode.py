"""A fake `opencode` CLI for test_opencode.py: `--version`, and `serve --hostname=H --port=P` running a small OpenCode 2
HTTP + SSE server (stdlib only). No model, no network beyond loopback.

What it imitates and where that comes from:
- the routes, under /api (GET /api/info, /api/event, /api/session[...], /api/model, /api/agent): OpenCode's own API reference
  (https://opencode.ai/docs/api, "V2 (latest)", read 2026-11-09);
- the body shapes ({data: ...} wrappers, {interrupted}, permission `decision`, form `answer`) and every event payload: T3 Code's
  recorded OpenCode 2.0.18 sessions (apps/server/src/orchestration-v2/testkit/fixtures/opencode2_*, MIT licence, clone at
  2da1b7431), cut into scenarios/*.jsonl by a one-off script. retry.jsonl and edit_delete.jsonl are scripted by hand from
  T3's tests (OpenCode2AdapterV2.test.ts "session.retry.scheduled") because no recording has them;
- HTTP Basic auth as `opencode:<password>` from OPENCODE_PASSWORD (2.x) or OPENCODE_SERVER_PASSWORD (1.x), and the
  "server listening on <url>" stdout line: T3 OpenCode2Server.ts, OpenCodeRuntime.ts:285.

A prompt whose text is `scenario:<name>` plays scenarios/<name>.jsonl into that session; any other text gets a one-line
reply. Scenario lines: {"event": {...}} ($SID is the session id), {"wait": "permission" | "form" | "interrupt" |
"release"}, {"raw": "<an SSE line>"}. Test-only routes under /__test (no auth): log, drop, refuse, accept, release, env.

FAKE_OPENCODE (comma list) changes the process: `v1` (reports 1.14.39), `locked=<file>` (the first start exits with a
database-lock error and creates <file>), `silent` (never prints the listening line)."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
MODES = dict((m.split('=', 1) + [''])[:2] for m in os.environ.get('FAKE_OPENCODE', '').split(',') if m)
VERSION = '1.14.39' if 'v1' in MODES else '2.0.28'


def now_ms():
    return int(time.time() * 1000)


def new_id(prefix):
    return prefix + '_' + uuid.uuid4().hex[:24]


class State:
    def __init__(self, password):
        self.password = password
        self.lock = threading.RLock()
        self.sessions, self.messages, self.active = {}, {}, set()
        self.permissions, self.forms = {}, {}          # pending, by id
        self.waits = {}          # (sid, kind) -> threading.Event
        self.subscribers = []
        self.refuse = False
        self.log = []
        self.steers = {}          # sid -> [inbox ids not yet delivered]
        self.playing = set()

    # ---- events ----
    def emit(self, ev=None, raw=None):
        if ev is not None:
            ev.setdefault('id', new_id('evt'))
            ev.setdefault('created', now_ms())
            self.project(ev)
            raw = 'data: ' + json.dumps(ev)
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            q.put(raw)

    def project(self, ev):
        """The projected history GET /api/session/:id/message returns, built from the events (OpenCode keeps a block's
        text only once the block ends: T3 200a373fb)."""
        t, d = ev.get('type', ''), ev.get('data') or {}
        sid = d.get('sessionID')
        with self.lock:
            msgs = self.messages.setdefault(sid, []) if sid else None
            cur = next((m for m in reversed(msgs) if m.get('id') == d.get('assistantMessageID')), None) if msgs is not None else None
            if t == 'session.execution.started':
                self.active.add(sid)
            elif t in ('session.execution.succeeded', 'session.execution.failed', 'session.execution.interrupted'):
                self.active.discard(sid)
                outcome = t.rsplit('.', 1)[1]
                msgs.append({'id': new_id('msg'), 'type': 'idle', 'time': {'created': now_ms()},
                             'outcome': {'succeeded': 'succeeded', 'failed': 'failed', 'interrupted': 'interrupted'}[outcome]})
            elif t == 'session.step.started' and cur is None:
                msgs.append({'id': d['assistantMessageID'], 'type': 'assistant', 'time': {'created': now_ms()}, 'agent': d.get('agent'),
                             'model': d.get('model'), 'content': []})
            elif cur is not None and t in ('session.text.ended', 'session.reasoning.ended'):
                cur['content'].append({'type': 'text' if 'text.' in t else 'reasoning', 'text': d.get('text', '')})
            elif cur is not None and t == 'session.tool.called':
                cur['content'].append({'type': 'tool', 'id': d['id'], 'name': self.tool_names.get(d['id'], 'tool'),
                                       'state': {'status': 'running', 'input': d.get('input') or {}}})
            elif cur is not None and t in ('session.tool.success', 'session.tool.failed'):
                part = next((p for p in cur['content'] if p.get('id') == d['id']), None)
                if part:
                    if t.endswith('success'):
                        part['state'].update(status='completed', content=d.get('content') or [], metadata=d.get('metadata') or {})
                    else:
                        part['state'].update(status='error', error=d.get('error'), metadata=d.get('metadata') or {})
            elif cur is not None and t in ('session.step.ended', 'session.step.failed'):
                cur.update(tokens=d.get('tokens'), finish=d.get('finish'), cost=d.get('cost', 0))
                cur['time']['completed'] = now_ms()
            if t == 'session.tool.input.started':
                self.tool_names[d['id']] = d.get('name')

    tool_names = {}

    # ---- scenarios ----
    def play(self, sid, name):
        self.playing.add(sid)
        try:
            self._play(sid, name)
        finally:
            self.playing.discard(sid)

    def _play(self, sid, name):
        path = HERE / 'scenarios' / (name + '.jsonl')
        lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        for line in lines:
            if 'wait' in line:
                with self.lock:
                    ev = self.waits.setdefault((sid, line['wait']), threading.Event())
                ev.wait(20)
                with self.lock:
                    self.waits.pop((sid, line['wait']), None)
                self.deliver_steers(sid)
                if line['wait'] == 'interrupt' and getattr(ev, 'cancelled', False):
                    continue
                continue
            if 'raw' in line:
                self.emit(raw=line['raw'])
                continue
            ev = json.loads(json.dumps(line['event']).replace('$SID', sid))
            if ev['type'] in ('session.execution.succeeded',) and self.steers.get(sid):
                # an undelivered steer starts another execution (T3 v2 adapter endExecution)
                self.emit(ev)
                self.emit({'type': 'session.execution.started', 'data': {'sessionID': sid}})
                self.deliver_steers(sid)
                self.reply_text(sid, 'STEERED')
                self.emit({'type': 'session.execution.succeeded', 'data': {'sessionID': sid}})
                return
            if ev['type'] == 'permission.asked':
                self.permissions[ev['data']['id']] = ev['data']
            if ev['type'] == 'form.created':
                self.forms[ev['data']['form']['id']] = ev['data']['form']
            self.emit(ev)
            time.sleep(0.002)

    def deliver_steers(self, sid):
        for inbox in self.steers.pop(sid, []):
            self.emit({'type': 'session.inbox.delivered', 'data': {'sessionID': sid, 'inboxID': inbox}})

    def reply_text(self, sid, text):
        mid = new_id('msg')
        self.emit({'type': 'session.step.started', 'data': {'sessionID': sid, 'assistantMessageID': mid, 'agent': 'build',
                                                             'model': {'id': 'big-pickle', 'providerID': 'opencode'}}})
        self.emit({'type': 'session.text.started', 'data': {'sessionID': sid, 'assistantMessageID': mid, 'ordinal': 0}})
        self.emit({'type': 'session.text.delta', 'data': {'sessionID': sid, 'assistantMessageID': mid, 'ordinal': 0, 'delta': text}})
        self.emit({'type': 'session.text.ended', 'data': {'sessionID': sid, 'assistantMessageID': mid, 'ordinal': 0, 'text': text}})
        self.emit({'type': 'session.step.ended', 'data': {'sessionID': sid, 'assistantMessageID': mid, 'finish': 'stop', 'cost': 0,
                                                           'tokens': {'input': 10, 'output': 2, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}}}})

    def simple(self, sid):
        self.emit({'type': 'session.execution.started', 'data': {'sessionID': sid}})
        self.reply_text(sid, 'Hello.')
        self.emit({'type': 'session.execution.succeeded', 'data': {'sessionID': sid}})


MODELS = [{'id': 'big-pickle', 'modelID': 'big-pickle', 'providerID': 'opencode', 'name': 'Big Pickle', 'variants': [],
           'limit': {'context': 200000, 'input': 160000, 'output': 32000}, 'status': 'active', 'enabled': True},
          {'id': 'space-bunny-free', 'modelID': 'space-bunny-free', 'providerID': 'opencode', 'name': 'Space Bunny Free',
           'variants': [{'id': 'low'}, {'id': 'medium'}, {'id': 'high'}], 'limit': {'context': 1048576}, 'status': 'active', 'enabled': True}]
AGENTS = [{'id': 'build', 'name': 'Build', 'mode': 'primary', 'hidden': False}, {'id': 'plan', 'name': 'Plan', 'mode': 'primary', 'hidden': False},
          {'id': 'general', 'name': 'General', 'mode': 'subagent', 'hidden': False}]


def handler_for(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, status, body=None):
            data = b'' if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            n = int(self.headers.get('Content-Length') or 0)
            return json.loads(self.rfile.read(n) or b'null') if n else None

        def authed(self):
            want = 'Basic ' + base64.b64encode(('opencode:' + state.password).encode('utf-8')).decode()
            return self.headers.get('Authorization') == want

        def route(self, method):
            path, _, qs = self.path.partition('?')
            body = self.body() if method in ('POST', 'PATCH', 'PUT') else None
            if path.startswith('/__test/'):
                return self.test_route(path[8:], body)
            with state.lock:
                state.log.append({'method': method, 'path': path, 'query': qs, 'body': body, 'auth': self.authed()})
            if not self.authed():
                return self.send(401, {'name': 'UnauthorizedError'})
            if 'v1' in MODES:
                return self.send(200, {'healthy': True, 'version': VERSION}) if path == '/global/health' else self.send(404, {})
            m = re.match(r'^/api/session/([^/]+)(/.*)?$', path)
            if path == '/api/info':
                return self.send(200, {'version': VERSION, 'pid': os.getpid()})
            if path == '/api/event':
                return self.events()
            if path == '/api/model':
                return self.send(200, {'location': {'directory': '/'}, 'data': MODELS})
            if path == '/api/agent':
                return self.send(200, {'location': {'directory': '/'}, 'data': AGENTS})
            if path == '/api/session' and method == 'GET':
                return self.send(200, {'data': list(state.sessions.values()), 'cursor': {'next': None, 'previous': None}})
            if path == '/api/session' and method == 'POST':
                return self.create(body or {})
            if path == '/api/session/active':
                return self.send(200, {'data': {s: {} for s in state.active}})
            if not m:
                return self.send(404, {'name': 'NotFound'})
            sid, rest = m.group(1), m.group(2) or ''
            if sid not in state.sessions:
                return self.send(404, {'name': 'SessionNotFoundError'})
            if rest == '' and method == 'GET':
                return self.send(200, {'data': state.sessions[sid]})
            if rest == '' and method == 'PATCH':
                state.sessions[sid].update({k: v for k, v in (body or {}).items() if k in ('title', 'permissions')})
                return self.send(200, {'data': state.sessions[sid]})
            if rest == '/prompt':
                return self.prompt(sid, body or {})
            if rest == '/interrupt':
                running = sid in state.active
                if sid in state.playing:          # a scenario runs: its next 'interrupt' wait passes (set before it is reached)
                    state.waits.setdefault((sid, 'interrupt'), threading.Event()).set()
                elif running:          # nothing scripted: end it here
                    state.emit({'type': 'session.execution.interrupted', 'data': {'sessionID': sid, 'reason': 'user'}})
                return self.send(200, {'interrupted': running})
            if rest == '/fork':
                return self.fork(sid, body or {})
            if rest == '/message':
                order = re.search(r'order=(\w+)', qs)
                msgs = list(state.messages.get(sid, []))
                if order and order.group(1) == 'desc':
                    msgs.reverse()
                lim = re.search(r'limit=(\d+)', qs)
                return self.send(200, {'data': msgs[:int(lim.group(1))] if lim else msgs, 'cursor': {'next': None, 'previous': None}})
            if rest == '/permission' and method == 'GET':
                return self.send(200, {'data': [p for p in state.permissions.values() if p['sessionID'] == sid]})
            if rest == '/form' and method == 'GET':
                return self.send(200, {'data': [f for f in state.forms.values() if f['sessionID'] == sid]})
            pm = re.match(r'^/permission/([^/]+)/reply$', rest)
            if pm:
                req = state.permissions.pop(pm.group(1), None)
                if not req:
                    return self.send(404, {'name': 'PermissionNotFoundError'})
                self.send(204)
                state.emit({'type': 'permission.replied', 'data': {'sessionID': sid, 'requestID': req['id'], 'reply': (body or {}).get('decision')}})
                with state.lock:          # kept for a scenario that has not reached its wait yet (lost before, 2026-11-22)
                    ev = state.waits.setdefault((sid, 'permission'), threading.Event())
                ev.set()
                return
            fm = re.match(r'^/form/([^/]+)(/reply)?$', rest)
            if fm:
                form = state.forms.pop(fm.group(1), None)
                if not form:
                    return self.send(404, {'name': 'FormNotFoundError'})
                self.send(204)
                if fm.group(2):
                    state.emit({'type': 'form.replied', 'data': {'id': form['id'], 'sessionID': sid, 'answer': (body or {}).get('answer')}})
                else:
                    state.emit({'type': 'form.cancelled', 'data': {'id': form['id'], 'sessionID': sid}})
                with state.lock:          # kept for a scenario that has not reached its wait yet (lost before, 2026-11-22)
                    ev = state.waits.setdefault((sid, 'form'), threading.Event())
                ev.set()
                return
            if re.match(r'^/inbox/[^/]+$', rest) and method == 'DELETE':
                inbox = rest.rsplit('/', 1)[1]
                if inbox in state.steers.get(sid, []):
                    state.steers[sid].remove(inbox)
                    state.emit({'type': 'session.inbox.cancelled', 'data': {'sessionID': sid, 'inboxID': inbox}})
                return self.send(204)
            if rest == '/revert/stage':
                return self.send(200, {'data': {'messageID': (body or {}).get('messageID'), 'snapshot': 'x', 'files': []}})
            if rest == '/revert/commit':
                return self.send(204)
            if rest == '/compact':
                return self.send(200, {'data': {'id': new_id('msg'), 'type': 'compaction'}})
            return self.send(404, {'name': 'NotFound'})

        def create(self, body):
            sid = new_id('ses')
            loc = body.get('location') or {'directory': '/'}
            s = {'id': sid, 'projectID': 'global', 'title': body.get('title') or 'New session', 'location': loc,
                 'model': body.get('model'), 'agent': body.get('agent'), 'time': {'created': now_ms(), 'updated': now_ms()},
                 'tokens': {'input': 0, 'output': 0, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}}, 'cost': 0,
                 'permissions': body.get('permissions') or [{'action': '*', 'resource': '*', 'effect': 'allow'}]}
            if body.get('parentID'):
                s['parentID'] = body['parentID']
            state.sessions[sid] = s
            state.messages[sid] = []
            self.send(200, {'data': s})
            state.emit({'type': 'session.created', 'location': loc, 'data': {'sessionID': sid, 'location': loc, 'title': s['title'],
                                                                             'model': s['model'], 'version': VERSION}})

        def fork(self, sid, body):
            parent = state.sessions[sid]
            nid = new_id('ses')
            boundary = {'type': 'before', 'messageID': body['before']} if body.get('before') else {'type': 'all'}
            s = dict(parent, id=nid, title=parent['title'] + ' (fork #1)', fork={'sessionID': sid, 'boundary': boundary},
                     time={'created': now_ms(), 'updated': now_ms()})
            state.sessions[nid] = s
            msgs = state.messages.get(sid, [])
            if body.get('before'):
                cut = next((i for i, m in enumerate(msgs) if m['id'] == body['before']), len(msgs))
                msgs = msgs[:cut]
            state.messages[nid] = json.loads(json.dumps(msgs))
            self.send(200, {'data': s})
            tmpl = json.loads((HERE / 'scenarios' / 'forked_event.jsonl').read_text().splitlines()[0])['event']
            tmpl['data'].update(sessionID=nid, parentID=sid, boundary=boundary)
            state.emit(tmpl)

        def prompt(self, sid, body):
            text = body.get('text') or ''
            mid = body.get('id') or new_id('msg')
            delivery = body.get('delivery') or 'steer'
            self.send(200, {'data': {'id': mid, 'sessionID': sid, 'time': {'created': now_ms()}, 'type': 'user',
                                     'payload': {'text': text}, 'delivery': delivery}})
            with state.lock:
                state.messages[sid].append({'id': mid, 'type': 'user', 'time': {'created': now_ms()}, 'text': text})
                running = sid in state.active
            state.emit({'type': 'session.inbox.enqueued', 'data': {'inboxID': mid, 'sessionID': sid,
                                                                   'item': {'type': 'user', 'payload': {'text': text}, 'delivery': delivery}}})
            if running:
                state.steers.setdefault(sid, []).append(mid)          # joins the running execution at its next step
                return
            def run():
                state.emit({'type': 'session.inbox.delivered', 'data': {'sessionID': sid, 'inboxID': mid}})
                if text.startswith('scenario:'):
                    state.play(sid, text.split(':', 1)[1].strip())
                else:
                    state.simple(sid)
            threading.Thread(target=run, daemon=True).start()

        def events(self):
            if state.refuse:
                return self.send(503, {'name': 'ServiceUnavailableError'})
            q = queue.Queue()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'data: ' + json.dumps({'id': new_id('evt'), 'type': 'server.connected', 'data': {}}).encode() + b'\n\n')
            self.wfile.flush()
            with state.lock:
                state.subscribers.append(q)
            try:
                while True:
                    try:
                        item = q.get(timeout=0.5)
                    except queue.Empty:
                        item = ':'          # a heartbeat comment
                    if item is None:
                        return          # dropped by the test
                    self.wfile.write(item.encode() + b'\n\n')
                    self.wfile.flush()
            except OSError:
                return
            finally:
                with state.lock:
                    if q in state.subscribers:
                        state.subscribers.remove(q)

        def test_route(self, what, body):
            if what == 'log':
                with state.lock:
                    return self.send(200, list(state.log))
            if what == 'env':
                return self.send(200, {k: ('set' if os.environ.get(k) else None) for k in ('OPENCODE_PASSWORD', 'OPENCODE_SERVER_PASSWORD')}
                                 | {'same': os.environ.get('OPENCODE_PASSWORD') == os.environ.get('OPENCODE_SERVER_PASSWORD')})
            if what == 'drop':
                with state.lock:
                    subs, state.subscribers = list(state.subscribers), []
                for q in subs:
                    q.put(None)
                return self.send(200, {'dropped': len(subs)})
            if what == 'refuse':
                state.refuse = True
                return self.send(200, {})
            if what == 'accept':
                state.refuse = False
                return self.send(200, {})
            if what == 'release':          # kept until the scenario reaches its wait: a release sent before it got there was lost (2026-11-22)
                with state.lock:
                    keys = {k for k in state.waits if k[1] == 'release'} | {(sid, 'release') for sid in state.playing}
                    evs = [state.waits.setdefault(k, threading.Event()) for k in keys]
                for ev in evs:
                    ev.set()
                return self.send(200, {})
            if what == 'emit':
                for ev in (body or []):
                    state.emit(ev)
                return self.send(200, {})
            return self.send(404, {})

        def do_GET(self):
            self.route('GET')

        def do_POST(self):
            self.route('POST')

        def do_PATCH(self):
            self.route('PATCH')

        def do_PUT(self):
            self.route('PUT')

        def do_DELETE(self):
            self.route('DELETE')
    return H


def main(argv):
    if '--version' in argv:
        print(VERSION)
        return 0
    if not argv or argv[0] != 'serve':
        print('usage: opencode serve --hostname=H --port=P', file=sys.stderr)
        return 2
    opts = dict(a[2:].split('=', 1) for a in argv[1:] if a.startswith('--') and '=' in a)
    if 'locked' in MODES and not Path(MODES['locked']).exists():
        Path(MODES['locked']).write_text('1')
        print('Error: SQLITE_BUSY: database is locked', file=sys.stderr)
        return 1
    password = os.environ.get('OPENCODE_PASSWORD') if VERSION.startswith('2') else os.environ.get('OPENCODE_SERVER_PASSWORD')
    if not password:
        password = uuid.uuid4().hex
        print('generated password: ' + password)          # OpenCode 2 prints one when none is given (T3 OpenCode2Server.ts)
    state = State(password)
    srv = ThreadingHTTPServer((opts.get('hostname', '127.0.0.1'), int(opts.get('port', 0))), handler_for(state))
    srv.daemon_threads = True
    if 'silent' not in MODES:
        print('opencode server listening on http://%s:%d' % srv.server_address, flush=True)
    srv.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
