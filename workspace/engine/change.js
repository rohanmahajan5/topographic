/* What an agent changed (2026-11-18, brief-change.md; the planner's design, chapter 6). Origin: the change builder.
   Where the transcript's head (or a label's words) says "N files changed", a click opens the agent's OWN recorded edits in the
   transcript's place: one scroll, each file once with its edits in time order, lines taken out and put in. A click on a line opens
   one line to write a comment on it; comments are kept like a half-typed reply (closing the view, or the page, does not lose them);
   "Send N comments" sends them to the agent as ONE message through the reply routine the box uses, and the conversation comes back
   with that message in it. Nothing else: no landing, no undo, no branches, no conflicts, no several agents at once.
   The server answers GET /api/edits?id=<agent> as
     {files: [{path, edits: [{t, kind: add|update|replace, hunks: [{oldStart?, newStart?, lines: [' x', '-y', '+z']}], numbered}], more}],
      commands, cut: {files, lines}}
   (edits.py); a made-up floor answers the same shape itself (engStressEdits, below).
   The page wires it (live.html): engChangeMount(tpEl, {C}) once; engChangeFrame(a) each transcript frame; engChangeW(w) in the panel's
   placing; the head's link and a label's words call engChangeOpen(a). Reads the page through PG only. */
const ENG_CHANGE = {
  key: 'ide.change-comments',   // (builder's) the browser storage key: {project: {agent session id: {line key: comment}}}
  noteMax: 2000,                // (naive, builder's) characters kept of one comment
};
const CHG = { sid: null, id: null, data: null, loading: false, err: null, facts: '', v: 0, drawn: -1, edit: null, focus: false, el: null, body: null, foot: null, sending: false };
const CHG_CM = { proj: null, mem: {} };          // mem: {sid: {key: {path, n, text, removed, note, at}}}

/* ---------- comments, kept like a half-typed reply: in memory and in the browser's storage, per project ---------- */
function engChgLoad() {
  const p = typeof engDraftProj === 'function' ? engDraftProj() : ''; if (CHG_CM.proj === p) return;
  CHG_CM.proj = p; CHG_CM.mem = {};
  try { const all = JSON.parse(localStorage.getItem(ENG_CHANGE.key) || '{}'); if (all && typeof all[p] === 'object') CHG_CM.mem = all[p] || {}; } catch (e) {}
}
function engChgSave() {
  try {
    const all = JSON.parse(localStorage.getItem(ENG_CHANGE.key) || '{}'), keep = {};
    Object.entries(CHG_CM.mem).forEach(([sid, m]) => { const k = Object.fromEntries(Object.entries(m || {}).filter(([, c]) => c && String(c.note || '').trim())); if (Object.keys(k).length) keep[sid] = k; });
    if (Object.keys(keep).length) all[CHG_CM.proj] = keep; else delete all[CHG_CM.proj];
    localStorage.setItem(ENG_CHANGE.key, JSON.stringify(all));
  } catch (e) {}
}
const engChgOf = sid => { engChgLoad(); return CHG_CM.mem[sid] || (CHG_CM.mem[sid] = {}); };
// the comments with words in them, in the order of the view (file, edit, hunk, line)
const engChgKept = sid => Object.entries(engChgOf(sid)).filter(([, c]) => c && String(c.note || '').trim())
  .sort((x, y) => { const a = x[0].split(':').map(Number), b = y[0].split(':').map(Number); for (let i = 0; i < 4; i++) if (a[i] !== b[i]) return a[i] - b[i]; return 0; });

/* ---------- open, close, width ---------- */
const engChangeOn = a => !!(a && CHG.sid && a.sid === CHG.sid);
const engChangeW = w => CHG.sid ? Math.max(w, ENG_NAIVE.changeW) : w;
// what an agent changed, counted whole (walk 2026-11-19: the chat head read "1 file changed" for a moment, then "1 file changed, 4
// lines"): while an agent's turn is on (running, or stopped on a request) and an edit of it has no line count yet, the count it last had whole stands (none at first); a count
// that never comes (the agent stopped) gives way to the files and the lines known
function engChgFacts(a) {          // the one count (engine/model.js engChgWhole, facts.chg): whole or nothing
  const r = typeof engRec === 'function' ? engRec(a) : null, c = r && r.facts && r.facts.chg; return c || { files: 0, lines: 0 };
}
function engChangeOpen(a) {
  if (!a) return;
  CHG.sid = a.sid; CHG.id = a.id; CHG.data = null; CHG.err = null; CHG.edit = null; CHG.facts = ''; CHG.v++;
  if (CHG.body) CHG.body.scrollTop = 0;
  PG.wake();
}
function engChangeClose() {
  if (!CHG.sid) return;
  const m = engChgOf(CHG.sid); if (CHG.edit && m[CHG.edit] && !String(m[CHG.edit].note || '').trim()) delete m[CHG.edit];
  CHG.sid = null; CHG.id = null; CHG.data = null; CHG.edit = null; CHG.v++;
  if (CHG.el && CHG.el.parentNode) CHG.el.parentNode.classList.remove('cvon');
  PG.wake();
}
function engChgFetch(sid) {
  CHG.loading = true;
  PG.perform(() => PG.api('edits?id=' + encodeURIComponent(sid))).then(r => {
    if (CHG.sid !== sid) return;
    CHG.loading = false; CHG.v++;
    if (r && Array.isArray(r.files)) { CHG.data = r; CHG.err = null; } else if (!CHG.data) CHG.err = 'its edits could not be read';
    PG.wake();
  });
}

/* ---------- the view ---------- */
const engChgEsc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const engChgPl = (n, one, many) => n + ' ' + (n === 1 ? one : many);
function engChgClock(t) {
  if (!(t > 0)) return '';
  const d = new Date(t * 1000), p = x => String(x).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes());
}
// one hunk's lines with their numbers: a removed line its old number, an added or unchanged one its new number (none when not recorded)
function engChgLines(h, numbered) {
  let o = numbered && h.oldStart != null ? +h.oldStart : null, n = numbered && h.newStart != null ? +h.newStart : null;
  return (h.lines || []).map(raw => {
    const s = String(raw), m = s[0] === '-' ? '-' : s[0] === '+' ? '+' : ' ', text = m === ' ' && s[0] !== ' ' ? s : s.slice(1);
    let num = null;
    if (m === '-') { num = o; if (o != null) o++; }
    else if (m === '+') { num = n; if (n != null) n++; }
    else { num = n; if (o != null) o++; if (n != null) n++; }
    return { m, text, num };
  });
}
function engChgTotals(f) {
  let add = 0, del = 0;
  (f.edits || []).forEach(e => (e.hunks || []).forEach(h => (h.lines || []).forEach(l => { const c = String(l)[0]; if (c === '+') add++; else if (c === '-') del++; })));
  return { add, del };
}
function engChgBodyHtml(a) {
  const d = CHG.data, fx = engChgFacts(a), cm = engChgOf(CHG.sid);
  const head = fx.files ? engCountWords(fx.files, fx.lines) : 'what it changed';
  let h = `<div class="cvt"><span><b>${engChgEsc(head)}</b> · its own edits</span><button type="button" data-cv="back">back to the conversation</button></div>`;
  if (!d) return h + `<div class="cvw">${engChgEsc(CHG.err || 'reading its edits…')}</div>`;
  if (d.commands > 0) h += `<div class="cvw">It also ran ${engChgPl(d.commands, 'command', 'commands')}; anything they changed is not shown here.</div>`;
  if (!d.files.length) h += `<div class="cvw">No edits of its own in its record.</div>`;
  d.files.forEach((f, fi) => {
    const tt = engChgTotals(f), first = (f.edits || [])[0], fresh = first && first.kind === 'add';
    h += `<div class="cvf"><span class="p">${engChgEsc(f.path)}</span><span class="s">${fresh ? 'new file · ' : ''}${engChgPl((f.edits || []).length, 'edit', 'edits')} · +${tt.add} −${tt.del}</span></div>`;
    (f.edits || []).forEach((e, ei) => {
      const words = [engChgClock(e.t), e.kind === 'replace' ? 'the whole file written anew' : '', e.numbered === false ? 'line numbers not recorded' : ''].filter(Boolean).join(' · ');
      h += `<div class="cve">${engChgEsc(words)}</div>`;
      (e.hunks || []).forEach((hk, hi) => {
        if (hi) h += '<div class="cvg">⋯</div>';
        engChgLines(hk, e.numbered !== false).forEach((l, li) => {
          const key = fi + ':' + ei + ':' + hi + ':' + li, c = cm[key], on = CHG.edit === key;
          h += `<div class="l${l.m === '-' ? ' o' : l.m === '+' ? ' n' : ''}${on || (c && String(c.note || '').trim()) ? ' sel' : ''}" data-k="${key}"><i>${l.num == null ? '' : l.num}</i><u>${l.m === ' ' ? '' : l.m === '-' ? '−' : '+'}</u><span>${engChgEsc(l.text) || ' '}</span></div>`;
          if (on) h += `<div class="cm"><textarea data-k="${key}" rows="2" placeholder="a comment on this line">${engChgEsc(c ? c.note : '')}</textarea></div>`;
          else if (c && String(c.note || '').trim()) h += `<div class="cm"><div class="kept" data-k="${key}" title="click to change it">${engChgEsc(c.note)}</div></div>`;
        });
      });
    });
    if (f.more > 0) h += `<div class="cvm">${engChgPl(f.more, 'more line', 'more lines')} in this file not shown</div>`;
  });
  const cut = d.cut || {};
  if (cut.files > 0 || cut.lines > 0) h += `<div class="cvm">${[cut.files > 0 ? engChgPl(cut.files, 'more file', 'more files') : '', cut.lines > 0 ? engChgPl(cut.lines, 'more line', 'more lines') : ''].filter(Boolean).join(' and ')} not shown: the view stops there</div>`;
  return h;
}
function engChgFootHtml(a) {
  const n = engChgKept(CHG.sid).length, watch = PG.replyWatch ? PG.replyWatch(a) : null;
  const left = watch && n ? watch : n ? engChgPl(n, 'comment', 'comments') + ', kept until sent' : 'click a line to comment on it';
  return `<span>${engChgEsc(left)}</span><button type="button" data-cv="send"${!n || watch || CHG.sending ? ' disabled' : ''}>Send ${engChgPl(n, 'comment', 'comments')}</button>`;
}
function engChgDrawFoot(a) { const f = engChgFootHtml(a); if (CHG.foot._h !== f) { CHG.foot.innerHTML = f; CHG.foot._h = f; } }

// each transcript frame: shown or not, read again when its count of edits moved, drawn when something it shows changed
function engChangeFrame(a) {
  if (!CHG.el) return;
  const on = engChangeOn(a), tp = CHG.el.parentNode;
  if (tp && tp.classList.contains('cvon') !== on) tp.classList.toggle('cvon', on);
  if (!on) return;
  const fx = engChgFacts(a), fk = fx.files + ':' + fx.lines;
  if (fk !== CHG.facts && !CHG.loading) { CHG.facts = fk; engChgFetch(CHG.sid); }
  if (CHG.drawn !== CHG.v) {
    CHG.drawn = CHG.v;
    const st = CHG.body.scrollTop; CHG.body.innerHTML = engChgBodyHtml(a); CHG.body.scrollTop = st;
    if (CHG.focus) { CHG.focus = false; const ta = CHG.body.querySelector('textarea'); if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = ta.value.length; } }
  }
  engChgDrawFoot(a);
}
const engChangeH = () => CHG.sid && CHG.body ? CHG.body.scrollHeight + CHG.foot.offsetHeight + 4 : 0;

/* ---------- comments: open, keep, send ---------- */
function engChgLineOf(key) {
  const [fi, ei, hi, li] = key.split(':').map(Number), f = CHG.data && CHG.data.files[fi], e = f && f.edits[ei], hk = e && e.hunks[hi];
  const l = hk && engChgLines(hk, e.numbered !== false)[li];
  return l ? { path: f.path, n: l.num, text: l.text, removed: l.m === '-' } : null;
}
function engChgEdit(key) {
  const m = engChgOf(CHG.sid);
  if (CHG.edit && CHG.edit !== key && m[CHG.edit] && !String(m[CHG.edit].note || '').trim()) delete m[CHG.edit];
  CHG.edit = key; CHG.focus = !!key; CHG.v++; engChgSave(); PG.wake();
}
function engChgMessage(kept) {
  const one = ([, c], i) => (kept.length > 1 ? (i + 1) + '. ' : '') + c.path + (c.n != null ? ' line ' + c.n : ' (line number not recorded)') + (c.removed ? ' (removed line)' : '') + ': ' + (String(c.text).trim() ? '`' + String(c.text).trim() + '`' : '(an empty line)') + '\n' + (kept.length > 1 ? '   ' : '') + String(c.note).trim();
  return (kept.length > 1 ? 'Comments on your edits:' : 'A comment on your edits:') + '\n\n' + kept.map(one).join('\n\n');
}
async function engChgSend() {
  const a = PG.agent(CHG.id), sid = CHG.sid; if (!a || CHG.sending) return;
  if (PG.replyWatch && PG.replyWatch(a)) return;
  const kept = engChgKept(sid); if (!kept.length) return;
  const text = engChgMessage(kept), was = JSON.parse(JSON.stringify(engChgOf(sid)));
  CHG.sending = true; CHG_CM.mem[sid] = {}; engChgSave();
  engChangeClose();          // back to the conversation: the message shows there with its delivery state
  try { await PG.sendTo(a, text, undefined, null); } finally { CHG.sending = false; }
  const m = (a.sent || []).filter(x => x.text === text).pop();
  if (m && m.note === 'not sent') { CHG_CM.mem[sid] = was; engChgSave(); }          // not through: the comments stay to send again
  PG.wake();
}

/* ---------- built once into the transcript panel ---------- */
function engChangeMount(tp, o) {
  const el = document.createElement('div'); el.className = 'cv';
  el.innerHTML = '<div class="cvb"></div><div class="cvfoot"></div>';
  tp.appendChild(el); CHG.el = el; CHG.body = el.children[0]; CHG.foot = el.children[1];
  const C = o.C, MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace';
  const st = document.createElement('style');
  st.textContent = `.tp .cv{display:none} .tp.cvon .cv{display:flex;flex-direction:column;flex:1 1 auto;min-height:0}
  .tp.cvon > .tpq, .tp.cvon > .rb, .tp.cvon > .tpnew, .tp.cvon > .tpl{display:none !important}
  .cv .cvb{flex:1 1 auto;min-height:0;overflow:auto;padding:6px 9px 4px}
  .cv .cvt{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:11px;padding:6px 0 5px;margin-top:-6px;border-bottom:1px solid #e7e1d6;margin-bottom:5px;position:sticky;top:-6px;background:#fffdf9;z-index:1}
  .cv .cvt b{font-weight:600}
  .tp .cv .cvt button{border:0;background:none;color:#5b6b8a;padding:0;white-space:nowrap}
  .tp .cv .cvt button:hover{color:${C.ink};background:none;text-decoration:underline}
  .cv .cvw{font-size:10.5px;color:${C.muted};margin:0 0 6px}
  .cv .cvf{display:flex;justify-content:space-between;gap:8px;font-size:11px;font-weight:600;margin:9px 0 1px}
  .cv .cvf .p{overflow-wrap:anywhere} .cv .cvf .s{font-weight:400;color:${C.muted};white-space:nowrap}
  .cv .cve{font-size:10px;color:#a49d90;padding:2px 0 1px 30px}
  .cv .cvg{font-size:10px;color:#b0a99c;padding:0 0 0 30px;line-height:12px}
  .cv .cvm{font-size:10px;color:#8f887b;padding:2px 0 0 30px}
  .cv .l{display:flex;font:10.5px/1.5 ${MONO};white-space:pre-wrap;overflow-wrap:anywhere;cursor:pointer;color:#2a2722}
  .cv .l:hover{box-shadow:inset 0 0 0 1px #cfc8bb}
  .cv .l i{font-style:normal;width:28px;flex:none;text-align:right;padding-right:3px;color:#b0a99c}
  .cv .l u{text-decoration:none;width:11px;flex:none;color:#b0a99c}
  .cv .l span{flex:1;min-width:0}
  .cv .l.o{background:#f6e1dd} .cv .l.o i,.cv .l.o u{color:#b9877e}
  .cv .l.n{background:#dff0de} .cv .l.n i,.cv .l.n u{color:#7fa47c}
  .cv .l.sel{box-shadow:inset 0 0 0 1px #8a9bbd}
  .cv .cm{margin:2px 0 4px 42px;font-size:11px}
  .cv .cm textarea{width:100%;box-sizing:border-box;font-size:11px;line-height:1.4;font-family:inherit;border:1px solid #9d978b;border-radius:4px;padding:3px 5px;resize:vertical;min-height:36px;background:#fffdf8;color:${C.ink}}
  .cv .cm .kept{background:#eef1f7;border-radius:4px;padding:3px 6px;color:#33415e;cursor:text;white-space:pre-wrap;overflow-wrap:anywhere}
  .cv .cvfoot{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:11px;color:${C.muted};border-top:1px solid #e7e1d6;padding:6px 9px 7px}
  .tp .cv .cvfoot button{font-weight:600;background:#33415e;color:#fff;border-color:#33415e;padding:1px 9px;white-space:nowrap}
  .tp .cv .cvfoot button:hover{background:#26324a}
  .tp .cv .cvfoot button:disabled{background:#c9c3b7;border-color:#c9c3b7;cursor:default}`;
  document.head.appendChild(st);
  el.addEventListener('click', ev => {
    ev.stopPropagation();
    const b = ev.target.closest('[data-cv]');
    if (b && b.dataset.cv === 'back') { engChangeClose(); return; }
    if (b && b.dataset.cv === 'send') { engChgSend(); return; }
    if (ev.target.closest('textarea')) return;
    const k = ev.target.closest('.kept, .l'); if (k && k.dataset.k) engChgEdit(CHG.edit === k.dataset.k && k.classList.contains('l') ? null : k.dataset.k);
  });
  el.addEventListener('input', ev => {
    const ta = ev.target.closest('textarea[data-k]'); if (!ta || !CHG.sid) return;
    const m = engChgOf(CHG.sid), key = ta.dataset.k, line = m[key] || engChgLineOf(key); if (!line) return;
    m[key] = Object.assign({}, line, { note: ta.value.slice(0, ENG_CHANGE.noteMax), at: Date.now() });
    engChgSave(); const a = PG.agent(CHG.id); if (a) engChgDrawFoot(a);
  });
  el.addEventListener('keydown', ev => {
    const ta = ev.target.closest('textarea[data-k]'); if (!ta) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    ev.stopPropagation();
    if ((ev.key === 'Enter' && !ev.shiftKey) || ev.key === 'Escape') { ev.preventDefault(); engChgEdit(null); }
  });
}

/* ---------- the made-up floor's answer (?stress=): its agents' write events, given made-up lines of the counts they carry; the
   second file's first edit has no line numbers, the third file (when there is one) is new and longer than a file's cut ---------- */
function engStressEdits(sid) {
  const W0 = PG.stressWorld(), w = W0.agents.find(x => x.id === sid); if (!w) return { files: [], commands: 0, cut: { files: 0, lines: 0 } };
  const now = PG.stressNow(), t0 = PG.STRESS.t0, FILE_MAX = 400, ALL_MAX = 3000;
  let seed = 0; for (const ch of String(sid)) seed = (seed * 31 + ch.charCodeAt(0)) % 2147483647; seed = seed || 7;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }, pick = A => A[Math.floor(rnd() * A.length)];
  const WORDS = ['const total = items.reduce((n, x) => n + x.price, 0);', 'if (!session) return null;', 'export function loadReport(id) {', 'await db.query(sql, [id]);',
    'return res.status(404).json({ error: "not found" });', 'const key = `report:${id}`;', '  cache.set(key, value, { ttl: 60 });', '}', 'import { parse } from "./parse";',
    'for (const row of rows) {', '  out.push(format(row));', 'expect(result).toEqual(expected);', 'const limit = Math.min(+req.query.limit || 50, 200);', 'logger.info("export done", { n });',
    'if (retries > 3) throw new Error("gave up");', 'const user = await users.find(req.params.id);', 'return { ...state, loading: false };', '// the cursor is the last id seen'];
  const order = [], by = new Map();
  w.evs.filter(e => e.kind === 'write' && e.t <= now).forEach(e => { if (!by.has(e.path)) { by.set(e.path, []); order.push(e.path); } by.get(e.path).push(e); });
  let total = 0; const files = [], cut = { files: 0, lines: 0 };
  order.forEach((path, fi) => {
    const edits = by.get(path).map((e, ei) => {
      const L = Math.max(1, e.lines || 1), t = t0 + e.t;
      if (fi === 2 && ei === 0) return { t, kind: 'add', numbered: true, hunks: [{ oldStart: 0, newStart: 1, lines: Array.from({ length: Math.max(L, 460) }, () => '+' + pick(WORDS)) }] };
      const del = Math.floor(L * 0.4), add = L - del;
      if (fi === 1 && ei === 0) return { t, kind: 'update', numbered: false, hunks: [{ lines: Array.from({ length: del }, () => '-' + pick(WORDS)).concat(Array.from({ length: add }, () => '+' + pick(WORDS))) }] };
      const at = 5 + Math.floor(rnd() * 300);
      return { t, kind: 'update', numbered: true, hunks: [{ oldStart: at, newStart: at, lines: [' ' + pick(WORDS), ' ' + pick(WORDS)].concat(Array.from({ length: del }, () => '-' + pick(WORDS)),
        Array.from({ length: add }, () => '+' + pick(WORDS)), [' ' + pick(WORDS)]) }] };
    });
    // the cuts, as the server makes them: lines past a file's share, then files past the whole answer's
    const n = edits.reduce((s, e) => s + e.hunks.reduce((m, h) => m + h.lines.length, 0), 0);
    if (total >= ALL_MAX) { cut.files++; cut.lines += n; return; }
    let room = Math.min(FILE_MAX, ALL_MAX - total), more = 0;
    edits.forEach(e => e.hunks.forEach(h => { if (h.lines.length > room) { more += h.lines.length - room; h.lines = h.lines.slice(0, room); } room -= h.lines.length; }));
    const kept = edits.filter(e => e.hunks.some(h => h.lines.length));
    total += n - more; files.push({ path, edits: kept, more });
  });
  const commands = (w.msgs || []).filter(m => m.role === 'tool' && m.t <= now && !/^(sed|cat|head|tail|rg|grep|ls|nl|wc)\b/.test(String(m.text || ''))).length;
  return { files, commands, cut };
}
