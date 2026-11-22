/* The legend (plan 2.3, D18, P5): the one mapping from an agent's condition to its look and base level, the same on every surface
   (floor labels, the agent tree, the list, edge icons, the mini-map). P5 is still to be graded; the stand-in is today's looks, made
   the same everywhere: the attention glyphs the page already draws (attnGlyph: speech bubble, padlock, tick, clock, cross) and the
   harness's own mark for an agent that is simply working or resting. A look means one condition; like conditions look alike. */
const ENG_LEGEND = {
  asking:   { level: 'strong', glyph: 'question', ink: '#e0622a', word: 'asks you',                     key: 'asking: waiting on your answer', keys: [['question', 'asking: a question to answer (speech bubble)'], ['approval', 'asking: an approval to give (padlock)']] },
  failed:   { level: 'plain',  glyph: 'error',    ink: '#c2402a', word: 'failed',                       key: 'failed: its last turn ended in an error' },
  finished: { level: 'plain',  glyph: 'finished', ink: '#2c3440', word: 'finished',                     key: 'finished: its answer is ready' },
  stalled:  { level: 'plain',  glyph: 'stalled',  ink: '#c4861f', word: 'silent, may be stuck',         key: 'stalled: running but silent ten minutes' },
  running:  { level: 'plain',  glyph: null,       ink: '#2c3440', word: 'working',                      key: 'running: working (its harness\'s mark)' },
  stopped:  { level: 'quiet',  glyph: 'stopped',  ink: '#85837d', word: 'stopped',                      key: 'stopped: you stopped its last turn (a hollow square)' },
  idle:     { level: 'quiet',  glyph: null,       ink: '#85837d', word: 'idle',                         key: 'idle: done and seen, resting (mark faded)' },
  archived: { level: 'ground', glyph: null,       ink: '#aaa59c', word: 'archived',                     key: 'archived: in the pile, untouched for two days' },
};
// on the engine's one list (engine/surfaces.js, engSurfWhy): it needs you until you attend to it. Kept on the record
// (one floor build), read after the build has set levels (round 2, item 4: every agent the count counts looks like it needs
// you in its row and its floor label until attended; a failed agent not yet attended is never quieter than a running one)
const engNeeds = r => { if (r.needs === undefined) r.needs = typeof engSurfWhy === 'function' && typeof ENGM !== 'undefined' && ENGM.by ? !!engSurfWhy(r, ENGM.by) : false; return r.needs; };
// the look of one agent: its mark, the ink and weight of its name, by condition and level. Seen finished or failed agents keep their
// sign, hollow and quiet (a shown change stays marked, quietly, until seen; once seen it is a fact, not news)
function engLook(r) {
  // a finish or failure seen, or a child's while its parent takes it (D4: low), keeps its sign, hollow and quiet (grading 7); one shown
  // but not seen (2.6) keeps its solid sign, quieter
  const L = ENG_LEGEND[r.cond], news = r.cond === 'failed' || r.cond === 'finished', low = r.level !== 'strong' && r.level !== 'call' && r.news !== 'shown';
  // an agent that needs you keeps its solid sign at full strength and a loud name while it does: being shown is not being seen
  // (round 2; before, a shown finish or failure turned quieter after ENG_NAIVE.shownAfter seconds)
  const needs = engNeeds(r);
  const seenNews = news && !needs && (r.facts.seen || low), shown = news && !seenNews && r.news === 'shown';
  const lv = r.level, approval = r.cond === 'asking' && (r.a.asks || []).some(q => !q.resolved && q.kind === 'approve');
  return {
    glyph: L.glyph ? (approval ? 'approval' : L.glyph) : null, hollow: seenNews, prov: r.a.prov,
    markAlpha: needs ? 1 : r.cond === 'idle' ? 0.45 : seenNews ? 0.7 : shown ? 0.8 : 1, shown, needs,
    nameInk: r.cond === 'asking' ? L.ink : !needs && (lv === 'quiet' || seenNews) ? '#85837d' : '#2c3440',
    nameW: needs || lv === 'call' || lv === 'strong' ? 700 : lv === 'quiet' || seenNews ? 500 : 600,
    level: lv, word: L.word,
  };
}
// a mark at (x, y), r = half size. Glyphs from the page's attention set; a hollow sign for seen news; else the harness mark.
function engDrawMark(look, x, y, rad, g) {
  const { attnGlyph, rgba, provMark } = PG; g = g || PG.ctx;
  if (look.glyph === 'stopped') {          // you stopped it (walk 2026-11-19): a quiet hollow ring with a small square, the quiet grey of a seen mark
    g.save(); g.globalAlpha = Math.min(0.7, look.markAlpha); g.strokeStyle = '#85837d'; g.fillStyle = '#85837d'; g.lineWidth = 1.3; g.beginPath(); g.arc(x, y, rad - 0.6, 0, 7); g.stroke();
    const k = rad * .32; g.fillRect(x - k, y - k, 2 * k, 2 * k); g.restore(); return;
  }
  if (look.glyph && !look.hollow) { attnGlyph(g, look.glyph, x, y, rad, look.markAlpha); if (look.level === 'call') { g.save(); g.strokeStyle = rgba('#e0622a', 0.35); g.lineWidth = 2; g.beginPath(); g.arc(x, y, rad + 3, 0, 7); g.stroke(); g.restore(); } return; }
  if (look.glyph && look.hollow) {
    const c = look.glyph === 'error' ? '#c2402a' : '#85837d';
    g.save(); g.globalAlpha = look.markAlpha; g.strokeStyle = c; g.lineWidth = 1.3; g.lineCap = 'round'; g.beginPath(); g.arc(x, y, rad - 0.6, 0, 7); g.stroke();
    g.beginPath(); if (look.glyph === 'error') { const k = rad * .36; g.moveTo(x - k, y - k); g.lineTo(x + k, y + k); g.moveTo(x + k, y - k); g.lineTo(x - k, y + k); } else { g.moveTo(x - rad * .42, y + rad * .02); g.lineTo(x - rad * .1, y + rad * .36); g.lineTo(x + rad * .45, y - rad * .3); }
    g.stroke(); g.restore(); return;
  }
  // the harness's own mark in its brand colour, whatever the state (2026-11-06: the original colour is kept; the page's agent
  // colour, brandOf); idle or seen only fades it (markAlpha). From 11-15 to 11-21 this drew every mark grey (MARK_REST / quiet grey)
  provMark(look.prov, x, y, rad * 2, look.ink || PG.brandOf(look.prov), look.markAlpha, g);
}
// the key you can call up: every look with its meaning, in the legend's order
// asking lists both its signs (grading 1: one colour for the one condition; the sign says which kind of answer is wanted)
const engLegendKey = () => Object.keys(ENG_LEGEND).filter(k => k !== 'archived').flatMap(k => ENG_LEGEND[k].keys ? ENG_LEGEND[k].keys.map(([g, key]) => ({ cond: k, glyph: g, key })) : [{ cond: k, key: ENG_LEGEND[k].key }]);
