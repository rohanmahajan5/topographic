/* The file window's outline (2026-11-22): what a file holds, as a list of entries the window shows on its left, folds from and
   names in its title line. Each entry: { line, end (both 1-based, end inclusive), name, kind, depth, parent (the enclosing type's
   name, or null) }.
   Kinds (one badge and one colour each, FVO.KINDS): class, struct, enum, protocol, interface, extension, actor, type (an alias),
   function, method, property, constant, variable, heading (Markdown, with its level).
   How (2026-11-22, parsed for real): Python, Swift and JavaScript are parsed with Tree-sitter (vendor/: the
   web-tree-sitter runtime and three grammars, loaded on first use only; vendor/LICENSES.md); TypeScript, shell and any file whose
   grammar did not load get careful patterns (indentation for Python, matched braces for the rest); Markdown headings are patterns.
   FVO.outline(text, path) answers { entries, how: 'tree-sitter' | 'patterns' | 'headings' | 'none', note }.
   Origins: the badge letters and colours are the builder's (asked for: a visual cue telling a function from a class or a
   variable); the parsing rules per language are the builder's, read off each grammar's own tree. */
const FVO = (() => {
  const KINDS = {          // badge, colour, the word a hover says
    class: ['C', '#8a3d93', 'class'], struct: ['S', '#1d6b78', 'struct'], enum: ['E', '#9c401c', 'enum'], protocol: ['P', '#2f5597', 'protocol'],
    interface: ['I', '#2f5597', 'interface'], extension: ['X', '#6a6f78', 'extension'], actor: ['A', '#8a5d14', 'actor'], type: ['T', '#50698a', 'type alias'],
    function: ['ƒ', '#7f5300', 'function'], method: ['m', '#246340', 'method'], property: ['p', '#3a7ca5', 'property'],
    constant: ['v', '#5c5f66', 'constant'], variable: ['v', '#5c5f66', 'variable'], heading: ['#', '#8a3d93', 'heading'] };
  const TYPEISH = new Set(['class', 'struct', 'enum', 'protocol', 'interface', 'extension', 'actor']);
  const WASM = { python: 'tree-sitter-python.wasm', swift: 'tree-sitter-swift.wasm', js: 'tree-sitter-javascript.wasm' };
  const BASE = 'vendor/';
  const S = { init: null, langs: {}, failed: {}, parsers: {} };

  function langOf(path) {
    const ext = ((String(path).match(/\.([^./]+)$/) || [])[1] || '').toLowerCase();
    return { py: 'python', pyi: 'python', swift: 'swift', js: 'js', mjs: 'js', cjs: 'js', jsx: 'js', ts: 'ts', tsx: 'ts', mts: 'ts', cts: 'ts',
      sh: 'shell', bash: 'shell', zsh: 'shell', command: 'shell', md: 'markdown', markdown: 'markdown' }[ext] || null;
  }
  // the runtime and one grammar, loaded once each, on first use (a failure is remembered: that language gets patterns)
  function script(src) {
    return new Promise((ok, no) => { const s = document.createElement('script'); s.src = src; s.onload = ok; s.onerror = () => no(new Error('could not load ' + src)); document.head.appendChild(s); });
  }
  async function parserFor(lang) {
    if (!WASM[lang] || S.failed[lang]) return null;
    if (S.parsers[lang]) return S.parsers[lang];
    try {
      if (!S.init) S.init = (typeof TreeSitter === 'undefined' ? script(BASE + 'tree-sitter.js') : Promise.resolve()).then(() => TreeSitter.init({ locateFile: n => BASE + n }));
      await S.init;
      if (!S.langs[lang]) S.langs[lang] = TreeSitter.Language.load(BASE + WASM[lang]);
      const L = await S.langs[lang], p = new TreeSitter(); p.setLanguage(L);
      return (S.parsers[lang] = p);
    } catch (e) { S.failed[lang] = e.message || String(e); return null; }
  }

  /* ---------- Tree-sitter: one visitor per language ---------- */
  const nm = n => n ? n.text.replace(/\s+/g, ' ') : '';
  function tsWalk(root, lang) {
    const out = [];
    const add = (node, name, kind, ctx, at) => { const e = { line: (at || node).startPosition.row + 1, end: node.endPosition.row + 1, name, kind, depth: ctx.depth, parent: ctx.type || null }; out.push(e); return e; };
    const kid = (ctx, e) => ({ depth: ctx.depth + 1, type: TYPEISH.has(e.kind) ? e.name : ctx.type, inType: TYPEISH.has(e.kind), inFn: !TYPEISH.has(e.kind) });
    const V = {
      python(n, ctx) {
        let node = n, decos = [];
        if (n.type === 'decorated_definition') { decos = n.namedChildren.filter(c => c.type === 'decorator').map(c => c.text); node = n.childForFieldName('definition') || n.namedChildren[n.namedChildren.length - 1]; }
        if (node.type === 'class_definition') {
          const sup = nm(node.childForFieldName('superclasses')), e = add(n, nm(node.childForFieldName('name')), /\b(Enum|IntEnum|StrEnum|Flag|IntFlag)\b/.test(sup) ? 'enum' : 'class', ctx, node);
          walk(node.childForFieldName('body'), kid(ctx, e)); return;
        }
        if (node.type === 'function_definition') {
          const prop = decos.some(d => /^@(property|cached_property|functools\.cached_property|\w+\.(setter|getter|deleter))\b/.test(d));
          const e = add(n, nm(node.childForFieldName('name')), ctx.inType ? (prop ? 'property' : 'method') : 'function', ctx, node);
          walk(node.childForFieldName('body'), kid(ctx, e)); return;
        }
        if (!ctx.inFn && n.type === 'expression_statement') {          // module and class level names given a value
          const a = n.namedChildren[0]; if (!a || a.type !== 'assignment') return;
          const left = a.childForFieldName('left'); if (!left || left.type !== 'identifier') return;
          const w = left.text; add(n, w, /^[A-Z][A-Z0-9_]*$/.test(w) ? 'constant' : 'variable', ctx); return;
        }
        if (/^(if_statement|try_statement|with_statement|block|else_clause|elif_clause|except_clause|finally_clause)$/.test(n.type) && !ctx.inFn) walk(n, ctx);          // a guarded import or def at the top
        else if (ctx.inFn && /statement|block|clause/.test(n.type)) walk(n, ctx);          // a def inside a loop or a branch of a function
      },
      swift(n, ctx) {
        const t = n.type;
        if (t === 'class_declaration' || t === 'protocol_declaration') {
          const k = t === 'protocol_declaration' ? 'protocol' : (n.children.find(c => /^(class|struct|enum|extension|actor)$/.test(c.type)) || { type: 'class' }).type;
          const e = add(n, nm(n.childForFieldName('name')), k, ctx); walk(n.childForFieldName('body'), kid(ctx, e)); return;
        }
        if (t === 'function_declaration' || t === 'protocol_function_declaration') { const e = add(n, nm(n.childForFieldName('name')), ctx.inType ? 'method' : 'function', ctx); walk(n.childForFieldName('body'), kid(ctx, e)); return; }
        if (t === 'init_declaration' || t === 'deinit_declaration' || t === 'subscript_declaration') { add(n, t.split('_')[0], 'method', ctx); return; }
        if (t === 'typealias_declaration') { add(n, nm(n.childForFieldName('name')), 'type', ctx); return; }
        if ((t === 'property_declaration' || t === 'protocol_property_declaration') && !ctx.inFn) {
          const pat = n.childForFieldName('name'), id = pat && (pat.childForFieldName('bound_identifier') || pat.namedChildren.find(c => c.type === 'simple_identifier'));
          const vb = (n.namedChildren.find(c => c.type === 'value_binding_pattern') || (pat && pat.namedChildren.find(c => c.type === 'value_binding_pattern')) || { text: 'var' }).text;
          if (id) add(n, id.text, ctx.inType ? 'property' : vb === 'let' ? 'constant' : 'variable', ctx); return;
        }
        if (/_body$|^statements$/.test(t) || (ctx.inFn && /statement|expression|block/.test(t))) walk(n, ctx);
      },
      js(n, ctx) {
        const t = n.type;
        if (t === 'export_statement') { const d = n.childForFieldName('declaration'); if (d) V.js(d, ctx); return; }
        if (t === 'class_declaration' || t === 'class') { const e = add(n, nm(n.childForFieldName('name')) || 'class', 'class', ctx); walk(n.childForFieldName('body'), kid(ctx, e)); return; }
        if (t === 'method_definition') { const g = n.children.some(c => c.type === 'get' || c.type === 'set'); const e = add(n, nm(n.childForFieldName('name')), g ? 'property' : 'method', ctx); walk(n.childForFieldName('body'), kid(ctx, e)); return; }
        if (t === 'field_definition') { const p = n.childForFieldName('property'); if (p) add(n, p.text, 'property', ctx); return; }
        if (t === 'function_declaration' || t === 'generator_function_declaration') { const e = add(n, nm(n.childForFieldName('name')), 'function', ctx); walk(n.childForFieldName('body'), kid(ctx, e)); return; }
        if ((t === 'lexical_declaration' || t === 'variable_declaration') && !ctx.inFn) {
          const isConst = n.children.some(c => c.type === 'const');
          n.namedChildren.filter(c => c.type === 'variable_declarator').forEach(d => {
            const name = d.childForFieldName('name'), v = d.childForFieldName('value'); if (!name || name.type !== 'identifier') return;
            if (v && /^(arrow_function|function|function_expression|generator_function)$/.test(v.type)) { const e = add(n, name.text, 'function', ctx); walk(v.childForFieldName('body'), kid(ctx, e)); }
            else if (v && v.type === 'class') { const e = add(n, name.text, 'class', ctx); walk(v.childForFieldName('body'), kid(ctx, e)); }
            else add(n, name.text, isConst ? 'constant' : 'variable', ctx);
          });
          return;
        }
        if (/^(class_body|statement_block|program)$/.test(t)) walk(n, ctx);
      }
    };
    function walk(node, ctx) { if (!node) return; for (const c of node.namedChildren) V[lang](c, ctx); }
    walk(root, { depth: 0, type: null, inType: false, inFn: false });
    return out;
  }

  /* ---------- patterns ---------- */
  // matched braces outside strings and comments: for each line holding an opening brace, the line of its partner
  function braces(lines, opts) {
    const close = new Map(), stack = []; let blk = false, str = null;
    lines.forEach((l, i) => {
      for (let k = 0; k < l.length; k++) {
        const c = l[k];
        if (blk) { if (c === '*' && l[k + 1] === '/') { blk = false; k++; } continue; }
        if (str) { if (str === '"""' ? l.startsWith('"""', k) : c === str) { if (str === '"""') k += 2; str = null; } else if (c === '\\') k++; continue; }
        if (opts.hash && c === '#') break;
        if (c === '/' && l[k + 1] === '/') break;
        if (c === '/' && l[k + 1] === '*') { blk = true; k++; continue; }
        if (l.startsWith('"""', k)) { str = '"""'; k += 2; continue; }
        if (c === '"' || c === "'" || (c === '`' && opts.tick)) { str = c; continue; }
        if (c === '{') stack.push(i);
        else if (c === '}' && stack.length) { const o = stack.pop(); if (!close.has(o)) close.set(o, i); }
      }
      if (str && str !== '"""' && str !== '`') str = null;          // a one-line string left open: not carried on
    });
    return close;
  }
  const endFrom = (close, i, lines) => { for (let j = i; j < Math.min(lines.length, i + 4); j++) if (close.has(j)) return close.get(j) + 1; return i + 1; };
  function nest(out) {          // depth and parent from the extents
    const st = [];
    out.sort((a, b) => a.line - b.line || b.end - a.end).forEach(e => {
      while (st.length && st[st.length - 1].end < e.line) st.pop();
      e.depth = st.length; const ty = [...st].reverse().find(x => TYPEISH.has(x.kind)); e.parent = ty ? ty.name : null;
      st.push(e);
    });
    return out;
  }
  const insideFn = (out, line) => out.some(x => (x.kind === 'function' || x.kind === 'method') && x.line < line && line <= x.end);
  function patterns(lines, lang) {
    const out = [], blank = i => !lines[i].trim(), ind = i => lines[i].match(/^[ \t]*/)[0].replace(/\t/g, '    ').length;
    if (lang === 'python') {
      lines.forEach((l, i) => {
        let m = l.match(/^(\s*)(?:async\s+)?(def|class)\s+(\w+)/);
        if (m) { out.push({ line: i + 1, kind: m[2] === 'class' ? 'class' : 'function', name: m[3], ind: ind(i), prop: i > 0 && /^\s*@(property|cached_property|\w+\.setter)\b/.test(lines[i - 1]) }); return; }
        if ((m = l.match(/^([A-Za-z_]\w*)\s*(?::[^=]*)?=(?!=)/))) out.push({ line: i + 1, kind: /^[A-Z][A-Z0-9_]*$/.test(m[1]) ? 'constant' : 'variable', name: m[1], ind: 0, leaf: true });
      });
      out.forEach(s => { if (s.leaf) { s.end = s.line; return; } let j = s.line; while (j < lines.length && (blank(j) || ind(j) > s.ind)) j++; while (j > s.line && blank(j - 1)) j--; s.end = j; });
      nest(out).forEach(s => { if (s.kind === 'function' && s.parent && out.find(x => x.name === s.parent && x.kind === 'class' && x.ind < s.ind && x.line < s.line && s.line <= x.end && s.ind === out.filter(y => y.line > x.line && y.line <= x.end).reduce((m, y) => Math.min(m, y.ind), 99))) s.kind = s.prop ? 'property' : 'method'; });
      return out;
    }
    if (lang === 'shell') {
      const close = braces(lines, { hash: true });
      lines.forEach((l, i) => {
        let m = l.match(/^\s*(?:function\s+([A-Za-z_][\w:.-]*)\s*(?:\(\s*\))?|([A-Za-z_][\w:.-]*)\s*\(\s*\))\s*\{?/);
        if (m) { out.push({ line: i + 1, end: endFrom(close, i, lines), name: m[1] || m[2], kind: 'function' }); return; }
        if ((m = l.match(/^(?:export\s+|readonly\s+|declare\s+(?:-\w+\s+)?)?([A-Za-z_]\w*)=/))) out.push({ line: i + 1, end: i + 1, name: m[1], kind: /^[A-Z][A-Z0-9_]*$/.test(m[1]) ? 'constant' : 'variable' });
      });
      return nest(out).filter(e => !(e.kind !== 'function' && insideFn(out, e.line)));
    }
    if (lang === 'swift' || lang === 'js' || lang === 'ts') {
      const close = braces(lines, { tick: lang !== 'swift' }), MOD = lang === 'swift'
        ? '(?:(?:@\\w+(?:\\([^)]*\\))?|public|private|internal|fileprivate|open|final|static|class|override|nonisolated|mutating|nonmutating|lazy|weak|unowned|convenience|required|dynamic|indirect|private\\(set\\)|fileprivate\\(set\\)|internal\\(set\\))\\s+)*'
        : '(?:(?:export|default|declare|abstract|public|private|protected|static|readonly|async|override|accessor)\\s+)*';
      const decl = new RegExp('^\\s*' + MOD + (lang === 'swift' ? '(class|struct|enum|protocol|extension|actor|func|init|deinit|subscript|var|let|typealias)\\b\\s*([A-Za-z_][\\w.]*)?'
        : '(class|interface|enum|type|function\\*?|const|let|var|namespace)\\s+([A-Za-z_$][\\w$]*)'));
      const meth = lang === 'swift' ? null : new RegExp('^\\s*' + MOD + '(?:(get|set)\\s+)?\\*?\\s*([A-Za-z_$#][\\w$]*)\\s*(?:<[^>]*>)?\\s*\\(');
      lines.forEach((l, i) => {
        const m = l.match(decl);
        if (m) {
          const w = m[1].replace('*', ''), name = m[2] || w, end = /^(var|let|const|typealias|type)$/.test(w) && !/=>|\{\s*$/.test(l) ? i + 1 : endFrom(close, i, lines);
          const kind = { class: 'class', struct: 'struct', enum: 'enum', protocol: 'protocol', extension: 'extension', actor: 'actor', interface: 'interface', namespace: 'class',
            func: 'function', function: 'function', init: 'method', deinit: 'method', subscript: 'method', typealias: 'type', type: 'type',
            let: lang === 'swift' ? 'constant' : 'variable', var: 'variable', const: /=\s*(async\s*)?(\([^)]*\)|[\w$]+)\s*(:\s*[^=]+)?=>|=\s*(async\s+)?function\b/.test(l) ? 'function' : 'constant' }[w];
          out.push({ line: i + 1, end: kind === 'function' && end === i + 1 ? endFrom(close, i, lines) : end, name, kind });
          return;
        }
        const k = meth && l.match(meth);
        if (k && !/^(if|for|while|switch|catch|return|function|await|new|typeof|super|else)$/.test(k[2]) && /\)\s*(:\s*[^{;]+)?\{\s*$/.test(l)) out.push({ line: i + 1, end: endFrom(close, i, lines), name: k[2], kind: k[1] ? 'property' : 'method', meth: true });
      });
      nest(out);
      return out.filter(e => {
        const inT = !!e.parent && out.some(x => TYPEISH.has(x.kind) && x.name === e.parent && x.line < e.line && e.line <= x.end && x.depth === e.depth - 1);
        if (e.meth) return inT;          // a call-shaped line counts as a method only directly inside a class
        if (insideFn(out, e.line) && !TYPEISH.has(e.kind) && e.kind !== 'function') return false;          // a function's own locals are not listed
        if (inT && e.kind === 'function') e.kind = 'method';
        if (inT && (e.kind === 'variable' || e.kind === 'constant')) e.kind = 'property';
        return true;
      });
    }
    return out;
  }
  function headings(lines) {
    const out = []; let fence = false;
    lines.forEach((l, i) => { if (/^\s*(```|~~~)/.test(l)) fence = !fence; const m = !fence && l.match(/^(#{1,6})\s+(.*)/); if (m) out.push({ line: i + 1, level: m[1].length, name: m[2].replace(/`/g, '').replace(/\s*#*\s*$/, ''), kind: 'heading' }); });
    out.forEach((s, k) => { let e = lines.length; for (let j = k + 1; j < out.length; j++) if (out[j].level <= s.level) { e = out[j].line - 1; break; } while (e > s.line && !lines[e - 1].trim()) e--; s.end = e; });
    const top = Math.min(...out.map(s => s.level)); out.forEach(s => { s.depth = s.level - top; s.parent = null; });
    return out;
  }

  // the whole outline of a text; trees are kept per window by the caller, the parser is shared
  async function outline(text, path) {
    const lang = langOf(path), lines = String(text).split('\n');
    if (lang === 'markdown') return { entries: headings(lines), how: 'headings', note: '' };
    if (!lang) return { entries: [], how: 'none', note: '' };
    const p = await parserFor(lang);
    if (p) {
      try { const tree = p.parse(String(text)); const entries = tsWalk(tree.rootNode, lang); tree.delete();
        return { entries: entries.filter(e => e.name).sort((a, b) => a.line - b.line), how: 'tree-sitter', note: '' }; }
      catch (e) { S.failed[lang] = e.message || String(e); }
    }
    return { entries: patterns(lines, lang).filter(e => e.name), how: 'patterns', note: WASM[lang] ? 'the parser did not load (' + S.failed[lang] + '): found by patterns' : '' };
  }
  const kindWords = e => (KINDS[e.kind] || KINDS.variable)[2] + (e.kind === 'heading' ? ' ' + e.level : '') + (e.parent && !TYPEISH.has(e.kind) ? ' of ' + e.parent : e.parent ? ' in ' + e.parent : '');
  return { outline, patterns, headings, langOf, KINDS, TYPEISH, kindWords, state: () => ({ loaded: Object.keys(S.parsers), failed: Object.assign({}, S.failed) }), _tsWalk: tsWalk };
})();
if (typeof module === 'object' && module.exports) module.exports = FVO;
