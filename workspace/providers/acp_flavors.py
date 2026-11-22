"""One record per agent spoken to over the Agent Client Protocol (providers/acp.py).

The generic client never tests an agent's name: everything that differs between agents is a field here, with a generic
default. Every non-default field below names the agent version it was seen on and where the reason is written down
(docs/providers-study/acp.md, "acp.md §n"; T3 Code's flavor records follow the same rule, acp.md §16).
"""
from dataclasses import dataclass, field
import calendar
import glob
import os
import re
import shutil
import sqlite3
import time

from . import planuse

AUTO = '@auto'          # a page mode that no agent mode matches: we answer the agent's permission requests ourselves (acp.md §5)
LAUNCH = '@launch'      # a page mode the agent takes only as a launch argument (launch_args): fixed for the process's life


@dataclass
class Flavor:
    name: str                                   # the id on every session: '<name>:<agent session id>'
    label: str                                  # how the page names it
    binary: str                                 # the program, looked up on PATH and in `search`
    args: tuple = ('acp',)                      # arguments that start its ACP server on stdio
    search: tuple = ()                          # extra places to look for the binary (globs; ~ expanded)
    install_hint: str = ''                      # shown by available() when the binary is missing
    auth_hint: str = ''                         # what the person runs themselves when the agent asks for sign-in
    client_name: str = 'agent-ide'              # clientInfo.name sent in initialize
    launch_args: dict = field(default_factory=dict)       # page mode -> extra arguments before `args` (a mode fixed at launch)
    # page mode ('read-only' | 'default' | 'full-access', server.PRESETS) -> the agent's own mode id, or AUTO. A mode the agent
    # does not advertise at runtime is never sent (acp.md §8).
    page_modes: dict = field(default_factory=lambda: {'default': 'default', 'full-access': AUTO})
    refused_modes: tuple = ()                   # agent mode ids we never set, even when advertised
    stdout_noise_prefixes: tuple = ()           # stdout lines that are not protocol frames (kept as notes, not fatal)
    question_prefix: str = ''                   # a permission request whose toolCallId starts with this is a question
    option_warning_meta: str = ''               # a permission option's _meta key holding a warning shown under its button
    cancel_timeout: float = 15.0                # seconds a stopped turn may take to settle before the process is killed (acp.md §4)
    recancel_after: float = 2.0                 # cancel sent again after this long when no update came since the prompt (acp.md §4: a guess)
    inactivity_timeout: float = 0.0             # seconds without output before a prompt is failed; 0 = never (acp.md §13)
    prefer_resume: bool = True                  # session/resume over session/load when both exist: we keep our own record (acp.md §6)
    per_turn_usage: bool = False                # read PromptResponse.usage (semantics unproven per agent, acp.md §11)
    load_idle: float = 2.0                      # session/load counts as done after this long without replay traffic (acp.md §6)
    load_bound: float = 90.0                    # ... and at the latest after this long
    # (key, turn) -> None or {'class', 'resetsAt', 'message'}: a turn the agent ended normally that in fact failed (a plan
    # limit some agents end with a plain end_turn); turn = {'text': the turn's assistant text, 'tools': its tool calls}
    silent_failure: object = None
    # the agent's own sign-in command (arguments after the binary), run by "Sign in" on the page with its browser launch caught
    # (BROWSER=/bin/echo, NO_OPEN_BROWSER=1) so the page opens the link it prints; () = none, the person runs auth_hint (acp.md §7)
    login_args: tuple = ()
    login_opens_browser: bool = False           # its sign-in opens the browser itself, whatever BROWSER says: the page opens no second tab
    # a regex for its sign-in asking for a pasted code (Google's out-of-band flow): the page then shows a box for the code,
    # which is written to the sign-in's stdin and nowhere else; '' = its sign-in finishes in the browser alone
    login_code_prompt: str = ''
    # a read-only command that reports the sign-in (arguments after the binary) and a regex its output matches when signed in;
    # () = none, and the page knows only what a start here showed
    status_args: tuple = ()
    status_ok: str = ''
    # subagents (acp.md §14: ACP has no lineage contract, so each agent needs its own): parent key -> [child {key, toolCallId,
    # role, type, prompt, started, updated, running}], and child key -> {messages, running}; the children are shown read only
    subagents: object = None
    subagent_history: object = None
    # its plan figures for the usage panel (providers/planuse.py): (provider) -> a plan reading, read in the background; None = the
    # agent exposes none, and the panel says so
    plan_reader: object = None

    def find(self):
        """The binary's full path, or None."""
        p = shutil.which(self.binary)
        if p:
            return p
        for pat in self.search:
            hits = sorted(glob.glob(os.path.expanduser(pat)))
            if hits:
                return hits[-1]
        return None

    def argv(self, binary, mode=None):
        return [binary, *self.launch_args.get(mode, ()), *self.args]


# ---------- turns an agent ends as if they went well (acp.md §13) ----------
def _resets_at(text, now=None):
    """When a plan limit lifts, from the agent's own error text: an RFC 3339 quotaResetTimeStamp, else "Resets in 4h29m39s",
    else a retryDelay in seconds."""
    now = time.time() if now is None else now
    m = re.search(r'"quotaResetTimeStamp"\s*:\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z"', text)
    if m:
        return float(calendar.timegm(time.strptime(m.group(1), '%Y-%m-%dT%H:%M:%S')))
    m = re.search(r'[Rr]esets in ((?:\d+h)?(?:\d+m)?(?:\d+(?:\.\d+)?s)?)', text)
    if m and m.group(1):
        parts = dict((u, float(v)) for v, u in re.findall(r'(\d+(?:\.\d+)?)([hms])', m.group(1)))
        return now + parts.get('h', 0) * 3600 + parts.get('m', 0) * 60 + parts.get('s', 0)
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text)
    return now + float(m.group(1)) if m else None


ANTIGRAVITY_STORES = ('~/.gemini/antigravity-cli/conversations', '~/.gemini/antigravity/conversations')


def antigravity_quota(key, turn, stores=ANTIGRAVITY_STORES, now=None):
    """Antigravity ends a quota-failed turn with no ACP error (T3 7c22e24e3, AntigravityTurnOutcome.ts): its own step log,
    conversations/<session id>.db, ends with an error step (step_type 17) whose payload carries RESOURCE_EXHAUSTED /
    QUOTA_EXHAUSTED and when the quota resets. Seen in ten conversations on this Mac (agy and the app, 2026-09/11)."""
    for base in stores:
        path = os.path.join(os.path.expanduser(base), '%s.db' % key)
        if os.path.exists(path):
            break
    else:
        return None
    try:
        c = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=2)
        try:
            row = c.execute('select step_type, step_payload, error_details from steps order by idx desc limit 1').fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    if not row or row[0] != 17:
        return None
    text = b' '.join(b for b in row[1:] if isinstance(b, (bytes, bytearray))).decode('utf-8', 'replace')
    if 'QUOTA_EXHAUSTED' not in text and 'RESOURCE_EXHAUSTED' not in text:
        return None
    m = re.search(r'"message"\s*:\s*"([^"]{5,300})"', text) or re.search(r'RESOURCE_EXHAUSTED \(code 429\): ([^\x00-\x1f]{5,300}?)(?:\.\s*Resets|$)', text)
    msg = m.group(1) if m else 'Antigravity reached its plan limit.'
    return {'class': 'usage_limit', 'resetsAt': _resets_at(text, now), 'message': msg}


def cursor_plan_refusal(key, turn, now=None):
    """Cursor answers a turn its plan does not cover with the single line "Upgrade your plan to continue" and a normal
    end_turn (seen live 2026-11-09, cursor-agent 2026.11.05, model composer-2.5 on this account): no tools ran and that
    line is the whole answer. A turn that did work and mentions the words is not matched."""
    text = (turn.get('text') or '').strip()
    if turn.get('tools') or not re.fullmatch(r'(?i)upgrade your plan to continue\.?', text):
        return None
    return {'class': 'usage_limit', 'resetsAt': None, 'message': 'Cursor refused this turn: "%s" (the plan does not cover this model or its limit was reached).' % text}


# ---------- Antigravity's subagents, from its own files (acp.md §14) ----------
ANTIGRAVITY_BRAINS = ('~/.gemini/antigravity-cli/brain', '~/.gemini/antigravity/brain')
_CHILD_CACHE = {}          # parent key -> (when, children): the brain folders are read at most every 5 s per parent


def _iso(t):
    try:
        return float(calendar.timegm(time.strptime(str(t)[:19], '%Y-%m-%dT%H:%M:%S')))
    except (ValueError, TypeError):
        return None


def antigravity_subagents(parent, roots=ANTIGRAVITY_BRAINS, since=None):
    """invoke_subagent starts a child conversation; the child's first message, brain/<child>/.system_generated/messages/*.json,
    has sender = the parent's session id, renderDetails "Message from Parent Agent" and sourceMetadata.tool.toolCall = the
    parent's invoke_subagent call (id, argumentsJson {Prompt, Role, TypeName}). Seen live 2026-11-09, agy 1.5.3. T3 shows
    Antigravity's children as read-only batches because ACP gives no child ids (AntigravityAdapterV2.ts:97); the files do."""
    hit = _CHILD_CACHE.get((parent, roots))
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    out = []
    for root in roots:
        base = os.path.expanduser(root)
        for d in glob.glob(os.path.join(base, '*')):
            try:
                if since and os.stat(d).st_mtime < since - 60:
                    continue
            except OSError:
                continue
            for f in glob.glob(os.path.join(d, '.system_generated', 'messages', '*.json')):
                try:
                    with open(f, encoding='utf-8') as fh:
                        m = __import__('json').load(fh)
                except (OSError, ValueError):
                    continue
                if m.get('sender') != parent or m.get('recipient') != os.path.basename(d):
                    continue
                call = ((m.get('sourceMetadata') or {}).get('tool') or {}).get('toolCall') or {}
                try:
                    args = __import__('json').loads(call.get('argumentsJson') or '{}')
                except ValueError:
                    args = {}
                log = os.path.join(d, '.system_generated', 'logs', 'transcript_full.jsonl')
                try:
                    updated = os.stat(log).st_mtime
                except OSError:
                    updated = None
                h = antigravity_subagent_history(os.path.basename(d), roots=(root,))
                out.append({'key': os.path.basename(d), 'toolCallId': call.get('id'), 'role': args.get('Role'), 'type': args.get('TypeName'),
                            'prompt': args.get('Prompt') or m.get('content'), 'started': _iso(m.get('timestamp')), 'updated': updated,
                            'running': h['running'] if h else False})
                break
    out.sort(key=lambda c: c.get('started') or 0)
    _CHILD_CACHE[(parent, roots)] = (time.time(), out)
    return out


def antigravity_subagent_history(child, roots=ANTIGRAVITY_BRAINS, now=None):
    """A child conversation in the page's history shape, from brain/<child>/.system_generated/logs/transcript_full.jsonl: its
    first message (the parent's prompt) as the user's, PLANNER_RESPONSE content as the agent's text, its tool_calls as tools,
    thinking as reasoning. Running while its last step is not a final answer (a response with no tool calls) and the log
    changed in the last 2 minutes."""
    import json as _json
    for root in roots:
        d = os.path.join(os.path.expanduser(root), child, '.system_generated')
        log = os.path.join(d, 'logs', 'transcript_full.jsonl')
        if os.path.exists(log):
            break
    else:
        return None
    msgs, reasoning, last = [], [], None
    for f in sorted(glob.glob(os.path.join(d, 'messages', '*.json'))):
        try:
            with open(f, encoding='utf-8') as fh:
                m = _json.load(fh)
        except (OSError, ValueError):
            continue
        if m.get('recipient') == child:
            msgs.append({'role': 'user', 'text': m.get('content') or '', 't': _iso(m.get('timestamp')), 'id': 'msg:' + str(m.get('id')), 'tokens': 0})
    with open(log, encoding='utf-8', errors='replace') as fh:
        for i, line in enumerate(fh):
            try:
                st = _json.loads(line)
            except ValueError:
                continue
            t, last = _iso(st.get('created_at')), st
            if st.get('type') != 'PLANNER_RESPONSE':
                continue
            if st.get('thinking'):
                reasoning.append({'text': st['thinking'], 't': t, 'end': t, 'id': 'thought:%d' % i})
            for j, call in enumerate(st.get('tool_calls') or []):
                a = call.get('args') or {}
                what = next((str(a[k]) for k in ('CommandLine', 'Path', 'AbsolutePath', 'Message', 'Prompt', 'Query') if a.get(k)), '')
                msgs.append({'role': 'tool', 'text': '', 'tool': ('%s %s' % (call.get('name'), what[:160])).strip(), 'kind': 'other',
                             'status': 'completed', 't': t, 'id': 'tool:%d:%d' % (i, j), 'tokens': 0})
            if st.get('content'):
                msgs.append({'role': 'assistant', 'text': st['content'], 't': t, 'id': 'step:%d' % i, 'tokens': int(st.get('output_tokens') or 0)})
    final = bool(last and last.get('type') == 'PLANNER_RESPONSE' and not last.get('tool_calls'))
    try:
        fresh = (now or time.time()) - os.stat(log).st_mtime < 120
    except OSError:
        fresh = False
    msgs.sort(key=lambda m: m.get('t') or 0)
    return {'messages': msgs, 'reasoning': reasoning, 'running': not final and fresh}


GENERIC = Flavor(name='acp', label='ACP agent', binary='acp-agent')


ANTIGRAVITY = Flavor(
    name='antigravity', label='Antigravity', binary='agy',
    # agy 1.5.3 installs to ~/.local/bin, which a server started from the Finder may not have on PATH (acp.md Part C).
    search=('~/.local/bin/agy', '/opt/homebrew/bin/agy', '/usr/local/bin/agy'),
    install_hint='Install the Antigravity CLI so `agy` is on PATH (https://antigravity.google).',
    # `agy acp --help` (1.5.3): "--login  Log in with Google interactively, then exit (ACP terminal auth)" (acp.md §7).
    auth_hint='Run `agy acp --login` in a terminal to sign in, then start the session again.',
    # agy 1.5.3 advertises modes default / accept-edits / plan (measured here 2026-11-09 with session/new), and also auto
    # (2026-11-22, the look ahead's session/new: fixtures/acp/recorded/antigravity/agy-1.5.3-offer.json); none is read-only.
    # Full access is answered by us from kind + path containment, the agent staying in `default` (acp.md Part C, §5).
    page_modes={'default': 'default', 'full-access': AUTO},
    # "The agy CLI rejects the managed runtime's yolo and auto_edit. It stays in default" (T3 fix 51675e047, acp.md Part C).
    refused_modes=('yolo', 'auto_edit'),
    # Antigravity writes "Open the following link to authenticate the ACP server: <url>" on stdout, against the transport
    # rule (acp.md §3, T3 antigravityAuthSupport.ts:525).
    stdout_noise_prefixes=('Open the following link to authenticate the ACP server',),
    # Antigravity's questions arrive as session/request_permission with toolCallId interaction_* (acp.md §5,
    # T3 AntigravityProtocol.ts:49).
    question_prefix='interaction_',
    # Its allow_always option can carry _meta["agy.security.warning"] {title, message} (acp.md §5, T3 AntigravityProtocol.ts).
    option_warning_meta='agy.security.warning',
    # "Antigravity can leave a prompt open without updates; long tools and approvals pause this" (acp.md §13, T3 3f905cac2).
    inactivity_timeout=15 * 60,
    silent_failure=antigravity_quota,
    login_args=('acp', '--login'),          # `agy acp --help` 1.5.3: "--login  Log in with Google interactively, then exit"
    # agy 1.5.3, seen 2026-11-09: opened the browser with BROWSER=/bin/echo and NO_OPEN_BROWSER=1 set, printed the link, then
    # "Then paste the authorization code, or the full URL of the page you land on, and press Enter:"; with no input it
    # ends "login cancelled: no authorization code was entered"
    login_opens_browser=True,
    login_code_prompt=r'paste the authorization code',
    subagents=antigravity_subagents,
    subagent_history=antigravity_subagent_history,
    plan_reader=planuse.antigravity_plan,          # `agy -p /usage --output-format json` (agy 1.1.11+, CodexBar's source too)
)


CURSOR = Flavor(
    name='cursor', label='Cursor Agent', binary='cursor-agent',
    search=('~/.local/bin/cursor-agent', '/opt/homebrew/bin/cursor-agent', '/usr/local/bin/cursor-agent'),
    # Not installed here: the Cursor app's shim installs the agent CLI from https://cursor.com/install (acp.md Part C).
    install_hint='Install the Cursor agent CLI (https://cursor.com/install) so `cursor-agent` is on PATH.',
    auth_hint='Run `cursor-agent login` in a terminal, or set CURSOR_API_KEY, then start the session again.',
    # T3's pre-2026-10-02 CursorAcpSupport.ts fixed the permission mode at launch: `cursor-agent [--auto-review | --force] acp`
    # (acp.md Part C). Unverified here (not installed).
    launch_args={'full-access': ('--force',)},
    page_modes={'default': LAUNCH, 'full-access': LAUNCH},
    silent_failure=cursor_plan_refusal,
    login_args=('login',),          # `cursor-agent --help` 2026.11.05: "login  Authenticate with Cursor. Set NO_OPEN_BROWSER to disable browser opening."
    # `cursor-agent status` 2026.11.05, seen 2026-11-09: "✓ Logged in as <email>"; reads only, opens nothing
    status_args=('status',),
    status_ok=r'logged in as',
    plan_reader=planuse.cursor_plan,          # cursor.com/api/usage-summary with the sign-in Cursor keeps here (CodexBar's source)
)


FLAVORS = (ANTIGRAVITY, CURSOR)          # Gemini CLI removed 2026-11-22 (retired, does not work)
