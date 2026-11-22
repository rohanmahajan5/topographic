// Which message a finished turn is quoted by (engine/model.js engFinalWords) without a browser: node final-words.mjs. The catch-up's
// "finished:" and the floor label's said/result line quote it (walk 2026-11-19: an interim remark quoted over the final answer)
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import assert from 'assert/strict';

const src = readFileSync(fileURLToPath(new URL('../workspace/engine/model.js', import.meta.url)), 'utf8');
const i = src.indexOf('function engFinalWords'), j = src.indexOf('// a change count in words');
assert.ok(i > 0 && j > i, 'engFinalWords not found in engine/model.js');
const engFinalWords = new Function(src.slice(i, j) + '\nreturn engFinalWords;')();

const you = (t, text) => ({ t, kind: 'you', who: 'you', text }), says = (t, text) => ({ t, kind: 'say', who: 'A', text }), act = (t, text) => ({ t, kind: 'act', who: 'A', text });
let n = 0;
const eq = (got, want, what) => { assert.deepEqual(got, want, what); n++; };
const turn = [you(10, 'append a comment'), says(12, "I'll append the requested comment using the patch tool."), act(13, 'apply_patch'), says(15, 'Appended # checked to receipts/__init__.py.')];
eq(engFinalWords(turn, 100), { t: 15, text: 'Appended # checked to receipts/__init__.py.' }, 'the final answer, not the interim remark');
eq(engFinalWords(turn, 14), { t: 12, text: "I'll append the requested comment using the patch tool." }, 'as of a time before the answer: what it had said by then');
eq(engFinalWords([you(1, 'hi'), says(2, 'hello')], 10), { t: 2, text: 'hello' }, 'a short answer is quoted');
eq(engFinalWords(turn.concat([you(20, 'and the refunds?')]), 100), null, 'your newer message: no answer yet');
eq(engFinalWords([you(1, 'go'), act(2, 'ls'), says(3, '   ')], 10), null, 'no words at all');
eq(engFinalWords([says(1, 'first turn answer'), you(5, 'again'), says(6, 'second turn answer')], 10).text, 'second turn answer', 'the latest turn');
eq(engFinalWords([you(1, 'go'), { t: 2, kind: 'say', who: 'A', text: 'short', full: 'short\nand the rest' }], 10).text, 'short\nand the rest', 'the whole message');
console.log(`final words: ${n} passed`);
