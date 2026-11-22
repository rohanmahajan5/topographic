"""The Claude Code bridge's worker: runs the Python Agent SDK (claude-agent-sdk) for server.py, which stays stdlib-only.

Run by server.ClaudeBridge as `.claude-bridge/venv/bin/python -I claude_worker.py` (the venv from claude_bridge_setup.sh).
It talks JSON lines on stdin/stdout, the same shape as the Codex app-server bridge:
  in:  {"id": n, "method": "...", "params": {...}}
  out: {"id": n, "result": {...}} or {"id": n, "error": "..."}, and notifications {"method": "...", "params": {...}}
Methods: hello, models, open, query, interrupt, permission, set_mode, set_model, close.
Notifications: message (an SDK message, serialised), permission (a can_use_tool request waiting for an answer),
closed (a client's process ended), log.

One ClaudeSDKClient per driven session (key = the session uuid). The client starts the official Claude Code binary, which
signs itself in; nothing here reads, stores or forwards credentials: the environment this process got from server.py is
passed on as it is (server.py drops API-key variables unless the API-key route is switched on).
"""
import asyncio
import atexit
import dataclasses
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import threading
import uuid as uuidlib

import claude_agent_sdk as sdk
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultAllow, PermissionResultDeny

OUT = sys.stdout
OUT_LOCK = threading.Lock()
WRAP = Path(__file__).resolve().parent / 'claude_wrap.py'


# robust (2026-11-21, ported from ide-providers-2026-11-09 5f5b8903f): the CLI runs through claude_wrap.py, in a process group of its
# own that ends when this worker is gone (killed or crashed), so a later resume never finds the old CLI still writing the same
# transcript. The SDK takes one executable path, so the path given is a two-line sh launcher. AGENT_IDE_CLAUDE_WRAP=off: the CLI as is.
def bundled_cli():
    p = Path(sdk.__file__).resolve().parent / '_bundled' / 'claude'
    return str(p) if p.is_file() else None


_LAUNCHERS = {}


def launcher(real):
    if not real or os.environ.get('AGENT_IDE_CLAUDE_WRAP') == 'off' or not WRAP.is_file():
        return real
    if real not in _LAUNCHERS:
        d = Path(tempfile.mkdtemp(prefix='agent-ide-claude-'))
        f = d / 'claude'
        f.write_text('#!/bin/sh\nexec %s -I %s %s "$@"\n' % (shlex.quote(sys.executable), shlex.quote(str(WRAP)), shlex.quote(real)))
        f.chmod(0o700)
        atexit.register(shutil.rmtree, str(d), True)
        _LAUNCHERS[real] = str(f)
    return _LAUNCHERS[real]


def emit(msg):
    line = json.dumps(msg, default=str)
    with OUT_LOCK:
        OUT.write(line + '\n')
        OUT.flush()


def plain(obj):
    """An SDK message as JSON-ready data, with its class name as `type`."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        d = {f.name: plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        d['_type'] = type(obj).__name__
        return d
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


async def keep_open(input_data, tool_use_id, context):
    """The Python SDK's documented pairing for can_use_tool: a PreToolUse hook that lets every call continue, so the
    stream stays open while a permission request waits."""
    return {'continue_': True}


class Session:
    def __init__(self, worker, key, params):
        self.w, self.key, self.p = worker, key, params
        self.client = None
        self.reader = None
        self.waiting = {}          # request id -> asyncio.Future of the answer

    def options(self):
        p = self.p
        env = dict(p.get('env') or {})
        env['AGENT_IDE_WORKER_PID'] = str(os.getpid())          # robust: what claude_wrap.py watches
        kw = dict(cwd=p['cwd'], permission_mode=p.get('permissionMode') or 'default', can_use_tool=self.ask,
                  include_partial_messages=True,
                  hooks={'PreToolUse': [HookMatcher(matcher=None, hooks=[keep_open])]},
                  setting_sources=p.get('settingSources') or ['user', 'project', 'local'], env=env)
        if p.get('model'):
            kw['model'] = p['model']
        if p.get('effort'):
            kw['effort'] = p['effort']
        cli = launcher(p.get('cli') or bundled_cli())          # robust: the CLI in its own group, ended with this worker
        if cli:
            kw['cli_path'] = cli
        if p.get('resume'):
            kw['resume'] = p['resume']
            if p.get('fork'):
                kw['fork_session'] = True
                if p.get('sessionId'):
                    kw['session_id'] = p['sessionId']
        elif p.get('sessionId'):
            kw['session_id'] = p['sessionId']
        kw['stderr'] = lambda line: emit({'method': 'log', 'params': {'key': self.key, 'line': str(line)[:2000]}})
        return ClaudeAgentOptions(**kw)

    async def ask(self, tool_name, tool_input, context):
        rid = 'claude-' + uuidlib.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self.waiting[rid] = fut
        emit({'method': 'permission', 'params': {'key': self.key, 'requestId': rid, 'toolName': tool_name, 'input': plain(tool_input),
                                                 'toolUseId': getattr(context, 'tool_use_id', None), 'agentId': getattr(context, 'agent_id', None),
                                                 'blockedPath': getattr(context, 'blocked_path', None), 'reason': getattr(context, 'decision_reason', None),
                                                 'title': getattr(context, 'title', None), 'description': getattr(context, 'description', None),
                                                 'suggestions': [u.to_dict() for u in getattr(context, 'suggestions', None) or [] if hasattr(u, 'to_dict')]}})          # piece B: Claude Code's own "don't ask again" (data, not secrets)
        try:
            a = await fut
        finally:
            self.waiting.pop(rid, None)
        if a.get('allow'):
            ups = [sdk.PermissionUpdate.from_dict(u) for u in a.get('updatedPermissions') or [] if isinstance(u, dict) and u.get('type')]          # piece B: rules and directories too (Claude Code's suggestions), not only a mode
            return PermissionResultAllow(updated_input=a.get('updatedInput') if isinstance(a.get('updatedInput'), dict) else tool_input, updated_permissions=ups or None)
        return PermissionResultDeny(message=a.get('message') or 'The user declined this.', interrupt=bool(a.get('interrupt')))

    async def open(self):
        self.client = ClaudeSDKClient(options=self.options())
        await self.client.connect()
        info = await self.client.get_server_info()
        self.reader = asyncio.create_task(self.read())
        return {'models': (info or {}).get('models'), 'cli': self.p.get('cli')}

    async def read(self):
        try:
            async for m in self.client.receive_messages():
                emit({'method': 'message', 'params': {'key': self.key, 'message': plain(m)}})
        except Exception as e:          # the process ended or the stream broke: the server marks the session idle
            emit({'method': 'log', 'params': {'key': self.key, 'line': 'stream ended: ' + repr(e)[:500]}})
        finally:
            for f in self.waiting.values():
                if not f.done():
                    f.set_result({'allow': False, 'message': 'The session closed.'})
            emit({'method': 'closed', 'params': {'key': self.key}})
            self.w.sessions.pop(self.key, None)

    async def close(self):
        try:
            await self.client.disconnect()
        finally:
            if self.reader:
                self.reader.cancel()


class Worker:
    def __init__(self):
        self.sessions = {}

    async def call(self, method, p):
        if method == 'hello':
            return {'sdk': getattr(sdk, '__version__', None), 'python': sys.version.split()[0]}
        if method == 'models':          # the SDK's model list: a client is connected (no message sent) and its initialize answer read
            probe = Session(self, '_models', {'cwd': p['cwd'], 'cli': p.get('cli'), 'env': p.get('env'), 'settingSources': ['user']})
            probe.client = ClaudeSDKClient(options=probe.options())
            await probe.client.connect()
            try:
                info = await probe.client.get_server_info()
            finally:
                await probe.client.disconnect()
            return {'models': (info or {}).get('models') or []}
        if method == 'open':
            key = p['key']
            if key in self.sessions:
                raise ValueError('This session is already open here.')
            s = Session(self, key, p)
            self.sessions[key] = s
            try:
                return await s.open()
            except Exception:
                self.sessions.pop(key, None)
                raise
        s = self.sessions.get(p.get('key'))
        if not s:
            raise ValueError('This session is not open here.')
        if method == 'query':
            await s.client.query(p['text'])
            return {}
        if method == 'interrupt':
            await s.client.interrupt()
            return {}
        if method == 'permission':
            f = s.waiting.get(p['requestId'])
            if not f or f.done():
                raise ValueError('This request has already been resolved.')
            f.set_result(p)
            return {}
        if method == 'set_mode':
            await s.client.set_permission_mode(p['mode'])
            return {}
        if method == 'set_model':
            await s.client.set_model(p.get('model'))
            return {}
        if method == 'close':
            await s.close()
            return {}
        raise ValueError('Unknown method ' + str(method))

    async def handle(self, msg):
        try:
            result = await self.call(msg.get('method'), msg.get('params') or {})
            emit({'id': msg.get('id'), 'result': result})
        except Exception as e:
            emit({'id': msg.get('id'), 'error': (type(e).__name__ + ': ' + str(e))[:2000]})

    async def main(self):
        loop = asyncio.get_running_loop()
        lines = asyncio.Queue()

        def pump():          # stdin in a thread: asyncio's pipe readers are fussy across platforms
            for line in sys.stdin:
                loop.call_soon_threadsafe(lines.put_nowait, line)
            loop.call_soon_threadsafe(lines.put_nowait, None)
        threading.Thread(target=pump, daemon=True).start()
        emit({'method': 'ready', 'params': {'sdk': getattr(sdk, '__version__', None)}})
        while True:
            line = await lines.get()
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            asyncio.create_task(self.handle(msg))
        for s in list(self.sessions.values()):
            try:
                await s.close()
            except Exception:
                pass


if __name__ == '__main__':
    asyncio.run(Worker().main())
