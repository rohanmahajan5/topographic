// The change count's words and its whole-or-nothing rule (engine/model.js engCountWords, engChgWhole) without a browser:
// node count-words.mjs. Every place that shows an agent's change count (floor label and its hover, chat head, change reader, catch-up)
// writes it with engCountWords and reads it from facts.chg, which engChgWhole makes (walk 2026-11-19: "1 lines"; "1 file changed" with
// no lines; the label and the chat head counting apart)
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import assert from 'assert/strict';

const src = readFileSync(fileURLToPath(new URL('../workspace/engine/model.js', import.meta.url)), 'utf8');
const i = src.indexOf('const engCountWords'), j = src.indexOf('// the first line with words');
assert.ok(i > 0 && j > i, 'engCountWords not found in engine/model.js');
const { engCountWords, engChgWhole } = new Function(src.slice(i, j) + '\nreturn { engCountWords, engChgWhole };')();

let n = 0;
const eq = (got, want, what) => { assert.deepEqual(got, want, what); n++; };
eq(engCountWords(1, 1), '1 file changed, 1 line', 'one and one');
eq(engCountWords(1, 4), '1 file changed, 4 lines', 'one file');
eq(engCountWords(3, 1), '3 files changed, 1 line', 'one line');
eq(engCountWords(3, 12), '3 files changed, 12 lines', 'many');
eq(engCountWords(2, 0), '2 files changed, 0 lines', 'lines known as none: said, never left out');
// whole or nothing: a first edit whose lines are not known shows nothing; once known, the whole count
eq(engChgWhole('a', 1, 0, true), null, 'first edit pending: nothing');
eq(engChgWhole('a', 1, 1, false), { files: 1, lines: 1 }, 'first edit known');
// a later edit pending: the last whole count stands, running or not
eq(engChgWhole('a', 2, 1, true), { files: 1, lines: 1 }, 'second edit pending: the last whole count');
eq(engChgWhole('a', 2, 4, false), { files: 2, lines: 4 }, 'second edit known');
eq(engChgWhole('b', 0, 0, false), null, 'no edits: nothing');
console.log(`count words: ${n} passed`);
