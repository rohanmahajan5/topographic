/* Forms (plan 3.1 to 3.4, 3.7, D19): which pieces a label has, in what arrangement. Content is what the pieces say now; a form
   reserves room for its content, so a step done or a new message changes words in place and nothing is laid out again.
   An agent's label is one chunk: the name line on top (mark, name, context, progress: the design minimum), details attached under it, reading
   downward, in one fixed order for every agent. Each larger form contains the smaller.
   Forms of an agent, least to most (3.4; piece 4, 2026-11-16, added the name line with words and fuller members, so the numbers
   from 3 up name new members):
     0  its mark
     1  mark and name
     2  the name line: mark, name, context, progress
     3  the name line ending in a few words of what it is doing or what came of it ("editing report.ts", "3 files changed")
     4  the name line and one line under it (the first piece of its condition, engine/model.js engPieces)
     5  the name line and two lines (the middle form)
     6  the fullest: the name line and up to detailLines[6] lines, a piece that holds still wrapping onto a second or third line
   Each step down loses least first: the fuller forms give lines back one at a time, then the words go, then the figures.
   The plan's steps, once listed around the current one in form 4, are now one piece ("step 2/4: Write the fix"): a list of steps beside
   every label at one to five agents said more than anyone needs at a glance.
   A label cut short for room ends its name in "…" (3.7: short for lack of room never looks like quiet because idle). Text that does
   not fit ends on a whole word with "…"; an aside whose "(" would be left open is dropped whole (as the page's fitCut). */
const ENG_FORM = {
  names: ['mark', 'mark and name', 'name line', 'name line with words', 'one line under it', 'two lines under it', 'the fullest'],
  px: 11.5, detailPx: 11, lineH: 16, detailH: 15, markR: 6, gap: 5,
  nameMax: 300,            // (builder) a name is cut to this many pixels at most
  detailMax: 300,          // (builder, naive; piece 4: 340 read as a slab beside a stack at one to five agents) a detail line's width
  detailLines: { 4: 1, 5: 2, 6: 4 },   // (builder, naive; the planner's start was about five at the fullest: four read better at three
                                       // agents, judged by the picture) lines under the name line, at most
};
const engTw = (t, px, w) => { const ctx = PG.ctx; ctx.font = `${w} ${px}px ${PG.FONT}`; return ctx.measureText(t).width; };
// engCut (the agent tree's, unchanged since eda801ba6): the most letters that fit with "…"
function engCut(t, px, w, max) {
  t = String(t == null ? '' : t); if (engTw(t, px, w) <= max) return { t, cut: false };
  let lo = 0, hi = t.length; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (engTw(t.slice(0, m) + '…', px, w) <= max) lo = m; else hi = m - 1; }
  return { t: t.slice(0, Math.max(1, lo)).trimEnd() + '…', cut: true };
}
// the floor labels' cut (piece 4): whole words with "…"; an open "(" aside is dropped whole, trailing joiners dropped. One word too
// long by itself (a long file name, a name with no spaces) is cut inside it, the last resort.
const engCutWords = (ws, k) => { let t = ws.slice(0, k).join(' '); const o = t.lastIndexOf('('); if (o > 0 && t.indexOf(')', o) < 0) t = t.slice(0, o); return t.replace(/[\s·:;,(—–-]+$/, '') + '…'; };
function engCutW(t, px, w, max) {
  t = String(t == null ? '' : t); if (engTw(t, px, w) <= max) return { t, cut: false };
  const ws = t.split(' ');
  let lo = 0, hi = ws.length - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (engTw(engCutWords(ws, m), px, w) <= max) lo = m; else hi = m - 1; }
  if (lo >= 1 && engCutWords(ws, lo) !== '…') return { t: engCutWords(ws, lo), cut: true };
  return engCut(ws[0], px, w, max);
}
// a number as wide as its widest digits allow: "97%" and "98%" (digits of unequal width in the page's font) take one width, so a
// figure ticking never changes a box (only a digit more does)
const ENG_DIGIT = new Map();
function engNumW(t, px, w) {
  const key = px + '|' + w; let d = ENG_DIGIT.get(key);
  if (!d) { d = '0123456789'.split('').reduce((m, c) => engTw(c, px, w) > engTw(m, px, w) ? c : m, '0'); ENG_DIGIT.set(key, d); }
  return engTw(String(t).replace(/\d/g, d), px, w);
}
// a piece in at most n lines of width max: its longest wording that fits whole in one line, else the first wording wrapped on whole
// words, the last line ending in "…" when it still does not fit; one line: the shortest wording cut on whole words
function engFit(p, px, w, max, n) {
  const ws = [p.text].concat(p.alts || []);
  for (const t of ws) if (engTw(t, px, w) <= max) return [t];
  if (n <= 1) return [engCutW(ws[ws.length - 1], px, w, max).t];
  const words = String(p.text).split(' '), out = []; let cur = '';
  for (let i = 0; i < words.length; i++) {
    const nx = cur ? cur + ' ' + words[i] : words[i];
    if (engTw(nx, px, w) <= max || !cur) { cur = nx; continue; }
    if (out.length === n - 1) { out.push(engCutW(cur + ' ' + words.slice(i).join(' '), px, w, max).t); return out; }
    out.push(cur); cur = words[i];
  }
  if (cur) out.push(engTw(cur, px, w) <= max ? cur : engCutW(cur, px, w, max).t);
  return out;
}
// the spec of agent r's label in form k: lines of segments, its width and height.
// o.sq: how far the name line is squeezed for room (ENG_ROW.squeeze, round 2 of piece 4): { name: px the name may take, meter: the
// context figure and the plan's pie and count stand, words: 'full' (its longest wording that fits wordsW) | 'short' (its shortest
// wording in two thirds of it) | null (none) }. A label in another's way loses, in turn: its figures, then the longer wording of its
// words, then letters of its name on a whole word; its words go last of all.
// o.fix ({ w, n, h }, from the arrangement): the box the arrangement decided; the label's current words are set into it (each line cut
// to its width, no more lines than it had, its height kept), so new words never change its size while the condition holds.
// o.noWords: the name line carries no words (the open card says the request under it); o.wordsW: their width by load
const ENG_SQ0 = { name: 300, meter: true, words: 'full' };
function engForm(r, k, o) {
  const { fmtK } = PG;
  o = o || {}; const F = ENG_FORM, look = engLook(r), f = r.facts, lines = [], fix = o.fix || null; let sq = o.sq || ENG_SQ0;
  const wl = r.say.words || (r.say.short ? [r.say.short] : []);
  // in place (crowded floors, coordinator's ruling): the words take the room the figures took, so the line is no wider than the bare
  // name line; a wording that does not fit there whole is not said, and the figures stay
  let inPlace = null;
  if (k >= 3 && k < 4 && o.inPlace && !o.noWords && wl.length && sq.words) {
    const mw = 6 + engNumW(fmtK(f.ctx), F.px - 0.5, 500) + (r.say.progress ? 7 + 9 + 3 + engNumW(r.say.progress.text, F.px - 0.5, 600) : 0) - 7;
    const t = wl.find(x => engTw(x, F.detailPx, 400) <= mw);
    if (t) { inPlace = { t, w: mw }; sq = Object.assign({}, sq, { meter: false }); }
  }
  const nameMax = Math.min(F.nameMax, sq.name || F.nameMax);
  const name = k >= 1 ? engCutW(r.a.full || r.id, F.px, look.nameW, nameMax) : { t: '', cut: false };
  const segs = [{ mark: look }];
  if (k >= 1) segs.push({ gap: F.gap }, { t: name.t, px: F.px, w: look.nameW, ink: look.nameInk, name: true });
  const near = f.ctxFrac >= ENG_NAIVE.ctxStandsOut && r.cond !== 'idle' && r.cond !== 'archived';
  if (k >= 2 && (sq.meter || near)) {          // a context near its point keeps its warning whatever the squeeze
    const ct = fmtK(f.ctx), pc = near ? ' ' + Math.round(100 * f.ctxFrac) + '%' : '';
    if (sq.meter) segs.push({ gap: 6 }, { t: ct, px: F.px - 0.5, w: 500, ink: '#aaa59c', minW: engNumW(ct, F.px - 0.5, 500) });
    if (near) segs.push(sq.meter ? { gap: 0 } : { gap: 6 }, { t: sq.meter ? pc : pc.trim(), px: F.px - 0.5, w: 700, ink: '#e0622a', minW: engNumW(pc, F.px - 0.5, 700) });
    if (sq.meter && r.say.progress) segs.push({ gap: 7 }, { pie: r.say.progress.frac, ink: r.cond === 'running' ? '#1f8a78' : r.cond === 'asking' ? '#e0622a' : '#85837d' }, { gap: 3 }, { t: r.say.progress.text, px: F.px - 0.5, w: 600, ink: '#5b5f66', minW: engNumW(r.say.progress.text, F.px - 0.5, 600) });
  }
  // form 3: the name line ends in a few words. They stand after the figures: a running agent's words keep their whole width (so a new
  // action never changes the box), and standing last that kept room is blank space after them, not a gap between name and figures
  const W0 = o.wordsW || ENG_NAIVE.nameWordsW;
  let ww = inPlace ? inPlace.w : o.inPlace ? 0 : sq.words === 'full' ? W0 : sq.words === 'short' ? Math.round(W0 * 2 / 3) : 0;
  // (piece A tidy) in a box the arrangement fixed, the words get only the room left in it: new words never run past the box onto a
  // neighbour's label; too little room, and the line says none
  if (fix && ww) { const pre = segs.reduce((s, g) => s + (g.gap || 0) + (g.mark ? 2 * F.markR : 0) + (g.pie != null ? 9 : 0) + (g.t ? Math.max(g.minW || 0, engTw(g.t, g.px, g.w)) : 0), 0);
    ww = Math.min(ww, fix.w - pre - 7); if (ww < 24) ww = 0; }
  if (k >= 3 && k < 4 && !o.noWords && wl.length && ww) {
    const wd = inPlace ? inPlace.t : sq.words === 'full' ? engFit({ text: wl[0], alts: wl.slice(1) }, F.detailPx, 400, ww, 1)[0] : engCutW(wl[wl.length - 1], F.detailPx, 400, ww).t;
    segs.push({ gap: 7 }, { t: wd, px: F.detailPx, w: 400, ink: r.cond === 'asking' ? '#e0622a' : r.cond === 'failed' ? '#c2402a' : look.nameInk === '#85837d' ? '#8f8b84' : '#5b5f66', minW: r.say.ticks ? ww : 0, words: true });
  }
  lines.push({ segs, h: F.lineH });
  if (k >= 4) {
    const quiet = look.nameInk === '#85837d' && r.cond !== 'asking', ind = 2 * F.markR + F.gap, max = (fix ? Math.min(F.detailMax, fix.w - ind) : F.detailMax);
    const needy = r.cond === 'asking' || r.cond === 'failed' || r.cond === 'stalled' || (r.cond === 'finished' && !f.seen);
    let left = F.detailLines[Math.min(k, 6)] + (needy && k === 5 && r.say.lines[0] ? Math.max(0, (r.say.lines[0].wrap || 1) - 1) : 0);
    for (const [pi, p] of r.say.lines.entries()) {
      if (left <= 0) break;
      if (p.minForm && k < p.minForm) continue;          // a piece said only from its form up (the compaction: ENG_NAIVE.compaction.minForm)
      // a quiet agent's lines (idle, or its news seen) in the quiet name's grey: there, but under a working agent's (builder H)
      const strong = p.piece === 'request', wt = strong ? 600 : p.piece === 'said' ? 'italic 400' : 400, ink = strong ? '#e0622a' : p.piece === 'failure' ? '#c2402a' : p.piece === 'compaction' ? '#8a7a52' /* the band's tan, muted */ : quiet ? '#8f8b84' : p.piece === 'said' || p.piece === 'result' ? '#4a5059' : '#5b5f66';
      const n = !p.ticks && (k >= 6 || (k === 5 && (strong || (needy && pi === 0)))) ? Math.min(left, p.wrap || 1) : 1;          // the middle form wraps only what needs you
      engFit(p, F.detailPx, wt, max, n).forEach(t => { lines.push({ segs: [{ t, px: F.detailPx, w: wt, ink }], h: F.detailH, indent: ind, piece: p.piece, minW: p.ticks ? ind + max : 0 }); left--; });
    }
  }
  let w = 0, h = 0;
  lines.forEach(l => { l.w = Math.max(l.minW || 0, (l.indent || 0) + l.segs.reduce((s, g) => s + (g.gap || 0) + (g.mark ? 2 * F.markR : 0) + (g.pie != null ? 9 : 0) + (g.t ? Math.max(g.minW || 0, engTw(g.t, g.px, g.w)) : 0), 0)); });
  if (fix) lines.splice(fix.n);
  engFormKeep(r.id, k, lines);
  lines.forEach(l => { w = Math.max(w, l.w); h += l.h; });
  if (fix) { w = Math.max(w, fix.w); h = Math.max(h, fix.h); }
  // cutAny: some words of it end cut ("…") for want of width or lines (piece A: an agent that needs you is never cut where room stands)
  const cutAny = name.cut || lines.some(l => l.segs.some(g => g.t && /…$/.test(g.t)));
  return { r, k, lines, w, h, cut: name.cut, cutAny, nameW: lines[0].w };
}
// a label's box through ticks of the clock (planner 2026-11-16: "a label's box does not change size or place for a tick of the clock").
// Traced on a made-up floor of 12 at a still camera: a finished or failed agent's name drops from weight 700 to 600 when its news turns
// from fresh to shown (ENG_NAIVE.shownAfter), and "silent 24 min" becomes "silent 25 min"; both changed the box by 0.1 to 4 px with
// nothing else changing. So each line keeps the widest width it has had for the same words (digits aside) in the same form: a weight
// or a number ticking never narrows or jogs the box; new words, a new form or more digits measure afresh. Reserving the boldest
// weight up front was tried and dropped: on a floor of 20 it let three fewer labels fit until the re-decision at shownAfter re-packed
// them, a bigger move than the one it removed.
const ENG_FORM_KEPT = new Map();          // agent id -> Map(words key -> line widths)
function engFormKeep(id, k, lines) {
  const key = k + '|' + lines.map(l => l.segs.map(g => g.t ? String(g.t).replace(/\d+/g, '#') : g.mark ? 'm' : g.pie != null ? 'p' : '').join('|')).join('/');
  let m = ENG_FORM_KEPT.get(id); if (!m) ENG_FORM_KEPT.set(id, m = new Map());
  const had = m.get(key);
  if (had && had.length === lines.length) lines.forEach((l, i) => { l.w = Math.max(l.w, had[i]); });
  m.delete(key); m.set(key, lines.map(l => l.w));
  while (m.size > 12) m.delete(m.keys().next().value);          // (builder, naive) the forms and cuts an agent is tried in, newest kept
}
// draw a spec with its top-left at (x, y); the mark's centre is at (x + markR, y + lineH/2). Returns the box.
function engDrawForm(sp, x, y, alpha) {
  const { ctx, planPie, haloText } = PG;
  const F = ENG_FORM; let yy = y;
  ctx.save(); if (alpha != null) ctx.globalAlpha *= alpha;
  sp.lines.forEach(l => {
    let xx = x + (l.indent || 0); const my = yy + l.h / 2;
    l.segs.forEach(g => {
      if (g.gap != null) { xx += g.gap; return; }
      if (g.mark) { engDrawMark(g.mark, xx + F.markR, my, F.markR); xx += 2 * F.markR; return; }
      if (g.pie != null) { planPie(xx + 4.5, my, 4.3, g.pie, g.ink); xx += 9; return; }
      haloText(g.t, xx, my, g.px, g.ink, 'left', g.w); xx += Math.max(g.minW || 0, engTw(g.t, g.px, g.w));
    });
    yy += l.h;
  });
  ctx.restore();
  return [x, y, x + sp.w, y + sp.h];
}
