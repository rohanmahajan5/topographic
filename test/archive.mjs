#!/usr/bin/env node
// Checks the archive stack on live.html headlessly (read only: nothing is sent; real pointer and keys, as drive.mjs does). For each
// floor: zero page errors; who is in the archive live and at replay moments either side of a chat's 48-hour line; the stack furthest
// left of the root lane; its label, the floor's lowest-priority text, a one-line stack beside the pile: at the fitted view, zoomed out
// and zoomed in, folded, hovered and open, it overlaps nothing (the page's own list of what it keeps off, and on its own the hit
// registry's targets and the texts printed before it), it is printed whenever a clean spot exists (on the first one), and printing it
// moves no name; open in place: the
// archived chats out oldest first, the lane's own columns moved right without a jump, the subfolders not moved, no attention icon on
// an archived chat; Esc folds it; a second click folds it; the mini-map's one archive row.
//   node agent-ide/test/archive.mjs [--port 4436] [--size 1440x900] [--shots DIR] [--floor many|few] [--cwd <the many floor>]
// The floors: this repository (many chats) and ~/projects/habit-tracker (few; its line moved to 24 h
// with ?line=24 so a replay moment crosses it).
import { chromium } from 'playwright';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
const REPO = execFileSync('git', ['-C', path.dirname(fileURLToPath(import.meta.url)), 'rev-parse', '--show-toplevel']).toString().trim();          // this repository
const argv = process.argv.slice(2), opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const port = opt('--port', '4436'), [W, H] = opt('--size', '1440x900').split('x').map(Number), shots = opt('--shots', null);
const FLOORS = { many: { cwd: opt('--cwd', REPO), extra: '' },
  few: { cwd: os.homedir() + '/projects/habit-tracker', extra: '&line=24' } };
const only = opt('--floor', null);
const fails = []; const ok = (c, msg) => { console.log((c ? 'ok   ' : 'FAIL ') + msg); if (!c) fails.push(msg); };
const browser = await chromium.launch({ headless: true });
async function load(floor) {
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 }), errs = [];
  page.on('pageerror', e => errs.push(String(e))); page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
  await page.goto(`http://127.0.0.1:${port}/?tick=1&minimap=inline&cwd=${encodeURIComponent(FLOORS[floor].cwd)}${FLOORS[floor].extra}`);
  for (let i = 0; i < 160; i++) { if (await page.evaluate(() => !!(window.__state && window.__state().archive && window.__state().columns > 0)).catch(() => false)) break; await page.waitForTimeout(250); }
  await page.waitForTimeout(1500);
  await still(page);
  return { page, errs };
}
async function still(page) {          // the camera settles (the git tree's first answer refits it): the stack still for a second
  let last = '', same = 0;
  for (let i = 0; i < 60 && same < 4; i++) { const f = await page.evaluate(() => JSON.stringify(window.__state().archive.foot)); same = f === last ? same + 1 : 0; last = f; await page.waitForTimeout(300); }
}
const st = page => page.evaluate(() => window.__state().archive);
const rewind = async (page, agoH) => { await page.evaluate(h => window.__p.rewindTo(window.__p.now - h * 3600), agoH); await page.waitForTimeout(900); return st(page); };
const settle = page => page.waitForTimeout(1200);
const away = async page => { await page.mouse.move(W - 3, Math.round(H / 2)); await page.waitForTimeout(500); };
const pile = page => page.evaluate(() => window.__hits('archive').filter(h => h.name === 'archive' && h.reachable));
// the label overlaps nothing: what the page keeps it off (clash), and on their own the hit registry's targets (minus the ground under
// it, the rail and the git tree's slanted lanes, whose boxes are mostly empty floor) and the texts printed before it; and it is fair:
// printed on the first spot that meets nothing, every spot before it (all of them when not printed) blocked by something real there
const GROUND = /^(archive|dir|plot|tile|page|block|rail|line|lane|lane-here|trunk)(#|$)/;
async function labelClean(page, floor, where) {
  const a = await st(page), l = a.label, hs = await page.evaluate(() => window.__hits(''));
  const meet = (b, q) => b[0] < q[2] && b[2] > q[0] && b[1] < q[3] && b[3] > q[1];
  const hitsOver = l.box ? hs.filter(h => h.x != null && !GROUND.test(h.kind) && meet(l.box, [h.x, h.y, h.x + h.w, h.y + h.h])).map(h => h.name) : [];
  const textsOver = l.box ? l.texts.filter(t => meet(l.box, t.split(',').map(Number))) : [];
  const blocked = l.tried.filter(t => t.by.length).length;
  console.log(floor + ' ' + where + ': zoom ' + l.zoom + ', label ' + (l.shown ? 'printed ' + l.why + ' at ' + l.box.join(',') : 'not printed (' + l.why + ')') + '; ' + blocked + ' of ' + l.tried.length + ' spots blocked' + (l.shown ? '' : ', first: ' + JSON.stringify(l.tried[0].by)));
  ok(l.clash.length === 0 && !hitsOver.length && !textsOver.length, floor + ': ' + where + ', the label overlaps nothing' + (l.clash.length + hitsOver.length + textsOver.length ? ' — ' + JSON.stringify({ clash: l.clash, hits: hitsOver, texts: textsOver }) : ''));
  ok(l.fair, floor + ': ' + where + ', the label is printed whenever a clean spot exists (' + (l.shown ? 'printed on the first of ' + l.tried.length + ' spots that is clean' : 'none of ' + l.tried.length + ' is') + ')');
  return a;
}
async function onPile(page) {          // re-aimed until the pointer rests on it (a late camera refit can move it after the hit list was read)
  let hs = [], tip = null;
  for (let i = 0; i < 6 && tip !== 'archive'; i++) { hs = await pile(page); if (!hs.length) break; await page.mouse.move(hs[0].cx, hs[0].cy); await page.waitForTimeout(500); tip = await page.evaluate(() => window.__state().hover); }
  return tip === 'archive' ? hs[0] : null;
}
for (const floor of only ? [only] : Object.keys(FLOORS)) {
  console.log('---', floor);
  const { page, errs } = await load(floor);
  // the mini-map, opened by its icon (it is closed until asked)
  const icon = (await page.evaluate(() => window.__hits('minimap'))).find(h => h.name === 'minimap' && h.reachable);
  if (icon) { await page.mouse.click(icon.cx, icon.cy); await page.waitForTimeout(900); }
  await away(page);
  let a = await st(page);
  console.log('live:', a.count, 'chats', a.kids, 'subagents', JSON.stringify(a.memberAgoH), 'harness', JSON.stringify(a.harness), 'range', a.range);
  ok(a.count > 0, floor + ': the archive holds chats live');
  ok(a.rootLane[0] === '§archive', floor + ': the stack stands on the root lane\'s first desk: ' + a.rootLane.slice(0, 4).join(', '));
  ok(Object.values(a.memberAgoH).every(h => h > a.line) || a.harness.length, floor + ': every member is past the line (or archived by its harness)');
  ok(a.drawn.length === 0, floor + ': closed, no archived chat is drawn on the floor');
  const xs = a.rootLane.filter(id => id !== '§archive').map(id => a.feet[id] && a.feet[id][0]).filter(x => x != null);
  ok(a.foot && xs.every(x => x > a.foot[0]), floor + ': the stack is left of the lane\'s live columns (' + (a.foot && a.foot[0]) + ' < ' + Math.min(...xs) + ')');
  // the label at the fitted view, then on hover: over nothing, printed wherever it can be, and no name moved for it
  ok(a.label.zoom <= 1.01, floor + ': the fitted view (zoom ' + a.label.zoom + ')');
  a = await labelClean(page, floor, 'at the fitted view');
  const textsAway = a.label.texts;
  if (shots) await page.screenshot({ path: shots + '/' + floor + '-fit.png' });
  const p0 = await onPile(page);
  ok(!!p0, floor + ': hover is on the stack');
  if (p0) {
    a = await labelClean(page, floor, 'on hover');
    ok(JSON.stringify(a.label.texts) === JSON.stringify(textsAway), floor + ': printing it moved no name or readout (' + a.label.texts.length + ' printed boxes, the same as without it)');
    if (shots) await page.screenshot({ path: shots + '/' + floor + '-hover.png' });
    // open in place: real click; sample the oldest live column of the lane while it moves
    const before = await st(page), live0 = before.rootLane.find(id => id !== '§archive');
    const samples = [];
    await page.mouse.click(p0.cx, p0.cy);
    for (let i = 0; i < 40; i++) { samples.push(await page.evaluate(id => { const f = window.__state().archive.feet[id]; return f ? f[0] : null; }, live0)); await page.waitForTimeout(25); }
    await settle(page); await away(page); a = await st(page);
    const steps = samples.filter(x => x != null).map((x, i, arr) => i ? Math.abs(x - arr[i - 1]) : 0), maxStep = Math.max(...steps), moved = (samples[samples.length - 1] || 0) - (samples[0] || 0);
    console.log('open in place: ' + live0 + ' moved ' + moved.toFixed(1) + ' px, largest step between samples ' + maxStep.toFixed(1) + ' px over ' + samples.length + ' samples');
    ok(a.open, floor + ': a click opens it in place');
    ok(JSON.stringify(a.rootLane.slice(1, 1 + a.count)) === JSON.stringify(a.members), floor + ': the archived chats take the desks after the stack, oldest first');
    ok(a.drawn.length === a.count, floor + ': every archived chat is drawn (' + a.drawn.length + '/' + a.count + ')');
    ok(a.folders === before.folders, floor + ': no subfolder moved');
    ok(!live0 || moved > 0, floor + ': the lane\'s own columns moved right');
    ok(!live0 || maxStep < Math.max(60, moved / 3), floor + ': ... without a jump (largest step ' + maxStep.toFixed(1) + ' of ' + moved.toFixed(1) + ')');
    ok(a.onFloorAttn.length === 0, floor + ': no attention icon on an archived chat');
    await labelClean(page, floor, 'open');
    if (shots) await page.screenshot({ path: shots + '/' + floor + '-open.png' });
    const sh = await page.evaluate(id => window.__hits('stub:' + id).filter(h => h.name === 'stub:' + id && h.reachable), a.members[a.members.length - 1]);
    if (sh.length) { await page.mouse.move(sh[0].cx, sh[0].cy); await page.waitForTimeout(400); ok((await page.evaluate(() => window.__state().hover || '')).startsWith('stub:'), floor + ': an archived stub can be pointed at'); }
    // Esc folds it back
    await away(page); await page.keyboard.press('Escape'); await settle(page); a = await st(page);
    ok(!a.open && a.drawn.length === 0 && a.rootLane[1] === live0, floor + ': Esc folds it back; the lane closes up');
    await labelClean(page, floor, 'folded again');
    // a second click folds it
    let p1 = await onPile(page); if (p1) { await page.mouse.click(p1.cx, p1.cy); await settle(page); }
    p1 = await onPile(page); if (p1) { await page.mouse.click(p1.cx, p1.cy); await settle(page); }
    a = await st(page); ok(p1 && !a.open, floor + ': a click opens it and a second click folds it');
    await away(page);
    // zoomed out and zoomed in, at the pile (the wheel: 1.21x a notch): over nothing at each zoom, printed wherever it can be
    const zoomAt = await pile(page);
    if (zoomAt.length) {
      const zoomTo = async (lo, hi) => {          // small wheel turns at the pile until the zoom is in [lo, hi)
        for (let i = 0; i < 80; i++) { const z = (await st(page)).label.zoom; if (z >= lo && z < hi) break;
          const at = (await pile(page))[0] || zoomAt[0]; await page.mouse.move(at.cx + 30, at.cy - 30);
          await page.mouse.wheel(0, z < lo ? (z * 1.15 < hi ? -40 : -8) : (z / 1.15 >= lo ? 40 : 8)); await page.waitForTimeout(220); }
        await page.waitForTimeout(1000); await page.mouse.move(W - 3, Math.round(H / 2)); await page.waitForTimeout(600); return st(page); };
      for (const [lo, hi, tag] of [[0.3, 0.45, 'zoomed out'], [0.6, 0.8, 'a little out'], [1.5, 1.8, 'zoomed in'], [4, 5, 'far in'], [10, 11, 'very far in']]) {
        a = await zoomTo(lo, hi);
        ok(a.label.zoom >= lo && a.label.zoom < hi, floor + ': ' + tag + ' to ' + a.label.zoom + 'x the fit');
        await labelClean(page, floor, tag);
        if (shots && floor === 'many' && lo === 0.3) await page.screenshot({ path: shots + '/' + floor + '-zoomed-out.png' });
        if (shots && floor === 'many' && lo === 0.6) await page.screenshot({ path: shots + '/' + floor + '-a-little-out.png' });
        if (shots && floor === 'many' && lo === 1.5) await page.screenshot({ path: shots + '/' + floor + '-zoomed.png' });
      }
    }
    // the mini-map's row opens it
    const row = (await page.evaluate(() => window.__hits('minimap-archive'))).find(h => h.name === 'minimap-archive' && h.cx != null);
    ok(row && row.reachable, floor + ': the mini-map\'s archive row can be pointed at');
    if (row) { await page.mouse.move(row.cx, row.cy); await page.waitForTimeout(800); const t = (await st(page)).minimap; ok(t && t.tipFor === 'archive' && /Archive · \d+/.test(t.tip[0]), floor + ': its hover names the archive: ' + (t && JSON.stringify(t.tip)));
      await page.mouse.click(row.cx, row.cy); await settle(page); a = await st(page); ok(a.open, floor + ': the mini-map\'s archive row opens it'); await away(page); await page.keyboard.press('Escape'); await settle(page); }
  }
  // the mini-map: one archive row at the bottom of the names' list, its one icon; no overlaps
  a = await st(page);
  const mmInfo = a.minimap;
  console.log('mini-map rows:', JSON.stringify(mmInfo && mmInfo.rows));
  ok(mmInfo && mmInfo.rows.length && mmInfo.rows[mmInfo.rows.length - 1].startsWith('arch:Archive · ' + a.count), floor + ': the mini-map\'s last row is "Archive · ' + a.count + '"');
  ok(mmInfo && mmInfo.archIcons === 1 && mmInfo.pile, floor + ': one key icon on the archive row, a pile on the mini floor');
  ok(mmInfo && mmInfo.overlaps === 0, floor + ': no overlapping texts in the mini-map (' + (mmInfo && JSON.stringify(mmInfo.overlapPairs)) + ')');
  // the 48-hour line in the replay
  if (floor === 'many') {
    const a0 = await st(page), ids = Object.entries(a0.memberAgoH).filter(([id]) => !a0.harness.includes(id)).sort((p, q) => p[1] - q[1]);
    const [other, agoH] = ids[0];          // the chat that crossed the line most recently
    const cross = agoH - a0.line;
    const sOn = await rewind(page, cross + 0.15), sIn = await rewind(page, Math.max(0.01, cross - 0.15));
    ok(!sOn.members.includes(other) && sIn.members.includes(other), 'many: "' + other + '" stands on the floor 9 min before its line (' + cross.toFixed(2) + ' h ago) and is in the archive 9 min after');
    const lane = sOn.rootLane.filter(x => x !== '§archive'), starts = await page.evaluate(ids => ids.map(i => window.__p.AG.find(a => a.id === i).start), lane);
    ok(starts.every((t, k) => !k || starts[k - 1] <= t) && lane.includes(other), 'many: woken, it is back at its place in the oldest-first order (' + sOn.rootLane.slice(0, 5).join(', ') + ')');
    await page.evaluate(() => window.__p.goLive()); await page.waitForTimeout(1200);
    ok((await st(page)).members.includes(other), 'many: live, "' + other + '" is in the archive again');
  }
  if (floor === 'few') {          // the line (24 h on this page) in the replay: the habit tracker's chats go in together
    const a0 = await st(page), m = a0.members[0], cross = a0.memberAgoH[m] - a0.line;
    const sOn = await rewind(page, cross + 0.15), sIn = await rewind(page, cross - 0.15);
    ok(!sOn.members.includes(m) && sIn.members.includes(m), 'few: "' + m + '" stands on the floor 9 min before its line (' + cross.toFixed(2) + ' h ago) and is in the archive 9 min after');
    await page.evaluate(() => window.__p.goLive()); await page.waitForTimeout(800);
  }
  ok(errs.length === 0, floor + ': no page errors' + (errs.length ? ' — ' + errs.slice(0, 3).join(' | ') : ''));
  await page.close();
}
await browser.close();
console.log(fails.length ? fails.length + ' failed' : 'all passed');
process.exit(fails.length ? 1 : 0);
