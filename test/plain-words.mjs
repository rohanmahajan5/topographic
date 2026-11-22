// The shared plain-words step (engine/model.js engPlain, engFirstLine) without a browser: node plain-words.mjs
// Every place outside the chat that quotes an agent's words in a line or a few (floor labels and their hover lines, the question
// card, the tree's lines, the catch-up) passes them through this one step (walk 2026-11-19: a link with its whole path on the floor).
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import assert from 'assert/strict';

const src = readFileSync(fileURLToPath(new URL('../workspace/engine/model.js', import.meta.url)), 'utf8');
const i = src.indexOf('function engPlain'), j = src.indexOf('const engBase');
assert.ok(i > 0 && j > i, 'engPlain not found in engine/model.js');
const { engPlain, engFirstLine } = new Function(src.slice(i, j) + '\nreturn { engPlain, engFirstLine };')();

const cases = [
  ['Fixed the rounding in [totals.py](/Users/someone/project/receipts/totals.py).', 'Fixed the rounding in totals.py.'],
  ['See [the spec](https://example.com/a_(b)) and ![chart](x.png)', 'See the spec and chart'],
  ['**Done**: `npm test` passes, *all* green, ~~one~~ none __left__', 'Done: npm test passes, all green, one none left'],
  ['## Summary\n- first point\n* second\n1. third\n> quoted', 'Summary first point second third quoted'],
  ['- [x] checked item', 'checked item'],
  ['```py\nx = 1\n```\nThat was it.', 'x = 1 That was it.'],
  ['snake_case_name and 2*3*4 stay as they are', 'snake_case_name and 2*3*4 stay as they are'],
  ['line one\nline two\n\nline three', 'line one line two line three'],
  ['Appended `# checked` to receipts/__init__.py and src/_util_.ts.', 'Appended # checked to receipts/__init__.py and src/_util_.ts.'],
  ['', ''], [null, ''],
];
let n = 0;
for (const [inp, want] of cases) { assert.equal(engPlain(inp), want, JSON.stringify(inp)); n++; }
assert.equal(engFirstLine('## Changes\nEdited [a.py](/abs/a.py)'), 'Changes'); n++;
assert.equal(engFirstLine('```\n\n```\n**Finished** the [report](/x/report.md)'), 'Finished the report'); n++;
assert.equal(engFirstLine(''), ''); n++;
console.log(`plain words: ${n} passed`);
