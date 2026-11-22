/* The agent tree at the top left (plan 3.9, 4.1, 4.2, 5.5; P3, D4, D18, D22). Second rough version (after the planner's grading 1,
   points 6 to 8).
   Every agent born at the moment shown has a row: children indented under their parent (whose child it is, never where it works),
   newer above older by start time, children newest first. The order changes only when an agent starts (D22, 5.5). A parent's
   finished-and-seen children fold into one quiet "N finished" line; archived agents are one quiet line at the foot. At a small window the
   box is capped (ENG_NAIVE.treeMaxArea); rows past it fold into one line at the foot that says what it holds in the condition words,
   and a click on it opens the full tree, scrolling inside its box, until closed (integration round 3).
   Each row: the legend's mark, the name, and at the right a quiet piece (a parent's children, else the agent's folder). The row's
   look follows the agent's level: bold and a solid sign only at strong and call, and for every agent on the engine's list until
   you attend to it (grading 7; round 2).
   Pointers (grading 6; round 2; 2026-11-22): a right-angled line from a row to its agent's stack top, only for the hovered tree
   row, and never for the selected agent (it is already in focus, so the line would only crowd it): a click on a row takes its line away
   at once. A stack out of view: the line leads to the window's edge toward it. Nothing else.
   The tree is an overlay like a chat window (grading 8): its own paper ground at every zoom; the floor does not move for it; its box
   is given out (engTreeBox) so other marks can keep out of it.
   Numbers: ENG_TREE only. Reads the page through PG and the engine (engFloor, engLook, engDrawMark, engCut, engRowSpot) only. */
const ENG_TREE = {
  // every entry: naive, builder's (chosen while building; to be replaced by looking and measuring)
  x: 16, y: 16,            // (naive, builder's) the tree's top-left corner on the screen, px (the rows' corner; the ground reaches beyond)
  w: 280,                  // (naive, builder's; brief: "about 280 px") the tree's width, mark to the end of the right-hand piece
  rowH: 18,                // (naive, builder's; brief: "about 18 px") one row
  indent: 14,              // (naive, builder's) one level of indent
  markR: 5,                // (naive, builder's) half size of a row's mark
  namePx: 12, sidePx: 11,  // (naive, builder's) the name's and the right-hand piece's type size
  sideMax: 92,             // (naive, builder's) the right-hand piece is cut to this many px
  sideGap: 10,             // (naive, builder's) room kept between a name and the right-hand piece
  pad: 10,                 // (naive, builder's) the ground reaches this far beyond the rows
  ground: '#fffdf9', edge: '#d8d2c6', radius: 8,   // (naive, builder's) the ground: the page's own card colour and line (like the chat windows)
  shadow: 'rgba(40,40,52,0.10)', shadowBlur: 14, shadowY: 3,   // (naive, builder's) its soft shadow, lighter than a chat window's
  guide: '#d8d2c6',        // (naive, builder's) the thin guide lines from a parent to its children
  hoverFill: '#f3efe6', focusFill: '#ebe6db',   // (naive, builder's) the hovered row; the selected agent's row
  footGap: 6,              // (naive, builder's) room above the tree's foot lines
  clearBelow: 10,          // (naive, builder's) room kept above the made-up floor's test controls (PG.tptBox) or the window's foot
  pointerOut: 10,          // (naive, builder's) a pointer to a stack under the tree goes round its foot this far out
  laneStep: 6,             // (naive, builder's) spacing of the lanes pointers take when they go round the tree
  edgeIn: 14,              // (naive, builder's) a pointer to a stack out of view stops this far inside the window's edge
  pointerInk: '#8f8a80', pointerHotInk: '#5b5f66',   // (naive, builder's) a standing pointer's ink; the hovered or selected one's
  // the head (integration brief C): how many agents need you, only while any do; a click (or the key in engine/keys.js) steps
  // to the next, most pressing first. The "For you" list and its tray went (2026-11-16)
  headPx: 12, headW: 600,  // (naive, builder's) the head's words: the names' size, between a quiet row's weight and a loud one's
  headInk: '#2c3440', headAskInk: '#e0622a',   // (builder's) the legend's inks: the page's ink; the asking orange while any of them asks
  alertsLine: true,        // (builder's) while something needs you and the browser has not been asked, one quiet foot line offers desktop alerts
  // the window's own line at its top (2026-11-22: the top-left agent list is a window that expands, and folded shows only the
  // number of active agents): "N active" and the list's disclosure chevron (down open, right folded, as the "N finished" lines' ▾ / ▸); folded,
  // the window is that line alone, a chip in the same corner, its top line where it was. The whole line is one button (agent list)
  barInk: '#5b5f66', barW: 600,
  chevR: 4, chevInk: '#85837d',   // (builder's) the chevron's half size and ink (the quiet grey of the tree's side pieces)   // (builder's) "N active": the names' quiet ink, the side text's size, a little heavier so it reads as the title
  saveKey: 'agent-ide.agent-list',   // (builder's) this browser's choice, '1' open / '0' folded, the page's other saved choices' key style
};
const ENGT = { state: null, box: null, open: false, scroll: 0, maxScroll: 0, grp: new Set(),
  shut: (() => { try { return localStorage.getItem(ENG_TREE.saveKey) === '0'; } catch (e) { return false; } })(), btn: null };          // shut: the list folded to its chip (default open: today's look)
// active: an agent whose turn runs now and is not waiting on you (the legend's running, and stalled: running but silent); not asking
// (it waits on you: the chip's orange mark says so), finished, failed, stopped, idle or archived
const engTreeTopbarY = () => { const v = parseFloat(document.documentElement.style.getPropertyValue('--topbar-y')); return isFinite(v) ? v : ENG_TREE.y - ENG_TREE.pad; };
const engTreeActive = r => r.cond === 'running' || r.cond === 'stalled';
function engTreeSetShut(shut) {
  ENGT.shut = !!shut; try { localStorage.setItem(ENG_TREE.saveKey, ENGT.shut ? '0' : '1'); } catch (e) {}
  if (typeof PG !== 'undefined' && PG.wake) PG.wake();
}
// the list's one control, a real button (Tab reaches it; Enter or Space toggles; its label says what it does and what the line says),
// laid over the window's top line each frame; transparent, so the canvas draws what it shows
function engTreeBtn() {
  if (ENGT.btn) return ENGT.btn;
  const st = document.createElement('style');
  st.textContent = '#agentListBtn{position:fixed;z-index:2;margin:0;padding:0;border:0;border-radius:6px;background:transparent;cursor:pointer;display:none}' +
    '#agentListBtn:hover{background:rgba(44,52,64,.05)}#agentListBtn:focus{outline:none}#agentListBtn:focus-visible{outline:2px solid #8f8a80;outline-offset:1px}';
  document.head.append(st);
  const b = document.createElement('button'); b.type = 'button'; b.id = 'agentListBtn';
  b.addEventListener('click', ev => { ev.stopPropagation(); engTreeSetShut(!ENGT.shut); });
  b.addEventListener('keydown', ev => { if (ev.key === ' ' || ev.key === 'Enter') ev.stopPropagation(); });          // the button's own keys, not the page's typing or play keys
  document.body.append(b); ENGT.btn = b; return b;
}
function engTreeBtnPlace(box, label) {
  const b = engTreeBtn();
  if (!box) { if (b.style.display !== 'none') b.style.display = 'none'; return; }
  const st = b.style, v = [box[0], box[1], box[2] - box[0], box[3] - box[1]].map(n => Math.round(n) + 'px');
  if (st.display !== 'block') st.display = 'block';
  if (st.left !== v[0]) st.left = v[0]; if (st.top !== v[1]) st.top = v[1]; if (st.width !== v[2]) st.width = v[2]; if (st.height !== v[3]) st.height = v[3];
  if (b.getAttribute('aria-label') !== label) { b.setAttribute('aria-label', label); b.title = label; b.setAttribute('aria-expanded', ENGT.shut ? 'false' : 'true'); }
}          // open: the full tree, scrolling in its box; grp: the parents whose "N finished" group is open (this page session only, as open is)

// the rows in order: a walk of the family tree, newest first at every level
function engTreeRows(recs) {
  const by = new Map(recs.map(r => [r.id, r])), live = r => r.cond !== 'archived';
  const newest = (x, y) => (y.a.start || 0) - (x.a.start || 0) || (x.id < y.id ? -1 : 1);
  // a root: no parent, or a parent that is archived or not born (an active child of an archived parent stands at the top: builder's)
  const roots = recs.filter(r => live(r) && !(r.facts.parent && by.has(r.facts.parent) && live(by.get(r.facts.parent)))).sort(newest);
  const kidsOf = r => r.facts.children.map(id => by.get(id)).filter(k => k && live(k)).sort(newest);
  // a finished child goes into its parent's "N finished" group with its own finished family (2026-11-21: under each parent, how
  // many are finished, when at least one is); one with a family member still at work stays a row of its own
  const doneSeen = k => k.facts.ended === 'answer' && k.facts.seen && !k.facts.running && !k.facts.asking && kidsOf(k).every(doneSeen);
  const out = [];
  const walk = (r, d, grp) => {
    out.push({ kind: 'agent', r, d, grp });
    const ks = kidsOf(r), fold = ks.filter(doneSeen);
    ks.filter(k => !doneSeen(k)).forEach(k => walk(k, d + 1, grp));
    if (!fold.length) return;
    const open = ENGT.grp.has(r.id);          // a click on the line opens the group in place: its finished subagents listed under it
    out.push({ kind: 'finished', d: d + 1, n: fold.length, ids: fold.map(k => k.id), parent: r.id, open, grp });
    if (open) fold.forEach(k => walk(k, d + 2, true));
  };
  roots.forEach(r => walk(r, 0, false));
  return { rows: out, archived: recs.filter(r => !live(r)).map(r => r.id) };
}

// A row's look by level (grading 7, D4). engLook gives the legend's look; in the tree, bold and a solid sign are only for strong and
// call. Below that a finished or failed agent takes the quiet seen look (hollow, grey, weight 500), so a child's low finish reads as
// quiet as its parent's count says; other signs (stalled) stay solid but faded, since the hollow mark has no stalled form.
// TO MAIN (legend.js): if every surface should do this, engLook itself could take it: nameW 700 only at strong/call (plain is 600
// today), and hollow for finished/failed below strong whether seen or not. Kept here until you decide.
function engTreeLook(r) {
  const look = Object.assign({}, engLook(r)), loud = r.level === 'strong' || r.level === 'call' || look.needs;          // on the list: loud until attended (round 2)
  if (loud) return Object.assign(look, { nameW: 700, hollow: false, markAlpha: 1 });
  look.nameW = 500;
  if (look.hollow) Object.assign(look, { markAlpha: 0.7, nameInk: '#85837d' });          // the legend says which are hollow (seen, or a child's low news)
  else if (look.glyph) Object.assign(look, { markAlpha: 0.6, nameInk: '#85837d' });
  return look;
}

// where an agent's stack top is on the screen this frame, in view or not (a folded child: its parent's stack; builder's)
function engTreeStackTop(r) {
  const REC = PG.REC, q = REC[r.id] || (r.facts.parent && REC[r.facts.parent]);
  return q ? (q.top || q.footS) || null : null;
}

function engTreeDraw() {
  const { ctx, W, H, stext, hit, rectTest } = PG;
  if (!ctx || !W) return;
  const t0 = performance.now();
  const T = ENG_TREE, recs = engFloor(), { rows, archived } = engTreeRows(recs);
  // who needs you (the engine's one list, engine/surfaces.js): the head counts them; their rows are the last to give way
  const need = typeof engAttnList === 'function' ? engAttnList() : [], needIds = new Set(need.map(e => e.id)), askN = need.filter(e => e.asking).length;
  const head = need.length ? 1 : 0;
  const alerts = T.alertsLine && head && 'Notification' in window && window.isSecureContext && Notification.permission === 'default';
  // yb: the window's top line; y0: the rows below it. The window's top edge is the top bar's icon row's top (--topbar-y, live.html
  // TOPBAR), or lower where the page puts the tree lower (ENG_TREE.y: in the app, under its title band)
  const x0 = T.x, yb = Math.max(T.y, engTreeTopbarY() + T.pad), y0 = yb + T.rowH, x1 = x0 + T.w;
  const act = recs.filter(engTreeActive).length, asks = recs.filter(r => r.cond === 'asking');
  const LS = { open: !ENGT.shut, active: act, asking: asks.length, text: act + ' active', box: null, line: null, mark: null };
  if (!rows.length && !archived.length) { ENGT.box = null; engTreeBtnPlace(null); ENGT.state = { box: null, rows: [], folded: [], pointers: [], head: null, list: LS }; return; }          // no agents: no tree (lead)
  const bcy = yb + T.rowH / 2, triW = 2 * T.chevR, textW = engTw(LS.text, T.sidePx, T.barW);
  // the waiting mark, folded only (open, the head line below says it): the first asking agent's own sign in the legend's orange, at
  // the rows' mark size, its count beside it only when more than one asks (builder's)
  const ask1 = need.find(e => e.asking), wq = ENGT.shut && asks.length ? (ask1 && asks.find(r => r.id === ask1.id)) || asks[0] : null;          // the most pressing asker's sign
  const wglyph = wq ? engLook(wq).glyph || 'question' : null, wnum = asks.length > 1 ? String(asks.length) : '', wnumW = wnum ? engTw(wnum, T.sidePx, 700) + 2 : 0;
  const barLabel = 'agent list: ' + LS.text + (asks.length ? ', ' + asks.length + (asks.length === 1 ? ' asks you' : ' ask you') : '') + (ENGT.shut ? '; show the list' : '; fold to the count');
  const drawBar = right => {          // "N active" at the left; the waiting mark after it (folded); the triangle at `right`
    stext(LS.text, x0, bcy, T.sidePx, T.barInk, 'left', T.barW);
    if (wglyph) { const mx = x0 + textW + 10 + T.markR; engDrawMark({ glyph: wglyph, hollow: false, markAlpha: 1, level: 'strong' }, mx, bcy, T.markR);
      if (wnum) stext(wnum, mx + T.markR + 2, bcy, T.sidePx, T.headAskInk, 'left', 700);
      LS.mark = { glyph: wglyph, n: asks.length, ink: 'orange', x: Math.round(mx), y: Math.round(bcy) }; }
    // the disclosure chevron, pointing down while open and right while folded (the "N finished" lines' ▾ / ▸), drawn so it reads at this size
    const k = T.chevR, cx = right - k;
    ctx.save(); ctx.strokeStyle = T.chevInk; ctx.lineWidth = 1.5; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.beginPath();
    if (ENGT.shut) { ctx.moveTo(cx - k / 2, bcy - k); ctx.lineTo(cx + k / 2, bcy); ctx.lineTo(cx - k / 2, bcy + k); }
    else { ctx.moveTo(cx - k, bcy - k / 2); ctx.lineTo(cx, bcy + k / 2); ctx.lineTo(cx + k, bcy - k / 2); }
    ctx.stroke(); ctx.restore();
  };
  if (ENGT.shut) {          // folded: the window's top line alone, as wide as it says, in the same corner and at the same height
    const cw = textW + (wglyph ? 10 + 2 * T.markR + wnumW : 0) + 10 + triW;
    const box = [x0 - T.pad, yb - T.pad, x0 + cw + T.pad, yb + T.rowH + T.pad];
    ENGT.box = box;
    ctx.save(); ctx.shadowColor = T.shadow; ctx.shadowBlur = T.shadowBlur; ctx.shadowOffsetY = T.shadowY;
    ctx.fillStyle = T.ground; ctx.beginPath(); ctx.roundRect(box[0], box[1], box[2] - box[0], box[3] - box[1], T.radius); ctx.fill();
    ctx.shadowColor = 'transparent'; ctx.strokeStyle = T.edge; ctx.lineWidth = 1; ctx.beginPath();
    ctx.roundRect(box[0] + 0.5, box[1] + 0.5, box[2] - box[0] - 1, box[3] - box[1] - 1, T.radius); ctx.stroke(); ctx.restore();
    drawBar(x0 + cw);
    LS.box = box.map(Math.round); LS.line = LS.box.slice(); engTreeBtnPlace(box, barLabel);
    ENGT.state = { box, bottomLimit: H - T.clearBelow, capLimit: null, rows: [], folded: [], hoveredRow: null, fold: { n: 0, words: '', ids: [], open: false, shown: null, scroll: 0, maxScroll: 0 },
      rowSpot: false, pointers: [], crossings: 0, head: null, need: { n: need.length, shown: [], cut: 0 }, list: LS, ms: +(performance.now() - t0).toFixed(3) };
    return;
  }
  // the foot: the test controls on made-up floors, else the window's foot
  let bot = H - T.clearBelow; { const tb = PG.tptBox(); if (tb && tb[0] < x1 + T.pad && tb[2] > x0 - T.pad) bot = Math.min(bot, tb[1] - T.clearBelow); }
  // room: idle rows (with nothing under them) fold first, all at once; past that, rows give way from the bottom. Everything that gives
  // way is told on one line at the foot, by the page's condition words ("3 working · 2 finished · 4 idle"); a click on it opens the
  // full tree, scrolling inside its box, until a second click closes it (integration round 3, the coordinator's ruling).
  // A row that needs you, the selected agent's row and the rows of their parents (so a kept row keeps its family line) are kept:
  // the quiet rows give way first. A row counted in "N need you" never folds; the selected agent's family gives way before it does.
  // The order never changes; which quiet row gives way can, when an agent starts or stops needing you.
  // A parent's "N finished" line (and its family line) never gives way: other rows do first (2026-11-21); an open group's rows
  // are kept like the selected agent's family.
  const keep = new Set(), fin = new Set();
  { const up = []; rows.forEach(row => { up.length = row.d;
    if (row.kind === 'finished') { fin.add(row); keep.add(row); up.forEach(q => { if (q) { fin.add(q); keep.add(q); } }); return; }
    up[row.d] = row;
    if (needIds.has(row.r.id) || row.r.id === PG.focus || row.grp) up.forEach(q => { if (q) keep.add(q); }); }); }
  const open = !!ENGT.open;
  let shown = rows.slice(), foldRows = [], cutNeed = 0, lim = bot, off = 0, vis = rows.length;
  // the foot: one quiet line for what is not shown as rows (the fold line, archived), joined (builder's: three lines took three rows)
  const footLines = () => (archived.length || foldRows.length || open ? 1 : 0) + (alerts ? 1 : 0);
  const fits = () => y0 + (head + shown.length) * T.rowH + (footLines() ? T.footGap + footLines() * T.rowH : 0) <= lim - T.pad;
  if (open) {
    // the full tree: every row, as many as the window holds between the head and the foot; the rest scroll inside the box (whole rows)
    const room = bot - T.pad - y0 - head * T.rowH - T.footGap - footLines() * T.rowH;
    vis = Math.max(1, Math.min(rows.length, Math.floor(room / T.rowH)));
    ENGT.maxScroll = Math.max(0, (rows.length - vis) * T.rowH);
    ENGT.scroll = Math.min(Math.max(0, ENGT.scroll || 0), ENGT.maxScroll);
    off = Math.floor(ENGT.scroll / T.rowH); shown = rows.slice(off, off + vis);
  } else {
    // the cap (ENG_NAIVE.treeMaxArea): the box covers at most that share of the window; it gives way to the rows that must stay
    const capA = typeof ENG_NAIVE !== 'undefined' && ENG_NAIVE.treeMaxArea, capBot = capA ? yb - T.pad + capA * W * H / (x1 - x0 + 2 * T.pad) : bot;
    const keptBot = y0 + (head + keep.size) * T.rowH + T.footGap + (1 + (alerts ? 1 : 0)) * T.rowH + T.pad;
    lim = Math.min(bot, Math.max(capBot, keptBot));
    ENGT.maxScroll = 0;
    if (!fits()) {
      const leaf = (row, i) => row.kind === 'agent' && row.r.cond === 'idle' && !keep.has(row) && !(shown[i + 1] && shown[i + 1].d > row.d);
      // all idle rows fold at once (no idle row comes and goes as room changes by a row or two: builder's)
      for (let i = shown.length - 1; i >= 0; i--) if (leaf(shown[i], i)) foldRows.unshift(shown.splice(i, 1)[0]);
      const pickFrom = ok => { for (let i = shown.length - 1; i >= 0; i--) if (ok(shown[i])) return i; return -1; };
      while (!fits() && shown.length) {
        let i = pickFrom(row => !keep.has(row));
        if (i < 0) i = pickFrom(row => !(row.kind === 'agent' && needIds.has(row.r.id)) && !fin.has(row));          // only the selected agent's family (or an open group) is left to give way
        if (i < 0) { cutNeed = 1; break; }          // rows that need you alone overrun the window: they stay, past the foot
        foldRows.push(shown.splice(i, 1)[0]);
      }
    }
  }
  // what the fold line holds, by the legend's condition words in the legend's order (a parent's "N finished" line counts as N finished)
  const foldIds = [], foldBy = new Map();
  foldRows.forEach(row => { const c = row.kind === 'agent' ? row.r.cond : 'finished', n = row.kind === 'agent' ? 1 : row.n;
    foldBy.set(c, (foldBy.get(c) || 0) + n); foldIds.push(...(row.kind === 'agent' ? [row.r.id] : row.ids)); });
  const foldWords = Object.keys(ENG_LEGEND).filter(c => foldBy.has(c)).map(c => foldBy.get(c) + ' ' + ENG_LEGEND[c].word).join(' · ');
  const h = (head + shown.length) * T.rowH + (footLines() ? T.footGap + footLines() * T.rowH : 0);
  const box = [x0 - T.pad, yb - T.pad, x1 + T.pad, y0 + h + T.pad];
  ENGT.box = box;
  // the ground: an overlay like a chat window, the same at every zoom
  ctx.save(); ctx.shadowColor = T.shadow; ctx.shadowBlur = T.shadowBlur; ctx.shadowOffsetY = T.shadowY;
  ctx.fillStyle = T.ground; ctx.beginPath(); ctx.roundRect(box[0], box[1], box[2] - box[0], box[3] - box[1], T.radius); ctx.fill();
  ctx.shadowColor = 'transparent'; ctx.strokeStyle = T.edge; ctx.lineWidth = 1; ctx.beginPath();
  ctx.roundRect(box[0] + 0.5, box[1] + 0.5, box[2] - box[0] - 1, box[3] - box[1] - 1, T.radius); ctx.stroke(); ctx.restore();
  drawBar(x1); LS.box = box.map(Math.round); LS.line = [x0 - T.pad, yb - T.pad, x1 + T.pad, yb + T.rowH].map(Math.round); engTreeBtnPlace(LS.line, barLabel);
  // the hovered row is the tree's own: the pointer over one of its rows (a hover of a stack on the floor draws no tree pointer)
  const m = PG.mouse, inBox = m && m[0] >= box[0] && m[0] <= box[2] && m[1] >= box[1] && m[1] <= box[3];
  const focus = PG.focus;
  let hovRow = null;
  const rowsOut = [], foldOut = [], anchor = new Map();
  let y = y0; const lastY = [];          // the centre line of the last agent row at each depth (where a guide line starts)
  // the head: "N need you" while any do (orange while any of them asks), and at its right the step and its key; a click steps
  let headOut = null;
  if (head) {
    const cy = y + T.rowH / 2, rb = [x0 - T.pad / 2, y, x1 + T.pad / 2, y + T.rowH];
    const over = inBox && m[1] >= rb[1] && m[1] < rb[3] && m[0] >= rb[0] && m[0] <= rb[2];
    if (over) { ctx.save(); ctx.fillStyle = T.hoverFill; ctx.fillRect(rb[0], rb[1], rb[2] - rb[0], rb[3] - rb[1]); ctx.restore(); }
    const text = need.length + (need.length === 1 ? ' needs you' : ' need you'), ink = askN ? T.headAskInk : T.headInk;
    const right = '';          // no "next ⌃." at its right (2026-11-22); a click still steps
    stext(text, x0, cy, T.headPx, ink, 'left', T.headW);
    const nx0 = typeof engAttnNext === 'function' ? engAttnNext(PG.focus, true) : null, nx = nx0 && nx0.e;
    const tip = nx ? 'next: ' + (nx.a.full || nx.id) + ', ' + nx.word : '';          // the line itself says what a click does and its key
    hit({ kind: 'treeneed', tip, test: rectTest(rb[0], rb[1], rb[2], rb[3]) });
    headOut = { text, right, n: need.length, asking: askN, ink: askN ? 'orange' : 'ink', box: rb, hovered: over };
    y += T.rowH;
  }
  shown.forEach(row => {
    const xi = x0 + row.d * T.indent, cy = y + T.rowH / 2, rb = [x0 - T.pad / 2, y, x1 + T.pad / 2, y + T.rowH];
    // guide lines: from the parent's mark down to this row, then a short tick across
    if (row.d > 0) {
      ctx.save(); ctx.strokeStyle = T.guide; ctx.lineWidth = 1; ctx.beginPath();
      const gx = xi - T.indent + T.markR + 0.5, from = lastY[row.d - 1] != null ? lastY[row.d - 1] + T.markR + 2 : y;
      ctx.moveTo(gx, from); ctx.lineTo(gx, cy + 0.5); ctx.lineTo(xi + 0.5, cy + 0.5); ctx.stroke(); ctx.restore();
    }
    lastY[row.d] = cy;          // an open group's rows hang from its "N finished" line
    if (row.kind === 'finished') {
      // a plain disclosure: the count and a small quiet triangle; a click opens the group in place, again folds it (a view choice only)
      const over = inBox && m[1] >= rb[1] && m[1] < rb[3] && m[0] >= rb[0] && m[0] <= rb[2];
      if (over) { ctx.save(); ctx.fillStyle = T.hoverFill; ctx.fillRect(rb[0], rb[1], rb[2] - rb[0], rb[3] - rb[1]); ctx.restore(); }
      engDrawMark({ glyph: 'finished', hollow: true, markAlpha: 0.7 }, xi + T.markR, cy, T.markR);
      const words = row.n + ' finished', tx = xi + 2 * T.markR + 6;
      stext(words, tx, cy, T.sidePx, over ? T.headInk : '#85837d', 'left', 500);
      stext(row.open ? '\u25BE' : '\u25B8', tx + engTw(words, T.sidePx, 500) + 5, cy, T.sidePx - 2, '#aaa59c', 'left', 500);
      hit({ kind: 'treefin', parent: row.parent, open: row.open, tip: row.open ? 'fold these back' : 'list them', test: rectTest(rb[0], rb[1], rb[2], rb[3]) });
      foldOut.push({ line: 'finished', parent: row.parent, n: row.n, ids: row.ids, open: row.open, hovered: over, box: rb });
      y += T.rowH; return;
    }
    const r = row.r, look = engTreeLook(r), f = r.facts;
    if (row.grp && f.ended === 'answer' && !f.running) Object.assign(look, { glyph: 'finished', hollow: true, markAlpha: 0.7, nameInk: '#85837d' });          // an open group's rows keep its check mark, idle or not
    const over = inBox && m[1] >= rb[1] && m[1] < rb[3] && m[0] >= rb[0] && m[0] <= rb[2];
    if (over) hovRow = r.id;
    if (r.id === focus || over) { ctx.save(); ctx.fillStyle = r.id === focus ? T.focusFill : T.hoverFill; ctx.fillRect(rb[0], rb[1], rb[2] - rb[0], rb[3] - rb[1]); ctx.restore(); }
    engDrawMark(look, xi + T.markR, cy, T.markR);
    // a planner's count is never cut: the name gives way to it (the name is cut, its full text goes to the hover tip)
    const side = r.cond === 'stopped' ? (f.children.length ? 'stopped · ' + engKidsText(f) : 'stopped') : f.children.length ? engKidsText(f) : r.s.rule && r.s.rule.dir ? r.s.rule.dir.split('/').filter(Boolean).pop() + '/' : '';
    const sideCut = side ? (f.children.length ? side : engCut(side, T.sidePx, 500, T.sideMax).t) : '', sideW = sideCut ? engTw(sideCut, T.sidePx, 500) : 0;
    if (sideCut) stext(sideCut, x1, cy, T.sidePx, f.children.length || r.cond === 'stopped' ? '#85837d' : '#aaa59c', 'right', 500);
    const nx = xi + 2 * T.markR + 6, name = engCut(r.a.full || r.id, T.namePx, look.nameW, x1 - nx - (sideW ? sideW + T.sideGap : 0));
    stext(name.t, nx, cy, T.namePx, look.nameInk, 'left', look.nameW);
    hit({ kind: 'readout', tree: true, cond: r.cond, a: r.a, tip: name.cut ? (r.a.full || r.id) : undefined, test: rectTest(rb[0], rb[1], rb[2], rb[3]) });          // tree: the drivers' name tree-row:<id>:<condition>
    anchor.set(r.id, [rb[2], cy]);
    rowsOut.push({ id: r.id, depth: row.d, level: r.level, mark: (look.glyph || 'harness') + (look.hollow ? ' hollow' : '') + (look.markAlpha < 1 ? ' faded' : ''),
      weight: look.nameW, cond: r.cond, name: name.t, right: sideCut, box: rb });
    y += T.rowH;
  });
  // the full tree scrolled: a thin quiet bar at the box's right edge says where the rows shown stand among all of them (builder's)
  if (open && ENGT.maxScroll > 0) {
    const ty0 = y0 + head * T.rowH, th = vis * T.rowH, k = th / (rows.length * T.rowH), bx = x1 + T.pad - 4;
    ctx.save(); ctx.fillStyle = T.edge; ctx.beginPath(); ctx.roundRect(bx - 1.5, ty0 + off * T.rowH * k, 3, Math.max(8, th * k), 1.5); ctx.fill(); ctx.restore();
  }
  // the foot: one quiet line. The fold line first (what gave way, in condition words; open: "close"), a click toggles it; archived after
  if (footLines()) y += T.footGap;
  { const sep = ' · ', sw = engTw(sep, T.sidePx, 500), cy = y + T.rowH / 2, arch = archived.length ? archived.length + ' archived' : '';
    let fx = x0;
    if (foldRows.length || open) {
      const full = open ? 'close' : foldWords, room = x1 - x0 - (arch ? sw + engTw(arch, T.sidePx, 500) : 0);
      const ct = engCut(full, T.sidePx, 500, room), tw = engTw(ct.t, T.sidePx, 500), fb = [x0 - T.pad / 2, y, x0 + tw + T.pad / 2, y + T.rowH];
      const over = inBox && m[1] >= fb[1] && m[1] < fb[3] && m[0] >= fb[0] && m[0] <= fb[2];
      if (over) { ctx.save(); ctx.fillStyle = T.hoverFill; ctx.fillRect(fb[0], fb[1], fb[2] - fb[0], fb[3] - fb[1]); ctx.restore(); }
      stext(ct.t, x0, cy, T.sidePx, over ? T.headInk : '#85837d', 'left', 500);
      const tip = open ? 'fold the tree back to fit its corner' : 'show every row (' + foldWords + '), scrolling inside the tree';
      hit({ kind: 'treefold', open, tip, test: rectTest(fb[0], fb[1], fb[2], fb[3]) });
      foldOut.push({ line: 'fold', n: open ? 0 : foldIds.length, words: open ? '' : foldWords, ids: foldIds, open, hovered: over, box: fb });
      fx += tw;
    }
    if (arch) { if (fx > x0) { stext(sep, fx, cy, T.sidePx, '#aaa59c', 'left', 500); fx += sw; }
      const tw = engTw(arch, T.sidePx, 500); stext(arch, fx, cy, T.sidePx, '#aaa59c', 'left', 500);
      hit({ kind: 'treearch', tip: arch, test: rectTest(fx - 2, y, fx + tw + 2, y + T.rowH) });          // a click opens the archive's pile, again closes it
      foldOut.push({ line: 'archived', n: archived.length, ids: archived, box: [fx, y, fx + tw, y + T.rowH] }); fx += tw; }
    if (fx > x0) y += T.rowH; }
  if (alerts) { const ab = [x0 - T.pad / 2, y, x1 + T.pad / 2, y + T.rowH];
    stext('desktop alerts are off · turn on', x0, y + T.rowH / 2, T.sidePx, '#aaa59c', 'left', 500);
    hit({ kind: 'treealerts', tip: 'a desktop alert when an agent asks you while this page is behind another window', test: rectTest(ab[0], ab[1], ab[2], ab[3]) });
    foldOut.push({ line: 'alerts', n: 0, ids: [], box: ab }); y += T.rowH; }
  // pointers: the hovered row only, and never the selected agent, whether picked from the list or the floor (2026-11-22: once
  // clicked it is in focus and the line only crowds it); nothing else (round 2, item 6: no line for an urgent agent merely
  // because the floor does not label it)
  const want = [], spot = typeof engRowSpot === 'function' ? engRowSpot : null;
  if (hovRow && hovRow !== focus) want.push({ id: hovRow, why: 'hovered row', hot: true });
  const ptrOut = [];
  let lane = 0;
  want.forEach(wp => {
    const r = engRec(wp.id), a = anchor.get(wp.id), top = r && engTreeStackTop(r);
    if (!top) { ptrOut.push({ id: wp.id, why: wp.why, drawn: false, reason: 'no stack drawn for it this frame' }); return; }
    // a stack out of view: the line stops inside the window's edge, on the way to it, and ends in an arrowhead pointing on
    const e = T.edgeIn, off = top[0] < 0 || top[0] > W || top[1] < 0 || top[1] > H;
    if (off && !wp.hot) { ptrOut.push({ id: wp.id, why: wp.why, drawn: false, reason: 'out of view: its edge mark tells it (one telling)' }); return; }
    const p = off ? [Math.min(Math.max(top[0], e), W - e), Math.min(Math.max(top[1], e), H - e)] : top;
    if (!off && p[0] >= box[0] && p[0] <= box[2] && p[1] >= box[1] && p[1] <= box[3]) { ptrOut.push({ id: wp.id, why: wp.why, drawn: false, reason: 'its stack is under the tree' }); return; }
    const tx = Math.round(p[0]) + 0.5, ty = Math.round(p[1]) - (off ? 0 : 3), ay = Math.round(a[1]) + 0.5;
    let path;
    // out of view: down (or up) beside the tree first, then across at the stack's own height to the edge, so the long run does not lie
    // along the top of the window through the page's own icons (builder's)
    if (off) { const lx = box[2] + T.pointerOut + lane * T.laneStep; lane++;
      path = tx > lx ? [[a[0], ay], [lx, ay], [lx, ty], [tx, ty]] : [[a[0], ay], [lx, ay], [lx, Math.max(ty, box[3] + T.pointerOut)], [tx, Math.max(ty, box[3] + T.pointerOut)]]; }
    else if (tx > box[2] + 2) path = [[a[0], ay], [tx, ay], [tx, ty]];          // out of the right edge, across, then down (or up) to the stack
    else { const lx = box[2] + T.pointerOut + lane * T.laneStep, ly = Math.max(box[3] + T.pointerOut + lane * T.laneStep, Math.min(ty, H - e)); lane++;
      path = [[a[0], ay], [lx, ay], [lx, ly], [tx, ly], [tx, ty]].filter((q, i, s) => !i || q[0] !== s[i - 1][0] || q[1] !== s[i - 1][1]); }   // round the tree's foot
    ctx.save(); ctx.strokeStyle = wp.hot ? T.pointerHotInk : T.pointerInk; ctx.fillStyle = ctx.strokeStyle; ctx.lineWidth = 1; ctx.beginPath();
    path.forEach((q, i) => i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1])); ctx.stroke();
    const end = path[path.length - 1];
    if (off) { const ang = Math.atan2(top[1] - end[1], top[0] - end[0]), k = 5;          // an arrowhead toward the stack out of view
      ctx.beginPath(); ctx.moveTo(end[0] + k * Math.cos(ang), end[1] + k * Math.sin(ang));
      ctx.lineTo(end[0] + k * Math.cos(ang + 2.5), end[1] + k * Math.sin(ang + 2.5)); ctx.lineTo(end[0] + k * Math.cos(ang - 2.5), end[1] + k * Math.sin(ang - 2.5)); ctx.fill(); }
    else { ctx.beginPath(); ctx.arc(end[0], end[1], 2, 0, 7); ctx.fill(); }
    ctx.restore();
    ptrOut.push({ id: wp.id, why: wp.why, drawn: true, to: off ? 'the window edge toward its stack' : 'its stack top', path });
  });
  ENGT.state = { box, bottomLimit: bot, capLimit: open ? null : lim, rows: rowsOut, folded: foldOut, hoveredRow: hovRow,
    fold: { n: open ? 0 : foldIds.length, words: open ? '' : foldWords, ids: open ? [] : foldIds, open,
      shown: open ? [off, off + shown.length, rows.length] : null, scroll: open ? ENGT.scroll : 0, maxScroll: ENGT.maxScroll || 0 }, rowSpot: !!spot, pointers: ptrOut,
    crossings: engTreeCrossings(ptrOut.filter(q => q.drawn).map(q => q.path)),
    head: headOut, need: { n: need.length, shown: need.filter(e => rowsOut.some(r => r.id === e.id)).map(e => e.id), cut: cutNeed }, list: LS,
    ms: +(performance.now() - t0).toFixed(3) };
}
// crossings between drawn pointers (counted, for the check in 11)
function engTreeCrossings(paths) {
  const segs = paths.map(p => p.slice(1).map((q, i) => [p[i], q]));
  const cross = (s, t) => { const [a, b] = s, [c, d] = t, hS = a[1] === b[1], hT = c[1] === d[1]; if (hS === hT) return false;
    const [hz, vt] = hS ? [s, t] : [t, s], y = hz[0][1], x = vt[0][0];
    const inn = (v, u, w) => v > Math.min(u, w) + 0.5 && v < Math.max(u, w) - 0.5; return inn(x, hz[0][0], hz[1][0]) && inn(y, vt[0][1], vt[1][1]); };
  let n = 0; for (let i = 0; i < segs.length; i++) for (let j = i + 1; j < segs.length; j++) segs[i].forEach(s => segs[j].forEach(t => { if (cross(s, t)) n++; }));
  return n;
}
// the tree's box as last drawn, [x0, y0, x1, y1], or null: for the marks that must keep out of it (edge marks for agents out of view)
function engTreeBox() { return ENGT.box ? ENGT.box.slice() : null; }
// driver: the rows in order (id, depth, level, mark kind, weight, right-hand piece, box), the folded lines, the pointers and why each
function engTreeState() { return ENGT.state; }
// the fold line's click (live.html's attnClick): opens the full tree, or closes it; it stays as it is until clicked again
function engTreeClick(h) {
  if (h && h.kind === 'treearch') { PG.archSetOpen(!PG.archOpen); return true; }          // "N archived": the pile opens (as a click on it), again closes
  if (h && h.kind === 'treefin') { if (ENGT.grp.has(h.parent)) ENGT.grp.delete(h.parent); else ENGT.grp.add(h.parent); return true; }          // (by the set, not the hit: a click can come with the last hover's hit)          // "N finished": the group opens in place, again folds; marks nothing read
  if (!h || h.kind !== 'treefold') return false;
  ENGT.open = !h.open; ENGT.scroll = 0;
  return true;
}
// the wheel over the open full tree scrolls it (whole rows); true when the point is inside it and it has rows to scroll
function engTreeWheel(x, y, dy) {
  const b = ENGT.box;
  if (!ENGT.open || !b || !(ENGT.maxScroll > 0) || x < b[0] || x > b[2] || y < b[1] || y > b[3]) return false;
  ENGT.scroll = Math.min(Math.max(0, (ENGT.scroll || 0) + dy), ENGT.maxScroll);
  return true;
}
// drivers' names: tree-fold (the fold line, closed), tree-close (the same line while the full tree is open)
function engTreeHitName(h) { return h && h.kind === 'treefold' ? (h.open ? 'tree-close' : 'tree-fold') : h && h.kind === 'treearch' ? 'tree-archived' : h && h.kind === 'treefin' ? 'tree-finished:' + h.parent : null; }
// what the list's window meets this frame: the top-right buttons, the made-up floor's test controls, and every shown page element laid
// over the canvas (windows, panels, the mini-map, the rail), by id or class; [] when it meets nothing
function engTreeOverlaps(box, br) {
  const meet = (q, n) => q && box[0] < q[2] && q[0] < box[2] && box[1] < q[3] && q[1] < box[3] ? n : null, out = [];
  if (br && br.boxes) Object.entries(br.boxes).forEach(([n, q]) => { if (meet(q, n)) out.push('button:' + n); });
  if (PG.tptBox && meet(PG.tptBox(), 1)) out.push('test-controls');
  [...document.body.children].forEach(el => { if (el === ENGT.btn || /^(CANVAS|SCRIPT|STYLE|LINK)$/.test(el.tagName) || el.id === 'appband') return;
    const cs = getComputedStyle(el); if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return;
    const r = el.getBoundingClientRect(); if (r.width < 1 || r.height < 1) return;
    if (meet([r.left, r.top, r.right, r.bottom], 1)) out.push(el.id || String(el.className || el.tagName).split(' ')[0]); });
  return out;
}
// drivers: __hits() name tree-toggle (the list's button: its top line open, the whole chip folded); __state().agentList: open, active
// (running or stalled), asking, text ("N active"), mark (the folded chip's waiting mark: glyph, n, where), box (the window), line (the
// button), saved (this browser's stored choice: '1', '0' or null). Wrapped last, once the page's own script and its own wrappers have
// run (they define both; the buttons' row is one of those wrappers)
addEventListener('load', () => setTimeout(() => {
  const s0 = window.__state, h0 = window.__hits;
  if (s0) window.__state = () => { const o = s0(), L = ENGT.state && ENGT.state.list; let saved = null; try { saved = localStorage.getItem(ENG_TREE.saveKey); } catch (e) {}
    o.agentList = L ? { open: L.open, active: L.active, asking: L.asking, text: L.text, mark: L.mark, box: L.box, corner: L.box ? L.box.slice(0, 2) : null, iconTop: engTreeTopbarY(), topAligned: !!L.box && L.box[1] === Math.round(engTreeTopbarY()), line: L.line, saved,
      focused: document.activeElement === ENGT.btn, overlaps: L.box ? engTreeOverlaps(L.box, o.buttons) : [] } : { open: !ENGT.shut, saved };
    return o; };
  if (h0) window.__hits = p => { const out = h0(p), b = ENGT.btn, n = 'tree-toggle';
    if (b && b.style.display !== 'none') { const r = b.getBoundingClientRect(), cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2), top = document.elementFromPoint(cx, cy);
      out.push(!p || n.startsWith(p) ? { name: n, kind: n, x: r.left, y: r.top, w: r.width, h: r.height, cx, cy, reachable: top === b } : { name: n, kind: n }); }
    return out; };
}, 0));
