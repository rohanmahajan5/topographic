"""Plan progress: the plan an agent keeps while it works, read from what its harness records, as one shape for every harness.

A history carries `plan`: the plan's states over time, [{t, steps: [{text, status}]}], status pending / in_progress /
completed, one state per update, each the whole list as it stood then (so the replay shows the step it was on at any moment).
Its reader keeps `planState` (the list as it stands, Claude Code's task ids) between incremental reads.

Where each harness records it (agent-ide/concepts/round-4/plan-findings.md has the sources):
- Codex: the model's `update_plan` call, saved in the rollout as a response_item of type function_call, its `arguments` a JSON
  string {explanation?, plan: [{step, status: pending | in_progress | completed}]}, the whole list each call. (The app-server's
  live `turn/plan/updated` notification carries the same list with status `inProgress`; the rollout gets the call as soon as it is
  written, so the floor reads that.)
  In code mode (gpt-6-luna on codex-cli 0.167, where the tool is off unless `tools.update_plan.enabled`), the call is
  `tools.update_plan({...})` inside the JavaScript of a custom_tool_call named `exec`, read out of the script (code_mode_plans).
- Claude Code: TodoWrite {todos: [{content, status, activeForm}]}, the whole list; the task tools TaskCreate {subject,
  activeForm?} (its id comes back in the result, "Task #<id> created successfully") and TaskUpdate {taskId, status? (deleted
  removes it), subject?}, one change each.
- The Claude desktop app's sessions: mcp__ccd_turn__update_plan {items: [{content, status}], scope} (the whole list) or
  {advance: true} (the step in progress done, the next pending one started) or {done: [i], start: j} (1-based); and
  mcp__ccd_turn__wrap_up, which checks the list off.
A call the harness refused (its result an error, as when a hook stops a helper agent's plan call) changes nothing: the list goes
back to what it was before the call.
"""
import json
import re

DESK = 'mcp__ccd_turn__update_plan'
WRAP = 'mcp__ccd_turn__wrap_up'
CLAUDE_TOOLS = ('TodoWrite', 'TaskCreate', 'TaskUpdate', DESK, WRAP)
MAX_STATES = 200
MAX_UNDO = 50          # refused calls: the lists before the calls still waiting for their results
CREATED = re.compile(r'Task #(\S+) created successfully')


def status_of(s):
    s = str(s or 'pending')
    if s in ('in_progress', 'inProgress', 'in-progress'):
        return 'in_progress'
    return s if s in ('pending', 'completed') else 'pending'


def fresh():
    return {'steps': [], 'tasks': {}, 'undo': {}}


def _state(c):
    st = c.get('planState')
    if st is None:
        st = c['planState'] = fresh()
    if c.get('plan') is None:
        c['plan'] = []
    return st


def _push(c, t):
    st = c['planState']
    states = c['plan']
    snap = {'t': t, 'steps': [dict(s) for s in st['steps'] if s['status'] != 'deleted']}          # a deleted task keeps its place (ids point at places), out of the list
    if states and states[-1]['steps'] == snap['steps']:
        return          # nothing changed (a repeated call, wrap_up on a list already done)
    states.append(snap)
    if len(states) > MAX_STATES:
        del states[:len(states) - MAX_STATES]


def _whole(c, t, pairs):
    st = _state(c)
    st['steps'] = [{'text': str(x).strip(), 'status': status_of(s)} for x, s in pairs if x and str(x).strip()]
    st['tasks'] = {}
    _push(c, t)


def codex_call(c, t, name, args, call_id=None):
    """A Codex function call (its arguments as recorded: the JSON text, or the script of code mode's `exec`); True when it
    carried the plan's. In a script, a plan call made before any other tool call counts at once; one made after another tool
    call waits for the script's output record (codex_output), since it ran only once those calls had finished."""
    if name == CODE_MODE:
        found = code_mode_plans(args if isinstance(args, str) else '')
        if not found:
            return False
        for a, after in found:
            if after and call_id:
                st = _state(c)
                later = st.setdefault('later', {})
                later.setdefault(str(call_id), []).append(a)
                while len(later) > MAX_UNDO:
                    later.pop(next(iter(later)))
            else:
                codex_call(c, t, 'update_plan', a)
        return True
    if name != 'update_plan':
        return False
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return True
    if isinstance(args, dict) and isinstance(args.get('plan'), list):
        _whole(c, t, [(p.get('step'), p.get('status')) for p in args['plan'] if isinstance(p, dict)])
    return True


def codex_output(c, t, call_id):
    """A Codex tool call's output record: a code-mode script's plan calls made after its other tool calls count now."""
    st = c.get('planState')
    for a in ((st or {}).get('later') or {}).pop(str(call_id), []):
        codex_call(c, t, 'update_plan', a)


def codex_live(c, t, params):
    """The app-server's turn/plan/updated notification (same list, status inProgress)."""
    if isinstance(params, dict) and isinstance(params.get('plan'), list):
        _whole(c, t, [(p.get('step'), p.get('status')) for p in params['plan'] if isinstance(p, dict)])


def claude_use(c, t, name, inp, use_id):
    """A Claude Code tool call; True when it was the plan's."""
    if name not in CLAUDE_TOOLS:
        return False
    inp = inp if isinstance(inp, dict) else {}
    st = _state(c)
    undo = st.setdefault('undo', {})
    undo[str(use_id)] = (len(c['plan']), [dict(s) for s in st['steps']], dict(st['tasks']))          # put back if the call is refused
    while len(undo) > MAX_UNDO:
        undo.pop(next(iter(undo)))
    if name == 'TodoWrite':
        if isinstance(inp.get('todos'), list):
            _whole(c, t, [(p.get('content'), p.get('status')) for p in inp['todos'] if isinstance(p, dict)])
        return True
    if name == DESK:
        if isinstance(inp.get('items'), list):
            _whole(c, t, [(p.get('content'), p.get('status')) for p in inp['items'] if isinstance(p, dict)])
            return True
        steps = st['steps']
        if not steps:
            return True
        if inp.get('advance'):
            cur = next((s for s in steps if s['status'] == 'in_progress'), None)
            if cur:
                cur['status'] = 'completed'
            nxt = next((s for s in steps if s['status'] == 'pending'), None)
            if nxt:
                nxt['status'] = 'in_progress'
        for i in inp.get('done') or []:
            if isinstance(i, int) and 0 < i <= len(steps):
                steps[i - 1]['status'] = 'completed'
        j = inp.get('start')
        if isinstance(j, int) and 0 < j <= len(steps):
            for s in steps:
                if s['status'] == 'in_progress':
                    s['status'] = 'completed'
            steps[j - 1]['status'] = 'in_progress'
        _push(c, t)
        return True
    if name == WRAP:
        if st['steps']:
            for s in st['steps']:
                s['status'] = 'completed'
            _push(c, t)
        return True
    if name == 'TaskCreate':
        step = {'text': str(inp.get('subject') or inp.get('description') or 'task'), 'status': 'pending'}
        st['steps'].append(step)
        st['tasks']['use:' + str(use_id)] = len(st['steps']) - 1
        _push(c, t)
        return True
    # TaskUpdate
    i = st['tasks'].get(str(inp.get('taskId')))
    if i is None or i >= len(st['steps']):
        return True
    if inp.get('status') == 'deleted':
        st['steps'][i]['status'] = 'deleted'
    elif inp.get('status'):
        st['steps'][i]['status'] = status_of(inp['status'])
    if inp.get('subject'):
        st['steps'][i]['text'] = str(inp['subject'])
    _push(c, t)
    return True


def claude_result(c, use_id, text, error=False):
    """A tool result: a refused plan call is undone; a TaskCreate's carries the id later updates name."""
    st = c.get('planState')
    if not st:
        return
    before = st.setdefault('undo', {}).pop(str(use_id), None)
    if before and error:
        n, steps, tasks = before
        del c['plan'][n:]
        st['steps'], st['tasks'] = steps, tasks
        return
    i = st['tasks'].get('use:' + str(use_id))
    if i is None:
        return
    m = CREATED.search(text or '')
    if m:
        st['tasks'][m.group(1)] = i


def relative(states, origin):
    """The canvas's shape: times in seconds after the floor's origin."""
    return [{'t': max(0, (s['t'] or origin) - origin), 'steps': s['steps']} for s in states or []]


# ---- Codex's code mode: the model writes one JavaScript script per step, its tools called inside it ----
# Measured on a real run (codex-cli 0.167, gpt-6-luna, `-c tools.update_plan.enabled=true`, 2026-11-09): the rollout records a
# custom_tool_call named `exec` whose `input` is the script, e.g. `const p = await tools.update_plan({plan:[{step:"…",status:
# "in_progress"}, …]});` then `tools.exec_command(…)`, and a later script may update the plan twice (before and after its
# commands). No record of its own carries the plan, so the floor reads the calls out of the script, in order. Only a literal
# argument is read (a plan built from variables is skipped); text inside the script's strings and comments is never taken for a call.
CODE_MODE = 'exec'
_CALL = re.compile(r'tools\s*(?:\.\s*update_plan|\[\s*(["\'])update_plan\1\s*\])\s*\(')
_ANY = re.compile(r'tools\s*(?:\.\s*[A-Za-z_$][\w$]*|\[\s*(["\'])[^"\']+\1\s*\])\s*\(')          # any tool call in the script
_IDENT = re.compile(r'[A-Za-z_$][\w$]*')
_NUM = re.compile(r'-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')
_ESC = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0'}


class _NotLiteral(Exception):
    pass


def code_mode_plans(src):
    """Every `tools.update_plan(...)` call in a code-mode script whose argument is a literal, in order: [(its argument, whether
    another tool call comes before it in the script)]."""
    out, i, n, other = [], 0, len(src), False
    while i < n:
        ch = src[i]
        starts = ch == 't' and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] in '_$.'))
        if ch in '"\'`':
            i = _skip_string(src, i)
        elif src.startswith('//', i) or src.startswith('/*', i):
            i = _ws(src, i)
        elif starts and (m := _CALL.match(src, i)):
            i = m.end()
            try:
                val, j = _value(src, _ws(src, i))
                if src[_ws(src, j)] not in '),':
                    raise _NotLiteral
                out.append((val, other))
                i = j
            except (_NotLiteral, IndexError, ValueError):
                pass
        elif starts and (m := _ANY.match(src, i)):
            other = True
            i = m.end()
        else:
            i += 1
    return out


def _skip_string(src, i):
    q, i = src[i], i + 1
    while i < len(src) and src[i] != q:
        i += 2 if src[i] == '\\' else 1
    return i + 1


def _ws(src, i):
    while i < len(src):
        if src[i].isspace():
            i += 1
        elif src.startswith('//', i):
            j = src.find('\n', i)
            i = len(src) if j < 0 else j
        elif src.startswith('/*', i):
            j = src.find('*/', i + 2)
            i = len(src) if j < 0 else j + 2
        else:
            break
    return i


def _string(src, i):
    q, i, s = src[i], i + 1, []
    while src[i] != q:
        ch = src[i]
        if q == '`' and src.startswith('${', i):
            raise _NotLiteral          # a template with a value in it is not a literal
        if ch == '\\':
            e = src[i + 1]
            if e == 'u' and src[i + 2] == '{':
                j = src.index('}', i)
                s.append(chr(int(src[i + 3:j], 16)))
                i = j + 1
            elif e == 'u':
                s.append(chr(int(src[i + 2:i + 6], 16)))
                i += 6
            elif e == 'x':
                s.append(chr(int(src[i + 2:i + 4], 16)))
                i += 4
            else:
                if e != '\n':          # a backslash before a newline continues the line
                    s.append(_ESC.get(e, e))
                i += 2
            continue
        s.append(ch)
        i += 1
    return ''.join(s), i + 1


def _value(src, i):
    """A JavaScript literal (object, array, string, number, true / false / null) at i: (value, the index after it)."""
    ch = src[i]
    if ch in '{[':
        close, obj, arr = '}' if ch == '{' else ']', {}, []
        i = _ws(src, i + 1)
        while src[i] != close:
            if ch == '{':
                if src[i] in '"\'':
                    key, i = _string(src, i)
                else:
                    m = _IDENT.match(src, i) or _NUM.match(src, i)
                    if not m:
                        raise _NotLiteral
                    key, i = m.group(0), m.end()
                i = _ws(src, i)
                if src[i] != ':':
                    raise _NotLiteral          # a shorthand property ({plan}) names a variable
                obj[key], i = _value(src, _ws(src, i + 1))
            else:
                v, i = _value(src, i)
                arr.append(v)
            i = _ws(src, i)
            if src[i] == ',':
                i = _ws(src, i + 1)
            elif src[i] != close:
                raise _NotLiteral
        return (obj if ch == '{' else arr), i + 1
    if ch in '"\'`':
        return _string(src, i)
    m = _IDENT.match(src, i)
    if m:
        word = {'true': True, 'false': False, 'null': None, 'undefined': None}
        if m.group(0) not in word:
            raise _NotLiteral          # a variable or a call
        return word[m.group(0)], m.end()
    m = _NUM.match(src, i)
    if m:
        return float(m.group(0)), m.end()
    raise _NotLiteral
