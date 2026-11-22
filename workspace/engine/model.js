/* The map engine, part I of the plan (agent-ide/map-engine-plan.md, sections 2.1 to 2.6): one set of facts per agent, its condition,
   its urgency with the reason, the pieces it would say with their interest, and its standing. Every surface reads these (D18); none
   keeps its own status words. Numbers come from ENG_NAIVE (engine/naive.js) only.
   Loaded before the page's own script; it reads the page's state (ST, AG, ATT, ARCH, GEO, phaseOf, planAt, ...) only when called.
   Recomputed once per drawn frame (FRAMES) and cheap: it changes what labels say, never where they stand (plan 5.2 step 1). */
const ENGM = { f: -1, list: [], by: new Map(), prevCond: new Map(), condAt: new Map(), front: new Map(), spans: [], timer: null };

// ---- the clock of time in front (2.6): wall-clock moments the page went into and out of view, kept apart from frames,
// so a still page that draws nothing still counts. Headless made-up floors (stress) count as in front, as before.
const engWall = () => Date.now();
function engIsFront() {
  if (typeof document === 'undefined') return true;
  if (typeof PG !== 'undefined' && PG.qs && PG.qs.has('stress')) return true;
  return document.visibilityState === 'visible' && (!document.hasFocus || document.hasFocus());
}
function engClockSync() {
  const f = engIsFront(), sp = ENGM.spans, last = sp[sp.length - 1], now = engWall();
  if (f && (!last || last.b != null)) sp.push({ a: now, b: null });
  else if (!f && last && last.b == null) last.b = now;
  if (sp.length > 400) sp.splice(0, sp.length - 400);
  if (typeof PG !== 'undefined' && PG.wake) PG.wake();
}
// milliseconds the page was in front since a wall-clock moment, from the spans only
function engFrontMs(since) {
  const now = engWall(); let ms = 0;
  if (typeof PG !== 'undefined' && PG.qs && PG.qs.has('stress')) return Math.max(0, now - since);          // made-up floors: always in front
  ENGM.spans.forEach(q => { const a = Math.max(q.a, since), b = Math.min(q.b == null ? now : q.b, now); if (b > a) ms += b - a; });
  return ms;
}
if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  document.addEventListener('visibilitychange', engClockSync); window.addEventListener('focus', engClockSync); window.addEventListener('blur', engClockSync);
  ENGM.spans.push({ a: engWall(), b: engIsFront() ? null : engWall() });
}
// has any agent's fresh/shown word changed by the clock since the last build? (then the cache is stale)
function engNewsStale() {
  const sp = ENGM.spans, last = sp[sp.length - 1];
  if (last && (last.b == null) !== engIsFront()) engClockSync();
  return ENGM.list.some(r => { const e = ENGM.front.get(r.id); return e && r.news !== (engFrontMs(e.since) / 1000 >= ENG_NAIVE.shownAfter ? 'shown' : 'fresh'); });
}

// a silent running agent you attended to (a click on it, its tree row, a step to it) is a quiet clock that was seen: it reads
// as running, not stalled, until it acts again and falls silent again (2026-11-07: a click closes a quiet clock, one time).
// Kept in the page's attention store with what else was seen (epoch seconds), so a reload keeps it.
// Only an agent that is silent now is marked (an attend to any other writes nothing).
function engStallSeen(sid) {
  const r = engFloor().find(x => x.a.sid === sid);
  if (r && r.cond === 'stalled') PG.ATT.markStallSeen(sid, PG.LIVE.origin + PG.NOWT);
}

// ---- facts (2.2): separate facts, never one merged status ----
function engFacts(s) {
  const { ATT, ATTN_SRV, LIVE, T, FC, ST, QUIET_OUT, ARCH, FOLD, GEO, phaseOf, parentOf, kidsAll, fcRunning, planAt, ctxNow, cmpPt, lastAct } = PG;
  const a = s.a, its =  ATT.bySid(a.sid), ph = phaseOf(s);
  const sv = ATTN_SRV.get(a.sid) || {};
  const errs = (sv.errors || []).filter(e => e.at - LIVE.origin <= T + 1e-6), lastErr = errs.reduce((m, e) => !m || e.at > m.at ? e : m, null);
  let say = sv.say && sv.say.at - LIVE.origin <= T + 1e-6 ? sv.say : null;
  const asking = ph === 'waiting' || its.some(it => it.waiting);
  // the words it is quoted by: its last word in its latest turn as the page has it (the history and the stream: a.turns), else the
  // server's (walk 2026-11-19: the catch-up quoted a turn's first, interim remark, or "its answer is ready" over an answer of "hello",
  // because the server's word came with an older canvas). Only the words: when it answered and whether it is seen stay the server's
  const fin = engFinalWords(a.turns, T), words = fin && (!say || fin.t + LIVE.origin >= say.at - 1) ? fin.text : say ? say.text : null;
  const running = ph === 'running' || ph === 'stalled';
  // how the last turn ended: stopped by you, with an answer, with a failure, not yet (still running, or never answered). Stopped: the
  // server says its last turn was stopped (walk 2026-11-19: the tree showed the finished check and the label said finished); a turn
  // it ran since, an answer or a failure after the stop, ends that at once
  const stops = (sv.stops || []).filter(x => x.at - LIVE.origin <= T + 1e-6), lastStop = stops.length ? Math.max(...stops.map(x => x.at)) : null;
  const stopped = !running && !asking && sv.ended === 'stopped' && (!(sv.stops || []).length || lastStop != null)
    && !(say && lastStop != null && say.at > lastStop + 1e-6) && !(lastErr && lastStop != null && lastErr.at > lastStop + 1e-6);
  const ended = running || asking ? 'not yet' : stopped ? 'stopped' : lastErr && (!say || lastErr.at >= say.at) ? 'failure' : say || a.end != null ? 'answer' : 'not yet';
  const par = parentOf(a), kidsA = kidsAll(a);
  const kst = k => FC && FC.st ? FC.st.get(k) : ST.find(q => q.a === k);
  const kRun = kidsA.filter(k => fcRunning(kst(k))).length;
  const looked = ATT.lookedAt(a.sid);
  // seen (P7): the open item for this change is closed. A child's finish counts as seen as soon as it finishes (2026-11-21: a
  // finished subagent need not wait for a read before it is labelled finished and folded), so it is labelled finished
  // and folds at once
  const errOpen = its.some(it => it.kind === 'error'), finOpen = its.some(it => it.kind === 'finished') || s.idle === 'unread';
  let seen;
  if (ended === 'failure') seen = !errOpen;
  else if (ended === 'answer') seen = !!par || !finOpen;
  else seen = true;
  const p = planAt(a), cur = p && p.cur >= 0 ? engPlain(p.text) : null;
  const ctx = GEO[a.id] ? GEO[a.id].ctx : ctxNow(a), cpt = cmpPt(a), lim = cpt ? cpt.threshold : (a.window || 0);
  const comp = (a.compacts || []).filter(c => c.t <= T), lastComp = comp.length ? comp[comp.length - 1] : null;
  const writes = s.cs.reduce((n, c) => n + (c.k === 'w' ? 1 : 0), 0), lines = s.cs.reduce((n, c) => n + (c.k === 'w' ? c.ln || 0 : 0), 0);
  const linesPending = s.cs.some(c => c.k === 'w' && c.lnK === false);          // an edit whose line count has not come yet (lnK: live.html canvasCalls)          // lines: added plus removed, from the server
  const last = lastAct(s);
  const wpaths = new Set(); s.cs.forEach(c => { if (c.k === 'w' && c.path) wpaths.add(c.path); });
  const chg = engChgWhole(a.sid, wpaths.size, lines, linesPending);
  return {
    doing: engDoing(s, kidsA), files: wpaths.size,
    turn: running ? 'running' : asking ? 'stopped on a request' : 'not running', ended, asking, running,
    stalled: ph === 'stalled' && !(ATT.stallSeenAt(a.sid) >= LIVE.origin + last), inactive: !!(s.born && QUIET_OUT[s.idle]), archived: ARCH.ids.has(a.id), stub: !!s.stub,
    folded: FOLD.has(a.id), lastAct: last, silentFor: Math.max(0, Math.round(T - last)),
    parent: par ? par.id : null, children: kidsA.map(k => k.id), kidsRunning: kRun, kidsBack: kidsA.length - kRun,
    ctx, ctxLimit: lim, ctxFrac: lim ? ctx / lim : 0, compactedAt: lastComp ? lastComp.t : null,
    compactIdx: lastComp ? comp.length - 1 : null, compactFirst: lastComp && lastComp.first ? engSummaryFirst(lastComp.first) || null : null,          // the latest compaction: its index in a.compacts, its summary's first line
    attended: looked, seen, lines, linesPending, chg, plan: p ? { done: p.done, n: p.n, cur, steps: p.steps.map(x => ({ text: engPlain(x.text), done: x.status === 'completed' })), at: p.cur } : null, writes,          // chg: THE change count every place shows (null: none whole yet)
    err: lastErr ? engPlain(lastErr.text) : null, say: words,          // say: engFirstLine makes it plain where it is quoted
    ask: asking ? engAskText(a) : null, lastCall: s.cs.length ? s.cs[s.cs.length - 1] : null,
  };
}
function engAskText(a) {
  const q = (a.asks || []).find(x => !x.resolved) || null;
  if (!q) return 'waiting on you';
  return q.kind === 'ask' ? engPlain(q.question) : q.kind === 'approve' ? 'allow: ' + q.cmd : q.cmd || engPlain(q.reason) || 'a request';          // a command stands as typed
}

// ---- condition (2.3): what the facts come to for the reader. Seen/unseen divides failed and finished.
function engCondition(f) {
  if (f.archived) return 'archived';
  if (f.asking) return 'asking';
  if (f.stalled) return 'stalled';
  if (f.running) return 'running';
  if (f.ended === 'stopped') return 'stopped';          // your own act: quiet, never on the list, until it runs again
  if (f.ended === 'failure' && (!f.seen || !f.inactive)) return 'failed';
  if (f.ended === 'answer' && (!f.seen || !f.inactive)) return 'finished';
  return 'idle';
}

// ---- urgency (2.4): time lost per minute unnoticed; from facts only, never the view (D21). Passed up to holders.
function engUrgency(f, cond, parRec) {
  const U = ENG_NAIVE.urgency, parRuns = !!(parRec && parRec.facts.running), child = !!f.parent;
  let rate = 0, why = '';
  if (cond === 'asking') { rate = U.ask; why = child ? 'a subagent asks; it and its parent stand still' : 'asks you; it stands still'; }
  else if (cond === 'failed' && !f.seen) { rate = child && parRuns ? U.failedChildParentRuns : U.failed; why = child && parRuns ? 'a subagent failed; its parent runs and can deal with it' : 'failed and you have not seen it'; }
  else if (cond === 'finished' && !f.seen) { rate = child ? U.finishedChild : U.finished; why = child ? 'a subagent finished; its parent takes the result' : 'finished and you have not seen it; your next move waits'; }
  else if (cond === 'stalled') { rate = child && parRuns ? U.failedChildParentRuns : U.stalled; why = 'silent ' + Math.round(f.silentFor / 60) + ' min while running: may be stuck'; }
  else if ((cond === 'running') && f.ctxFrac >= ENG_NAIVE.ctxStandsOut) { rate = U.ctxNear; why = 'context at ' + Math.round(100 * f.ctxFrac) + '%: a compaction is coming'; }
  return { rate, band: engBand(rate), why: why || 'nothing is lost', since: cond === 'asking' ? engAskSince(f) : null };
}
const engBand = r => r >= ENG_NAIVE.bands.high ? 'high' : r >= ENG_NAIVE.bands.medium ? 'medium' : r > 0 ? 'low' : 'none';
function engAskSince(f) { return null; }

// ---- what it would say (2.5, from the condition side): its pieces, most wanted first. Interest decides how much is said; never loudness.
// Piece 4 (planner's brief, 2026-11-16): the details under the name line, in the order you need them, per condition:
//   asking   the request; the plan's step; its subagents
//   failed   what failed; the last thing it was doing; the last thing it said
//   running  what it is doing now (the latest action: a file read or edited, a command, subagents started); the last thing it said;
//            the plan's step; its subagents
//   stalled  how long silent; the last thing it did; the last thing it said; the plan's step
//   finished what came of it (the opening of its last message); how much it changed (files, lines) when the page knows
//   idle     the last thing it said; how much it changed
//   and last in every condition but archived: a recent compaction (its summary's first line; ENG_NAIVE.compaction)
// A piece whose field is absent is not said (never a placeholder). Each piece may carry shorter wordings (alts, longest first: a path,
// then its file name) and how many lines it may wrap to in the fullest form (wrap: only text that holds still while the condition
// holds wraps; a running agent's pieces are one line each, so a new action never changes how many lines the label has).
// words: the few words the name line carries in form 3 ("editing report.ts", "3 files changed"), longest wording first.
// interest stays as the order's weight for the sheet and the surfaces; the forms take the pieces in this order.
function engPieces(f, cond) {
  const P = [], quiet = cond === 'idle' || cond === 'archived' || cond === 'stopped', add = (piece, text, o) => { if (text) P.push(Object.assign({ piece, text, interest: quiet ? 0.1 : 0.5 }, o || {})); };
  const prog = f.plan ? { piece: 'progress', text: f.plan.done + '/' + f.plan.n, frac: f.plan.n ? f.plan.done / f.plan.n : 0 } : null;
  const doing = f.doing !== undefined ? f.doing : f.lastCall ? engCallWords(f.lastCall, true) : null;          // the sheet's facts carry only the last call
  const past = doing && doing.pastText ? doing : f.lastCall ? engCallWords(f.lastCall, false) : doing;
  const said = engFirstLine(f.say), quote = said;          // set apart in italics, not quotation marks (a mark left open by a cut read as a cut)
  const step = f.plan && f.plan.cur ? 'plan: ' + f.plan.cur : '';          // the step's words; its figure stays on the name line ("3/5")
  const cg = f.chg !== undefined ? f.chg : { files: f.files != null ? f.files : f.writes || 0, lines: f.lines || 0 }, files = cg ? cg.files : 0;          // the one count (facts.chg); the sheet's made facts carry files and lines
  const changed = files ? engCountWords(files, cg.lines) : '';
  const kids = '';          // subagents are said once: by the tree's side text, and on the label only as the doing line when starting them is the latest
  if (cond === 'asking') { add('request', f.ask, { interest: 1, wrap: 3, strong: true }); add('step', step, { interest: 0.4 }); add('children', kids); }
  if (cond === 'failed') { add('failure', f.err || 'the turn failed', { interest: f.seen ? 0.4 : 0.9, wrap: 2 }); add('did', past && 'last: ' + past.pastText, { alts: past && past.pastAlts.map(t => 'last: ' + t) }); add('said', quote, { wrap: 2 }); }
  if (cond === 'running') { add('doing', doing && doing.text, { interest: 0.7, alts: doing && doing.alts, ticks: true });
    add('said', quote, { ticks: true }); add('step', step, { interest: 0.6, ticks: true }); add('children', kids, { ticks: true }); }
  if (cond === 'stalled') { add('silence', 'silent ' + Math.round(f.silentFor / 60) + ' min', { interest: 0.8 }); add('did', past && 'last: ' + past.pastText, { alts: past && past.pastAlts.map(t => 'last: ' + t) });
    add('said', quote); add('step', step, { interest: 0.4 }); }
  if (cond === 'finished') { add('result', said || 'finished', { interest: f.seen ? 0.3 : 0.8, wrap: 3 }); add('changed', changed); add('children', kids); }
  if (cond === 'stopped') { add('stopped', 'stopped', { interest: 0.1 }); add('did', past && 'last: ' + past.pastText, { alts: past && past.pastAlts.map(t => 'last: ' + t) }); add('changed', changed); }
  if (cond === 'idle' || cond === 'archived') { add('said', quote, { wrap: 2 }); add('changed', changed); }
  // the compaction (ENG_NAIVE.compaction): last of every condition's pieces, so every other piece takes the room first; only in the
  // fullest form (minForm), never the name line's words. The one compaction signal: its summary's first line, else "context compacted"
  const CP = ENG_NAIVE.compaction;
  if (cond !== 'archived' && f.compactedAt != null && f.ctxFrac < ENG_NAIVE.compactNoteUntil) {
    const fl = f.compactFirst ? engPlain(f.compactFirst) : '', cut = fl.length > CP.maxChars ? fl.slice(0, CP.maxChars).replace(/\s+\S*$/, '') + '…' : fl;
    add('compaction', cut ? 'compacted: ' + cut : 'context compacted', { interest: CP.interest, minForm: CP.minForm, ticks: cond === 'running', compactIdx: f.compactIdx });
  }
  // the name line's words (form 3): the first piece in its wordings, longest first; a finished agent says what it changed when known
  // ("12 files changed, 370 lines", then "12 files changed"), else the opening of its answer. short: the shortest wording
  const p0 = P.find(p => !p.minForm), words = !p0 ? [] : cond === 'finished' && changed ? [changed]
    : [p0.piece === 'said' ? said : p0.text].concat(p0.alts || []);
  return { name: true, ctx: true, progress: prog, lines: P, words, short: words[words.length - 1] || '', ticks: cond === 'running' || cond === 'stalled' };
}
// an agent's words reduced to plain words (walk 2026-11-19: a finished message's link "[totals.py](/Users/…/receipts/totals.py)"
// showed on the floor and in the catch-up with its brackets and whole path). The one step for every place outside the chat that
// quotes an agent's words in one or a few lines: floor labels and their hover lines, the question card, the tree's lines, the
// catch-up. A link shows its text only (an image its alt words); emphasis, strike and code marks go; headings, list, quote and fence
// marks go; line breaks become spaces. The chat draws the same words as Markdown.
function engPlain(t) {
  let s = String(t == null ? '' : t);
  s = s.replace(/^\s*(```|~~~).*$/gm, '');          // code fences: the fence lines go, the code stays
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)/g, '$1').replace(/\[([^\]]+)\]\[[^\]]*\]/g, '$1');
  s = s.replace(/<(https?:\/\/[^>\s]+)>/g, '$1');
  s = s.replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, '').replace(/^[ \t]*>[ \t]?/gm, '').replace(/^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?/gm, '');
  s = s.replace(/^[ \t]*(?:[-*_][ \t]*){3,}$/gm, '');          // a rule line
  s = s.replace(/`+([^`]*?)`+/g, '$1');
  // underscores mark emphasis only standing apart from a word or a path ("__init__.py", "receipts/_x_.ts" stay as written)
  s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '$1').replace(/(^|[\s(\[{"'])__(?=\S)([\s\S]*?\S)__(?![\w\/]|\.\w)/g, '$1$2').replace(/~~(?=\S)([\s\S]*?\S)~~/g, '$1');
  s = s.replace(/(^|[^\w*])\*(?=\S)([^*\n]*?\S)\*(?![\w*])/g, '$1$2').replace(/(^|[\s(\[{"'])_(?=\S)([^_\n]*?\S)_(?![\w\/]|\.\w)/g, '$1$2');
  return s.replace(/\s+/g, ' ').trim();
}
// which of its messages a turn is quoted by: the agent's last message with words in its latest turn (after your last message, as of t),
// never an earlier, interim remark of that turn; null when it said nothing since your last message. turns: a.turns (kind 'you' yours,
// kind 'say' its messages; others its work)
function engFinalWords(turns, t) {
  let fin = null;
  for (const x of turns || []) { if (x.t > t + 1e-6) continue;
    if (x.kind === 'you') fin = null; else if (x.kind === 'say' && x.who !== 'you' && String(x.full || x.text || '').trim()) fin = x; }
  return fin ? { t: fin.t, text: String(fin.full || fin.text) } : null;
}
// a change count in words, the one wording everywhere: "1 file changed, 1 line", "3 files changed, 12 lines" (walk 2026-11-19: "1 lines")
const engCountWords = (files, lines) => files + (files === 1 ? ' file changed, ' : ' files changed, ') + (lines || 0) + ((lines || 0) === 1 ? ' line' : ' lines');
// an agent's change count, whole or not at all (walk 2026-11-19: "1 file changed" with no lines; the label and the chat head counting
// apart): while an edit's line count has not come, the last whole count stands (null: none yet), whatever the agent is doing now.
// The one source: facts.chg, which the label, its hover, the chat head and the change reader all read
const ENG_CHG_WHOLE = new Map();          // sid -> { files, lines } last counted whole
function engChgWhole(sid, files, lines, pending) {
  if (pending) return ENG_CHG_WHOLE.get(sid) || null;
  const now = files ? { files, lines } : null; ENG_CHG_WHOLE.set(sid, now); return now;
}
// the first line with words, as plain words
const engFirstLine = t => t ? engPlain(String(t).replace(/^\s*(```|~~~).*$/gm, '').split('\n').map(x => x.trim()).find(x => engPlain(x)) || '') : '';
// a compaction summary's first line with content: a summary that opens with its own heading ("Summary:", "## Summary", "**Summary**")
// skips that line, and a "Summary:" label in front of words on the same line goes (2026-11-22: the signal read "compacted: Summary:")
function engSummaryFirst(t) {
  const lines = String(t == null ? '' : t).replace(/^\s*(```|~~~).*$/gm, '').split('\n').map(x => x.trim());
  for (const x of lines) {
    if (!engPlain(x)) continue;
    if (/^#{1,6}\s/.test(x) || (x.length < 60 && /^(\*\*|__)[^*_]+(\*\*|__):?$/.test(x))) continue;          // a markdown heading, or a short line all in bold
    const w = engPlain(x).replace(/^(summary|analysis)\s*:\s*/i, '').trim();
    if (!w || (w.endsWith(':') && w.length < 60)) continue;          // a bare label ("Summary:", "Primary Request and Intent:")
    return w;
  }
  return '';
}
const engBase = p => String(p || '').replace(/\/+$/, '').split('/').pop();
// one call in plain words: present while the agent runs ("editing src/core/report.ts", "running npm test"), past after ("edited ...").
// alts: the same with the file's name alone (a path never cut in its middle: the shorter wording is tried first)
function engCallWords(c, now) {
  const run = c.k === 'x' ? (c.it && c.it.done ? 'ran ' : 'running ') : '';
  const verb = c.k === 'w' ? (now ? 'editing ' : 'edited ') : c.k === 'x' ? '' : (now ? 'reading ' : 'read ');
  const cmd = String(c.what || '').trim().split(/\s+/).filter(Boolean), what = c.k === 'x' ? cmd.slice(0, 6).join(' ') || 'a command' : c.path || c.what || c.d || '';
  if (c.k === 'x') { const t = (now ? run || 'running ' : 'ran ') + what, s = (now ? run || 'running ' : 'ran ') + (cmd.slice(0, 3).join(' ') || 'a command');
    return { text: t, alts: s !== t ? [s] : [], pastText: 'ran ' + what, pastAlts: s !== t ? ['ran ' + cmd.slice(0, 3).join(' ')] : [] }; }
  const b = engBase(what), pv = c.k === 'w' ? 'edited ' : 'read ';
  return { text: verb + what, alts: b && b !== what ? [verb + b] : [], pastText: pv + what, pastAlts: b && b !== what ? [pv + b] : [] };
}
// what a running agent is doing now (facts: doing): the latest of its last tool call (s.cs: a read, an edit, a command), its last other
// action in the stream (a.turns kind 'act' not standing for a call: a web search, a tool of a server), and subagents it started since
function engDoing(s, kidsA) {
  const { T } = PG, a = s.a, c = s.cs.length ? s.cs[s.cs.length - 1] : null, tc = c ? c.t : -Infinity;
  const kids = kidsA.filter(k => k.start > tc && k.start <= T + 1e-6);
  let act = null;
  for (let i = (a.turns || []).length - 1; i >= 0; i--) { const x = a.turns[i]; if (x.t > T + 1e-6) continue; if (x.t <= tc) break;
    if (x.kind === 'act' && x.who !== 'you' && x.text && !(x.item && s.cs.some(q => q.item === x.item))) { act = x; break; } }
  if (kids.length && (!act || kids[kids.length - 1].start >= act.t)) { const t = 'started ' + kids.length + (kids.length === 1 ? ' subagent' : ' subagents'); return { text: t, alts: [], pastText: t, pastAlts: [] }; }
  if (act) { const t = engFirstLine(act.text), said = /^(edits |web search |views |subagent |review |context compacted)/.test(t);          // the stream's own words stand as they are; a command
    if (said) return { text: t, alts: [], pastText: t, pastAlts: [] };                                                                       // from the history reads "running npm test"
    const ws = t.split(/\s+/), s3 = ws.slice(0, 3).join(' '), al = s3 !== t ? [s3] : [];
    return { text: 'running ' + t, alts: al.map(x => 'running ' + x), pastText: 'ran ' + t, pastAlts: al.map(x => 'ran ' + x) }; }
  return c ? engCallWords(c, true) : null;
}
const engCallText = c => engCallWords(c, true).text;
// the tree's side text keeps its short wording ("2 running, 1 back"); the label says what the words are about
const engKidsText = f => [f.kidsRunning ? f.kidsRunning + ' running' : '', f.kidsBack ? f.kidsBack + ' back' : ''].filter(Boolean).join(', ');

// ---- the whole floor at this frame: every agent born at T, with its standing and level (levels are decided across agents: D5)
function engFloor() {
  const { FRAMES, ST } = PG;
  if (ENGM.f === FRAMES && !engNewsStale()) return ENGM.list;
  const recs = [], by = new Map();
  ST.forEach(s => { if (!s.born) return; const f = engFacts(s); const r = { a: s.a, s, id: s.a.id, facts: f, cond: engCondition(f) }; recs.push(r); by.set(s.a.id, r); });
  recs.forEach(r => { const pr = r.facts.parent ? by.get(r.facts.parent) : null; r.urg = engUrgency(r.facts, r.cond, pr); r.say = engPieces(r.facts, r.cond); });
  // fresh, shown, seen (2.6): an unseen finish or failure stands out (fresh) until the page has been in front of you for
  // ENG_NAIVE.shownAfter seconds while it stood out; then it is shown: still marked, quietly, until seen. Time away does not count.
  let nextAt = Infinity;
  recs.forEach(r => { const news = (r.cond === 'failed' || r.cond === 'finished') && !r.facts.seen; if (!news) { r.news = r.cond === 'failed' || r.cond === 'finished' ? 'seen' : null; ENGM.front.delete(r.id); return; }
    const key = r.cond + ':' + r.facts.lastAct, o = ENGM.front.get(r.id); const e = o && o.key === key ? o : { key, since: engWall() }; ENGM.front.set(r.id, e);
    const sec = engFrontMs(e.since) / 1000; r.news = sec >= ENG_NAIVE.shownAfter ? 'shown' : 'fresh';
    if (r.news === 'fresh') nextAt = Math.min(nextAt, ENG_NAIVE.shownAfter - sec);
    if (r.news === 'shown') { r.urg = Object.assign({}, r.urg, { rate: Math.min(r.urg.rate, ENG_NAIVE.bands.medium - 0.01) }); r.urg.band = engBand(r.urg.rate); r.urg.why += ' (shown to you; not yet seen)'; } });
  // the page redraws when an agent's time crosses shownAfter (a timer for that moment; set again after every build while any agent is fresh)
  if (ENGM.timer) { clearTimeout(ENGM.timer); ENGM.timer = null; }
  if (nextAt < Infinity && engIsFront() && typeof setTimeout === 'function') ENGM.timer = setTimeout(() => { ENGM.timer = null; PG.wake(); }, Math.ceil(nextAt * 1000) + 60);
  // urgency of a part passes up to what holds it (2.4): a folded child's need lifts its parent, so the holder can be found
  recs.forEach(r => { if (!r.facts.folded || !r.facts.parent) return; const p = by.get(r.facts.parent); if (p && r.urg.rate > p.urg.rate) p.urgUp = { rate: r.urg.rate, band: r.urg.band, why: 'its subagent ' + r.id + ': ' + r.urg.why }; });
  // standing (A19): mean of three ranks (recency as the order of last action, context size, file writes as the stand-in for lines changed)
  const live = recs.filter(r => r.cond !== 'archived'), n = live.length, rank = key => { const o = live.slice().sort((x, y) => key(x) - key(y) || (x.id < y.id ? -1 : 1)); const m = new Map(); o.forEach((r, i) => m.set(r, n > 1 ? i / (n - 1) : 1)); return m; };
  const rR = rank(r => r.facts.lastAct), rC = rank(r => r.facts.ctx), rW = rank(r => r.facts.lines), W = ENG_NAIVE.standingParts, ws = W.recency + W.context + W.lines;
  recs.forEach(r => { r.standing = r.cond === 'archived' ? 0 : (W.recency * rR.get(r) + W.context * rC.get(r) + W.lines * rW.get(r)) / ws; });
  // levels (3.6): the legend's base level, raised by urgency; the top level kept scarce (D5)
  recs.forEach(r => { const u = r.urgUp && r.urgUp.rate > r.urg.rate ? r.urgUp : r.urg; r.need = u; const base = ENG_LEGEND[r.cond].level, up = ENG_NAIVE.levelOfBand[u.band];
    r.level = up && ENG_NAIVE.levels.indexOf(up) > ENG_NAIVE.levels.indexOf(base) ? up : base; });
  const asking = recs.filter(r => r.cond === 'asking' || (r.urgUp && r.need === r.urgUp && r.urgUp.rate >= ENG_NAIVE.urgency.ask)).sort((x, y) => y.need.rate - x.need.rate || engWaitT(x) - engWaitT(y) || (x.id < y.id ? -1 : 1));
  asking.forEach((r, i) => { r.level = i < ENG_NAIVE.callMax ? 'call' : 'strong'; });
  // the condition's change time: forms may change at a still camera only when this moves (5.5)
  recs.forEach(r => { const pc = ENGM.prevCond.get(r.id); if (pc !== r.cond) { ENGM.prevCond.set(r.id, r.cond); ENGM.condAt.set(r.id, typeof performance !== 'undefined' ? performance.now() : 0); } r.condAt = ENGM.condAt.get(r.id); });
  ENGM.f = FRAMES; ENGM.list = recs; ENGM.by = by;
  return recs;
}
// when an asking agent started waiting (floor seconds): the oldest of its open requests; equals by urgency, the longest wait first
function engWaitT(r) { const qs = (r.a.asks || []).filter(q => !q.resolved); return qs.length ? Math.min(...qs.map(q => q.t)) : r.facts.lastAct; }
const engRec = a => { engFloor(); return ENGM.by.get(typeof a === 'string' ? a : a.id) || null; };

// ---- the sheet of step 1 (section 10): for each agent, its condition, urgency and why, what it would say (drivers, stills)
function engSheet() {
  return engFloor().map(r => ({
    agent: r.id, parent: r.facts.parent, folded: r.facts.folded, condition: r.cond + (r.cond === 'failed' || r.cond === 'finished' ? ', ' + r.news : ''),
    urgency: r.need.band + ' (' + r.need.rate.toFixed(2) + ')', why: r.need.why, level: r.level, standing: +r.standing.toFixed(2), lines: r.facts.lines,
    says: ['name', 'context ' + (r.facts.ctx >= 1000 ? Math.round(r.facts.ctx / 1000) + 'k' : r.facts.ctx) + (r.facts.ctxLimit ? ' (' + Math.round(100 * r.facts.ctxFrac) + '%)' : '')]
      .concat(r.say.progress ? ['progress ' + r.say.progress.text] : []).concat(r.say.lines.map(p => p.piece + ': ' + String(p.text).slice(0, 90) + ' [' + p.interest + ']')),
  }));
}
// drivers: the sheet, plus what the row and the tree report of themselves (engRowState, engTreeState, when those files define them)
window.__engine = () => ({ sheet: engSheet(), row: typeof engRowState === 'function' ? engRowState() : null, tree: typeof engTreeState === 'function' ? engTreeState() : null });

// ---- the catch-up (2.6, plan step 9): for one agent, what happened since you last attended to it (lookedAbs: wall seconds,
// as it stood before the click that opened it), as a short list; read when you turn to the agent, never pushed
function engCatchUp(a, lookedAbs) {
  const { LIVE, T, ST, kidsAll } = PG, t0 = (lookedAbs || 0) - LIVE.origin, out = [];
  const s = ST.find(q => q.a === a); if (!s) return out;
  const st = (a.plan || []).filter(x => x.t <= T + 1e-6), at0 = st.filter(x => x.t <= t0).pop(), now = st[st.length - 1];
  if (now) { const was = new Set(at0 ? at0.steps.filter(x => x.status === 'completed').map(x => x.text) : []);
    const done = now.steps.filter(x => x.status === 'completed' && !was.has(x.text)).map(x => engPlain(x.text));
    if (done.length) out.push({ k: 'steps', text: done.length + (done.length === 1 ? ' step done: ' : ' steps done: ') + done.join(' · ') }); }
  const f = engRec(a) && engRec(a).facts;
  // a turn you stopped is not news to you (walk 2026-11-19): the chat's own quiet line "you stopped it" is its one trace
  if (a.end != null && a.end > t0 && a.end <= T && f && f.ended !== 'stopped') out.push({ k: f.ended === 'failure' ? 'failure' : 'finish', text: f.ended === 'failure' ? 'failed: ' + (f.err || 'the turn failed') : 'finished: ' + (engFirstLine(f.say) || 'its answer is ready') });
  const asks = (a.asks || []).filter(q => !q.resolved && q.t > t0); if (asks.length) out.push({ k: 'request', text: asks.length === 1 ? 'asks you: ' + engAskText(a) : asks.length + ' requests waiting' });
  const comp = (a.compacts || []).filter(c => c.t > t0 && c.t <= T); if (comp.length) out.push({ k: 'compaction', text: comp.length === 1 ? 'compacted its context' : 'compacted ' + comp.length + ' times' });
  const back = kidsAll(a).filter(k => k.end != null && k.end > t0 && k.end <= T); if (back.length) out.push({ k: 'children', text: back.length + (back.length === 1 ? ' subagent back' : ' subagents back') });
  const ws = s.cs.filter(c => c.k === 'w' && c.t > t0 && c.t <= T), paths = new Set(ws.map(c => c.path)), ln = ws.reduce((n, c) => n + (c.ln || 0), 0);
  if (paths.size && !ws.some(c => c.lnK === false)) out.push({ k: 'files', text: engCountWords(paths.size, ln) });          // whole or not at all, as every count
  return out;
}
