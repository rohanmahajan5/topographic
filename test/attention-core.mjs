// The attention registry (live.html's AttnCore) on synthetic agents, without a browser: node attention-core.mjs
// It takes AttnCore's source out of live.html and runs it against stand-ins for the page (localStorage, the document, the clock),
// so each rule of the error mark (2026-11-09) is checked on its own: an error followed by work is no mark, an error left
// alone is a mark until you click or open the agent, a retried error is none, a subagent that failed and was resumed shows none,
// a folded subagent's mark goes with a click on the column it stands on, and a continued session keeps its seen marks.
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import assert from 'assert/strict';

const html = readFileSync(fileURLToPath(new URL('../workspace/live.html', import.meta.url)), 'utf8');
const a0 = html.indexOf('const AttnCore = (() => {'), a1 = html.indexOf('\n})();', a0);
assert.ok(a0 > 0 && a1 > a0, 'AttnCore not found in live.html');
const src = html.slice(a0, a1 + 6) + '\nreturn AttnCore;';

function page() {          // a fresh browser: empty storage, the tab in front
  const store = new Map();
  const g = {
    localStorage: { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)) },
    document: { title: 'floor', visibilityState: 'visible', hasFocus: () => true, querySelector: () => null, head: { appendChild: x => x }, createElement: () => ({}) },
    addEventListener: () => {}, window: {}, console
  };
  return Object.assign(new Function(...Object.keys(g), src)(...Object.values(g)), { store });
}
let NOW = Math.round(Date.now() / 1000);          // the registry counts what arose over an hour before its first load as read (since)
function floor(core, agents, extra = {}) {
  const ad = { now: () => NOW, agents: () => agents.map(a => Object.assign({ asks: [], errors: [], say: null, running: false, inactive: false, heard: true, lastAct: NOW - 5, parentSid: null }, a)),
    opened: () => extra.opened || null, hostOf: sid => (extra.fold || {})[sid] || sid };
  return { ad, att: core.create(ad) };
}
const errs = att => att.open().filter(i => i.kind === 'error').map(i => i.sid + '@' + i.t);
const tests = [];
const test = (name, f) => tests.push([name, f]);

test('an error followed by a new turn makes no mark', () => {
  const { att } = floor(page(), [{ sid: 'a', errors: [{ t: NOW - 60, text: 'API error', turn: 'e1', later: true }] }]);
  att.tick(); assert.deepEqual(errs(att), []);
});
test('an error left alone is a mark until the agent is clicked, also once the agent goes inactive', () => {
  const ag = [{ sid: 'a', errors: [{ t: NOW - 60, text: 'API error', turn: 'e1', later: false }] }], { att } = floor(page(), ag);
  att.tick(); assert.deepEqual(errs(att), ['a@' + (NOW - 60)]);
  NOW += 3600; ag[0].inactive = true; att.tick(); att.tick();
  assert.equal(errs(att).length, 1, 'inactive without new work: it stays');
  assert.equal(att.clear('a'), 1); att.tick(); assert.deepEqual(errs(att), [], 'the click cleared it and the next poll does not bring it back');
});
test('the mark closes when work comes after it: a later turn, a later answer, running again', () => {
  for (const after of [g => { g.errors[0].later = true; }, g => { g.say = { t: NOW, text: 'done' }; }, g => { g.running = true; }]) {
    const ag = [{ sid: 'a', errors: [{ t: NOW - 60, text: 'API error', turn: 'e1', later: false }] }], { att } = floor(page(), ag);
    att.tick(); assert.equal(errs(att).length, 1);
    after(ag[0]); att.tick(); assert.deepEqual(errs(att), []);
  }
});
test('a later failure replaces the earlier one: one mark, the newest', () => {
  const ag = [{ sid: 'a', errors: [{ t: NOW - 60, text: 'first', turn: 'e1', later: false }] }], { att } = floor(page(), ag);
  att.tick(); assert.deepEqual(errs(att), ['a@' + (NOW - 60)]);
  ag[0].errors = [{ t: NOW - 60, text: 'first', turn: 'e1', later: true }, { t: NOW - 10, text: 'second', turn: 'e2', later: false }];
  att.tick(); assert.deepEqual(errs(att), ['a@' + (NOW - 10)]);
});
test('a stream error the harness retries makes no mark; one it gave up on does, and survives the turn closing', () => {
  const ag = [{ sid: 'a', running: true }], { att } = floor(page(), ag);
  att.event('error', { threadId: 'a', turnId: 't1', willRetry: true, error: { message: 'reconnecting' } });
  att.tick(); assert.deepEqual(errs(att), []);
  att.event('error', { threadId: 'a', turnId: 't1', willRetry: false, error: { message: 'usage limit' } });
  att.tick(); assert.equal(errs(att).length, 1, 'still running for a moment while the failing turn closes: the mark stays');
  ag[0].running = false; att.tick(); assert.equal(errs(att).length, 1);
  ag[0].running = true; att.tick(); assert.deepEqual(errs(att), [], 'stopped, then running again: working again');
});
test('a subagent that failed, was resumed and finished shows no mark; one left failed does, on its own', () => {
  const { att } = floor(page(), [{ sid: 'p', running: true }, { sid: 'p/k1', parentSid: 'p', errors: [{ t: NOW - 600, text: 'API error', turn: 'x', later: true }], say: { t: NOW - 300, text: 'report' } },
    { sid: 'p/k2', parentSid: 'p', errors: [{ t: NOW - 500, text: 'API error', turn: 'y', later: false }] }]);
  att.tick(); assert.deepEqual(errs(att), ['p/k2@' + (NOW - 500)]);
  assert.deepEqual(att.bySid('p'), [], 'a parent carries no mark for its children');
});
test('a click on the column a folded subagent stands on clears its mark; a click on another row does not', () => {
  const ag = [{ sid: 'p', running: true, errors: [{ t: NOW - 50, text: 'own', turn: 'o', later: false }] }, { sid: 'p/k', parentSid: 'p', errors: [{ t: NOW - 500, text: 'API error', turn: 'y', later: false }] }];
  let r = floor(page(), ag, { fold: { 'p/k': 'p' } }); r.att.tick();
  assert.equal(r.att.clear('p'), 1, 'own marks only (the mini-map row)'); assert.deepEqual(errs(r.att), ['p/k@' + (NOW - 500)]);
  r = floor(page(), ag, { fold: { 'p/k': 'p' } }); r.att.tick();
  assert.equal(r.att.clear('p', true), 2, 'the column: its own and the folded subagent\'s'); r.att.tick(); assert.deepEqual(errs(r.att), []);
});
test('opening the chat reads the column and what stands on it', () => {
  const ag = [{ sid: 'p', errors: [{ t: NOW - 50, text: 'own', turn: 'o', later: false }] }, { sid: 'p/k', parentSid: 'p', errors: [{ t: NOW - 500, text: 'API error', turn: 'y', later: false }] }];
  const extra = { fold: { 'p/k': 'p' } }, { att } = floor(page(), ag, extra);
  att.tick(); assert.equal(errs(att).length, 2);
  extra.opened = 'p'; att.tick(); assert.deepEqual(errs(att), []);
  extra.opened = null; extra.fold = {}; att.tick(); assert.deepEqual(errs(att), [], 'unfolded later: still read');
});
test('a continued session keeps its seen marks and its unread mark, under the new id', () => {
  const core = page(), ag = [{ sid: 'old', errors: [{ t: NOW - 60, text: 'API error', turn: 'e1', later: false }] }];
  let { att } = floor(core, ag); att.tick(); assert.equal(att.clear('old'), 1);
  att.move(new Map([['old', 'new']])); ag[0].sid = 'new'; att.tick();
  assert.deepEqual(errs(att), [], 'seen under the old id: not back under the new one');
  ({ att } = floor(core, ag)); att.tick(); assert.deepEqual(errs(att), [], 'nor after a reload');
  const core2 = page(), ag2 = [{ sid: 'old', errors: [{ t: NOW - 60, text: 'API error', turn: 'e1', later: false }] }];
  ({ att } = floor(core2, ag2)); att.tick();
  att.move(new Map([['old', 'new']])); ag2[0].sid = 'new'; att.tick();
  assert.deepEqual(errs(att), ['new@' + (NOW - 60)], 'unread: one mark, moved, not a second under the new id');
  assert.equal(att.clear('new'), 1); att.tick(); assert.deepEqual(errs(att), []);
});

let bad = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { bad++; console.log('FAIL ' + name + '\n     ' + e.message.split('\n').join('\n     ')); }
}
console.log(bad ? bad + ' of ' + tests.length + ' failed' : 'all ' + tests.length + ' passed');
process.exit(bad ? 1 : 0);
