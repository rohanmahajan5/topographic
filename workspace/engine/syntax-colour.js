// Text colouring for the file window: a small hand-written tokenizer, one line at a time (moved here from the colouring concept,
// concepts-files/syntax-colour.js on branch ide-pub-fileview-concepts-2026-11-22; its take B, the warm full colour, was picked).
// Written for this project, no outside code. Each language turns one line into [class, text] pieces and hands a state to the
// next line (an open triple-quoted string, a block comment, a Markdown code fence, a YAML block, a shell here-document), so a
// changed line can be re-coloured from there on and every line stays its own row for line numbers and agents' line tints.
// Classes: kw keyword, bi built-in name, slf self/this, ty type name, tyd type being defined, fnd function being defined,
// call called name, str string, doc docstring, num number, com comment, dec decorator/attribute, prop key, op operator,
// pun punctuation, var shell variable, opt command option, sv plain YAML value; Markdown: mdh heading, mdmark the marks
// themselves, mdb bold, mdi italic, mdcode code, mdlink link text, mdurl link target, mdlist list marker, mdq quote.
// A line's own class (lineCls): imp import line; mh1..mh3 heading; mcode inside a fence; mq quote.
const SC = (() => {
  const W = s => new Set(s.split(/\s+/).filter(Boolean));
  const NUM = /0[xX][\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?[jJnLlfF]?/y;
  const WORD = /[A-Za-z_$][\w$]*/y, OP = /[+\-*/%=<>!&|^~?:@]+/y;
  const findClose = (line, k, close, esc) => {
    while (k < line.length) { if (esc && line[k] === '\\') { k += 2; continue; } if (line.startsWith(close, k)) return k; k++; }
    return -1;
  };
  const pusher = toks => (c, t) => { if (!t) return; const l = toks[toks.length - 1]; if (l && l[0] === c) l[1] += t; else toks.push([c, t]); };

  // Python, Swift, JS/TS and JSON share one scanner, set up per language
  function clike(cfg) {
    return (line, st) => {
      const toks = [], push0 = pusher(toks); let i = 0, lastWord = '', prev = '';
      const push = (c, t) => { push0(c, t); if (t && t.trim()) prev = c === 'pun' || c === 'op' ? t.trim().slice(-1) : c || 'id'; };
      const lineCls = cfg.imp && cfg.imp.test(line) ? 'imp' : '';
      if (st) {
        const j = findClose(line, 0, st.close, st.esc);
        if (j < 0) { push(st.cls, line); return { toks, st, lineCls }; }
        push(st.cls, line.slice(0, j + st.close.length)); i = j + st.close.length; st = null;
      }
      while (i < line.length) {
        const c = line[i];
        if (c === ' ' || c === '\t') { let j = i; while (j < line.length && (line[j] === ' ' || line[j] === '\t')) j++; push('', line.slice(i, j)); i = j; continue; }
        if (cfg.lc && cfg.lc.some(p => line.startsWith(p, i))) { push('com', line.slice(i)); break; }
        if (cfg.bc && line.startsWith(cfg.bc[0], i)) {
          const j = line.indexOf(cfg.bc[1], i + cfg.bc[0].length);
          if (j < 0) { push('com', line.slice(i)); st = { close: cfg.bc[1], cls: 'com' }; break; }
          push('com', line.slice(i, j + cfg.bc[1].length)); i = j + cfg.bc[1].length; continue;
        }
        let s = null, open = '';
        for (const x of cfg.str) { x.re.lastIndex = i; const m = x.re.exec(line); if (m) { s = x; open = m[0]; break; } }
        if (s) {
          const cls = s.doc && !line.slice(0, i).trim() ? 'doc' : 'str';
          const j = findClose(line, i + open.length, s.close, s.esc);
          if (j < 0) { push(cls, line.slice(i)); if (s.ml) st = { close: s.close, cls, esc: s.esc }; break; }
          const tok = line.slice(i, j + s.close.length); i = j + s.close.length;
          push(cfg.keys && /^\s*:/.test(line.slice(i)) ? 'prop' : cls, tok); lastWord = ''; continue;
        }
        if (cfg.regex && c === '/' && line[i + 1] !== '/' && line[i + 1] !== '*' && !/^[)\]]$|^(id|num|str|slf|call|bi|ty)$/.test(prev)) {          // a JS regex literal
          let k = i + 1, cl = false;
          while (k < line.length && (cl || line[k] !== '/')) { if (line[k] === '\\') k++; else if (line[k] === '[') cl = true; else if (line[k] === ']') cl = false; k++; }
          if (k < line.length) { while (/[a-z]/.test(line[k + 1] || '')) k++; push('str', line.slice(i, k + 1)); i = k + 1; lastWord = ''; continue; }
        }
        if (cfg.dec) { cfg.dec.lastIndex = i; const m = cfg.dec.exec(line); if (m) { push('dec', m[0]); i += m[0].length; continue; } }
        if ((c >= '0' && c <= '9') || (c === '.' && /\d/.test(line[i + 1] || ''))) { NUM.lastIndex = i; const m = NUM.exec(line); if (m && m[0]) { push('num', m[0]); i += m[0].length; lastWord = ''; continue; } }
        WORD.lastIndex = i; const m = WORD.exec(line);
        if (m) {
          const w = m[0]; i += w.length; let cls = '';
          if (lastWord && cfg.defs[lastWord]) cls = cfg.defs[lastWord];
          else if (cfg.arrow && /^(const|let|var)$/.test(lastWord) && cfg.arrow.test(line.slice(i))) cls = 'fnd';          // const f = (…) =>
          else if (cfg.kw.has(w)) cls = 'kw';
          else if (cfg.slf && cfg.slf.has(w)) cls = 'slf';
          else if (cfg.bi.has(w)) cls = 'bi';
          else if (/^\s*\(/.test(line.slice(i, i + 8))) cls = 'call';
          else if (cfg.caps && /^[A-Z]/.test(w) && /[a-z]/.test(w)) cls = 'ty';          // ROOT, SKIP: constants, not types
          push(cls, w); lastWord = w; continue;
        }
        if ('()[]{},;.'.includes(c)) { push('pun', c); i++; lastWord = ''; continue; }
        OP.lastIndex = i; const o = OP.exec(line);
        if (o) { push('op', o[0]); i += o[0].length; lastWord = ''; continue; }
        push('', c); i++;
      }
      return { toks, st, lineCls };
    };
  }
  const PYQ = p => [{ re: new RegExp(p + '"""', 'y'), close: '"""', ml: true, esc: true, doc: true }, { re: new RegExp(p + "'''", 'y'), close: "'''", ml: true, esc: true, doc: true },
    { re: new RegExp(p + '"', 'y'), close: '"', esc: true }, { re: new RegExp(p + "'", 'y'), close: "'", esc: true }];
  const python = clike({
    lc: ['#'], str: PYQ('[rRbBfFuU]{0,2}'), dec: /@[A-Za-z_][\w.]*/y, imp: /^\s*(import|from)\s/,
    kw: W('and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case'),
    bi: W('True False None print len range str int float dict list set tuple isinstance open enumerate zip sorted any all min max sum map filter super type object Exception ValueError KeyError TypeError RuntimeError OSError hasattr getattr setattr iter next repr bool bytes round abs Path'),
    slf: W('self cls'), defs: { def: 'fnd', class: 'tyd' }, caps: true,
  });
  const swift = clike({
    lc: ['//'], bc: ['/*', '*/'], dec: /[@#][A-Za-z_]\w*/y, imp: /^\s*import\s/, caps: true,
    str: [{ re: /#?"""/y, close: '"""', ml: true, esc: true }, { re: /#?"/y, close: '"', esc: true }],
    kw: W('associatedtype class deinit enum extension fileprivate func import init inout internal let open operator private precedencegroup protocol public rethrows static struct subscript typealias var break case catch continue default defer do else fallthrough for guard if in repeat return throw switch where while as is try throws async await some any final lazy mutating nonisolated override required weak unowned convenience dynamic indirect optional get set willSet didSet actor macro consuming borrowing'),
    bi: W('nil true false super print'), slf: W('self Self'),
    defs: { func: 'fnd', class: 'tyd', struct: 'tyd', enum: 'tyd', protocol: 'tyd', extension: 'tyd', actor: 'tyd', typealias: 'tyd' },
  });
  const js = clike({
    lc: ['//'], bc: ['/*', '*/'], regex: true, arrow: /^\s*=\s*(async\s+)?(\([^)]*\)|[\w$]+)\s*=>|^\s*=\s*(async\s+)?function\b/, dec: /@[A-Za-z_]\w*/y, imp: /^\s*import\s|^\s*(const|let|var)\s+\w+\s*=\s*require\(/, caps: true,
    str: [{ re: /`/y, close: '`', ml: true, esc: true }, { re: /"/y, close: '"', esc: true }, { re: /'/y, close: "'", esc: true }],
    kw: W('break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new return switch throw try typeof var void while with yield async await of static get set from as interface type enum implements private public protected readonly declare namespace abstract keyof satisfies'),
    bi: W('true false null undefined NaN Infinity console document window Math JSON Object Array Promise Map Set String Number Date RegExp Error'),
    slf: W('this super'), defs: { function: 'fnd', class: 'tyd', interface: 'tyd', type: 'tyd', enum: 'tyd' },
  });
  const json = clike({ str: [{ re: /"/y, close: '"', esc: true }], keys: true, kw: W(''), bi: W('true false null'), defs: {} });

  // YAML: keys, values, comments, anchors, and block text (| or >) that runs while lines are indented deeper than its key
  function yaml(line, st) {
    const toks = [], push = pusher(toks); let i = 0;
    const ind = line.match(/^\s*/)[0].length;
    if (st && st.blk != null) { if (!line.trim() || ind > st.blk) { push('str', line); return { toks, st, lineCls: '' }; } st = null; }
    if (/^(---|\.\.\.)\s*$/.test(line)) { push('pun', line); return { toks, st: null, lineCls: '' }; }
    push('', line.slice(0, ind)); i = ind;
    let m;
    while ((m = /^-(\s+|$)/.exec(line.slice(i)))) { push('pun', '-'); push('', m[1]); i += m[0].length; }
    const km = /^("[^"]*"|'[^']*'|[^\s#'"{[\]&*!|>][^:#{}[\],]*?)(\s*:)(?=\s|$)/.exec(line.slice(i));
    if (km) { push('prop', km[1]); push('pun', km[2]); i += km[0].length; }
    const valStart = i;
    while (i < line.length) {
      const c = line[i], rest = line.slice(i);
      if (c === ' ' || c === '\t') { push('', c); i++; continue; }
      if (c === '#' && (i === 0 || /\s/.test(line[i - 1]))) { push('com', rest); break; }
      if (c === '"' || c === "'") { const j = findClose(line, i + 1, c, c === '"'); const e = j < 0 ? line.length : j + 1; push('str', line.slice(i, e)); i = e; continue; }
      if ((m = /^[|>][+-]?\d*\s*(#.*)?$/.exec(rest)) && !line.slice(valStart, i).trim()) { push('op', rest.replace(/\s*#.*$/, '')); if (m[1]) push('com', m[1]); return { toks, st: { blk: ind }, lineCls: '' }; }
      if ((m = /^[&*!][^\s,\]}]+/.exec(rest))) { push('dec', m[0]); i += m[0].length; continue; }
      if ('{}[],'.includes(c)) { push('pun', c); i++; continue; }
      if ((m = /^([^\s,{}[\]#:'"][^,{}[\]#:]*?)(:)(?=\s)/.exec(rest))) { push('prop', m[1]); push('pun', ':'); i += m[0].length; continue; }          // a key inside { }
      if ((m = /^(?:[^\s,{}[\]#:]|:(?!\s|$))+(?:[ \t]+(?:[^\s,{}[\]#:]|:(?!\s|$))+)*/.exec(rest))) {
        const v = m[0];
        push(/^(true|false|yes|no|on|off|null|~)$/i.test(v) ? 'bi' : /^[-+]?(\d[\d_]*(\.\d*)?([eE][-+]?\d+)?|0x[\da-f]+)$/i.test(v) ? 'num' : 'sv', v);
        i += v.length; continue;
      }
      push('', c); i++;
    }
    return { toks, st: null, lineCls: '' };
  }

  // shell: comments, quotes, $variables, keywords, the command word, options, here-documents
  const SH_KW = W('if then else elif fi for while until do done case esac in function select time return local export readonly declare set unset shift exit source');
  function shell(line, st) {
    const toks = [], push = pusher(toks); let i = 0, cmd = true, m;
    if (st && st.here) { push('str', line); return { toks, st: line.trim() === st.here ? null : st, lineCls: '' }; }
    if (st && st.q) { const j = findClose(line, 0, st.q, st.q === '"'); if (j < 0) { push('str', line); return { toks, st, lineCls: '' }; } push('str', line.slice(0, j + 1)); i = j + 1; st = null; cmd = false; }
    let next = null;
    while (i < line.length) {
      const c = line[i], rest = line.slice(i);
      if (c === ' ' || c === '\t') { push('', c); i++; continue; }
      if (c === '#' && (i === 0 || /[\s;]/.test(line[i - 1]))) { push('com', rest); break; }
      if (c === "'" || c === '"') { const j = findClose(line, i + 1, c, c === '"'); if (j < 0) { push('str', rest); st = { q: c }; break; } push('str', line.slice(i, j + 1)); i = j + 1; cmd = false; continue; }
      if ((m = /^<<-?\s*['"]?(\w+)['"]?/.exec(rest))) { push('op', m[0]); next = { here: m[1] }; i += m[0].length; continue; }
      if ((m = /^\$(\{[^}]*\}|\w+|[@#?$!*0-9])/.exec(rest))) { push('var', m[0]); i += m[0].length; cmd = false; continue; }
      if ((m = /^(\|\||&&|[|;&()]|\$\(|`)/.exec(rest))) { push('op', m[0]); i += m[0].length; cmd = true; continue; }
      if ((m = /^[<>]+&?\d*/.exec(rest))) { push('op', m[0]); i += m[0].length; continue; }
      if (cmd && (m = /^([A-Za-z_]\w*)(=)/.exec(rest))) { push('var', m[1]); push('op', '='); i += m[0].length; continue; }
      if ((m = /^--?[A-Za-z][\w-]*/.exec(rest)) && /\s/.test(line[i - 1] || ' ')) { push('opt', m[0]); i += m[0].length; continue; }
      if ((m = /^[^\s'"$|;&()<>`#]+/.exec(rest))) {
        const w = m[0]; push(SH_KW.has(w) ? 'kw' : cmd ? 'call' : /^\d+$/.test(w) ? 'num' : '', w); i += w.length;
        cmd = SH_KW.has(w) && !/^(in|function|export|local|readonly|declare|return|exit|set|unset|shift)$/.test(w);
        continue;
      }
      push('', c); i++;
    }
    return { toks, st: next || st, lineCls: '' };
  }

  // Markdown as text: the marks kept as their own pieces, fenced code coloured in its own language
  const MD_IN = /(`+)([^`]+?)\1|\*\*(?=\S)(.+?)\*\*|\*(?=[^\s*])([^*]+?)\*|\[([^\]]+)\]\(([^)\s]+)\)/g;
  function mdInline(text, push, base) {
    let k = 0, m; MD_IN.lastIndex = 0;
    while ((m = MD_IN.exec(text))) {
      push(base, text.slice(k, m.index));
      if (m[1]) { push('mdmark', m[1]); push('mdcode', m[2]); push('mdmark', m[1]); }
      else if (m[3]) { push('mdmark', '**'); push('mdb', m[3]); push('mdmark', '**'); }
      else if (m[4]) { push('mdmark', '*'); push('mdi', m[4]); push('mdmark', '*'); }
      else { push('mdmark', '['); push('mdlink', m[5]); push('mdmark', ']('); push('mdurl', m[6]); push('mdmark', ')'); }
      k = m.index + m[0].length;
    }
    push(base, text.slice(k));
  }
  function markdown(line, st) {
    const toks = [], push = pusher(toks); let m;
    if (st && st.fence) {
      if (line.trim().startsWith(st.fence)) { push('mdmark', line); return { toks, st: null, lineCls: 'mcode' }; }
      const lx = st.lang && LEX[st.lang];
      if (lx) { const r = lx(line, st.inner); return { toks: r.toks, st: { ...st, inner: r.st }, lineCls: 'mcode' }; }
      push('mdcode', line); return { toks, st, lineCls: 'mcode' };
    }
    if ((m = /^(\s*)(```+|~~~+)\s*([\w+#.-]*)(.*)$/.exec(line))) { push('', m[1]); push('mdmark', m[2]); push('mdlang', m[3]); push('', m[4]); return { toks, st: { fence: m[2], lang: ALIAS[m[3].toLowerCase()] || null, inner: null }, lineCls: 'mcode' }; }
    if ((m = /^(\s{0,3})(#{1,6})(\s+)(.*)$/.exec(line))) { push('', m[1]); push('mdmark', m[2] + m[3]); mdInline(m[4], push, 'mdh'); return { toks, st: null, lineCls: 'mh' + Math.min(m[2].length, 3) }; }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) { push('mdmark', line); return { toks, st: null, lineCls: '' }; }
    if ((m = /^(\s*>\s?)(.*)$/.exec(line))) { push('mdq', m[1]); mdInline(m[2], push, 'mdq'); return { toks, st: null, lineCls: 'mq' }; }
    if ((m = /^(\s*)([-*+]|\d{1,4}[.)])(\s+)(.*)$/.exec(line))) { push('', m[1]); push('mdlist', m[2]); push('', m[3]); mdInline(m[4], push, ''); return { toks, st: null, lineCls: '' }; }
    if (/^\s*\|/.test(line)) { line.split(/(\|)/).forEach(p => p === '|' ? push('mdmark', p) : /^\s*:?-{2,}:?\s*$/.test(p) ? push('mdmark', p) : mdInline(p, push, '')); return { toks, st: null, lineCls: '' }; }
    mdInline(line, push, ''); return { toks, st: null, lineCls: '' };
  }
  const plain = line => ({ toks: line ? [['', line]] : [], st: null, lineCls: '' });

  const LEX = { python, swift, js, json, yaml, shell, markdown, plain };
  const ALIAS = { py: 'python', python: 'python', swift: 'swift', js: 'js', javascript: 'js', mjs: 'js', cjs: 'js', jsx: 'js', ts: 'js', tsx: 'js', typescript: 'js',
    json: 'json', jsonl: 'json', yaml: 'yaml', yml: 'yaml', sh: 'shell', bash: 'shell', zsh: 'shell', shell: 'shell', console: 'shell', command: 'shell', md: 'markdown', markdown: 'markdown' };
  const NAMES = { python: 'Python', swift: 'Swift', js: 'JavaScript / TypeScript', json: 'JSON', yaml: 'YAML', shell: 'Shell', markdown: 'Markdown', plain: 'Plain text' };
  const langOf = path => { const ext = (String(path).match(/\.([^./]+)$/) || [])[1]; return ALIAS[(ext || '').toLowerCase()] || (/(^|\/)(Makefile|Dockerfile)$/.test(path) ? 'shell' : 'plain'); };
  // the whole file: one entry per line ({toks, lineCls}); the state carried line to line
  function lex(text, lang) {
    const lx = LEX[lang] || plain, out = []; let st = null;
    for (const line of String(text).replace(/\r\n?/g, '\n').split('\n')) { const r = lx(line, st); st = r.st; out.push(r); }
    return out;
  }
  const lexer = lang => LEX[lang] || plain;          // one line at a time: (line, state) -> {toks, st, lineCls}, for re-colouring from an edited line down
  return { lex, lexer, langOf, NAMES };
})();
