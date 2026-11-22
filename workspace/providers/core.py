"""What every provider shares: the capability keys, a session's phase, how a message is delivered, failure classes and
the generic request shape. Pure functions and constants only (no I/O), so the rules can be read and tested in one place.

Design and reasons: docs/providers-design.md ("The four ideas", "Delivery", "Requests", "Capabilities")."""

# ---------- capabilities ----------
# Every provider answers every key (a test enforces it): adding a provider forces a decision on each behaviour, and the
# server and the page branch on these, never on a provider's name (T3 provider-capability-system.md; Nimbalyst's
# exhaustive record). Only keys something reads are listed; add one the day a branch needs it.
CAPABILITIES = {
    'start': 'can start a new session from the page',
    'send': 'can send a message to a session it drives',
    'stop': 'can stop a running turn',
    'reply': 'can answer approvals and questions',
    'steer': 'a message sent mid-turn joins the running turn now (native steering)',
    'native_queue': 'a message sent mid-turn waits inside the harness for its next safe point',
    'restart_steer': 'interrupt-and-restart may be used to deliver a message (destructive: kills subagents and tools)',
    'fork': 'can copy a session into a new one',
    'fork_at_turn': 'can copy a session up to a chosen turn',
    'rewind': 'can take the conversation back to a chosen turn',
    'compact': 'can compact the context on request',
    'rename': 'can rename a session',
    'archive': 'can archive a session',
    'models': 'offers a model choice',
    'effort': 'offers a reasoning-effort choice',
    'modes': 'offers approval or agent modes',
    'questions': 'the agent can ask the user questions',
    'subagents': 'subagents are reported with their own sessions',
    'plan_limits': 'reports plan usage windows',
    'context': "'none' | 'tokens' | 'window': how much of the context use it reports (never draw a false 0%)",
    'release': 'can let go of a session so its own app can continue it',
}
CONTEXT_LEVELS = ('none', 'tokens', 'window')


def check_capabilities(caps, who='provider'):
    """Raise when a capability table misses a key, has an unknown one, or a value of the wrong type."""
    missing = set(CAPABILITIES) - set(caps)
    extra = set(caps) - set(CAPABILITIES)
    if missing or extra:
        raise ValueError('%s capabilities: missing %s, unknown %s' % (who, sorted(missing), sorted(extra)))
    for k, v in caps.items():
        if k == 'context':
            if v not in CONTEXT_LEVELS:
                raise ValueError('%s capability context must be one of %s' % (who, CONTEXT_LEVELS))
        elif not isinstance(v, bool):
            raise ValueError('%s capability %s must be true or false' % (who, k))
    return caps


def capabilities(**on):
    """A full table: every key False (context 'none') except those given."""
    caps = {k: False for k in CAPABILITIES}
    caps['context'] = 'none'
    caps.update(on)
    return check_capabilities(caps)


# ---------- phase and delivery ----------
# A session's phase is set by what WE send and cleared by the stream; "running" is never inferred from "has a turn id"
# (T3's largest fix classes are status lagging the provider: stop, stale Working, ordering, queue, compaction).
PHASES = ('idle', 'starting', 'running', 'stopping', 'compacting')
INTENTS = ('auto', 'steer', 'queue', 'restart')
ROUTES = ('start', 'steer', 'native_queue', 'queue', 'restart', 'refuse')


def route(intent, phase, caps, owned=True, held=False):
    """How a message should be delivered (T3 resolveDeliveryIntent, CommandPolicy.ts:144-182, with its fixes):
    - not ours to drive (another client holds it): refuse; never queue into a conversation driven elsewhere;
    - idle: start (a held queue is released by this send, by the caller);
    - starting / stopping / compacting: queue (a steer has no live turn to join: b6295dd80, 10f7db7e3, a54a44c66);
    - running, auto: native steer, else the harness's own queue, else our queue; restart only when asked (9acb76929:
      interrupt-and-restart kills background agents);
    - running, steer: steer if native, else the same fallbacks as auto (never a silent restart);
    - running, restart: restart if allowed, else refuse.
    Returns one of ROUTES."""
    if intent not in INTENTS:
        raise ValueError('Unknown delivery intent: %s' % intent)
    if not owned or held:
        return 'refuse'
    if phase == 'idle':
        return 'start'
    if phase in ('starting', 'stopping', 'compacting') or intent == 'queue':
        return 'queue'
    if intent == 'restart':
        return 'restart' if caps.get('restart_steer') and caps.get('stop') else 'refuse'
    if caps.get('steer'):
        return 'steer'
    if caps.get('native_queue'):
        return 'native_queue'
    return 'queue'


# ---------- failures ----------
# T3's failure classes (orchestration.md §4). A usage_limit carries resetsAt; transport and provider errors do not.
FAILURE_CLASSES = ('usage_limit', 'provider_error', 'transport_error', 'permission_error', 'validation_error', 'unknown')
HOLDS_QUEUE = {'usage_limit', 'provider_error', 'transport_error', 'permission_error', 'unknown'}          # every class but validation_error (T3 Orchestrator.ts:1833-1850)


# ---------- requests ----------
# One shape for approvals and questions from every driven provider other than Codex (whose own request methods the page
# already reads). method 'item/agent/requestApproval' or 'item/agent/requestUserInput'; params:
#   threadId, requestId, provider, kind ('command' | 'file-change' | 'file-read' | 'permission' | 'question' | 'other'),
#   title, command?, paths?, reason?, options: [{id, label, kind: 'allow_once'|'allow_always'|'reject_once'|'reject_always'|'answer'}],
#   questions?: [{id, question, options: [{label, description}], multiSelect}]
REQUEST_KINDS = ('command', 'file-change', 'file-read', 'permission', 'question', 'other')
ANSWER_VIA = ('live', 'message', 'elsewhere', 'not_resumable')
REQUEST_STATUS = ('pending', 'resolved', 'expired', 'cancelled')


def request_message(provider, thread_id, request_id, kind, title, options, **extra):
    """A request in the generic shape, as published on the event stream and listed in /api/state."""
    if kind not in REQUEST_KINDS:
        kind = 'other'
    method = 'item/agent/requestUserInput' if kind == 'question' else 'item/agent/requestApproval'
    params = {'threadId': thread_id, 'requestId': request_id, 'provider': provider, 'kind': kind, 'title': title,
              'options': options, **{k: v for k, v in extra.items() if v is not None}}
    return {'id': request_id, 'method': method, 'params': params}
