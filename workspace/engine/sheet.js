/* The sheet of forms and the legend (plan section 10, step 3; P5): every condition across the range of forms, drawn by the same
   code the floor uses (engForm, engDrawForm, engDrawMark). Shown over the page with &sheet=forms on a made-up floor only. */
function engSheetDraw() {
  const { ctx, W, H, STRESS, qs, haloText } = PG;
  if (!STRESS.on || qs.get('sheet') !== 'forms') return;
  ctx.save(); ctx.fillStyle = '#f3f1ec'; ctx.fillRect(0, 0, W, H); ctx.restore();
  const plan = { done: 2, n: 5, cur: 'Write the fix', at: 2, steps: ['Read the failing test', 'Trace the call path', 'Write the fix', 'Add a regression test', 'Run the suite'].map((t, i) => ({ text: t, done: i < 2 })) };
  const base = { ctx: 48000, ctxLimit: 244800, ctxFrac: 0.2, plan, children: [], kidsRunning: 0, kidsBack: 0, seen: false, silentFor: 840, compactedAt: null,
    ask: 'Should the retry budget be per user or per tenant?', err: 'tool call failed: npm test exited with 1', say: 'Fixed the off-by-one in the cursor; the last page no longer repeats a row.',
    lastCall: { k: 'w', path: 'src/api/routes.ts' } };
  const rows = [
    ['asking (top level: call)', 'asking', 'call', {}], ['asking (past the first four: strong)', 'asking', 'strong', {}],
    ['failed, unseen', 'failed', 'strong', {}], ['failed, seen', 'failed', 'quiet', { seen: true }],
    ['finished, unseen', 'finished', 'strong', {}], ['finished, seen', 'finished', 'quiet', { seen: true }],
    ['stalled', 'stalled', 'strong', {}], ['running', 'running', 'plain', {}], ['running, context at 92%', 'running', 'plain', { ctx: 225000, ctxFrac: 0.92 }],
    ['running, a planner with children', 'running', 'plain', { children: ['x'], kidsRunning: 5, kidsBack: 3 }], ['idle', 'idle', 'quiet', { plan: null }],
  ];
  haloText('Forms and legend: each condition (rows) across the range of forms (columns), drawn by the floor\'s own code', 16, 18, 13, '#2c3440', 'left', 700);
  const cols = [0, 1, 2, 3, 4], x0 = 230, cw = [60, 230, 330, 360, 0], y0 = 50;
  let x = x0; cols.forEach((k, i) => { haloText(k + ' · ' + ENG_FORM.names[k], x, y0, 11, '#85837d', 'left', 600); x += cw[i]; });
  let y = y0 + 22;
  rows.forEach(([label, cond, level, o]) => {
    const f = Object.assign({}, base, o), r = { id: label, a: { full: 'Fix the flaky queue test', prov: 'codex', asks: [] }, facts: f, cond, level };
    r.say = engPieces(f, cond);
    haloText(label, 16, y + 8, 11, '#5b5f66', 'left', 500);
    let xx = x0, hmax = 16; cols.forEach((k, i) => { const sp = engForm(r, k); engDrawForm(sp, xx, y); hmax = Math.max(hmax, sp.h); xx += cw[i]; });
    y += hmax + 12;
  });
  y += 8; haloText('The key (one look per condition, the same on every surface)', 16, y, 12, '#2c3440', 'left', 700); y += 20;
  engLegendKey().forEach(e => { const r = { a: { prov: 'codex', asks: [] }, facts: { seen: false }, cond: e.cond, level: e.cond === 'failed' || e.cond === 'finished' ? 'strong' : ENG_LEGEND[e.cond].level };
    const lk = engLook(r); if (e.glyph) lk.glyph = e.glyph; engDrawMark(lk, 24, y, 6); haloText(e.key, 38, y, 11, '#2c3440', 'left', 500); y += 18; });
  haloText('A seen failure or finish keeps its sign, hollow and quiet; the harness\'s own mark stands for an agent simply working (faded when idle).', 16, y + 6, 11, '#85837d', 'left', 400);
}
