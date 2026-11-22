// The chat's Markdown step (live.html md / mdInline) without a browser: node markdown.mjs
// Fault 2026-11-19: "receipts/__init__.py" was drawn as "receipts/init.py" with a bold "init", in your message, in an agent's link
// text and in the page's own approval line. Underscores make emphasis only when what stands outside them is a space, the line's edge
// or punctuation, never a path or a name; a link's text follows the same rules (only its target is set aside); asterisks keep theirs.
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import assert from 'assert/strict';

const html = readFileSync(fileURLToPath(new URL('../workspace/live.html', import.meta.url)), 'utf8');
const i = html.indexOf('const MD_UL'), j = html.indexOf('\n// the same text without its marks', i);
assert.ok(i > 0 && j > i, 'md() not found in live.html');
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const FILES = new Set(['receipts/__init__.py']);          // the one file "on the floor"
const mdFile = t => FILES.has(String(t)) ? { f: { path: String(t) }, line: null } : null;
const mdFileAttr = (fl, inner) => `<a class="mdf" data-path="${esc(fl.f.path)}">${inner}</a>`;
const { md, mdInline } = new Function('esc', 'mdFile', 'mdFileAttr', html.slice(i, j) + '\nreturn { md, mdInline };')(esc, mdFile, mdFileAttr);

let n = 0;
const same = (inp, want) => { assert.equal(mdInline(inp), want, JSON.stringify(inp)); n++; };
// stay as written: attached to a path or a name
same('edit receipts/__init__.py now', 'edit receipts/__init__.py now');
same('__init__.py', '__init__.py');
same('see __init__.py.', 'see __init__.py.');
same('snake_case_name', 'snake_case_name');
same('call my_func_name() here', 'call my_func_name() here');
same('a/__main__/b', 'a/__main__/b');
same('the /__dunder__ folder', 'the /__dunder__ folder');
same('the __dunder__.attr', 'the __dunder__.attr');
same('x._private_', 'x._private_');
same('asked to edit receipts/__init__.py (+1 −0) · you allowed once', 'asked to edit receipts/__init__.py (+1 −0) · you allowed once');
// still emphasis: in prose, at the edges, in brackets or quotes, before closing punctuation
same('_italic_', '<em>italic</em>');
same('__bold__', '<strong>bold</strong>');
same('this is _quite_ fine', 'this is <em>quite</em> fine');
same('this is __very__ fine.', 'this is <strong>very</strong> fine.');
same('(_aside_)', '(<em>aside</em>)');
same('"__said__"', '&quot;<strong>said</strong>&quot;');
same("'_x_'", "'<em>x</em>'");
same('end, _two words_!', 'end, <em>two words</em>!');
same('_ not _ emphasis', '_ not _ emphasis');
// asterisks as they were
same('**bold** and *it* and 2*3*4', '<strong>bold</strong> and <em>it</em> and 2*3*4');
// a link's text goes through the same rules (walk 2026-11-19): its underscores by the underscore rule, asterisks and code marks work;
// only its target is set aside
same('[receipts/__init__.py](receipts/__init__.py)', '<a class="mdf" data-path="receipts/__init__.py">receipts/__init__.py</a>');
same('[receipts/**totals**.py](receipts/__init__.py)', '<a class="mdf" data-path="receipts/__init__.py">receipts/<strong>totals</strong>.py</a>');
same('[`a_b`, *it* and __x__](https://example.com/a_b_c)', '<a href="https://example.com/a_b_c" target="_blank" rel="noopener noreferrer"><code>a_b</code>, <em>it</em> and <strong>x</strong></a>');
same('[`receipts/__init__.py`](receipts/__init__.py)', '<a class="mdf" data-path="receipts/__init__.py"><code>receipts/__init__.py</code></a>');
same('see [_this_](nowhere/x.md) and _that_', 'see <em>this</em> and <em>that</em>');
same('`[not](a link)` but [a](https://example.com/x)', '<code>[not](a link)</code> but <a href="https://example.com/x" target="_blank" rel="noopener noreferrer">a</a>');
same('**[bold link](https://example.com/x)**', '<strong><a href="https://example.com/x" target="_blank" rel="noopener noreferrer">bold link</a></strong>');
// whole messages: a heading, a list item and a paragraph
assert.equal(md('## __init__.py\n- edit receipts/__init__.py\n\nPlease edit `receipts/__init__.py` and __main__.py, _carefully_.'),
  '<div class="mdh mdh2">__init__.py</div><ul><li>edit receipts/__init__.py</li></ul><p>Please edit <a class="mdf" data-path="receipts/__init__.py"><code>receipts/__init__.py</code></a> and __main__.py, <em>carefully</em>.</p>'); n++;
console.log(`markdown: ${n} passed`);
