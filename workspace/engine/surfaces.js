/* The surfaces that tell you what needs you (plan 4.1, 4.2, D18; step 8): the agent tree's head (the count, and the step to
   the next; the "For you" list and its tray went, 2026-11-16), the edge marks, the mini-map's
   icons, the tab's title and favicon. One list, from the engine's condition and level (engFloor), with
   the legend's look (engLook); no surface keeps its own rule or its own status words. The page's attention store (AttnCore) is only the
   engine's input: what is open (requests, failures, finishes) and what you read.
   Loaded after legend.js; reads the page through PG and the engine only, when called. */

// The rule: which agents need you, one named entry each. Origins: (brief) the planner's brief for step 8; (plan) the map engine
// plan, draft 4; (builder) chosen while building. All naive, to be replaced by measuring.
const ENG_SURF = {
  // (brief; plan 3.6) an agent whose condition and urgency raise it to these levels needs you
  loudLevels: ['strong', 'call'],
  // (brief; plan 2.6) a failure or a finish not yet seen: fresh, or shown and still marked quietly until seen
  newsConds: ['failed', 'finished'], newsStates: ['fresh', 'shown'],
  // (brief; plan 2.4) running but silent: may be stuck
  stalled: true,
  // (plan D4) a subagent's finish, failure or silence that is low while its parent can take it (its urgency before the shown cap is low
  // or none) is not on the list and draws no edge mark: the parent's count tells it, as the tree does
  childLowOff: true,
  // (builder) an agent raised only by its folded subagent's need (urgency passed up, plan 2.4) is not listed itself: the subagent is,
  // so one need is one row and one count; on the floor its mark stands on the parent's stack, where its edge mark points
  liftedOff: true,
  // (brief) the order: by need.rate, highest first; among equals the longest wait first (plan 2.4)
  order: 'rate, then wait',
  // (builder) stepping through the list (the tree's count, the key in engine/keys.js): a round of steps forgets which agents it
  // visited after this many seconds without a step, so coming back later starts again at the most pressing
  stepForget: 300,
};

const ENGS = { list: null, rows: [], by: new Map() };

// what is lost before the shown cap (plan 2.4): the engine's own urgency, asked again for the facts as they are
function engSurfLowChild(r, by) {
  if (!r.facts.parent) return false;
  const u = engUrgency(r.facts, r.cond, by.get(r.facts.parent) || null);
  return u.band === 'low' || u.band === 'none';
}
// the level an agent's own urgency gives it (the engine's rule in engFloor, without what its folded subagent passed up)
function engSurfOwnLevel(r) {
  const L = ENG_NAIVE.levels, base = ENG_LEGEND[r.cond].level, up = ENG_NAIVE.levelOfBand[r.urg.band];
  return up && L.indexOf(up) > L.indexOf(base) ? up : base;
}
// why an agent is on the list (the entry that takes it), or null
function engSurfWhy(r, by) {
  if (r.cond === 'archived') return null;
  // lifted: the level is its folded subagent's; the rule then reads the level its own urgency gives
  const lifted = ENG_SURF.liftedOff && r.urgUp && r.need === r.urgUp && r.cond !== 'asking';
  if (ENG_SURF.loudLevels.includes(lifted ? engSurfOwnLevel(r) : r.level)) return 'level';
  if (ENG_SURF.childLowOff && engSurfLowChild(r, by)) return null;
  if (ENG_SURF.newsConds.includes(r.cond) && ENG_SURF.newsStates.includes(r.news)) return 'news';
  if (ENG_SURF.stalled && r.cond === 'stalled') return 'stalled';
  return null;
}
// the piece most wanted (plan 2.5) under the reason: the request, what failed, the result's first line, the last action of a silent one
// (its silence is already in the reason)
function engSurfDetail(r) {
  const p = r.say && r.say.lines[0]; if (!p || !/^(request|failure|result|silence)$/.test(p.piece)) return '';
  return p.piece === 'silence' ? String(p.text).replace(/^silent \d+ min( · )?/, '') : String(p.text);
}
// the list: every agent that needs you, in order, with its condition, level, news, look, words and why
function engAttnList() {
  const recs = engFloor();
  if (ENGS.list === recs) return ENGS.rows;
  const by = ENGM.by, rows = [];
  recs.forEach(r => {
    const why = engSurfWhy(r, by); if (!why) return;
    const look = engLook(r);
    rows.push({ r, id: r.id, a: r.a, cond: r.cond, level: r.level, news: r.news, look, glyph: look.glyph || 'harness', asking: r.cond === 'asking',
      word: ENG_LEGEND[r.cond].word, why: r.need.why, rate: r.need.rate, wait: engWaitT(r), entry: why,
      detail: engSurfDetail(r) });
  });
  rows.sort((x, y) => y.rate - x.rate || x.wait - y.wait || (x.id < y.id ? -1 : 1));
  ENGS.list = recs; ENGS.rows = rows; ENGS.by = new Map(rows.map(e => [e.id, e]));
  return rows;
}
const engAttnOf = id => { engAttnList(); return ENGS.by.get(typeof id === 'string' ? id : id.id) || null; };
// the counts every surface shows: agents on the list, and how many of them ask (the tree's head, the title, the favicon)
function engAttnCounts() { const l = engAttnList(); const asking = l.filter(e => e.asking).length; return { n: l.length, asking, rest: l.length - asking }; }
// Stepping to the next agent that needs you (integration brief C, item 3): one click on the tree's count or one key. A step
// goes to the most pressing agent on the list that this round has not visited yet; the agent selected when the step is taken counts
// as visited (a step always moves on from where you are). When every one has been visited, the round starts again at the top (a
// wrap). The list is read afresh at every step, so a change between steps is taken as it is: one that left the list (answered,
// read, working again) is gone and forgotten, so if it comes back it is new; one that arrived and is more pressing than the rest
// comes next; the order of the rest is the list's order at that moment. A round is forgotten after ENG_SURF.stepForget seconds
// without a step.
const ENGSTEP = { seen: new Set(), at: -Infinity, log: [] };
// peek: true says who a step would go to without taking it (the tree's head names it on hover)
function engAttnNext(focusId, peek) {
  const l = engAttnList(); if (!l.length) return null;
  const now = performance.now(), seen = new Set(now - ENGSTEP.at > ENG_SURF.stepForget * 1000 ? [] : [...ENGSTEP.seen].filter(id => ENGS.by.has(id)));
  if (focusId && ENGS.by.has(focusId)) seen.add(focusId);
  let e = l.find(x => !seen.has(x.id)), wrapped = false;
  if (!e) { seen.clear(); wrapped = true; e = l.find(x => x.id !== focusId) || l[0]; }
  if (!peek) { seen.add(e.id); ENGSTEP.seen = seen; ENGSTEP.at = now; }
  return { e, wrapped, at: l.indexOf(e), of: l.length };
}
// one step: choose, then the page goes there (PG.goToNeed: back to now, fly to the agent, its transcript open with the cursor in its
// reply box, an asking agent's question in front). how: 'click' or 'key', for the drivers
function engNeedStep(how) {
  const pick = engAttnNext(PG.focus);
  if (!pick) { ENGSTEP.log.push({ how, to: null, t: Date.now() }); return null; }
  const done = typeof PG.goToNeed === 'function' ? PG.goToNeed(pick.e) : (PG.focusAgent(pick.e.id), PG.needHistory(pick.e.a), PG.wake(1200), true);
  ENGSTEP.log.push({ how, to: pick.e.id, cond: pick.e.cond, at: pick.at, of: pick.of, wrapped: pick.wrapped, went: !!done, t: Date.now() });
  if (ENGSTEP.log.length > 40) ENGSTEP.log.shift();
  return pick.e;
}
// drivers: the steps taken (to whom, its place in the list then, whether the round wrapped) and what this round has visited
function engStepState() { return { visited: [...ENGSTEP.seen], steps: ENGSTEP.log.slice() }; }
// a look's signature, for caching a drawn mark
const engLookSig = l => [l.glyph, l.hollow ? 1 : 0, l.level, l.markAlpha, l.prov].join('|');
