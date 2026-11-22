/* The file window's body (2026-11-22, the takes picked from the three file-viewing concepts, branch ide-pub-fileview-concepts-2026-11-22):
   - frame.html take A (docked, line numbers, current line, wrap, the whole-file strip at the right, ⌘F, Ctrl+G) with take B's
     outline on the left that follows your place, the path into it in the title line ("› Session › __init__"), folding from the
     arrow by a line number, and its one box (a name filters the outline, a number goes to that line, other text finds every line
     holding it);
   - outline kinds (a visual cue telling a function from a class or a variable): a small badge per entry, one colour
     per kind, its kind in words on hover (engine/outline.js);
   - syntax.html take B: the warm full colour (engine/syntax-colour.js); Markdown opens "styled", "page" one click away;
   - edits.html take 2: the whole file, each agent's changed lines tinted in that agent's floor colour, older ones fainter, removed
     lines folded into "− N" marks that open, the "Agent · X min ago" title on hover and for the change jumped to, p / n and the
     strip's marks to move between changes; no "Show changes" slider (it was removed as confusing).
   Editing (the builder's choice, 2026-11-22): the coloured rows are drawn under a transparent textarea with the same font, line
   height, tab size, wrap and width, so the file is typed into exactly as before (click, type, ⌘S, Esc) while the colours, tints,
   find marks and current line show through; an edited line is coloured again from there down. Two things a textarea cannot show
   (a folded section, removed lines opened out) make the text read-only while they are open; a click in the text closes them and
   puts the cursor where you clicked.
   The page (live.html) owns the file store, the window's place, saving and closing; it mounts this once (engFileMount) and calls
   sync() each frame. Origins: (design) the picks above; (concept) the code taken from the three concept pages; (builder) the rest. */
function engFileMount(el, H) {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const rgba = (hex, a) => { const n = parseInt(String(hex).slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };
  const LH = 18;          // px: one line (the textarea and the rows share it)
  const store = (k, d) => { try { const v = localStorage.getItem('fv-' + k); return v == null ? d : v === '1'; } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem('fv-' + k, v ? '1' : '0'); } catch (e) {} };

  /* ---------- the window's parts ---------- */
  { const st = document.createElement('style');
    st.textContent = `
  .fsheet .hd{display:block;padding:6px 8px 5px 12px}
  .fsheet .hd .r1{display:flex;gap:8px;align-items:center;min-width:0}
  .fsheet .hd .r2{display:flex;gap:8px;align-items:baseline;min-width:0;font-size:10.5px;color:var(--muted);margin-top:1px;min-height:14px}
  .fsheet .fvp{font:12px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto;direction:rtl;text-align:left}
  .fsheet .fvp i{font-style:normal;color:var(--muted)} .fsheet .fvp b{font-weight:600}
  .fsheet .fvc{font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1 1 0}
  .fsheet .fvc b{color:var(--ink);font:500 10.5px var(--mono);cursor:pointer} .fsheet .fvc b:hover{text-decoration:underline}
  .fsheet .who{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto}
  .fsheet .who .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin:0 4px 0 0;vertical-align:0}
  .fsheet .who b{color:var(--ink);font-weight:600}
  .fsheet #fsS{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1 1 0}
  .fsheet #fsS.err{color:#a8441a}
  .fsheet .btn{font:inherit;font-size:10.5px;border:1px solid #ddd6ca;background:#fff;color:#5c5f66;border-radius:4px;padding:0 6px;cursor:pointer;line-height:16px;flex:none}
  .fsheet .btn.on{background:#efeae1;color:var(--ink);border-color:#cfc8bb}
  .fsheet .mdm{display:flex;gap:2px;flex:none}.fsheet .mdm:empty{display:none}
  .fsheet .x{border:0;background:none;font-size:15px;line-height:1;color:var(--muted);cursor:pointer;padding:0 4px;flex:none}.fsheet .x:hover{color:var(--ink)}
  .fsheet .save{font:inherit;font-size:11px;border:1px solid var(--ask);background:var(--ask);color:#fff;border-radius:5px;padding:0 9px;cursor:pointer;flex:none;line-height:16px}
  .fvb{display:flex;flex:1 1 auto;min-height:0;position:relative}
  .fvs{flex:0 0 196px;width:196px;min-width:0;display:flex;flex-direction:column;border-right:1px solid #efeae1;background:#fcfbf8;min-height:0}
  .fsheet.noside .fvs{display:none}
  .fsheet.narrow .fvs{position:absolute;left:0;top:0;bottom:0;z-index:4;box-shadow:4px 0 14px rgba(40,40,52,.10)}
  .fvs input{margin:7px 7px 3px;font:11px -apple-system,system-ui,sans-serif;border:1px solid #ddd6ca;border-radius:5px;padding:2px 6px;outline:none;background:#fff;min-width:0}
  .fvs input:focus{border-color:#b9b2a6}
  .fvs .hint{font-size:10px;color:var(--muted);padding:0 9px 3px;min-height:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .fvo{overflow:auto;flex:1 1 auto;padding:0 0 10px}
  .fvo .s{font:11px var(--mono);padding:1px 34px 1px 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;color:#4b525d;display:block;position:relative;line-height:16px}
  .fvo .s .n{position:absolute;right:6px;top:1px;color:var(--faint);font-size:10px;font-variant-numeric:tabular-nums}
  .fvo .s:hover{background:#f1ede5} .fvo .s.on{background:#e9e4da;color:var(--ink)} .fvo .s.sel{box-shadow:inset 2px 0 0 var(--ink)}
  .fvo .s .k{display:inline-block;width:13px;height:13px;line-height:13px;margin:0 5px 0 0;border-radius:3px;text-align:center;font:600 9.5px -apple-system,system-ui,sans-serif;vertical-align:0;border:1px solid}
  .fvo .s.ty{font-weight:600;color:var(--ink)}
  .fvo .grp{font-size:10px;color:var(--muted);padding:7px 9px 2px}
  .fvo .s.m{padding-right:8px;color:#4b525d}
  .fvo .s.m .n{position:static;display:inline-block;min-width:3.5ch;margin:0 5px 0 0}
  .fvo .s.m em{font-style:normal;background:var(--hit);border-radius:2px}
  .fvx{position:relative;overflow:auto;flex:1 1 auto;min-width:0;min-height:0;background:#fff;outline:none}
  .fvx .code{position:relative;font:11.5px/18px var(--mono);color:var(--ink);tab-size:4;-moz-tab-size:4;font-variant-ligatures:none;padding:4px 0 40vh;min-width:100%;width:max-content;box-sizing:border-box}
  .fvx.wrap .code{width:auto}
  .fvx .r{display:flex;min-width:100%;min-height:18px;cursor:text}
  .fvx .r.hid{display:none}
  .fvx .g{position:sticky;left:0;z-index:2;flex:0 0 auto;width:var(--gw);padding:0 6px 0 8px;text-align:right;color:#b4aea3;background:#fff;user-select:none;font-variant-numeric:tabular-nums}
  .fvx .fd{position:sticky;left:calc(var(--gw) + 14px);z-index:2;flex:0 0 12px;width:12px;text-align:center;color:#b9b3a7;background:#fff;user-select:none;font-size:9px}
  .fvx .fd:not(:empty){cursor:pointer} .fvx .r:hover .fd:not(:empty){color:var(--ink)}
  .fvx .e{position:sticky;left:calc(var(--gw) + 26px);z-index:2;flex:0 0 3px;width:3px;margin-right:8px;box-shadow:4px 0 0 #fff;background:#fff}
  .fvx .t{white-space:pre;padding-right:24px}
  .fvx.wrap .t{white-space:pre-wrap;overflow-wrap:anywhere;flex:1 1 auto;min-width:0}
  .fvx .r.cur .g,.fvx .r.cur .fd{color:var(--ink)}
  .fvx .r.cur{box-shadow:inset 0 0 0 100vw rgba(44,52,64,.045)}
  .fvx .fp{display:none;margin-left:8px;font:10px -apple-system,system-ui,sans-serif;color:var(--muted);background:#f1ede5;border-radius:8px;padding:0 6px;cursor:pointer;white-space:nowrap;align-self:center;line-height:14px}
  .fvx .r.fold .fp{display:inline-block}
  .fvx mark{background:var(--hit);color:inherit;border-radius:2px} .fvx mark.on{background:var(--hiton)}
  .fvx textarea{position:absolute;z-index:1;margin:0;border:0;outline:none;resize:none;padding:0 24px 0 0;box-sizing:border-box;overflow:hidden;
    font:11.5px/18px var(--mono);tab-size:4;-moz-tab-size:4;font-variant-ligatures:none;letter-spacing:normal;white-space:pre;overflow-wrap:normal;
    color:transparent;caret-color:var(--ink);background:transparent}
  .fvx.wrap textarea{white-space:pre-wrap;overflow-wrap:anywhere}
  .fvx textarea::selection{background:rgba(47,85,151,.22);color:transparent}
  .fvx textarea[readonly]{caret-color:transparent}
  .fvx .wait{position:absolute;left:14px;top:10px;color:var(--muted);font-size:11px}
  .fvx .wait:empty{display:none}
  .fvx .cut{position:relative;height:0;z-index:3}
  .fvx .cut span{position:absolute;left:3px;top:-7px;font:10px/12px -apple-system,system-ui,sans-serif;padding:0 5px;border-radius:6px;background:#fff;border:1px solid;cursor:pointer;white-space:nowrap}
  .fvx .cut::after{content:"";position:absolute;left:var(--tl);right:0;top:0;border-top:1px dashed var(--cc);pointer-events:none}
  .fvx .r.del .t{color:#8c8a85} .fvx .r.del .g{color:transparent}
  .fvx .r.del{cursor:default;background-image:repeating-linear-gradient(135deg,rgba(0,0,0,.035) 0 3px,transparent 3px 7px)}
  .fvx .hov{position:absolute;z-index:3;display:none;gap:6px;align-items:center;transform:translateY(-100%);background:#fff;border:1px solid var(--line);border-bottom:0;
    border-radius:5px 5px 0 0;padding:1px 9px;font:11px -apple-system,system-ui,sans-serif;white-space:nowrap;box-shadow:0 -2px 6px rgba(40,40,52,.06);pointer-events:none;color:var(--ink)}
  .fvx .hov b{font-weight:600} .fvx .hov .mut{color:var(--muted)} .fvx .hov .dot{display:inline-block;width:7px;height:7px;border-radius:50%}
  .fvx .mdv{padding:10px 18px 30px;font:12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:var(--ink);max-width:700px}
  .fvx .mdv p{margin:0 0 7px}.fvx .mdv .mdh{font-weight:650;margin:10px 0 3px}.fvx .mdv .mdh1{font-size:15px}.fvx .mdv .mdh2{font-size:13.5px}.fvx .mdv .mdh3{font-size:12.5px}
  .fvx .mdv code{font:0.92em var(--mono);background:#f1ede5;border-radius:3px;padding:0 3px}
  .fvx .mdv .mdpre{margin:4px 0 7px;background:#f6f3ed;border-radius:5px}.fvx .mdv .mdpre .mdlang{display:none}
  .fvx .mdv .mdpre pre{margin:0;padding:5px 8px;overflow-x:auto;white-space:pre;font:10.5px/1.45 var(--mono);color:#3d4450}.fvx .mdv .mdpre code{background:none;padding:0;font:inherit}
  .fvx .mdv ul,.fvx .mdv ol{margin:2px 0 6px;padding-left:18px}.fvx .mdv blockquote{margin:3px 0;padding-left:7px;border-left:2px solid #e2ddd2;color:#5c5f66}
  .fvx .mdv a{color:#2f5597;text-decoration:none;border-bottom:1px solid rgba(47,85,151,.3)}
  .fvx .mdv table{border-collapse:collapse;font-size:11px;margin:2px 0 7px}.fvx .mdv td,.fvx .mdv th{border:1px solid #e6e0d5;padding:1px 5px;text-align:left;vertical-align:top}
  .fvx .mdv hr{border:0;border-top:1px solid #e6e0d5;margin:8px 0}
  .fsheet canvas.fvst{position:relative;inset:auto;display:block;height:auto;align-self:stretch;touch-action:auto;flex:0 0 14px;width:14px;border-left:1px solid #efeae1;cursor:pointer;background:#fcfbf8}          /* the page's own rule puts every canvas fixed over the window */
  .fsheet .ft{flex:0 0 auto;display:flex;gap:10px;align-items:center;border-top:1px solid #efeae1;padding:2px 10px;font-size:10.5px;color:var(--muted);user-select:none;white-space:nowrap;min-width:0}
  .fsheet .ft .ln{cursor:pointer;border-radius:3px;padding:0 3px} .fsheet .ft .ln:hover{background:#f1ede5;color:var(--ink)}
  .fsheet .ft .ln b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
  .fsheet .ft .chg{display:flex;gap:4px;align-items:center;overflow:hidden;min-width:0}
  .fsheet .ft .chg button{font:inherit;border:1px solid #ddd6ca;background:#fff;color:var(--ink);border-radius:4px;padding:0 5px;cursor:pointer;line-height:14px}
  .fsheet .ft .chg kbd{font:10px var(--mono);border:1px solid #ddd6ca;border-radius:3px;padding:0 3px;background:#fff}
  .fsheet .ft .meta{overflow:hidden;text-overflow:ellipsis;min-width:0}
  .fsheet .ft .sp{flex:1}
  /* colours: syntax.html take B (full colour, warm) */
  .fvx .t-kw{color:#8a3d93}.fvx .t-bi,.fvx .t-slf{color:#2f5597}.fvx .t-ty,.fvx .t-tyd{color:#1d6b78}.fvx .t-fnd,.fvx .t-call{color:#7f5300}
  .fvx .t-str,.fvx .t-sv{color:#9c401c}.fvx .t-doc{color:#4d6141}.fvx .t-num{color:#246340}.fvx .t-com{color:#4d6141;font-style:italic}
  .fvx .t-dec{color:#8a5d14}.fvx .t-prop{color:#2f5597}.fvx .t-op,.fvx .t-pun{color:#5c5f66}.fvx .t-var{color:#2f5597}.fvx .t-opt{color:#1d6b78}
  .fvx .t-mdh{color:#8a3d93;font-weight:650}.fvx .t-mdmark{color:#625d54}.fvx .t-mdb{font-weight:650}.fvx .t-mdi{font-style:italic}.fvx .t-mdcode{color:#9c401c}
  .fvx .t-mdlink{color:#2f5597}.fvx .t-mdurl{color:#5f646d}.fvx .t-mdlist{color:#9c401c}.fvx .t-mdq{color:#4d6141}.fvx .t-mdlang{color:#1d6b78}
  /* Markdown "styled": the marks stay (it is still the file); headings and code take their shape in weight and ground, never in size
     (a bigger heading would no longer sit under the line it is typed in) */
  .fvx.styled .mh1 .t-mdh,.fvx.styled .mh2 .t-mdh{color:#141a24;font-weight:700}
  .fvx.styled .mh1{box-shadow:inset 0 -1px 0 #e6e0d5}
  .fvx.styled .mh3 .t-mdh{color:#141a24;font-weight:650}
  .fvx.styled .mcode .t{background:#f6f3ed}
  .fvx.styled .t-mdcode{background:#f1ede5;border-radius:3px}.fvx.styled .mcode .t-mdcode{background:none}
  .fvx.styled .mq .t{color:#5c5f66}`;
    document.head.appendChild(st); }
  // the head (two rows: the path and where you are in it; who last touched it and the file's state), the body, the foot
  const hd = el.querySelector('.hd');
  hd.innerHTML = `<div class="r1"><span class="fvp" id="fsP"></span><span class="fvc"></span><span class="mdm"></span>
    <button type="button" class="btn fvol" title="the list of what the file holds">Outline</button><button type="button" class="btn fvwr" title="fold long lines">Wrap</button>
    <button id="fsSave" class="save" type="button">save</button><button id="fsX" class="x" type="button" aria-label="close">×</button></div>
    <div class="r2"><span class="who"></span><span id="fsS"></span></div>`;
  const old = el.querySelector('textarea'); if (old) old.remove();
  const body = document.createElement('div'); body.className = 'fvb';
  body.innerHTML = `<div class="fvs"><input placeholder="Name, line number or text" spellcheck="false" aria-label="find in the file"><div class="hint"></div><div class="fvo"></div></div>
    <div class="fvx" tabindex="-1"><div class="code"></div><div class="hov"></div><div class="wait"></div>
      <textarea id="fsT" spellcheck="false" autocomplete="off" autocapitalize="off" aria-label="file text"></textarea></div><canvas class="fvst" title="the whole file: where you are, find hits and agents' changes; click or drag"></canvas>`;
  el.appendChild(body);
  const ft = document.createElement('div'); ft.className = 'ft';
  ft.innerHTML = `<span class="ln" title="go to a line (Ctrl G)"></span><span class="chg"></span><span class="sp"></span><span class="meta"></span>`;
  el.appendChild(ft);
  const $ = s => el.querySelector(s);
  const pathEl = $('.fvp'), crumbEl = $('.fvc'), mdmEl = $('.mdm'), olBtn = $('.fvol'), wrBtn = $('.fvwr'), whoEl = $('.who'), stEl = $('#fsS'), saveBtn = $('#fsSave'), xBtn = $('#fsX');
  const side = $('.fvs'), inp = $('.fvs input'), hint = $('.fvs .hint'), ol = $('.fvo'), box = $('.fvx'), code = $('.fvx .code'), hov = $('.fvx .hov'), waitEl = $('.fvx .wait'), ta = $('#fsT'), strip = $('.fvst');
  const lnEl = $('.ft .ln'), chgEl = $('.ft .chg'), metaEl = $('.ft .meta');

  /* ---------- state ---------- */
  const V = { path: null, text: null, lines: [], lang: 'plain', lx: null, st: [null], html: [], cls: [], rows: [], cur: 1, gw: 2,
    out: [], at: new Map(), how: '', outV: 0, folded: new Set(), q: '', hits: [], hi: -1, sel: -1, listN: 0,
    md: 'styled', wrap: store('wrap', false), side: store('side', true), known: false, ro: true,
    ed: { changes: [], owner: [], sig: '', v: 0, foc: -1, hov: null, open: new Set(), t0: 0, t1: 0, raw: null, err: '' },
    textLeft: 0, lastTop: -1, timers: {}, pendingLine: null, mtime: null };
  const later = (k, ms, fn) => { clearTimeout(V.timers[k]); V.timers[k] = setTimeout(fn, ms); };

  /* ---------- rows: one per line, coloured by the tokenizer, line state carried line to line ---------- */
  const lexLang = path => typeof SC === 'object' ? SC.langOf(path) : 'plain';
  function tokHtml(toks, hits) {          // a line's coloured pieces, with find marks laid over them
    if (!hits || !hits.length) return toks.map(([c, s]) => c ? `<span class="t-${c}">${esc(s)}</span>` : esc(s)).join('');
    let h = '', off = 0;
    for (const [c, s] of toks) {
      let k = 0;
      while (k < s.length) {
        const a = off + k, hit = hits.find(x => x.at <= a && a < x.at + V.q.length), nxt = hits.reduce((m, x) => x.at > a && x.at < m ? x.at : m, off + s.length);
        const end = hit ? Math.min(off + s.length, hit.at + V.q.length) : Math.min(off + s.length, nxt), piece = esc(s.slice(k, end - off));
        const span = c ? `<span class="t-${c}">${piece}</span>` : piece;
        h += hit ? `<mark${V.hits[V.hi] === hit ? ' class="on"' : ''}>${span}</mark>` : span; k = end - off;
      }
      off += s.length;
    }
    return h;
  }
  function lexFrom(i, line) { const r = V.lx(line, V.st[i]); return r; }
  function rowEl(i, r) {
    const d = document.createElement('div'); d.className = 'r' + (r.lineCls ? ' ' + r.lineCls : '');
    d.innerHTML = `<span class="g">${i + 1}</span><span class="fd"></span><span class="e"></span><span class="t">${tokHtml(r.toks, hitsOf(i + 1))}</span><span class="fp"></span>`;
    d._toks = r.toks; return d;
  }
  const hitsOf = n => V.q && V.hits.length ? V.hits.filter(h => h.line === n) : null;
  function renderAll() {
    V.lines = V.text.split('\n'); V.lang = lexLang(V.path); V.lx = typeof SC === 'object' ? SC.lexer(V.lang) : (l => ({ toks: l ? [['', l]] : [], st: null, lineCls: '' }));
    V.st = [null]; const frag = document.createDocumentFragment(); V.rows = [];
    V.lines.forEach((l, i) => { const r = lexFrom(i, l); V.st[i + 1] = r.st; const d = rowEl(i, r); V.rows.push(d); frag.appendChild(d); });
    code.textContent = ''; code.appendChild(frag);
    V.gw = String(V.lines.length).length; code.style.setProperty('--gw', (V.gw + 0.2) + 'ch');
    V.folded.clear(); V.ed.open.clear();
    applyFolds(); paintFolds(); paintTints(); layout();
  }
  // an edit: rows from the first changed line to the last are drawn again, and colouring runs on down until a line's state settles
  function onTyped() {
    const nt = ta.value, nl = nt.split('\n'), ol0 = V.lines; let p = 0;
    while (p < ol0.length && p < nl.length && ol0[p] === nl[p]) p++;
    let s = 0; while (s < ol0.length - p && s < nl.length - p && ol0[ol0.length - 1 - s] === nl[nl.length - 1 - s]) s++;
    const oEnd = ol0.length - s, nEnd = nl.length - s;
    const fresh = [], st = V.st.slice(0, p + 1);
    for (let i = p; i < nEnd; i++) { const r = V.lx(nl[i], st[i]); st[i + 1] = r.st; fresh.push(rowEl(i, r)); }
    const before = V.rows[oEnd] || null; for (let i = p; i < oEnd; i++) V.rows[i].remove();
    fresh.forEach(d => code.insertBefore(d, before));
    const tail = V.rows.slice(oEnd), oldSt = V.st.slice(oEnd + 1);
    V.rows = V.rows.slice(0, p).concat(fresh, tail);
    for (let k = 0; k < tail.length; k++) {          // the unchanged lines below: coloured again while the carried state differs
      const i = nEnd + k, r = V.lx(nl[i], st[i]); st[i + 1] = r.st;
      if (JSON.stringify(r.st) === JSON.stringify(oldSt[k]) && JSON.stringify(st[i]) === JSON.stringify(V.st[oEnd + k])) { for (let j = k + 1; j < tail.length; j++) st[nEnd + j + 1] = oldSt[j]; break; }
      const d = rowEl(i, r); code.replaceChild(d, V.rows[i]); V.rows[i] = d;
    }
    V.st = st; V.lines = nl; V.text = nt;
    if (nEnd !== oEnd) for (let i = nEnd; i < V.rows.length; i++) V.rows[i].firstChild.textContent = i + 1;
    const gw = String(nl.length).length; if (gw !== V.gw) { V.gw = gw; code.style.setProperty('--gw', (gw + 0.2) + 'ch'); }
    H.edit(nt);
    if (V.q) later('find', 250, () => runFind(true));
    later('outline', 400, () => reOutline());
    later('edits', 700, () => placeEdits());
    sizeTa(); caretLine(); drawStrip();
  }

  /* ---------- the outline (engine/outline.js) ---------- */
  async function reOutline() {
    if (typeof FVO !== 'object') return;
    const path = V.path, text = V.text, v = ++V.outV;
    let r; try { r = await FVO.outline(text, path); } catch (e) { r = { entries: [], how: 'none', note: e.message }; }
    if (v !== V.outV || path !== V.path) return;
    V.out = r.entries; V.how = r.how; V.howNote = r.note; V.at = new Map(V.out.map(s => [s.line, s]));
    for (const l of [...V.folded]) if (!V.at.has(l)) V.folded.delete(l);
    paintFolds(); applyFolds(); list(); follow(true); meta(); H.wake();
  }
  function paintFolds() {
    V.rows.forEach((r, i) => { const s = V.at.get(i + 1), can = s && s.end > s.line, fd = r.children[1], fp = r.children[4];
      const t = can ? (V.folded.has(i + 1) ? '▸' : '▾') : ''; if (fd.textContent !== t) fd.textContent = t;
      const p = can ? `⋯ ${s.end - s.line} lines` : ''; if (fp.textContent !== p) fp.textContent = p; });
  }
  function applyFolds() {
    const hid = new Uint8Array(V.lines.length + 2);
    for (const l of V.folded) { const s = V.at.get(l); if (s) for (let j = s.line + 1; j <= s.end; j++) hid[j] = 1; }
    V.rows.forEach((r, i) => { r.classList.toggle('hid', !!hid[i + 1]); r.classList.toggle('fold', V.folded.has(i + 1)); });
    paintFolds(); editable(); layout();
  }
  function toggleFold(n) { V.folded.has(n) ? V.folded.delete(n) : V.folded.add(n); if (ta === document.activeElement) ta.blur(); applyFolds(); }
  const chainAt = n => V.out.filter(s => s.line <= n && n <= s.end).sort((a, b) => a.line - b.line);
  const sectionAt = n => { let b = null; for (const s of V.out) if (s.line <= n && n <= s.end && (!b || s.line > b.line)) b = s; return b; };
  const badge = s => { const k = (FVO.KINDS[s.kind] || FVO.KINDS.variable); return `<span class="k" style="color:${k[1]};border-color:${rgba(k[1], .45)};background:${rgba(k[1], .08)}">${esc(k[0])}</span>`; };
  function list() {          // the outline, filtered by the box; other text: the lines holding it
    const q = V.q.trim(), ql = q.toLowerCase(), num = /^\d+$/.test(q), types = FVO.TYPEISH;
    const syms = V.out.filter(s => !q || num || s.name.toLowerCase().includes(ql));
    let h = '';
    if (!V.out.length && V.known) h += `<div class="grp">${V.how === 'none' ? 'no outline for this kind of file' : /\.md$/i.test(V.path) ? 'no headings in this file' : 'nothing named in this file'}</div>`;
    h += syms.map(s => `<div class="s${types.has(s.kind) || s.kind === 'heading' && s.depth === 0 ? ' ty' : ''}" data-l="${s.line}" title="${esc(FVO.kindWords(s))}, line ${s.line}" style="padding-left:${8 + (q ? 0 : s.depth * 11)}px">${badge(s)}${esc(s.name)}<span class="n">${s.line}</span></div>`).join('');
    if (q && !num) {
      const ls = [...new Set(V.hits.map(x => x.line))];
      h += `<div class="grp">in the text: ${ls.length} line${ls.length === 1 ? '' : 's'}${ls.length > 200 ? ' (first 200)' : ''}</div>` + ls.slice(0, 200).map(l => { const t = V.lines[l - 1].replace(/\t/g, '  '), k = t.toLowerCase().indexOf(ql), a = Math.max(0, k - 10), lead = t.slice(a, k).replace(/^\s+/, '');
        return `<div class="s m" data-l="${l}" data-m="1"><span class="n">${l}</span>${a > 0 && lead ? '…' : ''}${esc(lead)}<em>${esc(t.slice(k, k + q.length))}</em>${esc(t.slice(k + q.length, k + 90))}</div>`; }).join('');
    }
    hint.textContent = num ? `go to line ${q} of ${V.lines.length}` : q ? `${syms.length} of ${V.out.length} names · ${V.hits.length} in the text` : V.out.length ? `${V.out.length} ${/\.md$/i.test(V.path) ? 'headings' : 'names'}${V.how === 'patterns' ? ' (found by patterns)' : ''}` : '';
    ol.innerHTML = h; V.sel = -1; V.listN = ol.querySelectorAll('.s').length; V.lastSec = null; follow(true);
  }
  function follow(force) {          // the entry you are in, marked in the list; the path into it in the title line
    if (!V.known) return;
    const top = topLine(), n = V.cur >= top && V.cur < top + shown() ? V.cur : top + 1, chain = chainAt(n), sec = chain[chain.length - 1];
    const ch = chain.map(s => `<b data-l="${s.line}" title="${esc(FVO.kindWords(s))}">${esc(s.name)}</b>`).join(' › '), c = chain.length ? '› ' + ch : '';
    if (crumbEl._h !== c) { crumbEl._h = c; crumbEl.innerHTML = c; }
    const id = sec ? sec.line : 0; if (!force && id === V.lastSec) return; V.lastSec = id;
    ol.querySelectorAll('.s.on').forEach(x => x.classList.remove('on'));
    const it = ol.querySelector(`.s:not(.m)[data-l="${id}"]`); if (it) { it.classList.add('on'); const r = it.offsetTop - ol.scrollTop; if (r < 0 || r > ol.clientHeight - 18) ol.scrollTop = it.offsetTop - ol.clientHeight / 3; }
  }

  /* ---------- the one box: a name filters, a number goes to the line, other text finds ---------- */
  function runFind(keep) {
    const q = V.q.trim(), ql = q.toLowerCase(), old = new Set(V.hits.map(h => h.line)), was = keep && V.hits[V.hi];
    V.hits = []; V.hi = -1;
    if (q && !/^\d+$/.test(q)) V.lines.forEach((t, i) => { const tl = t.toLowerCase(); let k = tl.indexOf(ql); while (k >= 0 && V.hits.length < 5000) { V.hits.push({ line: i + 1, at: k }); k = tl.indexOf(ql, k + ql.length); } });
    if (was) V.hi = V.hits.findIndex(h => h.line === was.line && h.at === was.at);
    const now = new Set(V.hits.map(h => h.line)); for (const l of new Set([...old, ...now])) paintRow(l);
    list(); drawStrip();
  }
  function paintRow(n) { const r = V.rows[n - 1]; if (r) r.children[3].innerHTML = tokHtml(r._toks, hitsOf(n)); }
  inp.addEventListener('input', () => { V.q = inp.value; V.qOrig = inp.value; runFind(); });
  inp.addEventListener('keydown', ev => {
    ev.stopPropagation();
    if (ev.key === 'Enter') { ev.preventDefault(); const q = inp.value.trim(); if (/^\d+$/.test(q)) { reveal(+q, null, true); return; } stepList(ev.shiftKey ? -1 : 1); return; }
    if (ev.key === 'Escape') { ev.preventDefault(); if (inp.value) { inp.value = ''; V.q = ''; runFind(); } else { box.focus({ preventScroll: true }); if (side._peek) showSide(false); } return; }
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 's') { ev.preventDefault(); H.save(); }
  });
  function stepList(d) {          // Enter in the box: the next entry of the list (names first, then the lines holding the text)
    const items = [...ol.querySelectorAll('.s')]; if (!items.length) return;
    V.sel = V.sel < 0 ? (d > 0 ? 0 : items.length - 1) : (V.sel + d + items.length) % items.length;
    items.forEach((x, i) => x.classList.toggle('sel', i === V.sel)); const it = items[V.sel];
    if (it.offsetTop < ol.scrollTop || it.offsetTop > ol.scrollTop + ol.clientHeight - 18) ol.scrollTop = it.offsetTop - ol.clientHeight / 3;
    pick(it, true);
  }
  function pick(it, keepFocus) {
    const l = +it.dataset.l;
    if (it.dataset.m) { const i = V.hits.findIndex(h => h.line === l); const prev = V.hits[V.hi]; V.hi = i; if (prev) paintRow(prev.line); paintRow(l); reveal(l, null, keepFocus); }
    else reveal(l, 'top', keepFocus);
    drawStrip();
  }
  ol.addEventListener('click', ev => { const it = ev.target.closest('.s'); if (it) { V.sel = [...ol.querySelectorAll('.s')].indexOf(it); ol.querySelectorAll('.s').forEach(x => x.classList.toggle('sel', x === it)); pick(it); } });
  crumbEl.addEventListener('click', ev => { const b = ev.target.closest('b[data-l]'); if (b) reveal(+b.dataset.l, 'top'); });
  function openBox() { if (!V.side || el.classList.contains('narrow')) showSide(true, true); inp.focus(); inp.select(); }
  function showSide(on, peek) {
    if (peek !== undefined) side._peek = !!(on && peek && (!V.side || el.classList.contains('narrow')));
    el.classList.toggle('noside', !(on || V.side) || (el.classList.contains('narrow') && !on));
    olBtn.classList.toggle('on', !el.classList.contains('noside')); layout();
  }

  /* ---------- where you are: current line, reveal, top line ---------- */
  function setCur(n) {
    n = clamp(n, 1, Math.max(1, V.lines.length)); if (V.rows[V.cur - 1]) V.rows[V.cur - 1].classList.remove('cur');
    V.cur = n; if (V.rows[n - 1]) V.rows[n - 1].classList.add('cur');
    lnEl.innerHTML = V.known ? `Ln <b>${n}</b> of ${V.lines.length}` : '';
    follow(); drawStrip();
  }
  function rowTop(n) { const r = V.rows[n - 1]; return r ? r.offsetTop : 0; }
  function reveal(n, where, keepFocus) {          // unfold what hides it, make it current, scroll it a third of the way down (or to the top)
    n = clamp(n, 1, V.lines.length); let ch = false;
    for (const l of [...V.folded]) { const s = V.at.get(l); if (s && n > s.line && n <= s.end) { V.folded.delete(l); ch = true; } }
    if (ch) applyFolds();
    setCur(n);
    box.scrollTop = Math.max(0, where === 'top' ? rowTop(n) - 4 : rowTop(n) - box.clientHeight / 3);
    if (!keepFocus && editable() && document.activeElement !== inp) { const off = offsetOf(n, 0); ta.focus({ preventScroll: true }); ta.setSelectionRange(off, off); }
    follow(true); drawStrip();
  }
  function topLine() {
    const y = box.scrollTop + 4; let lo = 0, hi = V.rows.length - 1; if (hi < 0) return 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; let k = m; while (k > lo && V.rows[k].classList.contains('hid')) k--; if (V.rows[k].offsetTop <= y) lo = m; else hi = m - 1; }
    while (lo > 0 && V.rows[lo].classList.contains('hid')) lo--;
    return lo + 1;
  }
  function lineAtY(y) {          // y in the code's own measure: the line under it (folded rows skipped)
    let lo = 0, hi = V.rows.length - 1; if (hi < 0) return 0;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; let k = m; while (k > lo && V.rows[k].classList.contains('hid')) k--; if (V.rows[k].offsetTop <= y) lo = m; else hi = m - 1; }
    return lo + 1;
  }
  const shown = () => Math.max(1, Math.floor(box.clientHeight / LH));
  const offsetOf = (n, col) => { let o = 0; for (let i = 0; i < n - 1 && i < V.lines.length; i++) o += V.lines[i].length + 1; return o + Math.min(col, (V.lines[n - 1] || '').length); };

  /* ---------- the textarea over the rows: same metrics; shown only while nothing is folded or opened out ---------- */
  function editable() {
    const on = V.known && V.md !== 'page' && !V.folded.size && !V.ed.open.size;
    const disp = on ? '' : 'none'; if (ta.style.display !== disp) ta.style.display = disp;
    ta.readOnly = V.ro; return on;
  }
  function layout() {          // the text's left edge, the textarea's size
    const r0 = V.rows[0]; if (!r0) return;
    V.textLeft = r0.children[3].offsetLeft; code.style.setProperty('--tl', V.textLeft + 'px');
    sizeTa(); placeHov(); drawStrip();
  }
  function sizeTa() {
    const r0 = V.rows[0], rl = V.rows[V.rows.length - 1]; if (!r0) return;
    const top = r0.offsetTop, h = rl.offsetTop + rl.offsetHeight - top;
    const w = box.classList.contains('wrap') ? r0.children[3].offsetWidth : Math.max(code.scrollWidth, box.clientWidth) - V.textLeft;
    const s = ta.style; const L = V.textLeft + 'px', T = top + 'px', Wd = w + 'px', Hh = h + 'px';
    if (s.left !== L) s.left = L; if (s.top !== T) s.top = T; if (s.width !== Wd) s.width = Wd; if (s.height !== Hh) s.height = Hh;
  }
  let chW = 0; const charW = () => { if (!chW) { const m = document.createElement('span'); m.textContent = 'x'.repeat(40); m.style.cssText = 'position:absolute;visibility:hidden;white-space:pre'; code.appendChild(m); chW = m.offsetWidth / 40; m.remove(); } return chW || 7; };
  function caretLine() {          // the caret's line becomes the current line, kept in view
    if (document.activeElement !== ta) return;
    const p = ta.selectionDirection === 'backward' ? ta.selectionStart : ta.selectionEnd, v = ta.value; let n = 1, k = -1;
    while ((k = v.indexOf('\n', k + 1)) >= 0 && k < p) n++;
    if (n !== V.cur) setCur(n);
    const r = V.rows[n - 1]; if (!r) return;
    const y = r.offsetTop, bh = box.clientHeight;
    if (y < box.scrollTop + 4) box.scrollTop = y - 4; else if (y + LH > box.scrollTop + bh - 4) box.scrollTop = y + LH - bh + 8;
    if (!box.classList.contains('wrap')) { const col = p - (v.lastIndexOf('\n', p - 1) + 1), x = V.textLeft + col * charW(), left = box.scrollLeft + V.textLeft;
      if (x < left + 8) box.scrollLeft = Math.max(0, x - V.textLeft - 40); else if (x > box.scrollLeft + box.clientWidth - 30) box.scrollLeft = x - box.clientWidth + 60; }
  }
  ta.addEventListener('input', () => { if (V.known && !V.ro) onTyped(); });
  document.addEventListener('selectionchange', () => { if (document.activeElement === ta) caretLine(); });
  ta.addEventListener('keydown', ev => {
    ev.stopPropagation();          // the page's own keys never see what is typed here
    const mod = ev.metaKey || ev.ctrlKey, k = ev.key.toLowerCase();
    if (mod && k === 's') { ev.preventDefault(); H.save(); return; }
    if ((mod && k === 'f') || (ev.ctrlKey && k === 'g')) { ev.preventDefault(); openBox(); return; }
    if (ev.key === 'Escape') { ev.preventDefault(); H.close(); return; }
    if (ev.key === 'Tab' && !mod) { ev.preventDefault(); document.execCommand('insertText', false, ev.shiftKey ? '' : '\t'); }
  });
  // the read state (a fold or removed lines open): a click on a line closes them and puts the cursor where it was clicked
  box.addEventListener('mousedown', ev => {
    const cut = ev.target.closest('.cut span'); if (cut) { ev.preventDefault(); V.ed.open.add(cut.dataset.cut); if (ta === document.activeElement) ta.blur(); paintTints(); editable(); layout(); return; }
    const r = ev.target.closest('.r'); if (r && r.classList.contains('del')) { ev.preventDefault(); return; }
    if (r && (ev.target.classList.contains('fd') && ev.target.textContent || ev.target.classList.contains('fp'))) { ev.preventDefault(); toggleFold(V.rows.indexOf(r) + 1); return; }
    if (ev.target === ta) return;          // the textarea takes its own clicks
    if (!V.known) return;
    if (r && (ev.target.closest('.g') || ev.target.closest('.e') || ev.target.closest('.fd'))) { ev.preventDefault(); setCur(V.rows.indexOf(r) + 1); box.focus({ preventScroll: true }); return; }
    let n, col = 0;
    if (r) { n = V.rows.indexOf(r) + 1; const cp = document.caretRangeFromPoint ? document.caretRangeFromPoint(ev.clientX, ev.clientY) : null, t = r.children[3];
      if (cp && t.contains(cp.startContainer)) { const rg = document.createRange(); rg.setStart(t, 0); rg.setEnd(cp.startContainer, cp.startOffset); col = rg.toString().length; } else col = (V.lines[n - 1] || '').length; }
    else { n = V.lines.length; col = (V.lines[n - 1] || '').length; }          // below the last line: its end
    ev.preventDefault();
    const top = box.scrollTop, y0 = r ? r.offsetTop - top : 0;
    if (V.folded.size || V.ed.open.size) { V.folded.clear(); V.ed.open.clear(); applyFolds(); paintTints(); }
    if (!editable()) { setCur(n); box.focus({ preventScroll: true }); return; }
    if (r) box.scrollTop = Math.max(0, V.rows[n - 1].offsetTop - y0);          // the clicked line stays under the pointer
    const off = offsetOf(n, col); ta.focus({ preventScroll: true }); ta.setSelectionRange(off, off); caretLine();
  });

  /* ---------- agents' edits: placed in the text as it is now (edits.html take 2, its placing code) ---------- */
  function lcs(a, b) {
    const n = a.length, m = b.length, W = m + 1, T = new Uint16Array((n + 1) * W);
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) T[i * W + j] = a[i] === b[j] ? T[(i + 1) * W + j + 1] + 1 : Math.max(T[(i + 1) * W + j], T[i * W + j + 1]);
    const out = []; let i = 0, j = 0;
    while (i < n && j < m) { if (a[i] === b[j]) { out.push([i, j]); i++; j++; } else if (T[(i + 1) * W + j] >= T[i * W + j + 1]) i++; else j++; }
    return out;
  }
  function ops(oldL, newL) {
    const pr = lcs(oldL.map(s => s.trimEnd()), newL.map(s => s.trimEnd())), out = []; let i = 0, j = 0;
    for (const [pi, pj] of pr.concat([[oldL.length, newL.length]])) {
      while (i < pi) out.push({ k: '-', text: oldL[i++] });
      while (j < pj) out.push({ k: '+', n: j++ });
      if (i < oldL.length && j < newL.length) { out.push({ k: '=', n: j }); i++; j++; }
    }
    return out;
  }
  function place(newL, file, taken) {
    const F = file.map(s => s.trimEnd()), N = newL.map(s => s.trimEnd()), map = Array(N.length).fill(-1);
    if (!N.length) return map;
    if (N.length <= 80) {
      let best = -1, bestFree = -1;
      for (let s = 0; s + N.length <= F.length; s++) {
        let k = 0; while (k < N.length && F[s + k] === N[k]) k++;
        if (k === N.length) { let free = 0; for (let q = 0; q < k; q++) if (!taken[s + q]) free++; if (free > bestFree) { best = s; bestFree = free; } }
      }
      if (best >= 0) { for (let k = 0; k < N.length; k++) map[k] = best + k; return map; }
      const key = N.findIndex(s => s.trim().length > 12 && F.indexOf(s) >= 0);
      if (key < 0) return map;
      const at = F.indexOf(N[key]), lo = Math.max(0, at - key - 10), hi = Math.min(F.length, at + N.length + 10);
      lcs(N, F.slice(lo, hi)).forEach(([i, j]) => { if (N[i].trim().length > 3) map[i] = lo + j; });
      return map;
    }
    lcs(N, F).forEach(([i, j]) => { map[i] = j; });
    return map;
  }
  function attribute(file, raw) {
    const owner = Array(file.length).fill(null), changes = [];
    raw.slice().sort((x, y) => y.t - x.t).forEach((r, idx) => {
      const oldL = r.lines.filter(l => l[0] !== '+').map(l => l.slice(1)), newL = r.lines.filter(l => l[0] !== '-').map(l => l.slice(1));
      const map = place(newL, file, owner), op = ops(oldL, newL);
      const c = { id: 'c' + idx, agent: r.agent, t: r.t, kind: r.kind, added: 0, removed: 0, lines: [], dels: [], placed: 0, lost: 0 };
      let pending = [], last = -1;
      const flush = at => { if (pending.length) { c.dels.push({ at, lines: pending }); pending = []; } };
      for (const o of op) {
        if (o.k === '-') { c.removed++; pending.push(o.text); continue; }
        const f = map[o.n];
        if (f >= 0) { flush(f); last = f; }
        if (o.k === '+') { c.added++; if (f >= 0 && !owner[f]) { owner[f] = c; c.lines.push(f); c.placed++; } else c.lost++; }
      }
      if (pending.length) flush(last >= 0 ? last + 1 : -1);
      c.lines.sort((a, b) => a - b);
      c.first = c.lines.length ? c.lines[0] : (c.dels.find(d => d.at >= 0) || {}).at ?? -1;
      c.last = c.lines.length ? c.lines[c.lines.length - 1] : c.first;
      changes.push(c);
    });
    changes.sort((x, y) => x.t - y.t); changes.forEach((c, i) => { c.n = i; });
    return { changes, owner };
  }
  async function fetchEdits() {          // every agent on this floor that wrote the file: its own recorded edits to it
    const path = V.path, ws = H.writers(path), v = ++V.ed.v;
    if (!ws.length) { V.ed.raw = []; placeEdits(); return; }
    const got = await Promise.all(ws.map(w => H.api('edits?id=' + encodeURIComponent(w.sid)).catch(() => null)));
    if (v !== V.ed.v || path !== V.path) return;
    const raw = []; let failed = 0;
    got.forEach((d, i) => { if (!d) { failed++; return; } const w = ws[i];
      (d.files || []).forEach(f => { const full = w.home ? w.home.replace(/\/+$/, '') + '/' + f.path : f.path; if (full !== path && f.path !== path) return;
        (f.edits || []).forEach(e => (e.hunks || []).forEach(h => raw.push({ agent: w, t: e.t, kind: e.kind, lines: h.lines || [] }))); }); });
    V.ed.raw = raw; V.ed.err = failed ? failed + ' agent' + (failed > 1 ? 's\'' : '\'s') + ' edits could not be read' : '';
    placeEdits();
  }
  function placeEdits() {
    const raw = V.ed.raw; if (!raw || !V.known) { V.ed.changes = []; V.ed.owner = []; paintTints(); return; }
    const foc = V.ed.changes[V.ed.foc];
    const r = attribute(V.lines, raw); V.ed.changes = r.changes; V.ed.owner = r.owner;
    V.ed.t0 = Math.min(...r.changes.map(c => c.t)); V.ed.t1 = Math.max(...r.changes.map(c => c.t));
    V.ed.foc = foc ? r.changes.findIndex(c => c.agent.sid === foc.agent.sid && c.t === foc.t) : -1;
    if (V.ed.foc >= 0 && order().indexOf(r.changes[V.ed.foc]) < 0) V.ed.foc = -1;
    paintTints(); H.wake();
  }
  const alphaOf = c => V.ed.t1 === V.ed.t0 ? 1 : 0.35 + 0.65 * (c.t - V.ed.t0) / (V.ed.t1 - V.ed.t0);          // newest change in the file 1, oldest 0.35
  const order = () => V.ed.changes.filter(c => c.first >= 0).sort((x, y) => x.first - y.first || x.t - y.t);
  function paintTints() {          // each changed line's ground and mark in its agent's colour; the cut marks; opened removed lines
    const own = V.ed.owner, hl = V.ed.hov || (V.ed.foc >= 0 && V.ed.changes[V.ed.foc] ? V.ed.changes[V.ed.foc].id : null);
    V.rows.forEach((r, i) => {
      const c = own[i], a = c ? alphaOf(c) : 0, strong = c && c.id === hl;
      const bg = c ? rgba(c.agent.col, strong ? .24 : .15 * a) : '', mk = c ? rgba(c.agent.col, strong ? 1 : a) : '';
      if (r._bg !== bg) { r._bg = bg; r.style.backgroundColor = bg; } const e = r.children[2]; if (e._mk !== mk) { e._mk = mk; e.style.background = mk || '#fff'; }
      r._ch = c ? c.id : null;
    });
    code.querySelectorAll('.cut, .r.del').forEach(x => x.remove());
    for (const c of V.ed.changes) for (const d of c.dels) {
      if (d.at < 0) continue; const key = c.id + ':' + d.at, before = V.rows[d.at] || null, col = c.agent.col;
      if (V.ed.open.has(key)) { d.lines.forEach(t => { const x = document.createElement('div'); x.className = 'r del'; x.style.backgroundColor = rgba(col, .07);
          x.innerHTML = `<span class="g"></span><span class="fd"></span><span class="e" style="background:${rgba(col, .35)}"></span><span class="t">${esc(t)}</span>`; x.dataset.ch = c.id; code.insertBefore(x, before); }); }
      else { const x = document.createElement('div'); x.className = 'cut'; x.style.setProperty('--cc', rgba(col, .55));
        x.innerHTML = `<span data-cut="${esc(key)}" style="color:${col};border-color:${rgba(col, .5)}" title="${esc(c.agent.name)} took out ${d.lines.length} line${d.lines.length > 1 ? 's' : ''} here ${ago(c.t)}; click to show them">− ${d.lines.length}</span>`;
        code.insertBefore(x, before); }
    }
    placeHov(); chgFoot(); drawStrip(); sizeTa();
  }
  const ago = t => { const s = H.now() - t; if (s < 45) return 'just now'; if (s < 3600) return Math.round(s / 60) + ' min ago'; if (s < 86400) return Math.round(s / 3600) + ' h ago'; const d = Math.round(s / 86400); return d + (d === 1 ? ' day ago' : ' days ago'); };
  const kindWord = c => c.kind === 'add' ? 'created the file' : c.kind === 'replace' ? 'wrote the whole file' : 'edited';
  const span = c => c.first < 0 ? 'no longer in the file' : c.first === c.last ? 'line ' + (c.first + 1) : 'lines ' + (c.first + 1) + '–' + (c.last + 1);
  function placeHov() {          // the change's title above its first line: on hover, else the change jumped to
    const id = V.ed.hov || (V.ed.foc >= 0 && V.ed.changes[V.ed.foc] ? V.ed.changes[V.ed.foc].id : null), c = id && V.ed.changes.find(x => x.id === id);
    if (!c || c.first < 0 || !V.rows[c.first] || V.rows[c.first].classList.contains('hid')) { hov.style.display = 'none'; return; }
    const h = `<span class="dot" style="background:${c.agent.col}"></span><b>${esc(c.agent.name)}</b> · ${esc(ago(c.t))} <span class="mut">· ${kindWord(c)} · ${span(c)} ·</span> <span style="color:${c.agent.col}">+${c.added}</span> <span class="mut">−${c.removed}</span>`;
    if (hov._h !== h) { hov._h = h; hov.innerHTML = h; } hov.style.display = 'flex'; hov.style.borderColor = rgba(c.agent.col, .6);
    hov.style.left = (V.textLeft + box.scrollLeft) + 'px'; hov.style.top = V.rows[c.first].offsetTop + 'px';
  }
  function setHov(id) { if (id === V.ed.hov) return; V.ed.hov = id; paintTints(); }
  box.addEventListener('mousemove', ev => {
    if (!V.ed.changes.length) return;
    const rb = box.getBoundingClientRect(), y = ev.clientY - rb.top + box.scrollTop, n = lineAtY(y), r = ev.target.closest && ev.target.closest('.r.del');
    setHov(r ? r.dataset.ch : (V.ed.owner[n - 1] ? V.ed.owner[n - 1].id : null));
  });
  box.addEventListener('mouseleave', () => setHov(null));
  function stepChange(d) {
    const o = order(); if (!o.length) return; const cur = V.ed.changes[V.ed.foc], k0 = o.indexOf(cur);
    const k = k0 < 0 ? (d > 0 ? Math.max(0, o.findIndex(c => c.first + 1 >= V.cur)) : (() => { let j = -1; o.forEach((c, i) => { if (c.first + 1 <= V.cur) j = i; }); return j < 0 ? o.length - 1 : j; })()) : (k0 + d + o.length) % o.length;
    jumpChange(o[k]);
  }
  function jumpChange(c) {
    V.ed.foc = V.ed.changes.indexOf(c); V.ed.hov = null;
    for (const l of [...V.folded]) { const s = V.at.get(l); if (s && c.first + 1 > s.line && c.first + 1 <= s.end) V.folded.delete(l); } applyFolds();
    setCur(c.first + 1); box.scrollTop = Math.max(0, rowTop(c.first + 1) - 60); paintTints();
  }
  function chgFoot() {
    const o = order(), cur = V.ed.changes[V.ed.foc], k = cur ? o.indexOf(cur) : -1, who = [...new Set(V.ed.changes.map(c => c.agent))];
    const h = !o.length ? (V.ed.err ? esc(V.ed.err) : '') : `<span title="${esc(who.map(a => a.name).join(', '))}">${who.map(a => `<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${a.col};margin-right:2px"></span>`).join('')} change ${k >= 0 ? k + 1 : '–'} of ${o.length}</span><button type="button" data-d="-1" title="previous change (p)">↑</button><button type="button" data-d="1" title="next change (n)">↓</button>`;
    if (chgEl._h !== h) { chgEl._h = h; chgEl.innerHTML = h; }
  }
  chgEl.addEventListener('click', ev => { const b = ev.target.closest('button[data-d]'); if (b) stepChange(+b.dataset.d); });

  /* ---------- the strip: the whole file at the right edge: where you are, find hits, agents' changes ---------- */
  function drawStrip() {
    const n = V.lines.length; if (!n || !strip.clientHeight) return;
    const dpr = devicePixelRatio || 1, W = strip.clientWidth, Hh = strip.clientHeight; if (strip.width !== Math.round(W * dpr) || strip.height !== Math.round(Hh * dpr)) { strip.width = Math.round(W * dpr); strip.height = Math.round(Hh * dpr); }
    const c = strip.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, W, Hh); const y = l => (l - 1) / n * Hh;
    c.fillStyle = '#e5e0d6'; for (let py = 0; py < Hh; py += 2) { const l = Math.floor(py / Hh * n), len = Math.min(80, (V.lines[l] || '').length); if (len) c.fillRect(5, py, Math.max(1, len / 80 * 7), 1); }
    const sh = box.scrollHeight || 1, top = box.scrollTop / sh * Hh, hh = Math.max(12, box.clientHeight / sh * Hh);
    c.fillStyle = 'rgba(44,52,64,.08)'; c.fillRect(0, top, W, hh); c.strokeStyle = 'rgba(44,52,64,.25)'; c.strokeRect(0.5, top + 0.5, W - 1, hh - 1);
    for (const ch of V.ed.changes) if (ch.first >= 0) { c.fillStyle = rgba(ch.agent.col, V.ed.changes[V.ed.foc] === ch ? 1 : alphaOf(ch) * .9); c.fillRect(0, y(ch.first + 1), 3, Math.max(2, (ch.last - ch.first + 1) / n * Hh)); }
    c.fillStyle = '#d8a634'; for (const h of V.hits) c.fillRect(4, y(h.line), W - 6, 2);
    c.fillStyle = '#2c3440'; c.fillRect(1, y(V.cur), W - 2, 2);
  }
  let sDrag = false;
  const stripTo = ev => { const r = strip.getBoundingClientRect(); box.scrollTop = (ev.clientY - r.top) / r.height * box.scrollHeight - box.clientHeight / 2; };
  strip.addEventListener('mousedown', ev => {
    ev.preventDefault(); const r = strip.getBoundingClientRect(), n = V.lines.length, y = ev.clientY - r.top;
    const hit = ev.clientX - r.left < 7 && order().find(c => { const a = c.first / n * r.height, b = (c.last + 1) / n * r.height; return y >= a - 3 && y <= Math.max(b, a + 2) + 3; });          // a change's mark: jump to it
    if (hit) { jumpChange(hit); return; }
    sDrag = true; stripTo(ev);
  });
  addEventListener('mousemove', ev => { if (sDrag) stripTo(ev); }); addEventListener('mouseup', () => { sDrag = false; });
  box.addEventListener('scroll', () => { requestAnimationFrame(() => { drawStrip(); follow(); placeHov(); }); });

  /* ---------- keys on the window outside the text and the box: p / n between changes, ↑ ↓ lines, ⌘F, Ctrl G, ⌘S, Esc ---------- */
  el.addEventListener('keydown', ev => {
    if (ev.target === ta || ev.target === inp) return;
    const mod = ev.metaKey || ev.ctrlKey, k = ev.key.toLowerCase();
    if (/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(ev.target.tagName) && ev.key !== 'Escape') return;
    ev.stopPropagation();
    if ((mod && k === 'f') || (ev.ctrlKey && k === 'g')) { ev.preventDefault(); openBox(); }
    else if (mod && k === 's') { ev.preventDefault(); H.save(); }
    else if (ev.key === 'Escape') { ev.preventDefault(); H.close(); }
    else if (!mod && !ev.altKey && (k === 'n' || k === 'p')) { ev.preventDefault(); stepChange(k === 'n' ? 1 : -1); }
    else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); let n = V.cur; const d = ev.key === 'ArrowDown' ? 1 : -1; do n += d; while (V.rows[n - 1] && V.rows[n - 1].classList.contains('hid'));
      if (V.rows[n - 1]) { setCur(n); const y = rowTop(n); if (y < box.scrollTop + 10 || y > box.scrollTop + box.clientHeight - 30) box.scrollTop = y - box.clientHeight / 3; } }
  });
  el.addEventListener('mousedown', ev => { if (!ev.target.closest('textarea, input, button, .fvo, .fvx, canvas, .grip') && !ev.target.closest('.hd')) box.focus({ preventScroll: true }); });

  /* ---------- head buttons ---------- */
  wrBtn.addEventListener('click', ev => { ev.currentTarget.blur(); V.wrap = !V.wrap; keep('wrap', V.wrap); setWrap(); });
  function setWrap() { const top = topLine(); box.classList.toggle('wrap', V.wrap); wrBtn.classList.toggle('on', V.wrap); ta.setAttribute('wrap', V.wrap ? 'soft' : 'off'); layout(); box.scrollTop = Math.max(0, rowTop(top) - 4); }
  olBtn.addEventListener('click', ev => { ev.currentTarget.blur(); if (el.classList.contains('narrow')) { showSide(el.classList.contains('noside'), true); return; } V.side = !V.side; keep('side', V.side); showSide(V.side); });
  mdmEl.addEventListener('click', ev => { const b = ev.target.closest('button[data-m]'); if (!b) return; b.blur(); V.md = b.dataset.m; mdMode(); });
  lnEl.addEventListener('click', () => openBox());
  function mdMode() {
    const isMd = V.lang === 'markdown';
    const h = isMd ? [['text', 'text'], ['styled', 'styled'], ['page', 'page']].map(([m, w]) => `<button type="button" class="btn${V.md === m ? ' on' : ''}" data-m="${m}" title="${m === 'page' ? 'read it as a page (not editable)' : m === 'styled' ? 'the file, its headings and code set apart' : 'the file, coloured'}">${w}</button>`).join('') : '';
    if (mdmEl._h !== h) { mdmEl._h = h; mdmEl.innerHTML = h; }
    box.classList.toggle('styled', isMd && V.md === 'styled');
    let pg = box.querySelector('.mdv');
    if (isMd && V.md === 'page') { if (!pg) { pg = document.createElement('div'); pg.className = 'mdv'; box.appendChild(pg); } pg.innerHTML = H.md(V.text); code.style.display = 'none'; hov.style.display = 'none'; if (ta === document.activeElement) ta.blur(); }
    else { if (pg) pg.remove(); code.style.display = ''; }
    editable(); layout();
  }

  /* ---------- the title's second row and the foot ---------- */
  function meta() {
    const langName = typeof SC === 'object' ? SC.NAMES[V.lang] || 'Text' : 'Text';
    const how = V.how === 'tree-sitter' ? ' · outline by Tree-sitter' : V.how === 'patterns' ? ' · outline by patterns' + (V.howNote ? ' (the parser did not load)' : '') : '';
    metaEl.textContent = V.known ? `${langName} · ${V.lines.length} line${V.lines.length === 1 ? '' : 's'}${how}` : '';
  }
  function pathHtml(p) { const k = p.lastIndexOf('/'); return `<bdi><i>${esc(p.slice(0, k + 1))}</i><b>${esc(p.slice(k + 1))}</b></bdi>`; }

  /* ---------- the page's calls ---------- */
  function open(path, line) {
    V.path = path; V.text = null; V.known = false; V.lines = []; V.rows = []; V.out = []; V.at = new Map(); V.how = ''; V.outV++; V.cur = 1; V.pendingLine = line || null;
    V.ed = { changes: [], owner: [], sig: '', v: V.ed.v + 1, foc: -1, hov: null, open: new Set(), t0: 0, t1: 0, raw: null, err: '' };
    V.q = ''; inp.value = ''; V.hits = []; V.hi = -1; V.md = 'styled'; V.lastSec = null; mdmEl._h = null; crumbEl._h = null; crumbEl.innerHTML = ''; hov.style.display = 'none';
    code.textContent = ''; ol.innerHTML = ''; hint.textContent = ''; chgEl._h = null; chgEl.innerHTML = ''; lnEl.innerHTML = ''; metaEl.textContent = '';
    const pg = box.querySelector('.mdv'); if (pg) pg.remove(); code.style.display = '';
    pathEl.innerHTML = pathHtml(path); pathEl.title = path;
    box.scrollTop = 0; box.scrollLeft = 0; ta.value = ''; side._peek = false;
    setWrap(); showSide(V.side);
  }
  // each frame: the file's text from the store (a new text redrawn unless you are typing in it), its state words, the writers
  function sync(o) {
    if (o.path !== V.path) open(o.path, o.line);
    el.classList.toggle('narrow', el.clientWidth < 600); if (el.classList.contains('narrow') && !side._peek && !el.classList.contains('noside')) showSide(false);
    if (!el.classList.contains('narrow') && el._wasNarrow) showSide(V.side); el._wasNarrow = el.classList.contains('narrow');
    V.ro = !o.known; waitEl.textContent = o.known ? '' : o.placeholder || '';
    if (o.known && o.text !== V.text && document.activeElement !== ta) {
      const first = !V.known, top = box.scrollTop; V.known = true; V.text = o.text; ta.value = o.text; renderAll(); mdMode(); reOutline(); meta(); setCur(first ? 1 : Math.min(V.cur, V.lines.length));
      if (!first) box.scrollTop = top; else fetchEdits();
      if (V.q) runFind(true); else list();
      if (V.ed.raw) placeEdits();
      if (first && V.pendingLine) { const n = clamp(V.pendingLine, 1, V.lines.length); reveal(n, null, true); if (editable()) { const a = offsetOf(n, 0); ta.focus({ preventScroll: true }); ta.setSelectionRange(a, a + V.lines[n - 1].length); } H.atLine(n); }
    }
    if (!o.known && V.known) { V.known = false; }
    editable();
    if (stEl.textContent !== o.state) stEl.textContent = o.state; stEl.classList.toggle('err', !!o.err);
    const w = o.who || ''; if (whoEl._h !== w) { whoEl._h = w; whoEl.innerHTML = w; }
    saveBtn.style.display = o.edited ? '' : 'none';
    if (V.known && o.writeSig !== V.ed.sig) { const was = V.ed.sig; V.ed.sig = o.writeSig; if (was) { H.reload(); } fetchEdits(); }          // an agent wrote the file meanwhile: read it again, place its edits again
    if (V.known && V.ed.changes.length && performance.now() - (V.agoAt || 0) > 30000) { V.agoAt = performance.now(); hov._h = null; placeHov(); }
    if (V.known && (el._w !== el.clientWidth || el._h !== el.clientHeight)) { el._w = el.clientWidth; el._h = el.clientHeight; layout(); }
  }
  // drivers: what the window shows
  function state() {
    if (!V.path) return null;
    return { lines: V.lines.length, cur: V.cur, top: topLine(), wrap: V.wrap, side: !el.classList.contains('noside'), md: V.lang === 'markdown' ? V.md : null, lang: V.lang,
      editable: ta.style.display !== 'none' && !ta.readOnly, typing: document.activeElement === ta, folded: [...V.folded], crumb: crumbEl.textContent,
      outline: { how: V.how, n: V.out.length, kinds: V.out.reduce((m, s) => (m[s.kind] = (m[s.kind] || 0) + 1, m), {}), first: V.out.slice(0, 6).map(s => s.kind + ' ' + s.name), shown: ol.querySelectorAll('.s:not(.m)').length },
      find: { q: V.q, hits: V.hits.length }, changes: V.ed.changes.map(c => ({ agent: c.agent.name, col: c.agent.col, t: c.t, first: c.first + 1, last: c.last + 1, added: c.added, removed: c.removed, placed: c.placed, dels: c.dels.length })),
      tinted: V.ed.owner.filter(Boolean).length, cuts: code.querySelectorAll('.cut').length, focusChange: V.ed.foc >= 0 ? V.ed.foc : null, title: hov.style.display === 'none' ? null : hov.textContent,
      taAligned: (() => { const r = V.rows[Math.min(V.rows.length - 1, 9)]; return r ? Math.round(ta.offsetTop + (Math.min(V.rows.length - 1, 9)) * LH - r.offsetTop) : null; })() };
  }
  return { sync, state, box, ta, input: inp, strip, outlineEl: ol, stepChange, reveal, get cur() { return V.cur; }, get scroll() { return box.scrollTop; } };
}
