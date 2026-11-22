/* The floor's labels (plan 3.5, 3.7, 3.8, 4.1, 5.3 to 5.6; D7, D8, D9, D10, D13, D15, D19, D20; the planner's grading 1, points 1 to 5).
   Far out, labels stand in a compact row just above the floor's back edge: each name line over its own stack on a straight vertical
   line, bending only where it had to shift, tiers filling upward from the floor. Stacks standing close are set as one list (B4): one
   block, one left edge, just right of its rightmost stack, members top to bottom in their stacks' order (leftmost on top). Every label
   takes one order of places (engPlaceOrder): above the floor in its form, then above the floor with fewer words, and only then at its
   own stack, just above its top, its form growing downward from a name that stays put (it covers floor detail, D7). A stack with no
   floor label carries the legend's mark on its top (form 0). A stack not in view has no floor label.
   Units are placed right to left, each at its cheapest free place (cost: every pixel of line, every bend); that keeps lines from
   crossing and from running through a label. The arrangement is decided only at rest, on a change of condition, of the candidates,
   of focus, or of the open card; in between labels ride with their stacks' feet and only their content changes.
   Every number below is naive and the builder's own (nothing measured behind any of them). */
const ENG_ROW = {
  gap: 10,            // (builder, naive) px between the floor's back edge and the bottom of the row's lowest tier
  tierGap: 5,         // (builder, naive) px between tiers (a shifted line runs its jog in this gap)
  colGap: 14,         // (builder, naive) px between labels side by side
  listDx: 46,         // (builder, naive) stacks whose tops stand closer than this across the screen form one list ...
  listDy: 90,         // (builder, naive) ... and closer than this up and down
  listPad: 9,         // (builder, naive) px from a list's rightmost stack to the list's left edge
  listRowGap: 2,      // (builder, naive) px between members of a list
  topPad: 4,          // (builder, naive) px below the top-right buttons' band (PG.PICK_CLEAR) that the row may reach
  tree: [0, 0, 300, 420],   // (brief) the agent tree's corner, used only until engTreeState() reports its box
  leadMin: 10,        // (builder, naive) a row label's bottom stands at least this far above its stack's top
  flagLift: 18,       // (builder, naive) a line starts this far higher when the stack carries the page's flag
  restMs: 220,        // (builder, naive) the camera counts as at rest after this long unmoved
  driftMax: 4,        // (builder, naive) px a list member's stack may move on the floor from its list's first stack before the list is re-decided
  keepBonus: 40,      // (builder, naive) px of line: a unit keeps its place on screen unless another is better by this
  keepStill: 5000,    // (builder, round 4) the same while the camera has not moved: a change of one agent moves no other label that
                      // still has its place free (grading 8: nothing moves without a cause)
  lineCost: 1,        // (builder, naive) cost of one px of line (5.1 tying)
  bendCost: 30,       // (builder, naive) cost of one bend, in px of line
  cutW: 2,            // (builder, naive) cost per px of label run off the window's side
  shiftMax: 260,      // (builder, naive) a row label shifts at most this far from over its stack
  rideScale: 0.15,    // (lead, piece A, naive) a refit the page makes by itself that changes the zoom by less than this share keeps the
                      // arrangement (labels ride with their stacks); a larger one is arranged afresh
  shutCost: 200,      // (lead, piece A round 2, naive) px of line a label pays for standing over another agent's stack (closing its way up)
  lowJog: 12,         // (lead, piece A round 2, naive) px over its stack's top where a line may turn across when labels above the stack stop it higher
  wideStep: 16,       // (lead, piece A round 2, naive) px between the places tried across the window when none near a label's stack is free
  stackStep: 8,       // (lead, piece A, naive) px a label at its stack may move up or down beside its column, a step at a time, to stand clear
  stackGap: 6,        // (builder, naive) an at-stack label starts this far right of its stack's top face
  treeClear: 12,      // (lead, round 4) px every label and every line keeps off the agent tree's box (never under it, never along its edge)
  winPad: 2,          // (builder, naive) a label stands wholly inside the window, this far in from each side at least
  // interest from the view (D15): the share of the window a stack takes (its column's box over the window's area), in bands
  shareBands: { middle: 0.004, near: 0.02 },   // (builder, naive; read off the stills at far, middle and near zoom of 12)
  // how much each label says by load: ENG_NAIVE.labelsByLoad and labelsOpenByBand (engine/naive.js; piece 4)
  quietNamesUpTo: 4,  // (builder, naive) quiet agents (idle; failed or finished and seen) keep a floor name while the floor has no more
                      // candidates than this or the load's row of ENG_NAIVE.labelsByLoad says quiet, unless nearer (middle or near band)
  formMin: 2,         // (design: name, context and progress are the bare minimum) a label never says less than the name line
  // (builder, naive; round 2 of piece 4) a label in the way of one with no place is squeezed a step at a time before anyone is left out:
  // its figures go, then the longer wording of its words, then letters of its name on a whole word; its words go last of all
  squeeze: [{ name: 300, meter: true, words: 'full' }, { name: 300, meter: false, words: 'full' }, { name: 300, meter: false, words: 'short' },
    { name: 200, meter: false, words: 'short' }, { name: 140, meter: false, words: 'short' }, { name: 140, meter: false, words: null }],
  hoverForm: 6,       // (builder) the hovered agent's label opens to this form (the fullest), over its neighbours
  markLift: 5,        // (builder, naive) form 0: the mark's centre stands this far above its stack's top plus its radius
  lineInk: '#bdb7ab', lineW: 1,  // (builder) the lines' look (as the concept's lead lines)
  hoverBg: 'rgba(252,251,248,0.97)', hoverEdge: '#d8d2c6',
  stackBg: '#f3f1ec',  // (builder H) the page's paper, under a label of more than one line at its own stack (drawn at 0.86)
};

const ENGR = { dec: null, n: 0, ms: 0, camKey: '', movedAt: 0, frameRide: null, marks: null, spots: new Map() };

const engRowCamKey = () => { const c = PG.cam; return c.ox.toFixed(1) + ',' + c.oy.toFixed(1) + ',' + c.s.toFixed(5) + ',' + PG.W + ',' + PG.H; };
// a unit rides with its first stack's foot (not its top: a stack growing with new calls, or its flag coming and going, moves nothing)
const engRowFoot = t => { const r = PG.REC[t.a.id]; return r && r.footS ? r.footS : t.at; };
const engRowLineTop = (t, face) => [t.at[0], t.at[1] - face - (t.flag ? ENG_ROW.flagLift : 0)];
const engOv = (a, b, px, py) => a[0] < b[2] + (px || 0) && a[2] > b[0] - (px || 0) && a[1] < b[3] + (py || 0) && a[3] > b[1] - (py || 0);
const engSegBox = (s, b, pad) => Math.min(s[0], s[2]) < b[2] + pad && Math.max(s[0], s[2]) > b[0] - pad && Math.min(s[1], s[3]) < b[3] + pad && Math.max(s[1], s[3]) > b[1] - pad;
function engSegX(p, q) {
  const pv = p[0] === p[2], qv = q[0] === q[2];
  if (pv === qv) return pv ? Math.abs(p[0] - q[0]) < 1 && Math.min(p[1], p[3]) < Math.max(q[1], q[3]) && Math.max(p[1], p[3]) > Math.min(q[1], q[3])
    : Math.abs(p[1] - q[1]) < 1 && Math.min(p[0], p[2]) < Math.max(q[0], q[2]) && Math.max(p[0], p[2]) > Math.min(q[0], q[2]);
  const v = pv ? p : q, h = pv ? q : p;
  return v[0] > Math.min(h[0], h[2]) + 0.5 && v[0] < Math.max(h[0], h[2]) - 0.5 && h[1] > Math.min(v[1], v[3]) + 0.5 && h[1] < Math.max(v[1], v[3]) - 0.5;
}
const engSegLen = s => Math.abs(s[2] - s[0]) + Math.abs(s[3] - s[1]);
const engPts2Segs = pts => pts.slice(1).map((q, i) => [pts[i][0], pts[i][1], q[0], q[1]]);

function engRowBackY() { const pl = PG.planeOf(); return Math.min(PG.P(pl[0][0], pl[0][1], 0)[1], PG.P(pl[1][0], pl[1][1], 0)[1]); }
function engRowTreeBox() {
  const b = typeof engTreeBox === 'function' ? engTreeBox() : null; if (b && b.length === 4 && b.every(Number.isFinite)) return b;
  const s = typeof engTreeState === 'function' ? engTreeState() : null; return s && s.box && s.box.every(Number.isFinite) ? s.box.slice() : ENG_ROW.tree.slice();
}
// a label wholly inside the window (lead, round 4: one that cannot be goes as an out-of-view stack goes: no floor label)
const engInWin = b => b[0] >= ENG_ROW.winPad && b[2] <= PG.W - ENG_ROW.winPad && b[1] >= 0 && b[3] <= PG.H;
// clear of the agent tree's box by treeClear (a label box, or a line's segment)
const engTreeFree = (b, tree) => !tree || !engOv(b, tree, ENG_ROW.treeClear, ENG_ROW.treeClear);
const engSegTreeFree = (sg, tree) => !tree || !engSegBox(sg, tree, ENG_ROW.treeClear);
function engRowObstacles() {
  const W = PG.W, o = [engRowTreeBox()];
  // the open card: an obstacle only while the page places it itself; once the card hangs under its agent's label (PG.cardFor), it is
  // that label's reserved block and must not push its own label away
  const ask = typeof PG.cardFor !== 'function' && PG.askBox && PG.askBox(); if (ask) o.push(ask.slice());
  const tp = PG.tpBox; if (tp) o.push(tp.slice());
  const tpt = PG.tptBox && PG.tptBox(); if (tpt) o.push(tpt);
  o.push([0, -1e4, W, PG.PICK_CLEAR + ENG_ROW.topPad]);          // the band of the top-right buttons, taken across (rough)
  const f = PG.focus && PG.REC[PG.focus]; if (f && f.block) o.push(f.block.slice());
  return o.filter(b => b && b.length === 4 && b.every(Number.isFinite));
}
const engCardFor = id => typeof PG.cardFor === 'function' && !!PG.cardFor(id);
const engCardSize = id => { const s = typeof PG.cardSize === 'function' ? PG.cardSize(id) : null; return s && s.length === 2 ? s : null; };
// a quiet agent: nothing for you here (idle, or a failure or finish already seen)
// a strong or call agent (by its level) is never left without a floor label for want of room (round 3)
// what a decision rests on per agent: its condition, whether the change was seen, its level (seen/unseen divides failed and finished: 2.3)
const engSig = r => r.cond + '|' + (r.facts.seen ? 's' : 'u') + '|' + r.level + '|' + r.condAt;
// which pieces a label has to say (piece A, fault 1): a label's box holds the lines it had when it was arranged, so a piece that arrives
// later (the first thing a running agent says, a plan's step) asks for its own label to be arranged again
const engPieceSig = r => r.say.lines.map(p => p.piece).join(',');
const engStrong = r => r.level === 'strong' || r.level === 'call' || (typeof engNeeds === 'function' && engNeeds(r));          // and every agent on the engine's list (round 2)
const engQuiet = r => r.cond === 'idle' || r.cond === 'stopped' || ((r.cond === 'failed' || r.cond === 'finished') && r.facts.seen);

// lists: stacks close on screen, chained in left-to-right order (so a list's members are neighbours in the order)
function engRowUnits(items) {
  // grouped by the stacks' own tops, without the flag's lift (a request coming or going changes no list: grading 8)
  const g = it => it.top0 || it.top;
  const xs = items.slice().sort((p, q) => p.top[0] - q.top[0] || (p.id < q.id ? -1 : 1)), units = [];
  xs.forEach(it => {
    const u = units[units.length - 1], last = u && u.m[u.m.length - 1];
    if (last && g(it)[0] - g(last)[0] < ENG_ROW.listDx && Math.abs(g(it)[1] - g(last)[1]) < ENG_ROW.listDy) u.m.push(it);
    else units.push({ m: [it] });
  });
  return units;
}

// the row: units right to left, each at its cheapest free place on the tiers; false at the first unit that has none
function engRowLayout(units, ctx) {
  const R = ENG_ROW, F = ENG_FORM, placed = [], segs = (ctx.fixedSegs || []).slice(), boxes = ctx.fixed.slice(), KB = ctx.still ? R.keepStill : R.keepBonus; let total = 0, minY = Infinity;
  const tierH = F.lineH + R.tierGap;
  // two passes (grading 8: nothing moves without a cause): first every unit whose place at the last decision is still free takes exactly
  // that place (right to left); then the rest find their cheapest place around them (right to left). Crossings and covered labels are
  // refused in any order.
  // (piece A, round 2) units of agents that need you take their places first, so free room above the floor goes to them before any other
  // (piece B, the one order) a unit that found no place at the last try of the whole arrangement goes before all others (ctx.first): the
  // others make room for it (a running label whose stack had moved under the needing ones' labels stood on the floor with room above)
  const nd = ui => units[ui].m.some(it => ctx.first && ctx.first.has(it.id)) ? 2 : units[ui].m.some(it => it.needs) ? 1 : 0;
  const order = units.map((u, i) => i).reverse().sort((a, b) => nd(b) - nd(a)), done = [];
  const tryUnit = (ui, oldOnly) => {
    const u = units[ui], m = u.m, ids = m.map(it => it.id);
    const specs = m.map(it => it.spec), w = Math.max(...specs.map(s => s.w)), hs = specs.map(s => s.h);
    const h = hs.reduce((s, x) => s + x, 0) + R.listRowGap * (m.length - 1);
    const xsMax = Math.max(...m.map(it => it.top[0])), single = m.length === 1, sx = m[0].top[0];
    const X0 = single ? sx - F.markR : xsMax + R.listPad;
    const yMax = Math.min(...m.map(it => it.top[1])) - R.leadMin;
    const leftLimit = ui > 0 ? Math.max(...units[ui - 1].m.map(it => it.top[0])) + R.colGap : -1e9;          // never over a stack still to come
    const Xs = [X0];
    boxes.concat(ctx.obst).forEach(b => {
      const xr = b[2] + R.colGap, xl = b[0] - R.colGap - w;
      if (xr > X0 && xr - X0 <= R.shiftMax) Xs.push(xr);          // a list may move right too, its lines running further across
      if (single && xl < X0 && X0 - xl <= R.shiftMax && xl >= leftLimit) Xs.push(xl);
    });
    if (single && X0 < 6 && 6 - X0 <= R.shiftMax) Xs.push(6);          // in from the window's left edge
    const xw = PG.W - 6 - w; if (single && xw < X0 && X0 - xw <= R.shiftMax && (xw >= leftLimit || m[0].card)) Xs.push(xw);
    // a quiet label may go as far as the window's sides for free room (builder H: it takes only room nobody else needs, and that room
    // is often beyond shiftMax, at the floor's far side under the tree)
    if (single && m[0].quiet) { if (6 < X0 && 6 >= leftLimit) Xs.push(6); if (xw < X0 && xw >= leftLimit) Xs.push(xw); }          // a card's label may move over a stack still to come; that one then goes under it or to its stack          // in from the window's right edge
    const old = ctx.old && ctx.old.get(ids.join('|'));
    if (oldOnly) { if (!old) return null; Xs.length = 0; Xs.push(old[0]); }
    else if (old && Math.abs(old[0] - X0) <= R.shiftMax && (old[0] >= X0 - 0.5 || (single && old[0] >= leftLimit))) Xs.push(old[0]);
    const scan = Xs => { let best = null;
    for (let k = 0; ; k++) {
      const Y1 = ctx.base - k * tierH, Y0 = Y1 - h;
      if (Y0 < ctx.top) break;
      if (Y1 > yMax) continue;
      if (oldOnly && Math.abs(Y0 - old[1]) >= 0.5) continue;
      if (best && m.reduce((a, it) => a + Math.max(0, it.top[1] - Y1), 0) - KB > best.cost) break;          // its lines alone cost more: nothing higher can win
      for (const X of Xs) {
        const box = [X, Y0, X + w, Y1];
        if (!engInWin(box) || !engTreeFree(box, ctx.tree)) continue;          // wholly in the window, clear of the tree
        if (ctx.obst.some(o => engOv(box, o, 2, 2)) || boxes.some(b => engOv(box, b, R.colGap, R.tierGap - 0.5))) continue;
        if (ctx.cols.some(c => !ids.includes(c.id) && engOv(box, c.box, 2, 0))) continue;          // over another stack's column
        // a single label's line: straight up when its mark stands over the stack; else up, across, up. Across runs just under the label,
        // or (piece A, round 2) low, just over its stack's top, when labels above the stack would stop it higher up
        const paths = [];
        if (single) {
          const my = Y0 + F.lineH / 2, ix = m[0].top[0], iy = m[0].top[1], mx = X + F.markR, mb = my + F.markR + 1;
          if (Math.abs(mx - ix) < 0.5) paths.push({ mine: [[ix, iy, ix, mb]], bends: 0, jy: null });
          else for (const jy of [Y1 + R.tierGap / 2, iy - R.lowJog]) if (jy < iy && jy > Y1) paths.push({ mine: [[ix, iy, ix, jy], [ix, jy, mx, jy], [mx, jy, mx, mb]], bends: 2, jy });
        } else { let y = Y0; const mine = []; for (let i = 0; i < m.length; i++) { const my = y + F.lineH / 2, ix = m[i].top[0], iy = m[i].top[1]; mine.push([ix, iy, ix, my], [ix, my, X - 2, my]); y += hs[i] + R.listRowGap; } paths.push({ mine, bends: m.length, jy: null }); }
        if (segs.some(s => engSegBox(s, box, 1.5))) continue;
        for (const pth of paths) {
          const { mine, bends } = pth;
          if (mine.some(sg => !engSegTreeFree(sg, ctx.tree))) continue;          // no line under the tree or along its edge
          if (mine.some(s => boxes.some(b => engSegBox(s, b, 1.5)) || segs.some(q => engSegX(s, q)))) continue;
          const len = mine.reduce((a, s) => a + engSegLen(s), 0);
          // (piece A, round 2) a label standing right over another agent's stack closes that stack's way up to the room above the floor
          // and so does its line running across over that stack (a crossing is refused, so a low line across is a wall to every stack under it)
          const shut = (ctx.tops || []).reduce((a, q) => a + (!ids.includes(q[0]) && ((q[1] >= X - 4 && q[1] <= X + w + 4 && Y1 < q[2])
            || mine.some(s => Math.abs(s[1] - s[3]) < 0.5 && s[1] < q[2] && q[1] >= Math.min(s[0], s[2]) - 2 && q[1] <= Math.max(s[0], s[2]) + 2)) ? 1 : 0), 0);
          const cost = R.lineCost * len + R.bendCost * bends + R.shutCost * shut + R.cutW * (Math.max(0, X + w - PG.W) + Math.max(0, -X))
            - (old && Math.abs(old[0] - X) < 0.5 && Math.abs(old[1] - Y0) < 0.5 ? KB : 0);
          if (!best || cost < best.cost) best = { cost, X, Y0, box, mine, hs, w, h, bends, len, jog: single && Math.abs(X + F.markR - sx) >= 0.5, jy: pth.jy };
        }
      }
    }
    return best; };
    let best = scan(Xs);
    // (piece A, round 2) none near its stack: any free room above the floor its line can reach, across the whole window and up to its top,
    // before it would stand at its stack over the floor (a list's lines run right to its block, so a list looks right of its stacks only)
    // (piece A tidy) not in the whole arrangement above five agents: there it moved 7 labels at a refit with no agent changing; an open
    // card, and an agent that needs you, still look across the window (piece B, d: the card hung at its stack at the window's foot with
    // the window above it empty; with it placed first, an asking label went down to its stack in its stead)
    if (!best && !oldOnly && (!ctx.crowd || m.some(it => it.card || it.needs))) { const g = [];
       for (let x = single ? 6 : X0; x <= PG.W - 6 - w; x += R.wideStep) g.push(x); best = scan(g); }
    return best;
  };
  const commit = (ui, best) => { done.push(ui); boxes.push(best.box); best.mine.forEach(s => segs.push(s)); placed[ui] = best; total += best.cost; minY = Math.min(minY, best.Y0); };
  // a unit placed first (ctx.first) takes its place before the others keep their old places: they make room for it
  for (const ui of order) { if (nd(ui) < 2) break; const b = tryUnit(ui, false); if (!b) return { ok: false, at: ui, blockers: units[ui].m.map(it => it.id) }; commit(ui, b); }
  if (ctx.old) for (const ui of order) { if (placed[ui]) continue; const b = tryUnit(ui, true); if (b) commit(ui, b); }
  for (const ui of order) {
    if (placed[ui]) continue;
    const best = tryUnit(ui, false);
    if (!best) return { ok: false, at: ui, blockers: units[ui].m.map(it => it.id).concat(done.flatMap(j => units[j].m.map(it => it.id))) };          // it and those placed before it
    commit(ui, best);
  }
  return { ok: true, placed, cost: total, rise: units.length ? ctx.base - minY : 0 };
}

// at its own stack: the name line's mark just right of the top face, the name level with the top, details growing downward
// (right of the column; left of it when the right side covers another stack or a label placed before; else it covers, drawn by claim)
function engStackPlace(c, spec, taken, cols, ctx) {
  const F = ENG_FORM, Y0 = c.top[1] - F.lineH, hw = c.t.hw + ENG_ROW.stackGap;
  // beside its top first; then a little higher or lower beside its column, nearest first (piece A: at 800x500 two labels at their
  // stacks overlapped by 4 px and one of them went, leaving a running agent with its mark only)
  const tries = [];
  for (let j = 0; j <= Math.ceil(spec.h / ENG_ROW.stackStep); j++) for (const dy of j ? [-j * ENG_ROW.stackStep, j * ENG_ROW.stackStep] : [0])
    for (const X of [c.top[0] + hw, c.top[0] - hw - spec.w]) tries.push({ X, Y0: Y0 + dy, box: [X, Y0 + dy, X + spec.w, Y0 + dy + spec.h], side: X > c.top[0] });
  // allowed: wholly in the window, clear of the tree and of the page's own boxes (lead, round 4); free: also over no other stack or label
  const ok = p => engInWin(p.box) && engTreeFree(p.box, ctx.tree) && !ctx.obst.some(o => o !== ctx.tree && engOv(p.box, o, 2, 2));
  // (piece A, round 2: over the floor a label never covers a stack, its own included, nor another label or the card; no such place: none)
  const free = p => ok(p) && !taken.some(b => engOv(p.box, b, 4, 2)) && !cols.some(k => engOv(p.box, k.box, 3, 2));
  const got = tries.find(free);
  return got ? Object.assign(got, { covers: !free(got), left: !got.side }) : null;          // null: it cannot stand wholly in view
}

// the box an arrangement decided for a label (piece 4, stillness): its width, its number of lines and its height. Until the next
// arrangement the label's words are set into this box (engForm's fix): a running agent's new action, message or step replaces the old
// words in place; a piece that arrives takes its place in the order and the last line gives way; a piece that leaves leaves its line
// empty. The arrangement changes it only on a change of condition, of the agents in view, of focus or card, or the camera at rest.
const engFixOf = sp => sp && !sp.card ? { w: sp.w, n: sp.lines.length, h: sp.h } : null;
// the label spec for a candidate: the card's agent reserves the card's block under its name line (grading 4: the card is its label)
function engRowSpec(c, k, nm, fix, ww, inPlace) {
  const sp = engForm(c.r, c.card ? 2 : k, { sq: ENG_ROW.squeeze[nm || 0], noWords: c.card, fix: c.card ? null : fix, wordsW: ww, inPlace });
  if (!c.card) return sp;
  const F = ENG_FORM, cs = engCardSize(c.id) || [0, 0];          // [w, h] from the label's left edge, under the name line
  // counted as at least form 4: the card says the request (form 4's piece) and its buttons, under the name line
  return Object.assign({}, sp, { k: Math.max(4, k), w: Math.max(sp.w, cs[0]), h: F.lineH + cs[1], card: cs, nameOnly: { lines: [sp.lines[0]], w: sp.w, h: F.lineH } });
}

// decide: who has a floor label, in which form, where. Starts from what is on screen when the camera has not moved.
// the agents in view that may have a floor label (candidates), each stack's column, and why the others have none
function engRowCands(tops, face) {
  const R = ENG_ROW, W = PG.W, H = PG.H;
  const out = new Map(), cands = [], cols = [], allCols = [];
  const backY = engRowBackY(), base = backY - R.gap, treeB = engRowTreeBox();
  tops.forEach(t => {
    const r = engRec(t.a); if (!r) return;
    const lt = engRowLineTop(t, face);
    if (lt[0] < 0 || lt[0] > W || lt[1] < 0 || lt[1] > H) { if (r.cond !== 'archived') out.set(r.id, 'not in view'); return; }
    if (lt[0] >= treeB[0] && lt[0] <= treeB[2] && lt[1] >= treeB[1] && lt[1] <= treeB[3]) { if (r.cond !== 'archived') out.set(r.id, 'under the agent tree'); return; }
    if (lt[1] < base) cols.push({ id: r.id, box: [t.at[0] - t.hw, lt[1], t.at[0] + t.hw, base] });
    allCols.push({ id: r.id, box: [t.at[0] - t.hw, lt[1], t.at[0] + t.hw, engRowFoot(t)[1]] });
    if (r.cond === 'archived') { out.set(r.id, 'archived'); return; }
    const share = (2 * t.hw) * (Math.max(0, t.px) + 2 * face) / (W * H);
    const band = share >= R.shareBands.near ? 'near' : share >= R.shareBands.middle ? 'middle' : 'far';
    cands.push({ t, r, id: r.id, top: lt, top0: [t.at[0], t.at[1] - face], share, band, quiet: engQuiet(r) && !engStrong(r), strong: engStrong(r), card: engCardFor(r.id),
      needs: !(engQuiet(r) && !engStrong(r)) && (r.need.rate >= ENG_NAIVE.bands.medium || engStrong(r)) });
  });
  // (piece A tidy) with one to five agents nobody is quiet for placing: an idle agent's label takes part like any other (at least its
  // name and a line of words) and goes to its stack only when no room is left above the floor; it still claims last
  const few = cands.length <= ENG_NAIVE.labelsByLoad[0].upTo;
  cands.forEach(c => { c.idle = c.quiet; if (few) c.quiet = false; });
  // claim order (3.5, grading 5): agents with something for you first, by urgency then standing; quiet agents last
  cands.sort((p, q) => (p.idle - q.idle) || q.r.need.rate - p.r.need.rate || q.r.standing - p.r.standing || (p.id < q.id ? -1 : 1));
  return { out, cands, cols, allCols, base };
}

// decide: who has a floor label, in which form, where. Starts from what is on screen when the camera has not moved.
function engRowDecide(tops, face, why) {
  const t0 = performance.now(), R = ENG_ROW, F = ENG_FORM, prev = ENGR.dec, camSame = prev && prev.camKey === engRowCamKey();
  const W = PG.W, H = PG.H, top = PG.PICK_CLEAR + R.topPad;
  // a change of some agents at a still camera changes only their labels (piece A, fault 3): tried first, the whole arrangement only
  // when that cannot be had
  // the same after the camera moved by itself (the floor refitting to a change of the floor): labels ride with their stacks, and only
  // those that now stand badly are placed again (at twelve, one refit re-arranged every label)
  const ride = !camSame && prev && !prev.provisional && why === 'camera at rest' && ENGR.allAuto && Math.abs(PG.cam.s / prev.s - 1) < ENG_ROW.rideScale;
  if (prev && !prev.provisional && (camSame && /^(condition of|words of|candidates|card|a label rode out of view|labels ride into each other)/.test(why) || ride) && engRowPatch(tops, face, why, prev, ride)) {
    engRowUncut(tops, face); ENGR.ms = performance.now() - t0; ENGR.n++; return;
  }
  const { out, cands, cols, allCols, base } = engRowCands(tops, face);
  // zooming in never shows less (5.3): while the camera has only come nearer (or panned) since the last decision, every agent that
  // was labelled keeps at least the form it had; zooming out lets forms fall again
  const nearer = prev && !prev.provisional && prev.s && PG.cam.s >= prev.s * 0.999, floorOf = id => nearer && prev.formOf ? prev.formOf.get(id) : undefined;
  const load = ENG_NAIVE.labelsByLoad.find(x => cands.length <= x.upTo);
  let pool = cands.filter(c => {
    if (!c.quiet || c.card || c.band !== 'far' || load.quiet || cands.length <= R.quietNamesUpTo || floorOf(c.id) != null) return true;
    out.set(c.id, 'quiet (' + c.r.cond + '): the tree names it; its stack shows its mark'); return false;
  });
  // every label starts above the floor; its stack comes only by the ways out below (the order of engPlaceOrder), or when the window
  // has no room above the floor at all
  const rowRoom = base - F.lineH >= top;
  pool.forEach(c => { c.at = rowRoom ? 'row' : 'stack'; });
  const obst = engRowObstacles(), ctx = { top, base, obst, tree: obst[0], cols, allCols, old: null, fixed: [], still: !!camSame && !prev.provisional, crowd: cands.length > ENG_NAIVE.labelsByLoad[0].upTo, first: new Set(), tops: cands.map(c => [c.id, c.top[0], c.top[1]]) };          // obst[0] is the tree's box
  if (prev) { ctx.old = new Map(); prev.units.forEach(u => { const a = tops.find(t => t.a.id === u.anchor); if (a) { const p = engRowFoot(a); ctx.old.set(u.ids.join('|'), [p[0] + u.off[0], p[1] + u.off[1]]); } }); }
  // forms per like set (D19, grading 2): like = same condition (seen or not) and same band of share of the view
  const setOf = c => c.r.cond + (c.r.facts.seen ? '' : '*') + '|' + c.band, form = new Map(), keep = camSame && !prev.provisional;
  const setMin = new Map();          // the least form a set may take: the richest floor among its members (zooming in never shows less)
  // a set whose agents have words never falls to the bare name line by forms (round 2 of piece 4): its words go only by squeezing, last
  const minOf = c => !c.card && c.r.say.words && c.r.say.words.length ? 3 : R.formMin;
  pool.forEach(c => { const s = setOf(c), f = floorOf(c.id) || minOf(c); setMin.set(s, Math.max(setMin.get(s) || R.formMin, f, minOf(c))); });
  // how much by load (piece 4): the number of agents in view picks a row of ENG_NAIVE.labelsByLoad; a set that needs you starts at its
  // need form, the rest at rest; a set may open further (to open, or by its band of zoom) only where it pushes nobody up a tier
  const needSet = new Set(pool.filter(c => !c.quiet && (c.r.need.rate >= ENG_NAIVE.bands.medium || c.strong)).map(setOf));          // a context near its point (low) does not count
  const startOf = s => needSet.has(s) ? load.need : load.rest;
  const openOf = s => Math.max(startOf(s), load.open, ENG_NAIVE.labelsOpenByBand[s.split('|')[1]] || 0);
  pool.forEach(c => { const s = setOf(c); if (!form.has(s)) form.set(s, Math.max(setMin.get(s), keep && prev.form.has(s) ? prev.form.get(s) : startOf(s))); });
  // names are cut per label, only for a label in the way of one with no place, and never carried from an earlier decision (round 5: one
  // floor-wide cut, kept by every later decision at rest, had cut every name on the floor for one label's want of room)
  let nameI = 0, tries = 0; pool.forEach(c => { c.nmI = 0; });
  const log = [], specCache = new Map();
  const spec = (c, k, nm) => { const key = c.id + '|' + k + '|' + nm; let s = specCache.get(key); if (!s) { s = engRowSpec(c, k, nm, null, load.words, load.inPlace); specCache.set(key, s); } return s; };
  const attempt = lab => {
    tries++;
    lab.forEach(c => { c.k = form.get(setOf(c)); c.nm = c.nmI; });
    const rowLab = lab.filter(c => c.at === 'row'), us = engRowUnits(rowLab);
    // never mix forms for one condition inside one list: the list takes the least of its members' forms per condition
    us.forEach(u => { if (u.m.length < 2) return; const lo = new Map(), fl = new Map();
      u.m.forEach(c => { lo.set(c.r.cond, Math.min(lo.has(c.r.cond) ? lo.get(c.r.cond) : 9, c.k)); fl.set(c.r.cond, Math.max(fl.get(c.r.cond) || 0, floorOf(c.id) || 0)); });
      u.m.forEach(c => { c.k = Math.max(lo.get(c.r.cond), fl.get(c.r.cond)); }); });
    lab.forEach(c => { c.spec = spec(c, c.k, c.nm); });
    // at-stack labels first (they are where their stacks are); the row keeps clear of them
    const stack = [], taken = [], gone = [];
    // (a quiet label covers no other stack)
    lab.filter(c => c.at === 'stack').forEach(c => { const q = engStackPlace(c, c.spec, taken, ctx.allCols, ctx); if (!q || (c.quiet && q.covers)) { gone.push(c); return; } const p = Object.assign({ c }, q); stack.push(p); taken.push(p.box); });
    ctx.fixed = stack.map(s => s.box);
    const res = engRowLayout(us, ctx);
    if (gone.length) log.push('cannot stand wholly in view at its stack: ' + gone.map(c => c.id).join(', '));
    log.push(res.ok ? 'fits (rise ' + Math.round(res.rise) + ')' : 'no place for ' + us[res.at].m.map(it => it.id).join(' + ') + ' (' + lab.filter(c => c.nmI).length + ' names cut, ' + lab.length + ' labelled)');
    return { us, res: gone.length ? Object.assign({}, res, { ok: false, gone }) : res, lab, stack, gone };
  };
  // quiet labels (idle; a failure or finish seen) are not in this: they join afterwards, in room nobody else needs (below)
  let got, lab = pool.filter(c => !c.quiet);
  while (lab.length) {
    got = attempt(lab);
    if (got.res.ok) break;
    // a label that cannot stand wholly in view at its stack goes as an out-of-view stack goes: no floor label (lead, round 4)
    // at one to five agents a label sent to its stack first (below) that cannot stand there goes back to the row, to shorten there
    const back = got.gone.filter(c => load.stackFirst && c.moved && !c.stackTried); back.forEach(c => { c.at = 'row'; c.stackTried = true; });
    got.gone = got.gone.filter(c => !back.includes(c)); if (back.length && !got.gone.length) continue;
    if (got.gone.length) { got.gone.forEach(c => out.set(c.id, c.quiet ? 'quiet (' + c.r.cond + '): no free room at its stack (its mark and the tree tell it)' : 'cannot stand wholly in view (its stack\'s mark and the tree tell it)')); lab = lab.filter(c => !got.gone.includes(c)); continue; }
    // the ways out, losing least first (5.6, rough fixed order): a shorter form for the richest, least urgent set; shorter names;
    // leave out the lowest claim on the row
    // (piece A tidy, planner's order) fewer words above the floor before any label goes down to its stack: forms fall first; at one to
    // five agents the stack comes only once no form can fall (round 1 sent a label to its stack first, and labels stood on the floor
    // with room above them)
    // (piece B, the one order) the same steps as engPlaceOrder, for the whole set: (2) the others make room for the unit with no place;
    // (3) fewer words above the floor: forms fall, then the unit with no place squeezes, then the labels in its way; (4) only then a stack
    // (2) above the floor in its form, the others making room: the unit with no place is placed before all others (once for each label)
    const nf = got.us[got.res.at].m.filter(c => c.at === 'row' && !ctx.first.has(c.id));
    if (nf.length) { nf.forEach(c => ctx.first.add(c.id)); continue; }
    const sets = [...new Set(lab.map(setOf))].filter(s => form.get(s) > setMin.get(s));
    if (sets.length) { const urg = s => Math.max(...lab.filter(c => setOf(c) === s).map(c => c.r.need.rate));
      sets.sort((a, b) => form.get(b) - form.get(a) || urg(a) - urg(b)); form.set(sets[0], form.get(sets[0]) - 1); continue; }
    // the labels in the way: the unit with no place and those already placed to its right. With no quieter one among them, the
    // strong or call one with no place stands at its own stack (before any name is cut: standing there leaves nothing out)
    const inWay = new Set(got.res.blockers || []), weak = c => c.at === 'row' && !c.strong;
    const drop = lab.filter(c => weak(c) && inWay.has(c.id)).pop();          // only a label in the way gives way: dropping others frees nothing
    // zooming in never shows less (5.3): a label the last decision showed, now with no place on the row, stands at its stack
    // rather than going (before anyone gives way for it, and before it would give way itself)
    // the unit with no place squeezes itself first, step by step (its figures, its longer wording, its name): no neighbour changes for it
    const nextOf = c => { for (let j = c.nmI + 1; j < R.squeeze.length; j++) if (spec(c, c.k, j).w < c.spec.w - 0.5) return j; return -1; };
    const own = got.us[got.res.at].m.filter(c => c.at === 'row' && nextOf(c) >= 0);
    if (own.length) { own.forEach(c => { c.nmI = nextOf(c); }); continue; }
    // squeeze the labels in the way one step: each to its next step that is narrower (a step that changes nothing for it is passed over)
    const cuttable = lab.filter(c => c.at === 'row' && inWay.has(c.id) && nextOf(c) >= 0);
    if (cuttable.length) { cuttable.forEach(c => { c.nmI = nextOf(c); }); nameI = Math.max(nameI, ...cuttable.map(c => c.nmI)); continue; }
    // (4) no room above the floor even with fewer words: at one to five agents the unit's label goes to its own stack before any label goes
    if (load.stackFirst) { const nx = got.us[got.res.at].m.filter(c => c.at === 'row' && !c.stackTried).pop(); if (nx) { nx.at = 'stack'; nx.moved = true; nx.stackTried = false; continue; } }
    const kept = got.us[got.res.at].m.filter(c => c.at === 'row' && !c.moved && floorOf(c.id) != null).pop();
    if (kept) { kept.at = 'stack'; kept.moved = true; continue; }
    if (!drop) { const stuck = got.us[got.res.at].m.filter(c => c.at === 'row').pop(); if (stuck) { stuck.at = 'stack'; stuck.moved = true; continue; } break; }
    // at one to five agents (the load's stackFirst) a working label with no room on the row stands at its own stack before it goes
    if (load.stackFirst && !drop.moved && !drop.quiet) { drop.at = 'stack'; drop.moved = true; continue; }
    out.set(drop.id, 'no room on the row: lowest claim (urgency ' + drop.r.need.rate.toFixed(2) + ', standing ' + drop.r.standing.toFixed(2) + ')');
    lab = lab.filter(c => c !== drop);
  }
  // a strong label sent to its stack goes back to the row if the row now has a place for it (another one sent away may have freed it)
  if (got && got.res.ok) lab.filter(c => c.moved).forEach(c => { c.at = 'row'; const g2 = attempt(lab); if (g2.res.ok) got = g2; else { c.at = 'stack'; } });
  if (got && got.res.ok && lab.some(c => c.moved)) got = attempt(lab);
  // whole names back wherever they fit now (a cut made on the way, before someone was left out or moved, may no longer be needed)
  if (got && got.res.ok) lab.filter(c => c.nmI).forEach(c => { const was = c.nmI; c.nmI = 0; let g2 = attempt(lab);
    while (!g2.res.ok && c.nmI < was) { c.nmI++; g2 = attempt(lab); } if (g2.res.ok) got = g2; else { c.nmI = was; got = attempt(lab); } });
  nameI = Math.max(0, ...lab.map(c => c.nmI || 0));
  const gaveWay = lab.length < pool.filter(c => !c.quiet).length;          // someone was left out or could not stand in view: no set opens at their cost
  if (got && got.res.ok && !keep && !lab.some(c => c.nmI) && !gaveWay) {
    // open forms where it pays (grading 2): past its start by load, a set opens only where that pushes nobody up a tier; at a still
    // camera nothing opens (round 2 of piece 4, coordinator: a change of one agent moves a neighbour only when truly forced)
    const empty = false, order = [...new Set(lab.map(setOf))];
    const maxOf = openOf;
    let moved = true;
    while (moved) {
      moved = false;
      for (const s of order) {
        const k = form.get(s); if (k >= maxOf(s)) continue;
        const mem = lab.filter(c => setOf(c) === s);
        if (mem.every(c => { if (c.card) return true; const a = spec(c, k, c.nmI), z = spec(c, k + 1, c.nmI); return z.h === a.h && z.w <= a.w + 0.5; })) { form.set(s, k + 1); moved = true; continue; }          // says nothing more (no taller, no wider): free
        form.set(s, k + 1);
        const g2 = attempt(lab), onRow = mem.some(c => c.at === 'row');
        if (g2.res.ok && (empty || !onRow || g2.res.rise <= got.res.rise + 0.5)) { got = g2; moved = true; } else form.set(s, k);
      }
    }
    got = attempt(lab);
  }
  if (!got) got = attempt(lab);          // nobody but quiet agents
  // quiet labels join last, one at a time by claim, each only where every label already placed keeps its exact box (builder H, piece 4:
  // an idle agent had no label at 5 and at 8, and at 5 its want of room had cut every other label to one line). Each tries the row, then
  // its own stack, at its set's form, then a form shorter; with no such room it goes, and its stack keeps its mark.
  const quietC = pool.filter(c => c.quiet && !out.has(c.id));
  if (got && got.res.ok && quietC.length) {
    const boxOf = g => { const m = new Map(); g.us.forEach((u, i) => { const p = g.res.placed[i]; let y = p.Y0; u.m.forEach((it, j) => { m.set(it.id, [p.X, y, it.spec.w, it.spec.h].map(v => Math.round(v * 2)).join()); y += p.hs[j] + R.listRowGap; }); });
      g.stack.forEach(q => m.set(q.c.id, [q.X, q.Y0, q.c.spec.w, q.c.spec.h].map(v => Math.round(v * 2)).join())); return m; };
    const old0 = ctx.old, still0 = ctx.still;
    for (const q of quietC) {
      const had = boxOf(got); ctx.still = true; ctx.old = new Map(); got.us.forEach((u, i) => ctx.old.set(u.m.map(it => it.id).join('|'), [got.res.placed[i].X, got.res.placed[i].Y0]));
      const keeps = g => { if (!g.res.ok) return false; const now = boxOf(g); return [...had].every(([id, b]) => now.get(id) === b); };
      const s = setOf(q), f0 = form.get(s), ks = []; for (let k = f0; k >= setMin.get(s); k--) ks.push(k);
      // the one order (engPlaceOrder): above the floor form by form, then with fewer words; its stack only when the window has no room above
      const ok = engPlaceOrder((k, nm, at) => { form.set(s, k); q.nmI = nm; q.at = at; const g2 = attempt(lab.concat([q])); return keeps(g2) ? g2 : null; }, ks, { row: rowRoom, stack: !rowRoom });
      if (ok) { got = ok; lab = lab.concat([q]); } else { q.nmI = 0; form.set(s, f0); out.set(q.id, 'quiet (' + q.r.cond + '): no room that nobody else needs (its stack shows its mark)'); }
    }
    ctx.old = old0; ctx.still = still0;
  }
  const dec = { camKey: engRowCamKey(), focus: PG.focus, why, provisional: why === 'first' || !(document.fonts ? document.fonts.status === 'loaded' : true),
    form, nameI, units: [], lab: new Map(), wordsW: load.words, inPlace: !!load.inPlace, out, base, card: tops.map(t => t.a.id).filter(engCardFor).join('|'),          // over every stack, as the trigger reads it
   
    s: PG.cam.s, formOf: new Map(), cols: allCols,
    cands: new Set(cands.map(c => c.id)), condAt: new Map(cands.map(c => [c.id, engSig(c.r)])), pieces: new Map(cands.map(c => [c.id, engPieceSig(c.r)])),
    loadAt: ENG_NAIVE.labelsByLoad.indexOf(load) };
  if (got && got.res.ok) {
    got.us.forEach((u, i) => {
      const p = got.res.placed[i], a = u.m[0], ft = engRowFoot(a.t);
      let y = p.Y0; const rows = u.m.map((it, j) => { const dy = y - p.Y0; y += p.hs[j] + R.listRowGap; return dy; });
      const U = { at: 'row', anchor: a.id, ids: u.m.map(it => it.id), off: [p.X - ft[0], p.Y0 - ft[1]], rows, w: p.w, h: p.h, idx: i, jog: p.jog, jyOff: p.jy != null && p.jy > p.Y0 + p.h + R.tierGap ? p.jy - a.top[1] : null,
        rel: u.m.map(it => { const f = engRowFoot(it.t); return [f[0] - ft[0], f[1] - ft[1]]; }) };          // each member's foot from the anchor's
      dec.units.push(U);
      u.m.forEach((it, j) => { dec.lab.set(it.id, { unit: U, row: j, k: it.spec.k, nm: it.nm, share: it.share, band: it.band, fix: engFixOf(it.spec) }); dec.formOf.set(it.id, it.k); });
    });
    got.stack.forEach(s => {
      const ft = engRowFoot(s.c.t), U = { at: 'stack', anchor: s.c.id, ids: [s.c.id], off: [s.X - ft[0], s.Y0 - ft[1]], rows: [0], w: s.c.spec.w, h: s.c.spec.h, idx: null, covers: s.covers, left: s.left };
      dec.units.push(U); dec.lab.set(s.c.id, { unit: U, row: 0, k: s.c.spec.k, nm: s.c.nm, share: s.c.share, band: s.c.band, fix: engFixOf(s.c.spec) }); dec.formOf.set(s.c.id, s.c.k);
    });
  }
  // for the checks: for each agent left out for want of room, a place in the row's band where its name line alone would have stood
  // clear of every label, fixed box and other stack (a coarse scan; it ignores lines and the order of the row)
  const placedBoxes = got && got.res.ok ? got.res.placed.map(p => p.box).concat(got.stack.map(x => x.box)) : [];
  ENGR.free = [...out.entries()].filter(([, w]) => /^no room/.test(w)).map(([id]) => {
    const c = cands.find(x => x.id === id), sp = c ? spec(c, R.formMin, R.squeeze.length - 1) : null; if (!sp) return { id, spot: null };
    for (let y = top; y + sp.h <= base; y += 4) for (let x = 0; x + sp.w <= W; x += 6) { const b = [x, y, x + sp.w, y + sp.h];
      if (!placedBoxes.some(q => engOv(b, q, 2, 2)) && !ctx.obst.some(q => engOv(b, q, 0, 0)) && !allCols.some(k => engOv(b, k.box, 0, 0))) return { id, spot: b.map(Math.round) }; }
    return { id, spot: null };
  });
  ENGR.ms = performance.now() - t0; ENGR.n++; ENGR.tries = tries; ENGR.log = log;
  ENGR.ctx = { base: Math.round(base), top, obst: ctx.obst.map(b => b.map(Math.round)) };
  ENGR.dec = dec;
  engRowUncut(tops, face); ENGR.ms = performance.now() - t0;
}

// after every decision (piece A, fault 4): a label of an agent that needs you that ends cut ("Can I drop the…") takes the least
// fuller form that says it whole, at its place or in free room, every other label keeping its exact place; with no such room it stays
function engRowUncut(tops, face) {
  const d = ENGR.dec; if (!d || d.provisional || !d.pieces) return;
  const ids = [];
  d.lab.forEach((L, id) => { const r = engRec(id); if (!r || engCardFor(id) || engQuiet(r)) return; if (!(r.need.rate >= ENG_NAIVE.bands.medium || engStrong(r))) return;
    if (engRowSpec({ r, id, card: false }, L.k, L.nm, L.fix, d.wordsW, d.inPlace).cutAny) ids.push(id); });
  // (piece A, round 2) a label standing at its stack over the floor goes up to free room above the floor when there is some now, the
  // labels of agents that need you first; every other label keeps its place (it came down for want of room and never came back up)
  const lift = new Set(); d.units.forEach(U => { if (U.at === 'stack' && U.ids.length === 1) lift.add(U.anchor); });
  lift.forEach(id => { if (!ids.includes(id)) ids.push(id); });
  if (ids.length) engRowPatch(tops, face, d.why, d, false, new Set(ids), lift);
}

// a unit as it stands now: its box (with the open card's block) and its lines (as engRowDraw draws them)
function engUnitNow(U, byId, face) {
  const F = ENG_FORM, R = ENG_ROW, a = byId.get(U.anchor); if (!a) return null;
  const p = engRowFoot(a), X = p[0] + U.off[0], Y0 = p[1] + U.off[1]; let w = U.w, h = U.h;
  if (U.ids.length === 1 && engCardFor(U.anchor)) { const cs = engCardSize(U.anchor); if (cs) { w = Math.max(w, cs[0]); h = Math.max(h, F.lineH + cs[1]); } }
  const segs = [];
  if (U.at === 'row') U.ids.forEach((id, j) => { const t = byId.get(id); if (!t) return; const top = engRowLineTop(t, face), my = Y0 + U.rows[j] + F.lineH / 2;
    const pts = U.ids.length > 1 ? [top, [top[0], my], [X - 2, my]] : !U.jog ? [top, [top[0], my + F.markR + 1]]
      : (jy => [top, [top[0], jy], [X + F.markR, jy], [X + F.markR, my + F.markR + 1]])(U.jyOff != null ? top[1] + U.jyOff : Y0 + U.h + R.tierGap / 2);
    engPts2Segs(pts).forEach(s => segs.push(s)); });
  return { box: [X, Y0, X + w, Y0 + h], segs };
}

// The one order of places for a label (piece B, the planner's root fix: eleven points in three roads could each send a label to its
// stack, one of them for its line's length alone, and a running label stood on the floor with the window above it empty). Every road
// that places one label goes through this: a patch, a label placed again after making room, the refit's re-placing, say-it-whole and
// the lift, an opened card (a label of a larger size), the quiet labels joining the whole arrangement; the whole arrangement's own ways
// out take the same steps for the whole set. at(k, nm, 'row' | 'stack') tries one place and returns it or null; ks: its forms, the
// first tried first.
//   (1) above the floor, in its form, form by form;
//   (2) above the floor in its form, another label making room (o.room), where the road allows it;
//   (3) above the floor with fewer words (its squeeze steps, at its last form);
//   (4) only then at its own stack, form by form, then with fewer words. o.stack false: a road that must not go down (it stays as it
//       was, or goes); o.row false: the window has no room above the floor at all.
function engPlaceOrder(at, ks, o) {
  o = o || {}; const nm0 = o.nm0 || 0, sq = o.squeeze !== false, kL = ks[ks.length - 1], N = ENG_ROW.squeeze.length;
  let g = null;
  if (o.row !== false) {
    for (const k of ks) if ((g = at(k, nm0, 'row'))) return g;
    if (o.room && (g = o.room())) return g;
    if (sq) for (let nm = nm0 + 1; nm < N; nm++) if ((g = at(kL, nm, 'row'))) return g;
  }
  if (o.stack === false) return null;
  for (const k of ks) if ((g = at(k, nm0, 'stack'))) return g;
  if (sq) for (let nm = nm0 + 1; nm < N; nm++) if ((g = at(kL, nm, 'stack'))) return g;
  return null;
}

// a change of some agents at a still camera (piece A, fault 3): every other label keeps its exact place, form and words; the changed
// agents alone are placed again, each at its old place if its new form fits there, else its cheapest free place on the row or at its
// stack, its form falling and its figures and words squeezing only for want of free room. A changed agent that needs you and finds
// no free room at all asks for the whole arrangement (that is the forced case); any other goes to its stack's mark and the tree.
// A list of stacks (one block for stacks standing close) with a changed member, a change of load row, or of the band of zoom of a set
// also asks for the whole arrangement. false: not patched.
function engRowPatch(tops, face, why, prev, ride, improve, lift) {
  const R = ENG_ROW, F = ENG_FORM, H = PG.H, top = PG.PICK_CLEAR + R.topPad;
  const { out, cands, cols, allCols, base } = engRowCands(tops, face);
  const load = ENG_NAIVE.labelsByLoad.find(x => cands.length <= x.upTo);
  if (prev.loadAt !== ENG_NAIVE.labelsByLoad.indexOf(load) || !prev.pieces) return false;
  const byId = new Map(tops.map(t => [t.a.id, t])), candIds = new Set(cands.map(c => c.id)), changed = new Set();
  cands.forEach(c => { const L = prev.lab.get(c.id);
    if (!prev.cands.has(c.id) || prev.condAt.get(c.id) !== engSig(c.r) || (L && L.k >= 4 && prev.pieces.get(c.id) !== engPieceSig(c.r))) changed.add(c.id); });
  if (improve) { changed.clear(); improve.forEach(id => changed.add(id)); }
  const cardNow = cands.filter(c => c.card).map(c => c.id).join('|');
  if (!improve && cardNow !== prev.card) prev.card.split('|').concat(cardNow.split('|')).forEach(id => { if (id && candIds.has(id)) changed.add(id); });
  const keepU = [], fixed = [], fixedSegs = [], obst0 = engRowObstacles(), oldU = new Map(), lists = [], ent = [], quietOut = new Map();
  // after a refit, a label that rode into the window's edge, the tree, a page box, another label or line, or over another stack, is placed again
  // (piece A tidy) after the page's own small refit a label is placed again only for a true fault: out of the window, over the tree or a
  // page box, or covering another label; lines that drifted near a box wait for the next decision (at 12, a 1.3% zoom had re-placed 6
  // labels for lines passing within 1.5 px of a box, with no agent changing)
  const bad = (g, U) => !engInWin(g.box) || !engTreeFree(g.box, obst0[0]) || obst0.slice(1).some(o => engOv(g.box, o, 2, 2)) || fixed.some(b => engOv(g.box, b, ride ? 0 : 2, ride ? 0 : 1))
    || (!ride && (g.segs.some(sg => !engSegTreeFree(sg, obst0[0]) || fixed.some(b => engSegBox(sg, b, 1.5)) || fixedSegs.some(q => engSegX(sg, q))) || fixedSegs.some(sg => engSegBox(sg, g.box, 1.5))))
    || (U.at === 'row' && cols.some(k => !U.ids.includes(k.id) && engOv(g.box, k.box, 2, 0)));
  for (const U of prev.units) {
    if (improve && U.ids.length > 1) U.ids.forEach(id => changed.delete(id));
    // a label being improved stands where it is until its own turn (so a label placed before it never lands on it)
    if (improve && U.ids.length === 1 && changed.has(U.anchor)) { oldU.set(U.anchor, U); const g = engUnitNow(U, byId, face);
      if (g) { keepU.push(U); fixed.push(g.box); g.segs.forEach(sg => fixedSegs.push(sg)); ent.push({ U, box: g.box, segs: g.segs }); } continue; }
    if (U.ids.some(id => changed.has(id) || !candIds.has(id))) { if (U.ids.length > 1) { if (U.ids.some(id => !candIds.has(id))) return false; lists.push(U); continue; } if (improve) oldU.set(U.anchor, U); continue; }
    const g = engUnitNow(U, byId, face); if (!g) return false;
    // (piece A, round 2) at any patch, not only after a refit: a kept label never stays outside the window or over the tree or a box
    if (bad(g, U)) { if (U.ids.length > 1) lists.push(U); else changed.add(U.anchor); continue; }
    keepU.push(U); fixed.push(g.box); g.segs.forEach(s => fixedSegs.push(s)); ent.push({ U, box: g.box, segs: g.segs });
  }
  const setOf = c => c.r.cond + (c.r.facts.seen ? '' : '*') + '|' + c.band, form = new Map(prev.form);
  const needs = c => !c.quiet && (c.r.need.rate >= ENG_NAIVE.bands.medium || c.strong);
  const minOf = c => !c.card && c.r.say.words && c.r.say.words.length ? 3 : R.formMin;
  const obst = engRowObstacles(), rowRoom = base - F.lineH >= top, lab = new Map(), units = [], log = [];
  const ctx = { top, base, obst, tree: obst[0], cols, allCols, old: new Map(), fixed, fixedSegs, still: true, tops: cands.map(c => [c.id, c.top[0], c.top[1]]) };
  const oldAt = new Map(); prev.units.forEach(U => { if (U.ids.length !== 1) return; const a = byId.get(U.anchor); if (!a) return; const p = engRowFoot(a); oldAt.set(U.anchor, { at: U.at, xy: [p[0] + U.off[0], p[1] + U.off[1]] }); });
  let tries = 0;
  // a list (stacks standing close, one block) with a changed member is laid again as one, at its old place if it fits there; its other
  // members keep their forms and words. With no free place for it: the whole arrangement
  for (const U of lists) {
    const m = U.ids.map(id => cands.find(c => c.id === id)); if (m.some(c => !c || c.card)) return false;
    const a = byId.get(U.anchor), p0 = engRowFoot(a); ctx.old.set(U.ids.join('|'), [p0[0] + U.off[0], p0[1] + U.off[1]]);
    const ch = m.filter(c => changed.has(c.id)), k0 = Math.max(...ch.map(c => { const s = setOf(c); return Math.max(minOf(c), form.has(s) ? form.get(s) : needs(c) ? load.need : load.rest); }), 0);
    let res = null;
    for (let k = k0; !res; k--) {
      m.forEach(c => { if (changed.has(c.id)) { c.k = Math.max(k, minOf(c)); c.nm = 0; c.spec = engRowSpec(c, c.k, 0, null, load.words, load.inPlace); }
        else { const L = prev.lab.get(c.id); c.k = L.k; c.nm = L.nm; c.spec = engRowSpec(c, L.k, L.nm, L.fix, load.words, load.inPlace); } });
      tries++; const r0 = engRowLayout([{ m }], ctx); if (r0.ok) res = r0;
      if (!res && ch.every(c => c.k <= minOf(c))) return false;
    }
    const pl = res.placed[0], ft = engRowFoot(m[0].t); let y = pl.Y0;
    const rows = m.map((c, j) => { const dy = y - pl.Y0; y += pl.hs[j] + R.listRowGap; return dy; });
    const U2 = { at: 'row', anchor: m[0].id, ids: m.map(c => c.id), off: [pl.X - ft[0], pl.Y0 - ft[1]], rows, w: pl.w, h: pl.h, idx: U.idx, jog: pl.jog, jyOff: null,
      rel: m.map(c => { const f = engRowFoot(c.t); return [f[0] - ft[0], f[1] - ft[1]]; }) };
    units.push(U2); m.forEach(c => { lab.set(c.id, { unit: U2, row: U2.ids.indexOf(c.id), k: c.k, nm: c.nm, share: c.share, band: c.band, fix: engFixOf(c.spec) }); changed.delete(c.id); });
    ctx.fixed.push(pl.box); pl.mine.forEach(sg => ctx.fixedSegs.push(sg)); ent.push({ U: U2, box: pl.box, segs: pl.mine });
    log.push(U2.ids.join(' + ') + ': list laid again');
  }
  const cById = new Map(cands.map(c => [c.id, c]));
  const setFixed = () => { ctx.fixed.length = 0; ctx.fixedSegs.length = 0; ent.forEach(e => { ctx.fixed.push(e.box); e.segs.forEach(sg => ctx.fixedSegs.push(sg)); }); };
  // a label that made room, placed again: the one order (engPlaceOrder) at its form, its fewer words before its stack
  const placeAlone = d => { const nm0 = d.nm, sp0 = d.spec;
    const at = (k, nm, w) => { tries++; d.nm = nm; d.spec = nm === nm0 ? sp0 : engRowSpec(d, k, nm, null, load.words, load.inPlace);
      if (w === 'row') { const r2 = engRowLayout([{ m: [d] }], ctx); if (!r2.ok) return null; const p = r2.placed[0]; return { at: 'row', p, box: p.box, segs: p.mine }; }
      const q = engStackPlace(d, d.spec, ctx.fixed, allCols, ctx); return q ? { at: 'stack', q, box: q.box, segs: [] } : null; };
    const g = engPlaceOrder(at, [d.k], { nm0, row: rowRoom }); if (!g) { d.nm = nm0; d.spec = sp0; } return g; };
  // an agent that needs you with no free room above the floor: the nearest label (up to four tried) of an agent that does not, alone on
  // the row, makes room, and is placed again itself (above the floor if it can, else at its stack); none can: null
  // (piece A, round 2) the labels whose box or line stands across its way straight up are tried first; a label of an agent that needs you
  // may make room too, for one that needs you, but only if it lands above the floor again itself
  const acrossOf = (c, e) => engOv(e.box, [c.top[0] - 2, 0, c.top[0] + 2, c.top[1]], 0, 0) || e.segs.some(s => Math.abs(s[1] - s[3]) < 0.5 && s[1] < c.top[1] && c.top[0] >= Math.min(s[0], s[2]) - 2 && c.top[0] <= Math.max(s[0], s[2]) + 2);
  // several labels stand across its way up: all of them step aside, it takes its place, then each is placed again; a label of an agent
  // that needs you must land above the floor again and a quiet one must find room, or nothing moves
  const displaceAll = (c, ks, place) => {
    const S = ent.filter(e => e.U && e.U.at === 'row' && e.U.ids.length === 1 && cById.get(e.U.anchor) && !cById.get(e.U.anchor).card && acrossOf(c, e));
    if (!S.length || S.length > 4) return null;
    S.forEach(e => ent.splice(ent.indexOf(e), 1)); setFixed();
    let g1 = null; for (const k of ks) { g1 = place(k, 0, 'row'); if (g1) break; }
    if (!g1) { S.forEach(e => ent.push(e)); setFixed(); return null; }
    const keep = [c.spec, c.k, c.nm], n0 = ent.length; ent.push({ U: null, box: g1.box, segs: g1.segs }); setFixed();
    const moved = []; let ok = true;
    for (const e of S) {
      const d = cById.get(e.U.anchor), L = lab.get(d.id) || prev.lab.get(d.id);
      d.k = L.k; d.nm = L.nm; d.spec = engRowSpec(d, L.k, L.nm, L.fix, load.words, load.inPlace); ctx.old.delete(d.id);
      const g2 = placeAlone(d); if (!g2 || (g2.at === 'stack' && (d.needs || d.quiet))) { ok = false; break; }
      ent.push({ U: null, box: g2.box, segs: g2.segs }); setFixed(); moved.push({ d, g2, e });
    }
    ent.length = n0; [c.spec, c.k, c.nm] = keep;
    if (!ok) { S.forEach(e => ent.push(e)); setFixed(); return null; }
    setFixed(); return { got: g1, moved };
  };
  const displace = (c, k0, place, toStack) => {
    const across = e => acrossOf(c, e);
    const dist = e => (across(e) ? 0 : 1e6) + Math.abs((e.box[0] + e.box[2]) / 2 - c.top[0]);
    const near = ent.filter(e => e.U && e.U.at === 'row' && e.U.ids.length === 1 && cById.get(e.U.anchor) && !cById.get(e.U.anchor).card
        && (!cById.get(e.U.anchor).needs ? (needs(c) || cById.get(e.U.anchor).quiet || !c.quiet) : needs(c) && !toStack))
      .sort((p, q) => dist(p) - dist(q)).slice(0, 6);
    for (const e of near) {
      ent.splice(ent.indexOf(e), 1); setFixed();
      let g1 = null; for (let k = k0; !g1 && k >= minOf(c); k--) g1 = place(k, 0, 'row');
      if (g1) { const keep = [c.spec, c.k, c.nm]; ent.push({ U: null, box: g1.box, segs: g1.segs }); setFixed();
        const d = cById.get(e.U.anchor), L = lab.get(d.id) || prev.lab.get(d.id);
        d.k = L.k; d.nm = L.nm; d.spec = engRowSpec(d, L.k, L.nm, L.fix, load.words, load.inPlace); ctx.old.delete(d.id);
        let g2 = placeAlone(d); if (g2 && g2.at === 'stack' && !toStack) g2 = null; ent.pop(); setFixed(); [c.spec, c.k, c.nm] = keep;
        if (g2) return { got: g1, moved: { d, g2, e } }; }
      ent.push(e); setFixed();
    }
    return null;
  };
  const placeMoved = ({ d, g2, e }) => {
    const i = keepU.indexOf(e.U); if (i >= 0) keepU.splice(i, 1); const j = units.indexOf(e.U); if (j >= 0) units.splice(j, 1);
    const ft = engRowFoot(d.t), sp = d.spec;
    const U = g2.at === 'row' ? { at: 'row', anchor: d.id, ids: [d.id], off: [g2.p.X - ft[0], g2.p.Y0 - ft[1]], rows: [0], w: g2.p.w, h: g2.p.h, idx: prev.units.length + units.length, jog: g2.p.jog, jyOff: g2.p.jy != null && g2.p.jy > g2.p.Y0 + g2.p.h + R.tierGap ? g2.p.jy - d.top[1] : null, rel: [[0, 0]] }
      : { at: 'stack', anchor: d.id, ids: [d.id], off: [g2.q.X - ft[0], g2.q.Y0 - ft[1]], rows: [0], w: sp.w, h: sp.h, idx: null, covers: false, left: g2.q.left };
    units.push(U); lab.set(d.id, { unit: U, row: 0, k: d.k, nm: d.nm, share: d.share, band: d.band, fix: engFixOf(sp) });
    ctx.fixed.push(g2.box); g2.segs.forEach(sg => ctx.fixedSegs.push(sg)); ent.push({ U, box: g2.box, segs: g2.segs });
    log.push(d.id + ': made room, now ' + g2.at);
  };
  for (const c of cands) {
    if (lab.has(c.id)) continue;
    if (!changed.has(c.id)) { if (!keepU.some(U => U.ids.includes(c.id)) && !out.has(c.id)) out.set(c.id, prev.out.get(c.id) || 'kept as it was (another agent changed)'); continue; }
    if (c.quiet && !c.card && c.band === 'far' && !load.quiet && cands.length > R.quietNamesUpTo) { out.set(c.id, 'quiet (' + c.r.cond + '): the tree names it; its stack shows its mark'); continue; }
    const s = setOf(c), k0 = Math.max(minOf(c), form.has(s) ? form.get(s) : needs(c) ? load.need : load.rest);
    const o = oldAt.get(c.id); if (o && o.at === 'row') ctx.old.set(c.id, o.xy);
    const place = (k, nm, at) => {
      tries++; const sp = engRowSpec(c, k, nm, null, load.words, load.inPlace); c.spec = sp; c.k = k; c.nm = nm;
      if (at === 'row') { const res = engRowLayout([{ m: [c] }], ctx); if (!res.ok) return null; const p = res.placed[0]; return { at, p, sp, box: p.box, segs: p.mine }; }
      const q = engStackPlace(c, sp, ctx.fixed, allCols, ctx); if (!q || (c.quiet && q.covers)) return null;
      return { at, q, sp, box: q.box, segs: [] };
    };
    let got = null, moved = null;
    if (improve) {          // a label of an agent that needs you, cut: the least fuller form that says it whole, anywhere free; else as it was.
                            // A label at its stack over the floor (lift): its form, on the row above the floor if room there is free; else as it was
      const U0 = oldU.get(c.id), e0 = ent.find(e => e.U === U0); if (e0) { ent.splice(ent.indexOf(e0), 1); setFixed(); }
      const L = prev.lab.get(c.id), ks = [], up = lift && lift.has(c.id), cut = engRowSpec(c, L.k, L.nm, L.fix, load.words, load.inPlace).cutAny;
      if (cut) { for (let k = Math.max(L.k, minOf(c)); k <= 6; k++) if (!engRowSpec(c, k, 0, null, load.words, load.inPlace).cutAny) ks.push(k); } else ks.push(L.k);
      // (piece A, round 2 and tidy) a fuller form is never bought by going down to its stack: this pass only ever places above the floor
      // (a cut label that cannot be said whole there stays as it was, with fewer words). The one order (engPlaceOrder) with no stack: in
      // its form; one that needs you, lifted from its stack, has the labels across its way up step aside for it; a lifted label that is
      // not being said whole may take fewer words above the floor rather than stay at its stack
      const room = up && needs(c) ? () => { const dm = displaceAll(c, ks, place); if (!dm) return null; moved = dm.moved; return dm.got; } : null;
      if (rowRoom) got = engPlaceOrder(place, ks, { room, squeeze: up && !cut, stack: false });
      // a quiet label over the floor with no room above it now: it goes, its stack keeps its mark (the tree names it)
      if (!got && up && c.quiet && !c.card && rowRoom) { if (U0) keepU.splice(keepU.indexOf(U0), 1); quietOut.set(c.id, 'quiet (' + c.r.cond + '): no room above the floor that nobody else needs (its stack shows its mark)'); continue; }
      if (!got) { if (e0) { ent.push(e0); setFixed(); } continue; }
      if (U0 && keepU.includes(U0)) keepU.splice(keepU.indexOf(U0), 1);
    }
    // the one order (engPlaceOrder): its old place or free room above the floor its line reaches, anywhere in the window, form by form;
    // then another label making room (one that does not need you, or for one that needs you any, that label placed again itself by the
    // same order); then fewer words above the floor; only then its stack (a quiet label with room above the floor never goes down)
    if (!got) { const ks = []; for (let k = k0; k >= minOf(c); k--) ks.push(k);
      const room = () => { let dm = null; if (!c.quiet) { dm = displace(c, k0, place, false); if (!dm && needs(c)) dm = displace(c, k0, place, true); }
        if (!dm && needs(c)) dm = displaceAll(c, ks, place); if (!dm) return null; moved = dm.moved; return dm.got; };
      // with room above the floor, a patch sends a label to its stack only above five agents, and never an opened card (piece B, d: the
      // card hung at its stack at the window's foot with the window above it empty); else it asks for the whole arrangement, where the
      // others make room for it (a quiet one with no room goes, its stack keeps its mark)
      const downOk = !rowRoom || (cands.length > ENG_NAIVE.labelsByLoad[0].upTo && !c.card && !c.quiet);
      got = engPlaceOrder(place, ks, { row: rowRoom, room, stack: downOk });
      if (!got && rowRoom && !c.quiet) return false; }
    if (!got) { if (needs(c) || c.card) return false;          // it needs you: the whole arrangement makes room (forced)
      out.set(c.id, c.quiet ? 'quiet (' + c.r.cond + '): no room that nobody else needs (its stack shows its mark)' : 'no free room without moving another label (its mark and the tree tell it)'); continue; }
    if (!form.has(s)) form.set(s, got.sp.k);
    const ft = engRowFoot(c.t);
    const U = got.at === 'row' ? { at: 'row', anchor: c.id, ids: [c.id], off: [got.p.X - ft[0], got.p.Y0 - ft[1]], rows: [0], w: got.p.w, h: got.p.h, idx: prev.units.length + units.length, jog: got.p.jog, jyOff: got.p.jy != null && got.p.jy > got.p.Y0 + got.p.h + R.tierGap ? got.p.jy - c.top[1] : null, rel: [[0, 0]] }
      : { at: 'stack', anchor: c.id, ids: [c.id], off: [got.q.X - ft[0], got.q.Y0 - ft[1]], rows: [0], w: got.sp.w, h: got.sp.h, idx: null, covers: got.q.covers, left: got.q.left };
    units.push(U); lab.set(c.id, { unit: U, row: 0, k: got.sp.k, nm: c.nm, share: c.share, band: c.band, fix: engFixOf(got.sp) });
    ctx.fixed.push(got.box); got.segs.forEach(sg => ctx.fixedSegs.push(sg)); ent.push({ U, box: got.box, segs: got.segs });
    if (moved) (Array.isArray(moved) ? moved : [moved]).forEach(placeMoved);
    log.push(c.id + ': ' + got.at + ' form ' + got.sp.k + (c.nm ? ' squeezed ' + c.nm : ''));
  }
  const dec = Object.assign({}, prev, { why, focus: PG.focus, camKey: engRowCamKey(), s: PG.cam.s, form, units: keepU.concat(units), lab: new Map(), out, base, card: tops.map(t => t.a.id).filter(engCardFor).join('|'),
    formOf: new Map(), cols: allCols, cands: candIds, condAt: new Map(cands.map(c => [c.id, engSig(c.r)])), pieces: new Map(cands.map(c => [c.id, engPieceSig(c.r)])), patched: [...changed] });
  keepU.forEach(U => U.ids.forEach(id => { dec.lab.set(id, prev.lab.get(id)); dec.formOf.set(id, prev.formOf.get(id)); }));
  if (improve) { dec.out = new Map(prev.out); quietOut.forEach((w, id) => dec.out.set(id, w)); dec.why = prev.why; }
  lab.forEach((L, id) => { dec.lab.set(id, L); dec.formOf.set(id, L.k); });
  ENGR.dec = dec; ENGR.tries = tries;
  if (improve) ENGR.log = (ENGR.log || []).concat(['said whole: ' + (log.join('; ') || 'no free room')]); else { ENGR.log = ['changed alone: ' + (log.join('; ') || 'none placed')]; ENGR.free = []; }
  return true;
}

// what would make a new decision now (5.5): rest after the camera moved, a change of condition, of the candidates, of focus, of the card
function engRowTrigger(tops, now) {
  const d = ENGR.dec; if (!d) return 'first';
  if (now - ENGR.movedAt < ENG_ROW.restMs) return null;
  if (d.provisional) return 'at rest after loading';
  if (d.camKey !== engRowCamKey()) return 'camera at rest';
  if (d.focus !== PG.focus) return 'focus';
  const ids = new Set(), card = [];
  for (const t of tops) { const r = engRec(t.a); if (!r) continue; if (d.cands.has(r.id) && d.condAt.get(r.id) !== engSig(r)) return 'condition of ' + r.id;
    if (!d.out.has(r.id) || d.out.get(r.id) !== 'not in view') ids.add(r.id); }
  for (const t of tops) { const r = engRec(t.a), L = r && d.lab.get(r.id);
    if (L && L.k >= 4 && !engCardFor(r.id) && d.pieces && d.pieces.get(r.id) !== engPieceSig(r)) return 'words of ' + r.id; }
  for (const t of tops) if (engCardFor(t.a.id)) card.push(t.a.id);
  for (const id of d.cands) if (!ids.has(id)) return 'candidates';
  for (const id of ids) if (!d.cands.has(id) && !d.out.has(id)) return 'candidates';
  if (card.join('|') !== d.card) return 'card';
  // a stack moved on the floor away from its list (the list rides with its first stack): re-decide once it has come to rest
  const byId = new Map(tops.map(t => [t.a.id, t])); let drift = '';
  d.units.forEach(U => { if (!U.rel || U.ids.length < 2) return; const a = byId.get(U.anchor); if (!a) return; const fa = engRowFoot(a);
    U.ids.forEach((id, j) => { const t = byId.get(id); if (!t) return; const f = engRowFoot(t), dx = f[0] - fa[0] - U.rel[j][0], dy = f[1] - fa[1] - U.rel[j][1];
      if (Math.abs(dx) > ENG_ROW.driftMax || Math.abs(dy) > ENG_ROW.driftMax) drift += id + ':' + Math.round(dx) + ',' + Math.round(dy) + ';'; }); });
  // labels riding into each other (builder H, piece 4): each label rides with its own stack's foot, so when the floor lays itself out
  // again (a folder appears, a stack grows) two stacks can move apart or together and their labels, or a label and the open card, come
  // to cover each other with no other cause for a new decision. Once they have come to rest covering, decide again (once per pair).
  const bx = [];
  d.units.forEach(U => { const a = byId.get(U.anchor); if (!a) return; const p = engRowFoot(a); let w = U.w, h = U.h;
    if (U.ids.length === 1 && engCardFor(U.anchor)) { const cs = engCardSize(U.anchor); if (cs) { w = Math.max(w, cs[0]); h = Math.max(h, ENG_FORM.lineH + cs[1]); } }
    const x = p[0] + U.off[0], y = p[1] + U.off[1]; bx.push([x, y, x + w, y + h, U.anchor]); });
  // Counted for any two labels at any load (piece A tidy): it is now a patch that places one of the two again, where a whole arrangement
  // for every pair at twelve had moved 15 labels and resized 8 over a still minute
  let ride = '';
  for (let i = 0; i < bx.length; i++) for (let j = i + 1; j < bx.length; j++)
    if (engOv(bx[i], bx[j], 0, 0)) ride += bx[i][4] + '+' + bx[j][4] + ';';          // (piece A tidy) at every load: the patch places one of the two again, nothing else
  if (!ride) ENGR.rideTried = '';
  // (piece A, round 2) a label carried by its stack out of the window, with no other cause for a decision: once at rest, it alone is
  // placed again (a patch; nothing else moves)
  const outs = bx.filter(b => b[0] < 0 || b[2] > PG.W || b[1] < 0 || b[3] > PG.H).map(b => b[4]).join(';');
  if (!outs) ENGR.outTried = '';
  const key = drift + '#' + ride + '#' + outs;
  if (key !== ENGR.driftKey) { ENGR.driftKey = key; ENGR.driftAt = now; return null; }
  if (ride && ride !== ENGR.rideTried && now - ENGR.driftAt >= ENG_ROW.restMs) { ENGR.rideTried = ride; return 'labels ride into each other'; }
  if (drift && now - ENGR.driftAt >= ENG_ROW.restMs) return 'a stack moved on the floor';
  if (outs && outs !== ENGR.outTried && now - ENGR.driftAt >= ENG_ROW.restMs) { ENGR.outTried = outs; return 'a label rode out of view'; }
  return null;
}

function engRowDraw(tops, face) {
  const now = performance.now(), key = engRowCamKey();
  if (key !== ENGR.camKey) {
    // whether every move of the camera since the last decision was the page's own (a refit to a change of the floor), not the person's
    if (ENGR.dec && ENGR.camKey === ENGR.dec.camKey) ENGR.allAuto = true;
    ENGR.allAuto = ENGR.allAuto && typeof PG.camAuto === 'function' && PG.camAuto();
    ENGR.camKey = key; ENGR.movedAt = now; }
  const why = engRowTrigger(tops, now);
  if (why) engRowDecide(tops, face, why);
  const d = ENGR.dec, R = ENG_ROW, F = ENG_FORM, { ctx } = PG;
  const byId = new Map(tops.map(t => [t.a.id, t]));
  const drawn = [], hovId = PG.hov; ENGR.spots = new Map(); ENGR.hover = null;
  // where each unit stands this frame: its anchor's foot plus the decided offset (riding with the stack while the camera moves)
  d.units.forEach(U => {
    const at = byId.get(U.anchor); if (!at) return;
    const p = engRowFoot(at), X = p[0] + U.off[0], Y0 = p[1] + U.off[1];
    U.ids.forEach((id, j) => {
      const t = byId.get(id), L = d.lab.get(id); if (!t) return;
      const r = engRec(t.a); if (!r) return;
      const c = { r, id, card: engCardFor(id) }, sp = engRowSpec(c, L.k, L.nm, L.fix, d.wordsW, d.inPlace), y0 = Y0 + U.rows[j], top = engRowLineTop(t, face), my = y0 + F.lineH / 2;
      let line = [];
      if (U.at === 'row') {
        if (U.ids.length > 1) line = [top, [top[0], my], [X - 2, my]];
        else if (!U.jog) line = [top, [top[0], my + F.markR + 1]];
        else { const jy = U.jyOff != null ? top[1] + U.jyOff : Y0 + U.h + R.tierGap / 2; line = [top, [top[0], jy], [X + F.markR, jy], [X + F.markR, my + F.markR + 1]]; }
      }
      drawn.push({ id, r, t, sp, x: X, y: y0, box: [X, y0, X + sp.w, y0 + sp.h], line, at: U.at, covers: !!U.covers, band: L.band, share: L.share, list: U.ids.length > 1 ? U.idx : null, k: L.k, cut: sp.cut });
      ENGR.spots.set(id, { x: X, y: y0, nameH: F.lineH });
    });
  });
  // hover fades nothing (round 5: a mouse crossing the floor made the picture pulse); only a selected agent quietens the rest (D17)
  const dim = a => PG.focus && typeof PG.dimOf === 'function' ? PG.dimOf(a) : 1;
  ctx.save(); ctx.strokeStyle = R.lineInk; ctx.lineWidth = R.lineW;
  drawn.forEach(o => { if (!o.line.length) return; ctx.globalAlpha = dim(o.t.a); ctx.beginPath(); o.line.forEach((q, i) => i ? ctx.lineTo(Math.round(q[0]) + 0.5, Math.round(q[1]) + 0.5) : ctx.moveTo(Math.round(q[0]) + 0.5, Math.round(q[1]) + 0.5)); ctx.stroke(); });
  ctx.restore();
  // form 0: a stack in view with no floor label carries the legend's mark on its top
  const marks = [], labelled = new Set(drawn.map(o => o.id));
  tops.forEach(t => {
    const r = engRec(t.a); if (!r || r.cond === 'archived' || labelled.has(r.id)) return;          // a hovered one keeps its mark and its hit (else the pointer loses it and the hover flickers)
    const lt = engRowLineTop(t, face); if (lt[0] < -20 || lt[0] > PG.W + 20 || lt[1] < -20 || lt[1] > PG.H + 20) return;
    const y = lt[1] - F.markR - R.markLift;
    engDrawMark(Object.assign(engLook(r), { ink: PG.markInk(t.a) }), lt[0], y, F.markR);          // markInk: its brand colour, listed in __state().marks marks.push({ id: r.id, at: [Math.round(lt[0]), Math.round(y)] });
    PG.hit({ kind: 'readout', a: t.a, test: PG.rectTest(lt[0] - F.markR - 2, y - F.markR - 2, lt[0] + F.markR + 2, y + F.markR + 2) });
  });
  // draw order (5.2 step 6): urgency, then standing; the most urgent drawn last. The card's agent: its name line only (the page draws
  // the question and buttons under it, at engRowSpot)
  drawn.slice().sort((p, q) => p.r.need.rate - q.r.need.rate || p.r.standing - q.r.standing).forEach(o => {
    if (o.id === hovId) return;
    const sp = o.sp.nameOnly ? Object.assign({}, o.sp, o.sp.nameOnly) : o.sp;
    // a label at its own stack stands over the floor's folders and files: a sheet of the page's paper under it keeps its words legible
    // (builder H, piece 4: an idle agent's lines at its stack read through a folder's title at 5 agents)
    if (o.at === 'stack' && sp.lines.length > 1) { ctx.save(); ctx.globalAlpha = 0.86 * dim(o.t.a); ctx.fillStyle = R.stackBg; PG.rrect(o.x - 4, o.y - 2, sp.w + 8, sp.h + 4, 3); ctx.fill(); ctx.restore(); }
    engDrawForm(sp, o.x, o.y, dim(o.t.a));
    PG.hit({ kind: 'readout', a: o.t.a, test: PG.rectTest(o.box[0] - 2, o.box[1] - 1, o.box[0] + sp.w + 2, o.box[1] + sp.h + 1) });
    engPieceHits(o.r, o.t.a, sp, o.x, o.y);
  });
  // the hovered agent opens over its neighbours for the moment; nothing else moves. An unlabelled one shows at its own stack.
  ENGR.hoverForm = null;          // the hovered agent's opened label as drawn this frame: where its words are (live.html: a click on "N files changed")
  if (hovId && byId.has(hovId)) {
    const t = byId.get(hovId), r = engRec(t.a), o = drawn.find(q => q.id === hovId);
    if (r && r.cond !== 'archived') {
      const c = { r, id: hovId, card: engCardFor(hovId) };
      let sp, x, y;
      if (o) { x = o.x; y = o.y; sp = engHoverOpen(c, o, t.a, drawn, hovId); }          // in place: one label, what it showed stays put
      else { sp = engHoverWho(engRowSpec(c, R.hoverForm), t.a); if (sp.nameOnly) sp = Object.assign({}, sp, sp.nameOnly);
        const p = engRowLineTop(t, face); const hp = engHoverPlace(sp, p[0] + t.hw + R.stackGap, p[1] - ENG_FORM.lineH, null, drawn, hovId); x = hp.x; y = hp.y; }
      ctx.save(); ctx.fillStyle = R.hoverBg; ctx.strokeStyle = R.hoverEdge; ctx.lineWidth = 1; PG.rrect(x - 5, y - 3, sp.w + 10, sp.h + 6, 4); ctx.fill(); ctx.stroke(); ctx.restore();
      engDrawForm(sp, x, y, 1); ENGR.hoverForm = { id: hovId, sp, x, y };
      PG.hit({ kind: 'readout', a: t.a, test: PG.rectTest(x - 5, y - 3, x + sp.w + 5, y + sp.h + 3) });
      engPieceHits(r, t.a, sp, x, y);
    }
  }
  ENGR.frameRide = drawn; ENGR.marks = marks;
}

// a piece of a drawn label that opens something of its own: the compaction's line opens what the agent kept (live.html: the summary
// sheet). A readout hit over the line only, pushed after the label's, so it is the one picked there; the rest of the label is as before
function engPieceBoxes(sp, x, y) {
  const out = []; let yy = y;
  sp.lines.forEach(l => { if (l.piece) out.push({ piece: l.piece, box: [x + (l.indent || 0), yy, x + Math.max(l.w || 0, l.indent || 0), yy + l.h] }); yy += l.h; });
  return out;
}
function engPieceHits(r, a, sp, x, y) {
  const c = engPieceBoxes(sp, x, y).filter(q => q.piece === 'compaction'); if (!c.length) return;
  const p = r.say.lines.find(q => q.piece === 'compaction'), b = [Math.min(...c.map(q => q.box[0])), c[0].box[1], Math.max(...c.map(q => q.box[2])), c[c.length - 1].box[3]];
  PG.hit({ kind: 'readout', a, piece: 'compaction', i: p ? p.compactIdx : null, tip: 'what it kept at the compaction · click to read it', test: PG.rectTest(b[0] - 2, b[1], b[2] + 2, b[3]) });
}

// the pointed-at label, opened in place (piece B: the labels jumped around too much; piece A had opened it as a second
// copy wherever it covered no other label's words, 86 px above the resting one at four agents). Its name and the lines it showed stay
// exactly where they were, under the pointer; what pointing adds (the pieces of its fullest form it did not show, then which harness
// and when it last acted) is added below them. An added line that would cover another label's words, the open card, the tree or leave
// the window is left out, last first: nothing else moves for a pointer. An opened card's label adds nothing (the card says it).
function engHoverOpen(c, o, a, drawn, hovId) {
  const rest = o.sp.nameOnly ? Object.assign({}, o.sp, o.sp.nameOnly) : o.sp;
  if (o.sp.card) return rest;
  const full = engRowSpec(c, Math.max(ENG_ROW.hoverForm, o.k)), had = new Set(rest.lines.map(l => l.piece).filter(Boolean));
  // the few words that end its name line (form 3), said there whole, are not added again by the fullest form's line for them (cut
  // short there, that line is added: pointing says them whole)
  const ws = rest.lines[0].segs.find(g => g.words), wk = ws && !/…$/.test(String(ws.t)) ? String(ws.t).trim().slice(0, 14) : '';
  const said = l => wk && l.segs.map(g => g.t || '').join('').trim().startsWith(wk);
  full.lines.slice(1).forEach(l => { if (l.piece && said(l)) had.add(l.piece); });          // its whole piece, every wrapped line of it
  const add = full.lines.slice(1).filter(l => l.piece && !had.has(l.piece)).concat(engHoverWho({ lines: [], h: 0, w: 0 }, a).lines);
  const words = drawn.filter(q => q.id !== hovId).map(q => { const s = q.sp.nameOnly ? q.sp.nameOnly : q.sp; return [q.x, q.y, q.x + s.w, q.y + s.h]; });
  const card = PG.askBox && PG.askBox(); if (card) words.push(card);
  const tree = engRowTreeBox(), mk = n => { const ls = rest.lines.concat(add.slice(0, n));
    return Object.assign({}, rest, { lines: ls, h: Math.max(rest.h, ls.reduce((h, l) => h + l.h, 0)), w: Math.max(rest.w, ...ls.map(l => l.w || 0)) }); };
  let n = add.length, sp = mk(n);
  for (; n > 0; n--) { sp = mk(n); const b = [o.x - 5, o.y - 3, o.x + sp.w + 5, o.y + sp.h + 3];
    if (b[2] <= PG.W && b[3] <= PG.H && !(tree && engOv(b, tree, 0, 0)) && !words.some(q => engOv(b, q, 0, 0))) break; }
  if (!n) sp = rest;
  ENGR.hover = { id: hovId, away: false, box: [o.x, o.y, o.x + sp.w, o.y + sp.h].map(v => Math.round(v)), covers: [], moved: [0, 0], added: n, left: add.length - n };
  return sp;
}

// where the pointed-at label opens (piece A, fault 2): it opened downward and rightward from its name line and covered the label
// under it. An opened label may cover floor, never another label's words (or the open card's) while free room stands open. It still
// covers the place its closed label had (so the pointer stays on it and the hover holds): it may grow down or up, right or left, from
// there; of the places that cover no other label's words, the one nearest its closed place; with none, the one covering least.
function engHoverPlace(sp, x0, y0, o, drawn, hovId) {
  const pad = 5, w0 = o ? o.sp.w : 0, h0 = o ? (o.sp.nameOnly ? ENG_FORM.lineH : o.sp.h) : ENG_FORM.lineH;
  const words = drawn.filter(q => q.id !== hovId).map(q => { const s = q.sp.nameOnly ? q.sp.nameOnly : q.sp; return [q.x, q.y, q.x + s.w, q.y + s.h, q.id]; });
  const card = PG.askBox && PG.askBox(); if (card && !(o && o.sp.card)) words.push(card.concat(['card']));
  const tree = engRowTreeBox(), top = PG.PICK_CLEAR;
  const ys = [], xs = [], dyMax = Math.max(0, sp.h - h0), dxMax = o ? Math.max(0, sp.w - w0) : 0;          // an unlabelled one opens right of its stack, never over it
  for (let d = 0; d < dyMax; d += ENG_FORM.detailH) ys.push(y0 - d); ys.push(y0 - dyMax);
  for (let d = 0; d < dxMax; d += 24) xs.push(x0 - d); xs.push(x0 - dxMax);
  // past those, beside it: left or right of its closed place, or above or below it (the closed place stays the pointer's, see engRowDraw)
  const pts = []; ys.forEach(y => xs.forEach(x => pts.push([x, y])));
  const gap = 10, sides = o ? [x0 - sp.w - gap, x0 + w0 + gap] : [];
  for (let d = 0; d <= sp.h; d += ENG_FORM.detailH) for (const x of sides) { pts.push([x, y0 - d]); if (d) pts.push([x, y0 + d]); }
  for (let d = 0; d <= sp.w; d += 24) for (const y of [y0 - sp.h - gap, y0 + h0 + gap]) { if (o) pts.push([x0 - d, y]); if (d || !o) pts.push([x0 + d, y]); }
  let best = null;
  for (const [x, y] of pts) {
    const b = [x - pad, y - pad + 2, x + sp.w + pad, y + sp.h + pad - 2];
    if (b[0] < 0 || b[2] > PG.W || b[1] < top || b[3] > PG.H) { if (!(x === x0 && y === y0)) continue; }
    if (!(x === x0 && y === y0) && tree && engOv(b, tree, 0, 0)) continue;
    const hit = words.filter(q => engOv(b, q, 0, 0));
    const area = hit.reduce((a, q) => a + (Math.min(b[2], q[2]) - Math.max(b[0], q[0])) * (Math.min(b[3], q[3]) - Math.max(b[1], q[1])), 0);
    const cost = area * 100 + Math.abs(y - y0) + Math.abs(x - x0);
    if (!best || cost < best.cost) best = { x, y, cost, covers: hit.map(q => q[4]) };
    if (!hit.length && x === x0 && y === y0) break;
  }
  best.away = !(best.x <= x0 + 0.5 && best.y <= y0 + 0.5 && best.x + sp.w >= x0 + w0 - 0.5 && best.y + sp.h >= y0 + h0 - 0.5);
  ENGR.hover = { id: hovId, away: best.away, box: [best.x, best.y, best.x + sp.w, best.y + sp.h].map(v => Math.round(v)), covers: best.covers, moved: [Math.round(best.x - x0), Math.round(best.y - y0)] };
  return best;
}

// the hover label's last line: "Claude Code · 22 min ago" (the harness, and how long ago the agent last acted)
function engHoverWho(sp, a) {
  const F = ENG_FORM;
  const prov = typeof PG.provLabel === 'function' ? PG.provLabel(a) : ({ claude: 'Claude Code', codex: 'Codex' })[a.prov || 'codex'] || String(a.prov || 'Codex');
  let ago = '';
  if (typeof PG.lastAct === 'function' && PG.ST) { const s = PG.ST.find(q => q.a === a || (q.a && q.a.id === a.id)); if (s) { const m = Math.max(0, PG.NOWT - PG.lastAct(s)) / 60;
    ago = m < 1 ? 'just now' : m < 60 ? Math.round(m) + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + (Math.round(m / 1440) === 1 ? ' day ago' : ' days ago'); } }
  const hw = typeof PG.hoverWord === 'function' ? PG.hoverWord(a.id) : null;          // the page's word: harness and time, or the hand-back or archive word
  const t = hw || (prov + (ago ? ' · ' + ago : '')), px = F.detailPx, ind = 2 * F.markR + F.gap;
  const line = { segs: [{ t, px, w: 400, ink: '#85837d' }], h: F.detailH, indent: ind, piece: 'who' }; line.w = ind + engTw(t, px, 400);
  return Object.assign({}, sp, { lines: sp.lines.concat([line]), h: sp.h + line.h, w: Math.max(sp.w, line.w) });
}

// the top-left of an agent's name line on the floor this frame, or null when it has no floor label (the page hangs the card under it)
function engRowSpot(id) { return ENGR.spots.get(id) || null; }

// driver: per labelled agent its form, place (row or stack), box, list, line, its length and bends, whether cut; per unlabelled why
function engRowState() {
  const d = ENGR.dec; if (!d) return null;
  const rnd = v => Math.round(v * 10) / 10;
  const lab = (ENGR.frameRide || []).map(o => {
    const segs = engPts2Segs(o.line);
    return { id: o.id, cond: o.r.cond, form: o.k, at: o.at, band: o.band, share: +(o.share || 0).toFixed(4), box: o.box.map(rnd), list: o.list, line: o.line.map(q => q.map(rnd)),
      lineLen: Math.round(segs.reduce((a, s) => a + engSegLen(s), 0)), bends: Math.max(0, segs.length - 1), cut: o.cut, covers: o.covers, strong: engStrong(o.r), level: o.r.level,
      pieces: [...new Set((o.sp.nameOnly ? [] : o.sp.lines).map(l => l.piece).filter(Boolean))] };          // the detail pieces it says, in order (the compaction among them)
  });
  return {
    labelled: lab, unlabelled: [...d.out.entries()].map(([id, why]) => ({ id, why })), marks: ENGR.marks,
    stacks: (d.cols || []).map(k => ({ id: k.id, box: k.box.map(rnd) })),          // every stack in view at the decision: its column's box
    card: (() => { const o = (ENGR.frameRide || []).find(q => q.sp && q.sp.card); return o ? { id: o.id, nameLine: [o.x, o.y, o.x + o.sp.nameOnly.w, o.y + ENG_FORM.lineH].map(rnd), reserved: [o.x, o.y + ENG_FORM.lineH, o.x + o.sp.w, o.y + o.sp.h].map(rnd), cardSize: o.sp.card } : null; })(),
    freeForOmitted: ENGR.free || [],          // per agent left out for room: a spot in the row's band where its name line would have fit, or null
    forms: Object.fromEntries(d.form), names: ENG_ROW.squeeze[d.nameI], lastWhy: d.why, base: Math.round(d.base),
    totals: { labels: lab.length, onRow: lab.filter(o => o.at === 'row').length, atStack: lab.filter(o => o.at === 'stack').length,
      lineLen: lab.reduce((a, o) => a + o.lineLen, 0), bends: lab.reduce((a, o) => a + o.bends, 0),
      tiers: lab.filter(o => o.at === 'row').length ? Math.round((d.base - Math.min(...lab.filter(o => o.at === 'row').map(o => o.box[1]))) / (ENG_FORM.lineH + ENG_ROW.tierGap)) : 0 },
    hover: ENGR.hover && ENGR.hoverForm ? Object.assign({}, ENGR.hover, { pieces: [...new Set(ENGR.hoverForm.sp.lines.map(l => l.piece).filter(Boolean))] }) : ENGR.hover,          // the pointed-at label as opened: its box, the labels it covers (none while free room stands), how far it moved
    decisionMs: Math.round(ENGR.ms * 10) / 10, tries: ENGR.tries, decisions: ENGR.n, log: ENGR.log, room: ENGR.ctx,
  };
}
