#!/usr/bin/env python3
"""A scripted ACP agent on stdin/stdout for test_acp.py (stdlib only).

argv[1] is the scenario, which fixes how the agent introduces itself and its quirks:
  v1                 v1 initialize shape, loadSession, session close
  v2                 v2 shape ({info, capabilities}); the prompt response only acknowledges; the turn ends on an idle state_update;
                     the prompt is echoed as a user_message
  agy                protocolVersion 2 in the v1 shape (Antigravity, acp.md §1), session resume
  ignore-cancel      v1, but session/cancel is ignored (the client must kill it after its bound)
  drop-early-cancel  v1, a cancel within 0.3 s of the prompt is dropped (Kiro's bug, acp.md §4)
  load-only          v1 with loadSession and no resume: session/load replays two messages, then answers
  load-noreply       like load-only, but session/load is never answered (the client's idle rule must end it)
  auth               session/new fails with -32000 (sign-in required)
The prompt's text picks the turn: hello, edit, edit-outside, malformed, garbage, bye, slow, usage, plan, run, ask, tools,
refuse (an error answer refusing it for the plan), upgrade (Cursor's one-line refusal with a normal end).
"""
import json
import os
import sys
import threading
import time

SCEN = sys.argv[1] if len(sys.argv) > 1 else 'v1'
V2 = SCEN == 'v2'
lock = threading.Lock()
waiting = {}          # our request id -> [Event, response]
cancelled = set()
prompt_at = {}
ids = iter(range(1000, 10 ** 6))
cwds = {}
config = {'model': 'm1', 'mode': 'default', 'thought_level': 'medium'}


def write(msg, newline=True):
    with lock:
        sys.stdout.write(json.dumps(msg) + ('\n' if newline else ''))
        sys.stdout.flush()


def raw(text):
    with lock:
        sys.stdout.write(text)
        sys.stdout.flush()


def update(sid, u):
    write({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': u}})


def result(rid, r):
    write({'jsonrpc': '2.0', 'id': rid, 'result': r})


def error(rid, code, message):
    write({'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}})


def ask(method, params, timeout=30):
    rid = next(ids)
    ev = threading.Event()
    waiting[rid] = [ev, None]
    write({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
    ev.wait(timeout)
    return (waiting.pop(rid, [None, None])[1] or {})


def config_options():
    return [
        {'id': 'model', 'name': 'Model', 'category': 'model', 'type': 'select', 'currentValue': config['model'],
         'options': [{'value': 'm1', 'name': 'Model One'}, {'value': 'm2', 'name': 'Model Two'}]},
        {'id': 'effort', 'name': 'Thinking', 'category': 'thought_level', 'type': 'select', 'currentValue': config['thought_level'],
         'options': [{'group': 'all', 'name': 'All', 'options': [{'value': 'low', 'name': 'Low'}, {'value': 'medium', 'name': 'Medium'}, {'value': 'high', 'name': 'High'}]}]},
        {'id': 'mode', 'name': 'Mode', 'category': 'mode', 'type': 'select', 'currentValue': config['mode'],
         'options': [{'value': 'default', 'name': 'Default'}, {'value': 'plan', 'name': 'Plan'}, {'value': 'yolo', 'name': 'YOLO'}]},
    ]


def initialize(rid, p):
    assert 'clientCapabilities' in p and 'clientInfo' in p and 'info' in p, 'superset initialize expected'
    if V2:
        result(rid, {'protocolVersion': 2, 'info': {'name': 'mock', 'version': '2.0'},
                     'capabilities': {'session': {'resume': {}, 'close': {}, 'prompt': {'image': {}}}}, 'authMethods': []})
    elif SCEN == 'agy':
        result(rid, {'protocolVersion': 2, 'agentCapabilities': {'loadSession': True, 'sessionCapabilities': {'resume': {}, 'close': {}}},
                     'agentInfo': {'name': 'Mock Antigravity', 'version': '1.5.0'}, 'authMethods': [{'id': 'oauth-personal', 'name': 'Google'}]})
    else:
        sc = {} if SCEN.startswith('load') else {'close': {}}
        result(rid, {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True, 'sessionCapabilities': sc},
                     'agentInfo': {'name': 'mock', 'version': '1.0'}, 'authMethods': []})


def finish(rid, sid, reason):
    if V2:
        update(sid, {'sessionUpdate': 'state_update', 'state': 'idle', 'stopReason': reason})
    else:
        result(rid, {'stopReason': reason})


def chunk(sid, text, mid=None, kind='agent_message_chunk'):
    u = {'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}
    if mid:
        u['messageId'] = mid
    update(sid, u)


def edit(rid, sid, path):
    update(sid, {'sessionUpdate': 'tool_call', 'toolCallId': 't1', 'title': 'Write ' + os.path.basename(path), 'kind': 'edit',
                 'status': 'pending', 'locations': [{'path': path}], 'rawInput': {'path': path, 'content': 'hi\n'}})
    r = ask('session/request_permission', {'sessionId': sid, 'toolCall': {'toolCallId': 't1'},
                                           'options': [{'optionId': 'yes', 'name': 'Allow once', 'kind': 'allow_once'},
                                                       {'optionId': 'always', 'name': 'Always', 'kind': 'allow_always'},
                                                       {'optionId': 'no', 'name': 'Reject', 'kind': 'reject_once'}]})
    out = (r.get('result') or {}).get('outcome') or {}
    if out.get('outcome') == 'cancelled' or sid in cancelled:
        update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 't1', 'status': 'cancelled'})
        return finish(rid, sid, 'cancelled')
    if out.get('optionId') in ('yes', 'always'):
        with open(path, 'w') as f:
            f.write('hi\n')
        update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 't1', 'status': 'completed',
                     'content': [{'type': 'diff', 'path': path, 'oldText': None, 'newText': 'hi\n'}]})
        chunk(sid, 'done', 'm2')
    else:
        update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 't1', 'status': 'failed'})
        chunk(sid, 'not allowed', 'm2')
    finish(rid, sid, 'end_turn')


def run_prompt(rid, sid, text):
    if V2:
        result(rid, {'messageId': 'u-%d' % rid})
        update(sid, {'sessionUpdate': 'user_message', 'messageId': 'u-%d' % rid, 'content': [{'type': 'text', 'text': text}]})
        update(sid, {'sessionUpdate': 'state_update', 'state': 'running'})
    cwd = cwds.get(sid, os.getcwd())
    if text == 'hello':
        chunk(sid, 'thinking…', 'th1', kind='agent_thought_chunk')
        chunk(sid, 'Hel', 'm1')
        chunk(sid, 'lo', 'm1')
        return finish(rid, sid, 'end_turn')
    if text == 'refuse':          # the prompt refused for the plan (an error answer, the agent's own words over two lines)
        return error(rid, -32603, "You've hit your usage limit for premium models on the Free plan.\nUpgrade to Pro or switch to Auto. Resets in 2h30m.")
    if text == 'upgrade':          # Cursor's own refusal: one line and a normal end_turn (acp_flavors.cursor_plan_refusal)
        chunk(sid, 'Upgrade your plan to continue', 'm1')
        return finish(rid, sid, 'end_turn')
    if text == 'edit':
        return edit(rid, sid, os.path.join(cwd, 'hello.txt'))
    if text == 'edit-outside':
        return edit(rid, sid, os.path.join(os.path.dirname(cwd), 'outside-%d.txt' % os.getpid()))
    if text == 'malformed':
        update(sid, {'sessionUpdate': 'tool_call_update'})                      # no toolCallId
        update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': 'not a block'})
        update(sid, {'sessionUpdate': 'brand_new_kind', 'x': 1})
        write({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': 5}})
        chunk(sid, 'still here', 'm1')
        return finish(rid, sid, 'end_turn')
    if text == 'garbage':
        raw('this is not json\n')
        time.sleep(30)
        return
    if text == 'bye':
        chunk(sid, 'bye', 'm1')
        write({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}}, newline=False)
        sys.stdout.close()
        os._exit(0)
    if text == 'slow':
        first = 0.5 if SCEN == 'drop-early-cancel' else 0.05
        time.sleep(first)
        for i in range(400):
            if sid in cancelled:
                cancelled.discard(sid)
                return finish(rid, sid, 'cancelled')
            chunk(sid, '%d\n' % i, 'm1')
            time.sleep(0.05)
        return finish(rid, sid, 'end_turn')
    if text == 'usage':
        update(sid, {'sessionUpdate': 'usage_update', 'used': 1200, 'size': 200000, 'cost': {'amount': 0.01, 'currency': 'USD'}})
        update(sid, {'sessionUpdate': 'usage_update', 'used': 5300, 'size': 200000})
        return finish(rid, sid, 'end_turn')
    if text == 'plan':
        e = lambda c, s: {'content': c, 'priority': 'medium', 'status': s}
        update(sid, {'sessionUpdate': 'plan', 'entries': [e('a', 'pending'), e('b', 'pending')]})
        update(sid, {'sessionUpdate': 'plan', 'entries': [e('a', 'completed'), e('b', 'in_progress')]})
        update(sid, {'sessionUpdate': 'plan', 'entries': [e('a', 'completed'), e('b', 'cancelled')]})
        update(sid, {'sessionUpdate': 'plan', 'entries': []})
        return finish(rid, sid, 'end_turn')
    if text == 'run':
        update(sid, {'sessionUpdate': 'tool_call', 'toolCallId': 'r1', 'title': 'Run', 'kind': 'execute', 'status': 'in_progress',
                     'rawInput': {'command': ['seq', '1', '5000']}})
        out = ''
        for i in range(40):
            out += ('line %04d ' % i) * 50 + '\n'
            update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 'r1',
                         'content': [{'type': 'content', 'content': {'type': 'text', 'text': out}}]})
        update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': 'r1', 'status': 'completed'})
        return finish(rid, sid, 'end_turn')
    if text == 'ask':
        r = ask('session/request_permission', {'sessionId': sid, 'toolCall': {'toolCallId': 'interaction_1', 'title': 'Which color?'},
                                               'options': [{'optionId': 'red', 'name': 'Red', 'kind': 'allow_once'},
                                                           {'optionId': 'blue', 'name': 'Blue', 'kind': 'allow_once'}]})
        chunk(sid, 'you chose ' + str(((r.get('result') or {}).get('outcome') or {}).get('optionId')), 'm1')
        return finish(rid, sid, 'end_turn')
    if text == 'tools':          # a read and a run, permission asked for each (full-access auto-answer checks)
        for tid, kind, title, extra in (('k1', 'read', 'Read a.txt', {'locations': [{'path': os.path.join(cwd, 'a.txt')}]}),
                                        ('k2', 'execute', 'ls', {'rawInput': {'command': 'ls'}})):
            update(sid, {'sessionUpdate': 'tool_call', 'toolCallId': tid, 'title': title, 'kind': kind, 'status': 'pending', **extra})
            r = ask('session/request_permission', {'sessionId': sid, 'toolCall': {'toolCallId': tid},
                                                   'options': [{'optionId': 'ok', 'name': 'Allow', 'kind': 'allow_once'},
                                                               {'optionId': 'no', 'name': 'Reject', 'kind': 'reject_once'}]})
            ok = ((r.get('result') or {}).get('outcome') or {}).get('optionId') == 'ok'
            update(sid, {'sessionUpdate': 'tool_call_update', 'toolCallId': tid, 'status': 'completed' if ok else 'failed'})
        return finish(rid, sid, 'end_turn')
    chunk(sid, 'echo: ' + text, 'm1')
    finish(rid, sid, 'end_turn')


def handle(msg):
    method, rid, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if method is None and rid in waiting:
        waiting[rid][1] = msg
        waiting[rid][0].set()
        return
    if method == 'initialize':
        return initialize(rid, p)
    if method == 'session/new':
        if SCEN == 'auth':
            return error(rid, -32000, 'Authentication required')
        sid = 'mock-%d-%d' % (os.getpid(), len(cwds) + 1)          # unique across processes, as real agents' ids are
        cwds[sid] = p.get('cwd')
        r = {'sessionId': sid, 'configOptions': config_options(),
             'modes': {'currentModeId': 'default', 'availableModes': [{'id': 'default', 'name': 'Default'}, {'id': 'plan', 'name': 'Plan'}]}}
        result(rid, r)
        update(sid, {'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'compact', 'description': 'x'}]})
        return
    if method == 'session/set_config_option':
        cat = {'model': 'model', 'effort': 'thought_level', 'mode': 'mode'}.get(p.get('configId'))
        if cat is None:
            return error(rid, -32602, 'unknown config')
        config[cat] = p.get('value')
        result(rid, {'configOptions': config_options()})
        return
    if method == 'session/set_mode':
        config['mode'] = p.get('modeId')
        return result(rid, {})
    if method in ('session/resume',):
        cwds[p['sessionId']] = p.get('cwd')
        return result(rid, {'configOptions': config_options()})
    if method == 'session/load':
        sid = p['sessionId']
        cwds[sid] = p.get('cwd')
        chunk(sid, 'earlier question', 'old-u', kind='user_message_chunk')
        chunk(sid, 'earlier answer', 'old-a')
        if SCEN != 'load-noreply':
            result(rid, {'configOptions': config_options()})
        return
    if method == 'session/close':
        return result(rid, {})
    if method == 'session/cancel':
        sid = p.get('sessionId')
        if SCEN == 'ignore-cancel':
            return
        if SCEN == 'drop-early-cancel' and time.time() - prompt_at.get(sid, 0) < 0.3:
            return
        cancelled.add(sid)
        return
    if method == 'session/prompt':
        sid = p['sessionId']
        prompt_at[sid] = time.time()
        cancelled.discard(sid)
        text = ''.join(b.get('text', '') for b in p.get('prompt') or [] if b.get('type') == 'text')
        threading.Thread(target=run_prompt, args=(rid, sid, text), daemon=True).start()
        return
    if rid is not None:
        error(rid, -32601, 'Method not found: %s' % method)


def main():
    sys.stderr.write('mock agent starting; token=sk-abcdefghijklmnopqrstuvwxyz in ' + os.path.expanduser('~') + '\n')
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        handle(msg)


if __name__ == '__main__':
    main()
