/* Map engine test controls (made-up floors only; nothing on a real floor). Origin: builder, from the brief engine-testctl.
   Buttons in the test strip's #tpTestEng and the same through the address, applied once the floor is up:
     stress=N                         number of agents (0, 1, 3, 8, 12, 20; buttons reload with the other keys kept)
     &eng=ask|fail|finish|stall|idle  the clicked agent (else &tagent=i) asks, fails, finishes, goes silent (stalled), or is long finished and seen
     &six=1                           six live agents ask at once: question, approval, options, question, approval, options
     &planner=1                       one running agent with eight children in four folders, three of them back (finished, not seen)
     &ctx=92                          the clicked agent (else &tagent) at that whole percent of its compaction point
     &place=one|spread                every agent's folder: all one, or one each in turn
     &move=1                          the clicked agent's next events, from now, are in another folder
     &run=12                          a minute of twelve agents running, replay on (implies stress=12&replay=1; reloads to it): reads and
                                      writes, messages arriving, plan steps ticking, and four changes of condition inside the minute (at
                                      +14 s one asks, +27 s one finishes, +40 s one fails, +52 s one stalls; listed in __engTest().applied)
     &pause=1                         the made-up world's clock stopped (PG.stressPause)
     &engsel=1                        (extra) also select the target agent, as a click would
   Everything changes the made-up world and the page reads it again; nothing leaves the page. window.__engTest() says what was applied. */
(() => {
  const TC = { applied: [], notes: [], addressDone: false, polls: 0 };
  window.__engTest = () => ({ applied: TC.applied.slice(), notes: TC.notes.slice(), addressDone: TC.addressDone,
    stress: window.PG ? { on: PG.STRESS.on, n: PG.STRESS.n, replay: PG.STRESS.replay, mix: PG.STRESS.mix, seed: PG.STRESS.seed, paused: PG.STRESS.pausedAt != null } : null });
  let rs = 12345; const rnd = () => { rs = (rs * 16807) % 2147483647; return rs / 2147483647; };
  const pick = a => a[Math.floor(rnd() * a.length)];
  const note = (t) => { TC.notes.push(t); try { PG.note(t); } catch (e) {} };
  const SAYS = ['Ran the suite: all green; nothing left on my list.', 'Fixed it and checked the edge cases; the change is on the branch.', 'Wrote the migration and its rollback; verified against the seed data.', 'The report is done; two follow-ups noted in the README.'];
  const ERRS = ['stream disconnected before completion', 'tool call failed: npm test exited with 1 after the time limit', 'the model returned an error: context window exceeded'];
  const CMDS = [['npm test', 'run the suite after the fix'], ['git push origin feature/cache', 'publish the branch for review'], ['npm run migrate -- --apply', 'apply the migration to the dev database']];
  const OPTS = [['Which timezone should the nightly report use?', ['UTC', 'Europe/London', 'Each tenant\'s own']], ['Should the retry budget be per user or per tenant?', ['Per user', 'Per tenant']]];
  const TALK = ['Reading the handlers to see where the session is made.', 'Ran the suite: two failures left, both in the queue tests.', 'The fix is in; checking the callers next.', 'Profiling the slow path; the sort dominates.', 'Wrote the test for the edge case first.'];
  const QS = ['Should the retry budget be per user or per tenant?', 'Can I drop the legacy v1 endpoints, or are they still used?', 'Is it fine to add a dependency on a date library?'];

  const W = () => PG.stressWorld(), now = () => PG.stressNow();
  const live = (w, n) => w.end == null || w.end > n;
  const targetW = () => {
    const a = PG.tptTarget(); if (a) { const w = W().agents.find(x => x.id === a.sid); if (w) return w; }
    const n = W().agents.length; return n ? W().agents[Math.max(0, Math.min(n - 1, +PG.qs.get('tagent') || 0))] : null;
  };
  const filesIn = d => W().files.filter(f => f.startsWith(d + '/'));
  const mkEv = (w, t, dir) => {
    const fs = filesIn(dir), k = rnd() < 0.3 ? 'write' : 'read';
    return { session: w.id, t, kind: k, path: fs.length ? pick(fs) : pick(W().files), text: k === 'write' ? 'File change' : 'Read', tokens: 1, lines: k === 'write' ? 1 + Math.floor(rnd() * rnd() * 80) : undefined };
  };
  // events of w between t0 and t1 (floor seconds), every 6 to 36 s, in dir (default its home)
  const fillEvs = (w, t0, t1, dir) => { for (let t = t0 + 6 + rnd() * 30; t < t1; t += 6 + rnd() * 30) w.evs.push(mkEv(w, t, dir || w.home)); w.evs.sort((p, q) => p.t - q.t); };
  const redir = (w, dir) => { w.evs.forEach(e => { const fs = filesIn(dir); if (fs.length) e.path = pick(fs); }); };
  const silence = (w, n) => { w.evs = w.evs.filter(e => e.t <= n); w.msgs = w.msgs.filter((m, i) => i === 0 || m.t <= n); };          // nothing after n
  const unseen = w => { w.seen = null; const a = PG.AG.find(x => x.sid === w.id); if (a) PG.ATT.setLooked(a.sid, 0); };
  const born = (w, n) => { if (w.start > n - 60) { w.start = n - 300; if (w.msgs[0]) w.msgs[0].t = w.start + 5; } };
  // the page re-reads the world: on its own every 1.5 s when replay is on; with replay off the canvas is taken afresh (that also clears the selection and refits the camera)
  let busy = Promise.resolve();
  let lastIds = [];          // the agents the last control changed: the page reads their history again (as tptReload does)
  const refresh = () => { const ids = lastIds; busy = busy.then(() => { ids.forEach(id => { const a = PG.AG.find(x => x.sid === id); if (a) PG.needHistory(a, true); }); }).then(() => PG.perform(() => PG.fetchCanvas(!PG.STRESS.replay))).then(() => PG.perform(PG.refresh)).then(() => PG.wake(1500)); return busy; };
  const log = (control, w, extra) => { lastIds = [].concat(w).filter(Boolean).map(x => x.id); TC.applied.push(Object.assign({ control, ids: lastIds, at: +now().toFixed(1) }, extra || {})); };
  const toAsk = (w, n, kind, i) => {
    w.end = null; w.st = 'waiting'; w.err = null; w.say = null; w.old = false; born(w, n); silence(w, n - 1); unseen(w);
    const at = n - 1 - (i || 0) * 0.01;
    if (kind === 'approval') { const c = CMDS[(i || 0) % CMDS.length]; w.ask = { kind, text: c[0], reason: c[1], at, n: w.i }; }
    else if (kind === 'options') { const o = OPTS[(i || 0) % OPTS.length]; w.ask = { kind: 'question', text: o[0], options: o[1], at, n: w.i }; }
    else w.ask = { kind: 'question', text: QS[(i || 0) % QS.length], at, n: w.i };
  };

  const ACT = {
    ask(w, n) { toAsk(w, n, 'question', 0); },
    fail(w, n) { born(w, n); w.ask = null; silence(w, n - 1); w.st = 'failed'; w.end = n - 1; w.err = pick(ERRS); w.say = null; w.old = false; unseen(w); },
    finish(w, n) { born(w, n); w.ask = null; silence(w, n - 1); w.st = 'finished'; w.end = n - 1; w.say = pick(SAYS); w.err = null; w.old = false; unseen(w); },
    stall(w, n) {
      const S = ENG_NAIVE.stalledAfter, back = S + 100;          // the engine calls it stalled after S seconds of silence
      w.end = null; w.st = 'quiet'; w.ask = null; w.err = null; w.say = null; w.old = false; unseen(w);
      if (w.start > n - back - 60) { w.start = n - back - 300; if (w.msgs[0]) w.msgs[0].t = w.start + 5; }
      silence(w, n);
      w.evs.forEach(e => { if (e.t > n - S) e.t = Math.max(w.start + 1, e.t - back); });          // move the recent ones back in time rather than wait
      w.msgs.forEach((m, i) => { if (i && m.t > n - S) m.t = Math.max(w.start + 1, m.t - back); });
      if (w.plan) w.plan.at = w.plan.at.map(x => x != null && x > n - S ? x - back : x);
      w.evs.forEach(e => { if (e.t > n - S - 5) e.t = n - S - 20; }); w.msgs.forEach((m, i) => { if (i && m.t > n - S - 5) m.t = n - S - 20; });          // none left inside the window
    },
    idle(w, n) {
      w.ask = null; w.err = null; w.old = false; w.st = 'finished'; w.end = Math.max(w.start + 30, n - 1500); silence(w, w.end); w.say = w.say || pick(SAYS);
      w.seen = Math.max(w.end + 5, n - 1200); const a = PG.AG.find(x => x.sid === w.id); if (a) PG.ATT.setLooked(a.sid, Date.now() / 1000);
    },
  };
  const doAct = kind => { const w = targetW(); if (!w) { note('no made-up agent to change'); return; } ACT[kind](w, now()); log(kind, w); refresh(); };

  function six() {
    const n = now(), ags = W().agents.filter(w => w.start <= n && !w.old), liveOnes = ags.filter(w => live(w, n) && w.st !== 'quiet'), rest = ags.filter(w => !liveOnes.includes(w));
    const pickd = liveOnes.concat(rest).slice(0, 6), kinds = ['question', 'approval', 'options', 'question', 'approval', 'options'];
    if (pickd.length < 6) note('six ask: the floor has only ' + pickd.length + ' agents that can; used what there is');
    pickd.forEach((w, i) => { if (!live(w, n)) note(w.id + ' had finished; it was made live again to ask'); toAsk(w, n, kinds[i], i); });
    log('six', pickd, { kinds: kinds.slice(0, pickd.length) }); refresh();
  }
  function planner() {
    const n = now(), all = W().agents.filter(w => w.start <= n && !w.old);
    const P = all.find(w => w.st === 'running' && !w.par) || all.find(w => !w.par); if (!P) { note('planner: no agent to be the planner'); return; }
    P.st = 'running'; P.end = null; P.ask = null; P.err = null; P.say = null; unseen(P);
    const kids = all.filter(w => w !== P && !w.par).concat(all.filter(w => w !== P && w.par)).slice(0, 8);
    if (kids.length < 8) note('planner: the floor has ' + all.length + ' agents; the planner has ' + kids.length + ' children, not eight');
    const dirs = ['src/api', 'src/ui', 'tests', 'docs'].filter(d => W().dirs.includes(d)), use = dirs.slice(0, Math.min(4, Math.max(1, kids.length)));
    if (kids.length < 4) note('planner: fewer than four children, so fewer than four folders');
    const t0 = Math.min(P.start, ...kids.map(k => k.start)); if (t0 < P.start) { P.start = t0 - 20; if (P.msgs[0]) P.msgs[0].t = P.start + 5; }
    const back = Math.min(3, kids.length);
    kids.forEach((k, i) => {
      k.par = P; k.home = use[i % use.length]; redir(k, k.home);
      if (i < back) { k.ask = null; k.err = null; k.st = 'finished'; k.end = Math.max(k.start + 40, n - 20 - i * 15); silence(k, k.end); k.say = pick(SAYS); k.old = false; unseen(k); }
      else { k.st = 'running'; k.end = null; k.ask = null; k.err = null; k.say = null; k.old = false; unseen(k); if (!k.evs.some(e => e.t > n - 120)) fillEvs(k, Math.max(k.start, n - 100), n + 600); }
    });
    if (!P.evs.some(e => e.t > n - 120)) fillEvs(P, Math.max(P.start, n - 100), n + 600);
    log('planner', [P].concat(kids), { planner: P.id, children: kids.map(k => k.id), back: kids.slice(0, back).map(k => k.id), folders: use }); refresh();
  }
  function ctxTo(pct) {
    const w = targetW(); if (!w) { note('no made-up agent to change'); return; }
    const n = now(), goal = w.thr * pct / 100 - w.msgs.filter(m => m.t <= n).reduce((a, m) => a + (m.tokens || 0), 0), before = w.evs.filter(e => e.t <= n);
    if (!before.length) { w.base = goal; }
    else { const each = Math.floor(goal / before.length); before.forEach(e => { e.tokens = each; }); before[before.length - 1].tokens += Math.round(goal - each * before.length); }          // the page's context is the events' tokens (as the made-up floor's own generator sets them)
    w.goal = goal; log('ctx', w, { pct }); refresh();
  }
  function place(how) {
    const dirs = W().dirs;
    W().agents.forEach((w, i) => { w.home = how === 'one' ? 'src/api' : dirs[i % dirs.length]; redir(w, w.home); });
    log('place', W().agents, { how }); refresh();
  }
  function move() {
    const w = targetW(); if (!w) { note('no made-up agent to change'); return; }
    const n = now(), dirs = W().dirs, to = dirs[(dirs.indexOf(w.home) + 4) % dirs.length];
    if (PG.STRESS.replay && live(w, n) && w.st !== 'quiet') {
      w.evs = w.evs.filter(e => e.t <= n);
      for (let k = 0; k < 40; k++) w.evs.push(mkEv(w, n + 2 + k * 8 + rnd() * 5, to));          // its next events, from now, are in the other folder
    } else {
      note('move: ' + (PG.STRESS.replay ? 'the agent is not running' : 'the floor is still (replay=0)') + ', so its latest events were put in the other folder instead of waiting for new ones');
      w.evs.filter(e => e.t <= n).slice(-6).forEach(e => { const fs = filesIn(to); if (fs.length) e.path = pick(fs); });
    }
    log('move', w, { to }); refresh();
  }
  function run(N) {
    const n = now();
    W().agents.forEach((w, i) => {
      w.st = 'running'; w.end = null; w.ask = null; w.err = null; w.say = null; w.old = false; w.par = w.par && w.par.start <= n ? w.par : null; unseen(w);
      if (w.start < 0 || w.start > n - 120) { w.start = 200 + i * 40; if (w.msgs[0]) w.msgs[0].t = w.start + 5; w.evs = []; }
      w.evs = w.evs.filter(e => e.t <= n); w.msgs = w.msgs.filter((m, i2) => i2 === 0 || m.t <= n);
      const last = Math.max(w.start, ...w.evs.map(e => e.t)); if (last < n - 60) fillEvs(w, Math.max(w.start, n - 80), n);
      fillEvs(w, n, n + 400);          // enough for the minute and after (reads and writes, as the generator draws them)
      const b = w.evs.filter(e => e.t <= n); if (b.length && b.every(e => e.tokens <= 1)) b.forEach(e => { e.tokens = Math.floor(w.goal / b.length); });
      w.evs.filter(e => e.t > n).forEach(e => { e.tokens = 300 + Math.floor(rnd() * 900); });          // context grows over the minute
      // a plan whose steps tick over the minute: two done before, one or two done inside it, the rest after
      const steps = typeof STRESS_STEPS !== 'undefined' ? STRESS_STEPS : ['Read the code', 'Write the fix', 'Add a test', 'Run the suite', 'Open the review'];
      const d1 = n + 5 + ((i * 13) % 50), d2 = i % 2 ? n + 8 + ((i * 29) % 48) : null;
      w.plan = { shape: rnd() < 0.6 ? 'codex' : 'claude', steps: [0, 1, 2, 3, 4].map(() => pick(steps)), at: [n - 400 + i, n - 150 + i, Math.min(d1, d2 || d1), d2 ? Math.max(d1, d2) : null, null] };
      // messages arriving through the minute
      for (let t = n + 3 + rnd() * 10; t < n + 400; t += 9 + rnd() * 18) w.msgs.push({ role: 'assistant', text: pick(TALK), t, tokens: 60 + Math.floor(rnd() * 200) });
      w.msgs.sort((p, q) => p.t - q.t);
    });
    // two to four changes of condition at spread moments inside the minute (lead, round 4): one asks, one finishes, one fails, one stalls
    const A = W().agents, at = k => A[Math.min(A.length - 1, k)], sched = [];
    const cut = (w, t) => { w.evs = w.evs.filter(e => e.t <= t); w.msgs = w.msgs.filter((m, i) => i === 0 || m.t <= t); if (w.plan) w.plan.at = w.plan.at.map(x => x != null && x > t ? null : x); };
    if (A.length >= 2) { const w = at(2), t = n + 14; cut(w, t); w.ask = { kind: 'question', text: QS[1], at: t, n: w.i }; sched.push({ id: w.id, title: w.title, change: 'asks', at: t }); }
    if (A.length >= 4) { const w = at(5), t = n + 27; cut(w, t); w.end = t; w.st = 'finished'; w.say = pick(SAYS); if (w.plan) w.plan.at = w.plan.at.map((x, j) => x != null && x <= t ? x : t - 1 + j * 0.01); sched.push({ id: w.id, title: w.title, change: 'finishes', at: t }); }
    if (A.length >= 6) { const w = at(8), t = n + 40; cut(w, t); w.end = t; w.st = 'failed'; w.err = pick(ERRS); sched.push({ id: w.id, title: w.title, change: 'fails', at: t }); }
    if (A.length >= 8) { const w = at(10), S = ENG_NAIVE.stalledAfter, t = n + 52, last = t - S;          // silent from t - S: the engine calls it stalled at t
      cut(w, last); if (w.plan) w.plan.at = w.plan.at.map(x => x != null && x > last ? null : x); sched.push({ id: w.id, title: w.title, change: 'stalls', at: t }); }
    log('run', W().agents, { n: N, from: +n.toFixed(1), changes: sched }); refresh();
  }
  const pauseLabel = () => { const l = document.getElementById('engPauseL'); if (l) l.textContent = PG.STRESS.pausedAt != null ? 'paused: resume' : 'running: pause'; };
  const pause = on => { PG.stressPause(on); log('pause', [], { on }); pauseLabel(); };

  // ---- the address, once the floor is up
  const KEYS = ['engsel', 'eng', 'six', 'planner', 'ctx', 'place', 'move', 'run', 'pause'];
  async function fromAddress() {
    const q = PG.qs; TC.addressDone = true;
    if (q.has('run')) run(+q.get('run') || 12);
    if (q.has('place')) place(q.get('place') === 'one' ? 'one' : 'spread');
    if (q.get('planner') === '1') planner();
    if (q.get('six') === '1') six();
    if (q.has('ctx')) ctxTo(Math.max(0, Math.min(500, Math.round(+q.get('ctx') || 0))));
    if (q.get('move') === '1') move();
    const e = q.get('eng'); if (e && ACT[e]) doAct(e); else if (e) note('eng=' + e + ': not one of ask, fail, finish, stall, idle');
    await busy;
    if (q.get('engsel') === '1') { const a = PG.tptTarget(); if (a) { PG.focusAgent(a.id); TC.applied.push({ control: 'engsel', ids: [a.sid], at: +now().toFixed(1) }); } }
    if (q.get('pause') === '1') pause(true);
  }

  // ---- the buttons
  function reloadWith(set) {
    const u = new URL(location.href); Object.keys(set).forEach(k => set[k] == null ? u.searchParams.delete(k) : u.searchParams.set(k, set[k]));
    location.href = u.toString();
  }
  function build(box) {
    const b = (e, v, label) => `<button data-e="${e}"${v != null ? ` data-ev="${v}"` : ''}>${label}</button>`;
    box.innerHTML =
      '<div class="r"><b>engine</b> agents ' + [0, 1, 3, 8, 12, 20].map(n => b('n', n, n)).join('') + '</div>' +
      '<div class="r">the clicked agent ' + ['ask', 'fail', 'finish', 'stall', 'idle'].map(k => b('eng', k, k)).join('') + b('ctx', 92, 'at 92% context') + b('move', 1, 'moves folder') + '</div>' +
      '<div class="r">' + b('six', 1, 'six ask at once') + b('planner', 1, 'planner, eight children') + b('place', 'one', 'all in one folder') + b('place', 'spread', 'spread') + b('run', 12, 'one minute of twelve running') + b('pause', 1, '<span id="engPauseL"></span>') + '</div>';
    pauseLabel();
    box.addEventListener('click', ev => {
      const bt = ev.target.closest('button'); if (!bt || !bt.dataset.e) return; ev.stopPropagation(); bt.blur();          // the strip's own handler acts only on data-x
      const e = bt.dataset.e, v = bt.dataset.ev;
      if (e === 'n') reloadWith({ stress: v });
      else if (e === 'eng') doAct(v);
      else if (e === 'ctx') ctxTo(+v);
      else if (e === 'move') move();
      else if (e === 'six') six();
      else if (e === 'planner') planner();
      else if (e === 'place') place(v);
      else if (e === 'run') reloadWith({ run: 12, stress: 12, replay: 1 });
      else if (e === 'pause') pause(PG.STRESS.pausedAt == null);
    });
  }

  // ---- wait for the page (this file loads before the page's own script), then the floor
  const timer = setInterval(() => {
    if (++TC.polls > 600) { clearInterval(timer); return; }
    if (!window.PG) return;
    if (PG.qs.has('run') && !(PG.STRESS.on && PG.STRESS.replay)) { clearInterval(timer); reloadWith({ stress: PG.qs.get('stress') || '12', replay: '1' }); return; }          // &run=12 implies stress=12&replay=1
    if (!PG.STRESS.on) { clearInterval(timer); return; }          // a real floor: nothing here, ever
    const box = document.getElementById('tpTestEng'); if (!box) return;
    if (!box.dataset.built) { build(box); box.dataset.built = '1'; }
    if (PG.STRESS.n === 0) { clearInterval(timer); TC.addressDone = true; return; }          // no agents: nothing to apply the address to
    if (!PG.M || !PG.AG.length) return;
    clearInterval(timer);
    const q = PG.qs;
    if (KEYS.some(k => q.has(k))) fromAddress(); else TC.addressDone = true;
  }, 100);
})();
