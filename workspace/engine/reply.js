/* The reply box: one view of an agent's conversation (integration brief B, 2026-11-16). Origin: builder B, from brief B.
   The transcript and the chat box had come to do the same job, and a conversation should be one click away. So the transcript panel (live.html, region "transcript panel") gains
   the box and the chat window and the chat on the ground go. This file holds what stands apart from the page:
   - ENG_REPLY: every number of the box (naive; none of them a hard invariant);
   - the half-typed replies, one per agent (and one per folder for a new agent), kept in memory and in the browser's storage;
   - the plain words for a message's delivery state;
   - the box itself (a textarea, Enter sends, Shift+Enter a new line, / or $ or Cmd/Ctrl+K the command list), with a quiet line
     under it whose summary and settings the page fills;
   - what the made-up floor (?stress=) answers for the box: starting an agent, stopping one, the model and mode lists, and a short
     answer from a live made-up agent, so you can try every path without the server;
   - the box's test controls and address cases (made-up floors only).
   The page wires it (live.html): engReplyBox() is built into the transcript panel, PG gives it the page's send, answer and start.
   Reads the page through PG only. */
const ENG_REPLY = {
  // every entry: naive, builder's (chosen while building; to be replaced by looking and measuring)
  boxLines: 6,            // (naive, builder's) the box grows with what is typed up to this many lines, then scrolls inside
  lineH: 17,              // (naive, builder's) px: one line of the box (12.5 px type)
  draftMax: 4000,         // (naive, builder's) characters of a half-typed reply kept per agent (longer: the start is kept)
  draftKey: 'ide.drafts', // (builder's) the browser storage key, one object per project
  gapLine: 10 * 60,       // (naive, builder's) s: a pause this long between two written transcript entries prints "25 min later" between them
  echoAfter: 2.5,         // (naive, builder's) s: on a made-up floor a live agent answers your message this long after it
  menuRows: 8,            // (naive, builder's) rows of the command list in view before it scrolls
};

/* ---------- half-typed replies: key = the agent's session id, or 'new:' + a folder's path for a new agent ---------- */
const ENG_DRAFTS = { mem: new Map(), proj: null };
function engDraftProj() { const S = PG.STRESS; return S && S.on ? 'stress:' + S.seed + ':' + S.n : (PG.LIVE && PG.LIVE.cwd) || ''; }
function engDraftLoad() {
  const p = engDraftProj(); if (ENG_DRAFTS.proj === p) return;
  ENG_DRAFTS.proj = p; ENG_DRAFTS.mem = new Map();
  try { const all = JSON.parse(localStorage.getItem(ENG_REPLY.draftKey) || '{}'); Object.entries(all[p] || {}).forEach(([k, v]) => { if (typeof v === 'string' && v) ENG_DRAFTS.mem.set(k, v); }); } catch (e) {}
}
function engDraftSave() {
  try {
    const all = JSON.parse(localStorage.getItem(ENG_REPLY.draftKey) || '{}');
    if (ENG_DRAFTS.mem.size) all[ENG_DRAFTS.proj] = Object.fromEntries(ENG_DRAFTS.mem); else delete all[ENG_DRAFTS.proj];
    localStorage.setItem(ENG_REPLY.draftKey, JSON.stringify(all));
  } catch (e) {}
}
function engDraft(key) { engDraftLoad(); return ENG_DRAFTS.mem.get(key) || ''; }
function engDraftSet(key, text) {
  engDraftLoad(); if (!key) return;
  const v = String(text || '').slice(0, ENG_REPLY.draftMax);
  if (v.trim()) ENG_DRAFTS.mem.set(key, v); else ENG_DRAFTS.mem.delete(key);
  engDraftSave();
}
const engDraftKeys = () => { engDraftLoad(); return [...ENG_DRAFTS.mem.keys()]; };

/* ---------- a message's delivery state, in plain words (the page keeps it as the turn's note: builder A's "your messages") ---------- */
function engSendWord(note) {
  if (note === 'sending') return { word: 'sending…', tone: 'wait' };
  if (note === 'delivered' || note === 'sent') return { word: 'delivered', tone: 'ok' };
  if (note === 'steered') return { word: 'delivered while it worked', tone: 'ok' };
  if (note === 'queued') return { word: 'queued: it takes it when its current step ends', tone: 'wait' };
  if (note === 'not sent') return { word: 'not sent', tone: 'bad' };
  return note ? { word: note, tone: '' } : null;
}

/* ---------- a pause between two entries, in words ("25 min later", "next day · Nov 15 09:12") ---------- */
function engGapWords(from, to, origin) {
  const d0 = new Date((origin + from) * 1000), d1 = new Date((origin + to) * 1000), p2 = n => String(n).padStart(2, '0');
  const days = Math.round((new Date(d1.getFullYear(), d1.getMonth(), d1.getDate()) - new Date(d0.getFullYear(), d0.getMonth(), d0.getDate())) / 864e5);
  const at = d1.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + p2(d1.getHours()) + ':' + p2(d1.getMinutes());
  if (days === 1) return 'next day · ' + at;
  if (days > 1) return days + ' days later · ' + at;
  const m = Math.max(1, Math.round((to - from) / 60)); if (m < 60) return m + ' min later';
  return Math.floor(m / 60) + ' h' + (m % 60 ? ' ' + (m % 60) + ' min' : '') + ' later';
}

/* ---------- the box ----------
   h: the page's handlers, all optional:
     send(text)        Enter (or the send button) with something typed; the page decides what it is (a message, an answer, a command)
     escape(text)      Escape with no command list open; the page decides (text kept: leave the box; empty: close the transcript)
     change(text)      every edit (the page keeps the draft)
     items(filter)     the command list for what is typed after / or $ (null: no list here, e.g. a new agent or a watch-only one)
     pick(item)        an item chosen from the list
     chipClear()       Backspace in an empty box while a command is chosen
   Returns { el, ta, go, held, line, sum, more, choice, menu, get, set, focus, grow, setChip, openMenu, menuOpen, closeMenu }. */
let ENG_RB_CSS = false;
function engReplyCss(look) {
  if (ENG_RB_CSS) return; ENG_RB_CSS = true;
  const C = (look || PG).C, F = (look || PG).FONT, st = document.createElement('style');
  st.textContent = `.rb{position:relative;padding:6px 9px 6px;border-bottom:1px solid #e7e1d6;font:12px/1.4 ${F};color:${C.ink}}
  .rb .rbheld{font-size:11px;color:${C.muted};padding:0 1px 4px}.rb .rbheld:empty{display:none}.rb .rbheld b{color:${C.ink};font-weight:600}
  .rb .rbrow{display:flex;align-items:flex-end;gap:5px;border:1px solid #d3ccbf;border-radius:6px;background:#fff;padding:3px 4px 3px 7px}
  .rb .rbrow:focus-within{border-color:#9d978b}
  .rb .rbchip{display:none;flex:0 0 auto;align-self:center;font-size:11px;font-weight:600;color:#2f5597;background:#eef2f9;border-radius:4px;padding:0 5px}
  .rb .rbchip.skill{color:#7a4fa0;background:#f4eef9}
  .rb textarea{flex:1;min-width:0;resize:none;border:0;outline:none;background:transparent;font:12.5px/${ENG_REPLY.lineH}px ${F};color:${C.ink};padding:1px 0;margin:0;height:${ENG_REPLY.lineH}px;overflow-y:hidden}
  .rb textarea::placeholder{color:#a9a39a}
  .rb textarea:disabled{color:${C.muted}}
  .rb textarea.secret{-webkit-text-security:disc;text-security:disc}
  .rb .rbgo{flex:0 0 auto;font:inherit;font-size:11px;line-height:16px;border:1px solid #d3ccbf;background:#fff;color:${C.muted};border-radius:4px;padding:0 7px;cursor:pointer}
  .rb .rbgo.on{background:${C.ink};border-color:${C.ink};color:#fff}
  .rb .rbgo:disabled{display:none}
  .rb .rbline{display:flex;align-items:baseline;gap:6px;margin-top:4px;font-size:10.5px;color:${C.muted};min-height:14px}
  .rb .rbsum{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .rb .rbline a{color:${C.muted};text-decoration:underline;text-decoration-color:#cfc8bb;cursor:pointer}
  .rb .rbline a:hover{color:${C.ink}}
  .rb .rbline button{font:inherit;font-size:10.5px;line-height:14px;border:1px solid #e3b3a5;background:#fff;color:#c2402a;border-radius:4px;padding:0 6px;cursor:pointer}
  .rb .rbmore{display:none;flex-wrap:wrap;gap:4px;margin-top:4px}.rb .rbmore.on{display:flex}
  .rb .rbmore select{font:inherit;font-size:11px;height:21px;border:1px solid #d8d1c4;border-radius:5px;background:#fbfaf7;color:${C.ink};padding:0 2px;min-width:0;flex:1 1 0}
  .rb .rbmore label{display:flex;align-items:center;gap:3px;font-size:11px;height:21px;border:1px solid #d8d1c4;border-radius:5px;background:#fbfaf7;padding:0 6px 0 3px;white-space:nowrap}
  .rb .rbchoice:empty{display:none}
  .rb .rbmenu{display:none;position:absolute;left:9px;right:9px;z-index:3;max-height:${ENG_REPLY.menuRows * 24}px;overflow-y:auto;background:#fffdf9;border:1px solid #d8d2c6;border-radius:6px;box-shadow:0 4px 14px rgba(40,40,52,.14)}
  .rb .rbmenu.on{display:block}
  .rb .rbmenu .h{font-size:10px;color:${C.muted};padding:4px 8px 1px;text-transform:lowercase}
  .rb .rbmenu .i{display:flex;gap:6px;align-items:baseline;padding:3px 8px;cursor:pointer;font-size:11.5px}
  .rb .rbmenu .i b{font-weight:600;white-space:nowrap}.rb .rbmenu .i span{color:${C.muted};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  .rb .rbmenu .i.sel,.rb .rbmenu .i:hover{background:#f3efe6}
  .rb .rbmenu .n{padding:5px 8px;color:${C.muted};font-size:11px}`;
  document.head.appendChild(st);
}
function engReplyBox(h, look) {          // look: { C, FONT } when the page builds the box before PG exists
  engReplyCss(look); h = h || {};
  const el = document.createElement('div'); el.className = 'rb';
  el.innerHTML = '<div class="rbheld"></div><div class="rbrow"><span class="rbchip"></span><textarea rows="1" autocomplete="off" spellcheck="false" aria-label="reply" data-reply=""></textarea>'
    + '<button type="button" class="rbgo">send</button></div><div class="rbmenu"></div><div class="rbchoice"></div>'
    + '<div class="rbline"><span class="rbsum"></span></div><div class="rbmore"></div>';
  const q = s => el.querySelector(s), ta = q('textarea'), go = q('.rbgo'), menu = q('.rbmenu'), line = q('.rbline'), chip = q('.rbchip');
  const B = { el, ta, go, chip, held: q('.rbheld'), line, sum: q('.rbsum'), more: q('.rbmore'), choice: q('.rbchoice'), menu, items: null, sel: 0, filter: null };
  const grow = () => {          // the box as tall as what is typed, up to boxLines, then it scrolls inside
    ta.style.height = ENG_REPLY.lineH + 'px';
    const want = Math.min(ta.scrollHeight, ENG_REPLY.lineH * ENG_REPLY.boxLines);
    ta.style.height = want + 'px'; ta.style.overflowY = ta.scrollHeight > want + 1 ? 'auto' : 'hidden';
    go.classList.toggle('on', !!ta.value.trim() || chip.style.display === 'inline-block');
  };
  B.get = () => ta.value;
  B.set = v => { if (ta.value !== v) { ta.value = v || ''; grow(); } };
  B.focus = (end) => { ta.focus({ preventScroll: true }); if (end !== false) { const n = ta.value.length; ta.setSelectionRange(n, n); } };
  B.grow = grow;
  // robust (2026-11-21): a question marked secret: the box masks what is typed, and what it held before (a draft) is set aside and back
  B.isSecret = false;
  B.secret = on => { on = !!on; if (B.isSecret === on) return; B.isSecret = on; ta.classList.toggle('secret', on); ta.setAttribute('autocomplete', on ? 'new-password' : 'off');
    if (on) { B.before = ta.value; ta.value = ''; } else { ta.value = B.before || ''; B.before = ''; } grow(); };
  B.menuOpen = () => !!B.items;
  B.closeMenu = () => { B.items = null; B.filter = null; menu.classList.remove('on'); menu.innerHTML = ''; };
  B.renderMenu = () => {
    const items = B.items; if (!items) { B.closeMenu(); return; }
    B.sel = Math.max(0, Math.min(B.sel, items.length - 1));
    let html = '', last = null;
    items.forEach((it, i) => {
      if (it.kind !== last) { html += `<div class="h">${it.kind === 'skill' ? 'skills' : 'commands'}</div>`; last = it.kind; }
      html += `<div class="i${i === B.sel ? ' sel' : ''}" data-i="${i}"><b>${engEsc(it.label)}</b><span>${engEsc((it.param ? '<' + it.param + '> · ' : '') + (it.desc || ''))}</span></div>`;
    });
    menu.innerHTML = html || `<div class="n">no command or skill matches ${engEsc(B.filter || '')}</div>`;
    menu.style.top = (q('.rbrow').offsetTop + q('.rbrow').offsetHeight + 2) + 'px';
    menu.classList.add('on');
    const s = menu.querySelector('.i.sel'); if (s) s.scrollIntoView({ block: 'nearest' });
  };
  B.openMenu = f => { if (!h.items) return false; const items = h.items(f || ''); if (!items) return false; if (B.filter !== (f || '')) B.sel = 0; B.filter = f || ''; B.items = items; B.renderMenu(); return true; };
  B.setChip = (label, skill) => { chip.textContent = label || ''; chip.className = 'rbchip' + (skill ? ' skill' : ''); chip.style.display = label ? 'inline-block' : 'none'; grow(); };
  const pick = i => { const it = B.items && B.items[i]; B.closeMenu(); if (it && h.pick) h.pick(it); grow(); };
  ta.addEventListener('input', () => {
    grow(); if (h.change) h.change(ta.value);
    const v = ta.value;
    if (/^[\/$][^\s]*$/.test(v)) B.openMenu(v); else if (B.items && B.filter) B.closeMenu();
  });
  ta.addEventListener('keydown', ev => {
    const mod = ev.metaKey || ev.ctrlKey || ev.altKey;
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'k') { ev.preventDefault(); ev.stopPropagation(); if (B.items) B.closeMenu(); else B.openMenu(''); return; }
    if (mod) return;          // a key held with Command, Control or Option reaches the page's key table (the planner's ruling); every plain key belongs to the box
    ev.stopPropagation();          // plain keys are typing: the page's single-letter keys never fire from the box
    if (B.items) {
      const n = B.items.length;
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); if (n) { B.sel = (B.sel + (ev.key === 'ArrowDown' ? 1 : -1) + n) % n; B.renderMenu(); } return; }
      if ((ev.key === 'Enter' && !ev.shiftKey) || ev.key === 'Tab') { ev.preventDefault(); if (n) pick(B.sel); return; }
      if (ev.key === 'Escape') { ev.preventDefault(); B.closeMenu(); return; }
    }
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); if (h.send) h.send(ta.value); grow(); return; }
    if (ev.key === 'Escape') { ev.preventDefault(); if (h.escape) h.escape(ta.value); return; }
    if (ev.key === 'Backspace' && !ta.value && chip.style.display === 'inline-block') { ev.preventDefault(); if (h.chipClear) h.chipClear(); B.setChip(null); }
  });
  menu.addEventListener('mousedown', ev => ev.preventDefault());          // the box keeps the cursor
  menu.addEventListener('click', ev => { const r = ev.target.closest('[data-i]'); if (r) pick(+r.dataset.i); });
  go.addEventListener('mousedown', ev => ev.preventDefault());
  go.addEventListener('click', () => { if (h.send) h.send(ta.value); grow(); });
  el.addEventListener('wheel', ev => { if (ev.ctrlKey) ev.preventDefault(); }, { passive: false });
  return B;
}
const engEsc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

/* ---------- the quiet line under the box: what a new agent will be started with, or a running agent's settings, in one line ----------
   v: { harness: 'Codex' | 'Claude Code', model, effort, mode, plan (true/false/null), planWord ('checklist' / 'plan mode'), planMode (Codex's
   Plan mode on), watch (string: why nothing can be sent), changed }
   Returns the line's words, e.g. "Codex · gpt-6-luna · medium · Default · plan on". */
function engSettingsWords(v) {
  if (!v) return '';
  if (v.watch) return v.watch;
  return [v.harness, v.model || 'default model', v.effort || null, v.mode || null, v.plan == null ? null : (v.planWord || 'plan') + ' ' + (v.plan ? 'on' : 'off'),
    v.planMode ? 'Plan mode' : null].filter(Boolean).join(' · ') + (v.changed ? ' (changed: goes with your next message)' : '');          // questions (2026-11-21): the checkbox's own word; Codex's Plan mode when on
}

/* ---------- the made-up floor's answers for the box (nothing leaves the page) ----------
   engStressRead(path): the reads the box makes (models, modes, a session's settings, /status); undefined: not the box's.
   engStressWrite(path, data): start, stop, rename, a send to an agent held elsewhere or watch-only, and a live made-up agent's
   short answer after a send; undefined: the page's own made-up writes (reply, send) handle it.
   engStressState(state): the made-up sessions as the test controls left them (watch only, held in the app). */
const ENG_RB_MODELS = { codex: [
    { id: 'gpt-6-luna', isDefault: true, description: 'made-up floor', defaultReasoningEffort: 'medium', supportedReasoningEfforts: ['low', 'medium', 'high'].map(e => ({ reasoningEffort: e, description: '' })) },
    { id: 'gpt-6-sol', description: 'made-up floor', defaultReasoningEffort: 'high', supportedReasoningEfforts: ['medium', 'high', 'xhigh'].map(e => ({ reasoningEffort: e, description: '' })) }],
  claude: [{ id: 'claude-sonnet', isDefault: true, description: 'made-up floor', defaultReasoningEffort: 'medium', supportedReasoningEfforts: ['low', 'medium', 'high'].map(e => ({ reasoningEffort: e, description: '' })) }] };
const ENG_RB_PRESETS = { codex: [
    { id: 'read-only', label: 'Read Only', approvalPolicy: 'on-request', sandbox: 'read-only', description: 'reads only; asks to edit (made-up floor)' },
    { id: 'default', label: 'Default', approvalPolicy: 'on-request', sandbox: 'workspace-write', default: true, description: 'edits the workspace; asks for the rest (made-up floor)' },
    { id: 'full-access', label: 'Full Access', approvalPolicy: 'never', sandbox: 'danger-full-access', description: 'asks nothing (made-up floor)' }],
  claude: [{ id: 'cc-default', label: 'Ask first', approvalPolicy: 'default', sandbox: null, default: true, description: 'asks before edits and commands (made-up floor)' },
    { id: 'cc-acceptEdits', label: 'Accept edits', approvalPolicy: 'acceptEdits', sandbox: null, description: 'edits go ahead (made-up floor)' }] };
const ENG_RB_CMDS = [{ name: 'status', description: 'show the session\'s settings and context' }, { name: 'diff', description: 'show git diff' },
  { name: 'compact', description: 'summarize conversation to prevent hitting the context limit' }, { name: 'rename', description: 'rename the current thread', param: 'new name' },
  { name: 'archive', description: 'archive this session' }, { name: 'model', description: 'choose what model and reasoning effort to use' }];
function engStressAgent(id) { return PG.stressWorld().agents.find(x => x.id === id) || null; }
function engStressRead(path) {
  if (!PG.STRESS || !PG.STRESS.on) return undefined;
  const p = path.split('?')[0], qs = new URLSearchParams(path.split('?')[1] || ''), w = qs.get('id') ? engStressAgent(qs.get('id')) : null;
  if (p === 'models') return { data: ENG_RB_MODELS.codex };
  if (p === 'controls') return { presets: ENG_RB_PRESETS.codex, commands: ENG_RB_CMDS, skills: [{ name: 'tidy', description: 'a made-up skill', path: '/stress/skills/tidy' }], config: {} };
  if (p === 'claude/state') return { ready: true, cli: 'the made-up floor' };
  if (p === 'claude/models') return { data: ENG_RB_MODELS.claude };
  if (p === 'claude/controls') return { presets: ENG_RB_PRESETS.claude, commands: ENG_RB_CMDS, skills: [], config: {} };
  if (p === 'settings' && w) {
    const cl = w.prov === 'claude', s = w.settings || {};
    return { model: s.model || (cl ? 'claude-sonnet' : 'gpt-6-luna'), effort: s.effort || 'medium', approvalPolicy: s.approvalPolicy || (cl ? 'default' : 'on-request'), sandbox: cl ? null : s.sandbox || 'workspace-write',
      permissionMode: cl ? s.approvalPolicy || 'default' : undefined, plan: s.plan != null ? s.plan : !cl, source: 'the made-up floor' };
  }
  if (p === 'status' && w) {
    const now = PG.stressNow(), tokens = w.base + w.evs.filter(e => e.t <= now).reduce((m, e) => m + e.tokens, 0);
    return { settings: engStressRead('settings?id=' + encodeURIComponent(w.id)), context: { tokens, window: w.window, source: 'the made-up floor' }, running: w.end == null,
      rateLimits: {}, webSearch: { on: false, source: 'the made-up floor' }, mcp: { servers: [], threadScoped: false } };
  }
  if (p === 'mcp') return { servers: [], threadScoped: false };
  return undefined;
}
function engStressGoOn(w) { setTimeout(() => { const a = PG.AG.find(x => x.sid === w.id); if (a) PG.needHistory(a, true); PG.perform(() => PG.fetchCanvas(false)); PG.perform(PG.refresh); PG.wake(1200); }, 0); }
// a new made-up agent, as /api/start makes a session: nothing said yet; the first send gives it its title and its work
function engStressStart(data, from) {
  const W0 = PG.stressWorld(), now = PG.stressNow(), cl = (data && data.harness) === 'claude' || (from && from.prov === 'claude');
  const i = W0.agents.length, id = 'stress-' + String(i).padStart(3, '0') + '-new' + Date.now().toString(16).slice(-5);
  const thr = cl ? 160000 : 244800, w = { id, i, st: 'running', role: 'started here', prov: cl ? 'claude' : 'codex', par: null, start: now - 1, end: null, dur: 0,
    home: from ? from.home : W0.dirs[0], window: cl ? 200000 : 272000, thr, goal: thr * 0.05, base: 9000, evs: [], msgs: from ? from.msgs.filter(m => m.t <= now).map(m => Object.assign({}, m)) : [],
    ask: null, plan: null, seen: null, old: false, title: from ? from.title + ' (copy)' : 'New agent', say: null, err: null, fresh: true,
    settings: { model: data && data.model, effort: data && data.effort, approvalPolicy: data && data.approvalPolicy, sandbox: data && data.sandbox, plan: data && data.plan } };
  W0.agents.push(w); engStressGoOn(w);
  return w;
}
function engStressWrite(path, data) {
  if (!PG.STRESS || !PG.STRESS.on) return undefined;
  const p = path.split('?')[0], now = PG.stressNow(), w = data && data.threadId ? engStressAgent(data.threadId) : null;
  if (p === 'start') { const n = engStressStart(data); return { thread: { id: n.id } }; }
  if (p === 'stop') { if (!w) throw new Error('no such made-up agent'); if (w.end == null) { w.end = now; w.st = 'finished'; w.stopped = true; (w.stops = w.stops || []).push(now); w.msgs.push({ role: 'assistant', text: 'Stopped: you interrupted the turn.', t: now, tokens: 10 }); } engStressGoOn(w); return { ok: true }; }
  if (p === 'rename') { if (!w) throw new Error('no such made-up agent'); w.title = String(data.name || w.title); engStressGoOn(w); return { ok: true }; }
  if (p === 'send' && w) {
    if (w.watch) throw new Error('watch only: this made-up session cannot be written to');
    if (w.held && !data.fork) return { how: 'choice', heldBy: w.held, model: w.prov === 'claude' ? 'claude-sonnet' : 'gpt-6-luna', desktopUrl: null };
    if (w.held && data.fork) {          // a copy with your message; the original stays where it is open
      const n = engStressStart({ harness: w.prov }, w); n.held = null;
      n.msgs.push({ role: 'user', text: String(data.text || ''), t: now, tokens: 20 }); engStressEcho(n, data.text);
      return { ok: true, how: 'forked', threadId: n.id };
    }
    if (w.fresh) {          // a new agent's first message: its title, and the folder it was told to work in
      w.fresh = false; const m = /^Work in the folder (\S+?)\/ of this project\. ([\s\S]*)$/.exec(String(data.text || ''));
      if (m && PG.stressWorld().dirs.includes(m[1])) w.home = m[1];
      const t = (m ? m[2] : String(data.text || '')).replace(/\s+/g, ' ').trim(); w.title = t.length > 46 ? t.slice(0, 45) + '…' : t || 'New agent';
    }
    w.msgs.push({ role: 'user', text: String(data.text || ''), t: now, tokens: 20 }); w.msgs.sort((x, y) => x.t - y.t);
    if (w.stopped) { w.stopped = false; w.end = null; w.st = 'running'; }          // a stopped one runs again on your message, as a real one does
    if (w.end == null || w.end > now) engStressEcho(w, data.text);
    engStressGoOn(w);
    return { ok: true, how: 'sent' };          // the page says steered when the agent was working (as it does for a real Codex send)
  }
  if (['compact', 'review', 'init', 'fork', 'desktop'].includes(p)) throw new Error('the made-up floor: /' + p + ' does nothing here');
  return undefined;
}
// a live made-up agent says something back a moment later, so a send can be followed to its answer (builder's)
function engStressEcho(w, text) {
  const said = String(text || '').replace(/\s+/g, ' ').trim(), short = said.length > 60 ? said.slice(0, 59) + '…' : said;
  setTimeout(() => {
    const t = Date.now() / 1000 - PG.STRESS.t0;
    w.msgs.push({ role: 'assistant', text: 'Got it: "' + short + '". Carrying on with that.', t, tokens: 30, x: true });
    engStressGoOn(w);
  }, ENG_REPLY.echoAfter * 1000);
}
function engStressState(state) {
  if (!PG.STRESS || !PG.STRESS.on || !state || !state.sessions) return state;
  state.sessions.forEach(s => { const w = engStressAgent(s.id); if (!w) return;
    if (w.watch) { s.provider = 'claude'; s.drivable = false; }
    if (w.held) { s.heldElsewhere = true; s.heldBy = w.held; } });
  return state;
}

/* ---------- test controls (made-up floors only): a row in the test strip, and the same through the address ----------
     &draft=<text>     a half-typed reply in the target's box (the clicked agent, else &tagent=i)
     &watch=1          the target becomes a Claude Code session watched only (nothing can be sent)
     &held=1           the target becomes a Claude Code session open in the Claude app (a send offers a copy)
     &newagent=1       the new-agent transcript opens at the first open folder's "+" (or &newagent=<folder path>)
     &box=1            the target's transcript opens with the cursor in its box (as one click on its stack does)
   Each applied control is listed in window.__replyTest(). The page provides PG.openTranscript(id, {box}) and PG.openNewAgent(dir). */
const ENG_RBT = { applied: [], done: false };
window.__replyTest = () => ({ applied: ENG_RBT.applied.slice(), drafts: engDraftKeys() });
function engRbtTarget() { const a = PG.tptTarget && PG.tptTarget(); return a ? { a, w: engStressAgent(a.sid) } : null; }
function engRbtDo(act, v) {
  const t = engRbtTarget(), log = x => ENG_RBT.applied.push(Object.assign({ control: act, at: +PG.stressNow().toFixed(1) }, x || {}));
  if (act === 'newagent') {
    const M = PG.M, d = typeof v === 'string' && M && M.dirBy && M.dirBy[v] ? M.dirBy[v] : M ? M.root : null;
    if (d && PG.openNewAgent) { PG.openNewAgent(d); log({ dir: d.path || '' }); } return;
  }
  if (!t || !t.w) { PG.note('no made-up agent to change'); return; }
  if (act === 'draft') { engDraftSet(t.a.sid, typeof v === 'string' && v !== '1' ? v : 'Half-typed: can you also check the'); log({ id: t.a.id }); if (PG.openTranscript) PG.openTranscript(t.a.id, { box: true }); return; }
  if (act === 'watch') { t.w.watch = true; t.w.held = null; t.w.prov = 'claude'; log({ id: t.a.id }); }
  if (act === 'held') { t.w.held = 'app'; t.w.watch = false; t.w.prov = 'claude'; log({ id: t.a.id }); }
  if (act === 'box') { log({ id: t.a.id }); if (PG.openTranscript) PG.openTranscript(t.a.id, { box: true }); return; }
  PG.perform(() => PG.fetchCanvas(false)); PG.perform(PG.refresh); PG.needHistory(t.a, true);
  if (PG.openTranscript) PG.openTranscript(t.a.id, { box: true }); PG.wake(1200);
}
function engRbtStrip() {
  const host = document.getElementById('tpTest'), body = host && host.querySelector('.tbody'); if (!body || body.querySelector('.rbt')) return;
  const r = document.createElement('div'); r.className = 'r rbt';
  r.innerHTML = 'reply <button data-r="box">open with the box</button><button data-r="draft">a half-typed reply</button><button data-r="watch">watch only</button><button data-r="held">held in the app</button><button data-r="newagent">new agent from +</button>';
  body.appendChild(r);
  r.addEventListener('click', ev => { const b = ev.target.closest('[data-r]'); if (!b) return; ev.stopPropagation(); b.blur(); engRbtDo(b.dataset.r); });
}
// once a frame from the page (cheap): the strip's row, then the address's case once the floor is up
function engReplyFrame() {
  if (!PG.STRESS || !PG.STRESS.on) return;
  engRbtStrip();
  if (ENG_RBT.done || !PG.M || !PG.AG.length) return; ENG_RBT.done = true;
  const q = PG.qs;
  if (q.get('watch') === '1') engRbtDo('watch');
  if (q.get('held') === '1') engRbtDo('held');
  if (q.has('draft')) engRbtDo('draft', q.get('draft'));
  if (q.get('box') === '1') engRbtDo('box');
  if (q.has('newagent')) engRbtDo('newagent', q.get('newagent'));
}
