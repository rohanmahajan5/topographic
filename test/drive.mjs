#!/usr/bin/env node
// Drives agent-ide/workspace/live.html as a user: real pointer and keyboard events through Playwright, targets found by name
// in the page's hit registry (window.__hits), outcomes checked against window.__state() AFTER each action.
//   node drive.mjs <plan.json> [--url http://127.0.0.1:4321/?tick=1] [--size 800x500] [--out <dir>] [--var name=value ...] [--chrome]
//   --chrome: the installed Google Chrome, headless, in place of Playwright's own Chromium (when that is not downloaded)
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const planPath = argv.find((a, i) => !a.startsWith('--') && (i === 0 || !argv[i - 1].startsWith('--') || argv[i - 1] === '--chrome'));
const ARGS_OK = argv.every((a, i) => !a.startsWith('--') || ['--url', '--size', '--out', '--var', '--chrome'].includes(a));
if (!ARGS_OK) { console.error('unknown option; options are --url, --size, --out, --var, --chrome'); process.exit(2); }
if (!planPath) { console.error('usage: node drive.mjs <plan.json> [--url URL] [--size WxH] [--out DIR]'); process.exit(2); }
const plan = JSON.parse(fs.readFileSync(planPath, 'utf8'));
const actions = Array.isArray(plan) ? plan : plan.actions;
const url = opt('--url', (plan.url) || 'http://127.0.0.1:4321/?tick=1');
const [VW, VH] = opt('--size', plan.size || '800x500').split('x').map(Number);
const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
const out = path.resolve(opt('--out', path.join(path.dirname(new URL(import.meta.url).pathname), 'out', path.basename(planPath, '.json') + '-' + VW + 'x' + VH + '-' + stamp)));
fs.mkdirSync(out, { recursive: true });

const t0 = Date.now();
const log = [], consoleLog = [], pageErrors = [], asserts = [], shots = [];
const ts = () => ((Date.now() - t0) / 1000).toFixed(2).padStart(7) + 's';
const say = line => { const l = ts() + '  ' + line; console.log(l); log.push(l); };
const vars = {};
argv.forEach((a, i) => { if (a === '--var') { const [k, ...v] = argv[i + 1].split('='); vars[k] = v.join('='); } });      // --var name=value, used as $name
// "$x" is a saved name; "$x.id" the part after its first colon; "$x.session" its second segment (column, label, block names);
// "$x.column" what lies between its first and last colon (an attention icon's column, whose name may hold a colon: attn:<column>:<kind>)
const sub = v => typeof v !== 'string' ? v : v.replace(/\$([A-Za-z_]\w*)(?:\.(id|session|column))?/g, (m, k, f) => {
  if (!(k in vars)) throw new Error('unknown variable $' + k);
  const n = vars[k]; if (!f) return n;
  const rest = n.slice(n.indexOf(':') + 1);
  return f === 'id' ? rest : f === 'column' ? rest.slice(0, rest.lastIndexOf(':')) : rest.split(':')[0];
});
const subAll = o => Array.isArray(o) ? o.map(subAll) : o && typeof o === 'object' ? Object.fromEntries(Object.entries(o).map(([k, v]) => [k, subAll(v)])) : sub(o);

const browser = await chromium.launch(argv.includes('--chrome') ? { headless: true, channel: 'chrome' } : { headless: true });
const page = await browser.newPage({ viewport: { width: VW, height: VH }, deviceScaleFactor: 1 });
page.on('console', m => consoleLog.push({ at: (Date.now() - t0) / 1000, type: m.type(), text: m.text() }));
page.on('pageerror', e => { pageErrors.push({ at: (Date.now() - t0) / 1000, message: e.message, stack: e.stack }); say('PAGEERROR ' + e.message); });

let failed = null;
const ready = () => page.evaluate(() => typeof window.__hits === 'function' && !!window.__state && window.__state().frames > 0).catch(() => false);
const state = () => page.evaluate(() => window.__state());
const hitsNow = prefix => page.evaluate(p => window.__hits(p), prefix || '');
async function settle(ms = 3000) {         // camera flights and drags finish, then one more frame is drawn
  const end = Date.now() + ms;
  while (Date.now() < end) { const s = await state(); if (!s.moving) break; await page.waitForTimeout(50); }
  const f = (await state()).frames; const e2 = Date.now() + 1000;
  while (Date.now() < e2 && (await state()).frames < f + 2) await page.waitForTimeout(20);
}
// a target: an exact name, or "prefix*" for the first name that starts with it; "with": "notch:" keeps only names X:<id>
// for which notch:<id> (or notch:<id>:...) also exists; a list of prefixes requires all of them
// "provider": "claude" keeps only targets whose column (the name's second segment) is a session of that harness (__state().providers)
// "not": "<id>" drops targets whose column (the name's second segment) is that one ("another column than $c"); a list drops each
// walk robustness (2026-11-07), the floor is live and its layout glides: a target is aimed at only once its rectangle has held still
// over two reads (a folder closed or a row folded back slides its neighbours for a moment; a click aimed mid-glide lands where the
// target was); "reveal": true brings a target that is registered but off screen or under the rail into view first, by dragging the
// floor (a drag on bare canvas pans it), as a user would; still not clickable after that, it is a miss as before
// a '*' inside a name stands for any text there ("attn:*:error": an error icon on any column); the registry is read from what comes
// before the first '*'
const glob = spec => new RegExp('^' + spec.split('*').map(x => x.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$');
async function resolve(spec, withPre, prov, not, rev) {
  const star = spec.endsWith('*'), inner = spec.slice(0, -1).includes('*'), key = inner ? spec.slice(0, spec.indexOf('*')) : star ? spec.slice(0, -1) : spec;
  const hs = await hitsNow(key);
  const names = new Set((withPre ? await hitsNow('~') : hs).map(h => h.name));
  let cand = hs.filter(h => h.x != null && (inner ? glob(spec).test(h.name) : star ? h.name.startsWith(key) : h.name === key));
  if (withPre) cand = cand.filter(h => [].concat(withPre).every(w => { const k = w + h.name.slice(h.name.indexOf(':') + 1).replace(/#\d+$/, ''); return names.has(k) || [...names].some(n => n.startsWith(k + ':')); }));
  if (prov) { const pm = (await state()).providers || {}; cand = cand.filter(h => pm[h.name.slice(h.name.indexOf(':') + 1).split(':')[0].replace(/#\d+$/, '')] === prov); }
  if (not) cand = cand.filter(h => ![].concat(not).includes(h.name.slice(h.name.indexOf(':') + 1).split(':')[0].replace(/#\d+$/, '')));          // a list: none of them
  const ok = cand.find(h => h.reachable);
  if (ok) return still(ok);
  if (rev && cand.length && await panToward(cand[0])) { await settle(); return resolve(spec, withPre, prov, not, rev === true ? 4 : rev - 1); }
  const why = cand.length ? `registered but no point on it is clickable (covered or off screen): ${JSON.stringify(cand[0])}` : 'not found';
  const present = [...names].sort();
  throw Object.assign(new Error(`miss: ${spec}${withPre ? ' with ' + withPre : ''}${prov ? ' of provider ' + prov : ''} ${why}`), { present });
}
const sameBox = (p, q) => p && q && q.x != null && Math.abs(p.x - q.x) < 0.75 && Math.abs(p.y - q.y) < 0.75 && Math.abs(p.w - q.w) < 0.75 && Math.abs(p.h - q.h) < 0.75;
async function still(h) {
  let prev = h;
  for (let w0 = Date.now(); Date.now() - w0 < 2500;) {
    await page.waitForTimeout(120);
    const now = (await hitsNow(h.name)).find(q => q.name === h.name);
    if (!now || now.x == null) return prev;          // gone: the caller's action reports it
    if (sameBox(prev, now)) return now.reachable ? now : prev;
    prev = now;
  }
  say(`           ${h.name} still moving after 2.5 s; aimed at its latest place`);
  return prev;
}
// one drag of the floor that moves a target's centre toward the middle of the view (at most 30% of the view's width and 25% of its
// height per drag); false when it is already there or the grab point is not bare canvas (a window or the rail would not pan)
async function panToward(h) {
  const lim = (v, m) => Math.max(-m, Math.min(m, v)), tx = VW / 2, ty = VH * 0.42;
  const dx = Math.round(lim(tx - (h.x + h.w / 2), VW * 0.3)), dy = Math.round(lim(ty - (h.y + h.h / 2), VH * 0.25));
  if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return false;
  const sx = Math.round(tx - dx / 2), sy = Math.round(ty - dy / 2);
  const bare = await page.evaluate(([x, y, x1, y1]) => {          // the floor is the page's largest canvas (the inline mini-map is a canvas too)
    const fl = [...document.querySelectorAll('canvas')].sort((p, q) => q.clientWidth * q.clientHeight - p.clientWidth * p.clientHeight)[0];
    return [[x, y], [x1, y1]].every(([u, v]) => document.elementFromPoint(u, v) === fl);
  }, [sx, sy, sx + dx, sy + dy]);
  if (!bare) return false;
  await page.mouse.move(sx, sy); await page.mouse.down(); await page.mouse.move(sx + dx, sy + dy, { steps: 12 }); await page.mouse.up();
  await page.mouse.move(1, 1);          // the pointer off the floor: left where the drag ended, its hover tip can cover the target
  say(`           reveal ${h.name}: was at (${Math.round(h.x + h.w / 2)},${Math.round(h.y + h.h / 2)}), floor dragged by (${dx},${dy})`);
  return true;
}
// a condition as in an assert: state paths ("!" negates) and "has"/"lacks" over the registry's names; true when every entry holds
async function holds(cond) {
  const s = await state(), names = ('has' in cond || 'lacks' in cond) ? (await hitsNow('~')).map(h => h.name) : [];
  return Object.entries(cond).every(([k, exp]) => {
    if (k === 'has') return names.some(n => match(exp, n));
    if (k === 'lacks') return !names.some(n => match(exp, n));
    const neg = k.startsWith('!'); return match(exp, getPath(s, neg ? k.slice(1) : k)) !== neg;
  });
}
const rect = h => `${h.name} [${h.x.toFixed(1)},${h.y.toFixed(1)} ${h.w.toFixed(1)}x${h.h.toFixed(1)}] at (${h.cx},${h.cy})`;
const getPath = (o, k) => k.split('.').reduce((v, p) => v == null ? v : v[p], o);
function match(exp, got) {
  if (exp === true) return !!got && (!Array.isArray(got) || got.length > 0);
  if (exp === false) return !got || (Array.isArray(got) && !got.length);
  if (exp === null) return got == null;
  if (Array.isArray(got) && !Array.isArray(exp)) return got.some(g => match(exp, g));
  if (typeof exp === 'string' && exp.slice(0, -1).includes('*')) return typeof got === 'string' && glob(exp).test(got);          // a '*' inside: any text there
  if (typeof exp === 'string' && exp.endsWith('*')) return typeof got === 'string' && got.startsWith(exp.slice(0, -1));
  if (typeof exp === 'object') return JSON.stringify(exp) === JSON.stringify(got);
  return exp === got;
}

try {
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  // polled with evaluate (the page's CSP forbids the string eval waitForFunction uses)
  for (const w0 = Date.now(); !(await ready()); await page.waitForTimeout(100)) if (Date.now() - w0 > 30000) throw new Error('the page drew no frame in 30 s (no window.__hits or no project loaded)');
  await settle();
  const s0 = await state();
  say(`loaded ${url} at ${VW}x${VH}: ${s0.columns} columns, connected=${s0.connected}, project=${s0.project}`);
  for (let i = 0; i < actions.length; i++) {
    const tag = `#${String(i + 1).padStart(2, '0')}`;
    // walk robustness (2026-11-06): {"if": "<name>", "with"/"provider"/"not": ..., "then": [actions]} runs its actions only when that
    // target is on screen and clickable now; otherwise it logs why and skips them (the floor is live: a path strip or an uncovered
    // stub is not always there). Not an assert: a skip is never a miss.
    // "when": {state path: expected, ...} (as in an assert, "!" negates) is a further condition, read from the page's state before the
    // target is looked for (2026-11-07: a folder name is a toggle, so "open it again" must not run on a folder that was never closed)
    // A variable the walk never saved (its step was skipped) makes the condition false: "if we closed it, open it again".
    // {"when": {...}, "then": [...]} without "if": the state condition alone (no target is looked for)
    if (actions[i].if || (actions[i].when && actions[i].then)) {
      const g = actions[i];
      let ok = null, why = '', spec = g.if || 'state', unmet = [];
      if (g.if) try { spec = sub(g.if); } catch (e) { why = e.message; }
      if (!why && g.when) { try { for (const [k, exp] of Object.entries(subAll(g.when))) if (!(await holds({ [k]: exp }))) unmet.push(k + ' ' + JSON.stringify(exp)); } catch (e) { why = e.message; } }
      if (why) {}
      else if (unmet.length) why = 'state: not ' + unmet.join(', ');
      else if (!g.if) ok = { name: JSON.stringify(subAll(g.when)) };
      else try { ok = await (async () => { await settle(); return resolve(spec, g.with ? subAll(g.with) : g.with, g.provider, g.not ? sub(g.not) : g.not, g.reveal); })(); } catch (e) { why = e.message.replace(/^miss: /, ''); }
      if (ok) { say(`${tag} if ${spec}: ${ok.name} is there; running ${(g.then || []).length} steps`); actions.splice(i + 1, 0, ...(g.then || [])); }
      else say(`${tag} if ${spec}: skipped ${(g.then || []).length} steps (${why.slice(0, 160)})`);
      continue;
    }
    const a = subAll(actions[i]);
    if (a.click || a.hover || a.drag) {
      await settle();
      let h = await resolve(a.click || a.hover || a.drag, a.with, a.provider, a.not, a.reveal);
      const rectOf = async n => { const x = (await hitsNow(n)).find(q => q.name === n); return x && x.x != null ? x : null; };
      const g0 = a.grows ? await rectOf(a.grows) : null;
      if (a.save) vars[a.save] = h.name.replace(/#\d+$/, '');
      if (a.click) {
        await page.mouse.click(h.cx, h.cy); say(`${tag} click ${rect(h)}`);
        // "reaim": n with "expect": {state}: a live column's labels restack as its turns arrive, so a click can land where the target
        // was a moment ago. When the expected state has not come and the target is still there but no longer under the click point,
        // click it again at its new place, at most n times. A click that landed on the target is never repeated, so a click the
        // page gets wrong still fails the assert that follows
        for (let k = 0; a.expect && k < (a.reaim || 0); k++) {
          await page.waitForTimeout(a.after != null ? a.after : 150);
          if (await holds(a.expect)) break;
          const now = (await hitsNow(h.name)).find(q => q.name === h.name);
          if (!now || now.x == null || (h.cx >= now.x && h.cx <= now.x + now.w && h.cy >= now.y && h.cy <= now.y + now.h)) break;
          await settle(); h = await resolve(h.name, null, null, null, a.reveal);
          await page.mouse.click(h.cx, h.cy); say(`${tag} re-aimed (it moved): click ${rect(h)}`);
        }
      }
      else if (a.hover) {
        await page.mouse.move(h.cx, h.cy, { steps: 4 }); say(`${tag} hover ${rect(h)}`);
        // "reaim": n: the hover itself or the live floor can move the target out from under a still pointer (a row folding back, a
        // running parent's readout taking a second line); while the pointer is no longer inside the target's rectangle, aim again,
        // at most n times. A target that stays put is never re-aimed, so a hover the page gets wrong still fails its assert
        for (let k = 0; k < (a.reaim || 0); k++) {
          await page.waitForTimeout(a.after != null ? a.after : 150);
          const now = (await hitsNow(h.name)).find(q => q.name === h.name);
          if (now && now.x != null && h.cx >= now.x && h.cx <= now.x + now.w && h.cy >= now.y && h.cy <= now.y + now.h) break;
          await settle(); h = await resolve(h.name, null, null, null, a.reveal);
          await page.mouse.move(h.cx, h.cy, { steps: 4 }); say(`${tag} re-aimed (it moved): hover ${rect(h)}`);
        }
      }
      else {
        // "to": "<name>", "frac": f: released at that fraction of the named target's width (its left edge 0, its right 1), on the
        // dragged target's own row; for the rail's knob, a point on the track read just before the drag, so a plan names a moment's
        // share of the rail rather than pixels that hold for one track length only. Without "to": moved by dx, dy as before
        let dx = a.dx || 0, dy = a.dy || 0, how = `by (${dx},${dy})`;
        if (a.to) {
          const t = await rectOf(a.to); if (!t) throw new Error(`miss: ${a.to} (the drag's "to") is not on screen`);
          const f = a.frac != null ? a.frac : 0.5; dx = Math.round(t.x + f * t.w - h.cx); dy = 0;
          how = `to ${Math.round(f * 100)}% of ${a.to} [${t.x.toFixed(1)}+${t.w.toFixed(1)}]: by (${dx},0)`;
        }
        await page.mouse.move(h.cx, h.cy); await page.mouse.down();
        await page.mouse.move(h.cx + dx, h.cy + dy, { steps: 12 }); await page.mouse.up();
        say(`${tag} drag ${rect(h)} ${how}`);
      }
      await page.waitForTimeout(a.after != null ? a.after : 150);
      if (a.grows) {          // "grows": "<name>": that target's rectangle must be wider or taller after the action, and neither smaller
        await settle(); const g1 = await rectOf(a.grows);
        const ok = !!(g0 && g1 && g1.w >= g0.w - 0.5 && g1.h >= g0.h - 0.5 && (g1.w > g0.w + 0.5 || g1.h > g0.h + 0.5));
        const fails = ok ? [] : [`${a.grows}: ${g0 ? g0.w.toFixed(0) + 'x' + g0.h.toFixed(0) : 'absent'} -> ${g1 ? g1.w.toFixed(0) + 'x' + g1.h.toFixed(0) : 'absent'}`];
        asserts.push({ step: i + 1, assert: { grows: a.grows }, ok, fails, before: g0 && [g0.w, g0.h], after: g1 && [g1.w, g1.h] });
        say(`${tag} grows ${a.grows}: ${g0 ? g0.w.toFixed(0) + 'x' + g0.h.toFixed(0) : '-'} -> ${g1 ? g1.w.toFixed(0) + 'x' + g1.h.toFixed(0) : '-'} ${ok ? 'PASS' : 'FAIL'}`);
      }
    } else if (a.type != null) {
      await page.keyboard.type(a.type, { delay: 15 }); say(`${tag} type ${JSON.stringify(a.type)}`);
    } else if (a.until && !a.key) {
      // {"until": {state path: expected, ...}, "timeout": ms}: waits until every entry matches the page's state (as in an assert), at
      // most timeout ms (default 10000); logs how long it took or that it did not come (the assert that follows decides). For results
      // that come from the server, whose answer time depends on its load, not on the page
      const lim = a.timeout || 10000, w0 = Date.now();
      let ok = false; while (!(ok = await holds(a.until)) && Date.now() - w0 < lim) await page.waitForTimeout(150);
      say(`${tag} until ${JSON.stringify(a.until)} ${ok ? 'reached after ' + (Date.now() - w0) + ' ms' : 'not reached in ' + lim + ' ms'}`);
    } else if (a.key && a.until) {
      // {"key": k, "until": {state path: expected, ...}, "max": n}: press k until every entry matches the page's state, at most n times
      // (the merge of ide-closed, 2026-11-06: a moment on the rail is found by stepping through moments, not by a pixel offset that
      // means another time at every window size and as the repository's history grows)
      const lim = a.max || 200, hit = s => Object.entries(a.until).every(([k, exp]) => match(exp, getPath(s, k)));
      let n = 0; for (; n < lim && !hit(await state()); n++) { await page.keyboard.press(a.key); await page.waitForTimeout(a.after != null ? a.after : 150); }
      const ok = hit(await state());
      say(`${tag} key ${a.key} x${n} until ${JSON.stringify(a.until)} ${ok ? 'reached' : 'not reached'}`);
    } else if (a.key) {
      await page.keyboard.press(a.key); say(`${tag} key ${a.key}`); await page.waitForTimeout(a.after != null ? a.after : 150);
    } else if (a.wheel != null) {          // {"wheel": 400, "at": "<name>"}: the wheel over that target (or the view's centre); positive zooms out
      await settle(); const h = a.at ? await resolve(a.at, a.with, a.provider, a.not, a.reveal) : null, x = h ? h.cx : VW / 2, y = h ? h.cy : VH / 2;
      await page.mouse.move(x, y); await page.mouse.wheel(0, a.wheel); say(`${tag} wheel ${a.wheel} at (${x},${y})`); await page.waitForTimeout(a.after != null ? a.after : 400);
    } else if (a.mock) {          // {"mock": "answer"}: the attention mock's next outside event (GET /api/mock, mock.act), as the person or another app would cause it; never through the page
      const r = await fetch(new URL('/api/mock?do=' + encodeURIComponent(a.mock), url)); const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error('mock ' + a.mock + ': ' + (body.error || r.status));
      say(`${tag} mock ${a.mock}: ${JSON.stringify(body)}`); await page.waitForTimeout(a.after != null ? a.after : 0);
    } else if (a.clock != null) {          // {"clock": 660}: the page's clock moved that many seconds forward (window.__clock: a mock floor only); "after" waits
      const v = await page.evaluate(sec => window.__clock ? window.__clock(sec) : null, a.clock);
      if (v == null) throw new Error('clock: the page did not move its clock (window.__clock needs a mock floor)');
      say(`${tag} clock +${a.clock} s (moved ${v} s in all)`); await page.waitForTimeout(a.after != null ? a.after : 0);
    } else if (a.reload) {          // {"reload": true}: the page loads again (same browser, so its local storage stays) and is waited on as at the start
      await page.reload({ waitUntil: 'domcontentloaded' });
      for (const w0 = Date.now(); !(await ready()); await page.waitForTimeout(100)) if (Date.now() - w0 > 30000) throw new Error('the page drew no frame in 30 s after the reload');
      await settle(); say(`${tag} reload`); await page.waitForTimeout(a.after != null ? a.after : 0);
    } else if (a.wait != null) {
      await page.waitForTimeout(a.wait); say(`${tag} wait ${a.wait} ms`);
    } else if (a.waitFor) {
      const pre = a.waitFor.replace(/\*$/, ''), lim = a.timeout || 10000, w0 = Date.now(); let found = null;
      while (Date.now() - w0 < lim) { const hs = await hitsNow(pre); found = hs.find(h => h.name.startsWith(pre) && h.x != null); if (found) break; await page.waitForTimeout(200); }
      if (!found) { const hs = await hitsNow('~'); throw Object.assign(new Error(`waitFor ${a.waitFor}: none after ${lim} ms`), { present: hs.map(h => h.name).sort() }); }
      if (a.save) vars[a.save] = found.name.replace(/#\d+$/, '');
      say(`${tag} waitFor ${a.waitFor}: ${rect(found)} after ${Date.now() - w0} ms`);
    } else if (a.shot) {
      const f = path.join(out, a.shot.endsWith('.png') ? a.shot : a.shot + '.png');
      await page.screenshot({ path: f }); shots.push(path.basename(f)); say(`${tag} shot ${path.basename(f)}`);
    } else if (a.assert) {
      await page.waitForTimeout(a.settle != null ? a.settle : 100);
      const s = await state(), hs = await hitsNow('~'), names = hs.map(h => h.name), fails = [];
      for (const [k, exp] of Object.entries(a.assert)) {
        if (k === 'has') { if (!names.some(n => match(exp, n))) fails.push(`no hit ${exp}`); continue; }
        if (k === 'lacks') { if (names.some(n => match(exp, n))) fails.push(`hit ${exp} present`); continue; }
        if (k === 'has_html') { if (!(await page.$(exp))) fails.push(`no element matches ${exp}`); continue; }      // a CSS selector that must match in the page
        const neg = k.startsWith('!'), kk = neg ? k.slice(1) : k, got = getPath(s, kk);      // "!key": must NOT match
        if (match(exp, got) === neg) fails.push(`${kk}: expected ${neg ? 'not ' : ''}${JSON.stringify(exp)}, got ${JSON.stringify(got)}`);
      }
      const ok = !fails.length;
      asserts.push({ step: i + 1, assert: a.assert, ok, fails, note: a.note || null });
      say(`${tag} assert ${JSON.stringify(a.assert)} ${ok ? 'PASS' : 'FAIL: ' + fails.join('; ')}`);
      if (!ok && a.stop) throw new Error(`stopping: required assert at step ${i + 1} failed`);
    } else if (a.sweep != null) {
      // the registry against the hit-test: the real mouse onto every registered target with that prefix; the page's own
      // hover pick (the same pick() a click uses) must name the same object (a block names its column)
      await settle();
      const names0 = (await hitsNow('~')).map(h => h.name).filter(n => n.startsWith(a.sweep) && n !== 'line' && !n.startsWith('chat'));
      const bad = [], skipped = []; let n = 0;
      for (const nm of names0.slice(0, a.limit || 400)) {
        const h = (await hitsNow(nm)).find(x => x.name === nm);
        if (!h || h.x == null) { skipped.push(nm + ' (gone)'); continue; }
        if (!h.reachable) { skipped.push(nm + ' (covered or off screen)'); continue; }
        await page.mouse.move(h.cx, h.cy);
        const f = (await state()).frames; for (let k = 0; k < 20 && (await state()).frames < f + 2; k++) await page.waitForTimeout(20);
        const want = nm.startsWith('block:') ? 'column:' + nm.split(':')[1] : nm.replace(/#\d+$/, ''), got = (await state()).hover;
        n++; if (got !== want) bad.push(`${nm} at (${h.cx},${h.cy}) hovered ${got}`);
      }
      await page.mouse.move(1, 1);
      asserts.push({ step: i + 1, assert: { sweep: a.sweep }, ok: !bad.length, fails: bad, skipped, checked: n });
      say(`${tag} sweep ${a.sweep || '(all)'}: ${n} targets hovered, ${bad.length} mismatched, ${skipped.length} skipped${bad.length ? ': ' + bad.join('; ') : ''}`);
      if (skipped.length) say(`           skipped: ${skipped.join(', ')}`);
    } else if (a.log) {
      say(`${tag} log ${a.log}`);
    } else throw new Error(`unknown action at step ${i + 1}: ${JSON.stringify(actions[i])}`);
  }
} catch (e) {
  failed = { message: e.message, present: e.present || null };
  say('FAILED ' + e.message + (e.present ? '\n           names present: ' + e.present.join(', ') : ''));
  try { await page.screenshot({ path: path.join(out, 'failure.png') }); shots.push('failure.png'); } catch (e2) {}
}
const final = await state().catch(() => null);
const summary = { plan: path.resolve(planPath), url, size: `${VW}x${VH}`, startedAt: new Date(t0).toISOString(), seconds: (Date.now() - t0) / 1000,
  ok: !failed && asserts.every(x => x.ok) && !pageErrors.length, failed, asserts: { pass: asserts.filter(x => x.ok).length, fail: asserts.filter(x => !x.ok).length, list: asserts },
  pageErrors, console: consoleLog, shots, log, finalState: final };
fs.writeFileSync(path.join(out, 'run.json'), JSON.stringify(summary, null, 1));
say(`${summary.ok ? 'PASS' : 'FAIL'}: ${summary.asserts.pass} asserts passed, ${summary.asserts.fail} failed, ${pageErrors.length} page errors; ${out}/run.json`);
await browser.close();
process.exit(summary.ok ? 0 : 1);
