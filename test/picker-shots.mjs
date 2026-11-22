#!/usr/bin/env node
// The model picker under the reply box (live.html, 2026-11-22: one menu per setting) on the made-up floor with its long lists
// (&lists=long), in one headless Chrome, at 1440x900 and 800x500: pictures closed, open, filtered and changed, and a check that what
// was picked is what the page would send (the made-up floor records the request; nothing leaves the page).
//   node picker-shots.mjs --url http://127.0.0.1:PORT/live.html --out DIR
import { chromium } from 'playwright';
import fs from 'node:fs';
const argv = process.argv.slice(2), opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const base = opt('--url'), out = opt('--out');
if (!base || !out) { console.error('usage: node picker-shots.mjs --url http://127.0.0.1:PORT/live.html --out DIR'); process.exit(2); }
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const results = [], fails = [];
const check = (name, ok, got) => { results.push({ name, ok: !!ok, got }); if (!ok) fails.push(name); console.log((ok ? 'ok   ' : 'FAIL ') + name + (got !== undefined ? '  ' + JSON.stringify(got) : '')); };
async function open(size, extra) {
  const [w, h] = size.split('x').map(Number);
  const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 2 });
  page.errs = []; page.on('pageerror', e => page.errs.push(String(e)));
  await page.goto(`${base}?stress=6&tick=1&lists=long${extra}`);
  for (let i = 0; i < 60 && !(await page.evaluate(() => !!(window.__picker && document.querySelector('.rb .rbmore.on'))).catch(() => false)); i++) await page.waitForTimeout(250);
  await page.waitForTimeout(1500);
  return page;
}
async function shot(page, file, full) {          // the reply box and its open list with room around them; or the whole window
  if (full) { await page.screenshot({ path: `${out}/${file}` }); return; }
  const r = await page.evaluate(() => { const rs = [document.querySelector('.tp .rb'), document.querySelector('.pkpop')].filter(Boolean).map(e => e.getBoundingClientRect());
    return { x0: Math.min(...rs.map(r => r.left)), y0: Math.min(...rs.map(r => r.top)), x1: Math.max(...rs.map(r => r.right)), y1: Math.max(...rs.map(r => r.bottom)), W: innerWidth, H: innerHeight }; });
  const pad = 24, x = Math.max(0, r.x0 - pad), y = Math.max(0, r.y0 - pad);
  await page.screenshot({ path: `${out}/${file}`, clip: { x, y, width: Math.min(r.W, r.x1 + pad) - x, height: Math.min(r.H, r.y1 + pad) - y } });
}
const P = page => page.evaluate(() => window.__picker());
const inView = (r, page) => { const v = page.viewportSize(); return r && r.x >= 0 && r.y >= 0 && r.x + r.w <= v.width + 0.5 && r.y + r.h <= v.height + 0.5; };
const last = (s, p) => [...s.sent].reverse().find(x => x.path === p);
async function typeAndSend(page, text) { await page.focus('.tp .rb textarea'); await page.keyboard.type(text); await page.keyboard.press('Enter'); await page.waitForTimeout(800); }
const pick = async (page, btn, row) => { await page.click(`[data-pk="${btn}"]`); await page.click(`.pkpop .pki:has-text("${row}")`); await page.waitForTimeout(250); };

for (const size of ['1440x900', '800x500']) {
  // a new agent: OpenCode's long list filtered, then model, effort, access; the start request carries them
  let page = await open(size, '&newagent=1');
  await shot(page, `final-${size}-closed.png`); await shot(page, `final-${size}-window.png`, true);
  let s = await P(page);
  check(`${size}: no line repeating the settings`, s.line === null, s.line);
  check(`${size}: buttons say the choice in words`, s.buttons.some(b => b.text === 'GPT-6 Luna'), s.buttons.map(b => b.text));
  await page.click('[data-pk="model"]'); await page.waitForTimeout(250);
  s = await P(page); check(`${size}: list opens inside the window`, s.open === 'model' && inView(s.popRect, page), s.popRect);
  await shot(page, `final-${size}-open.png`);
  await page.keyboard.press('Escape'); await page.waitForTimeout(150);
  await pick(page, 'harness', 'OpenCode'); await page.waitForTimeout(700);
  await page.focus('[data-pk="model"]'); await page.keyboard.press('Enter'); await page.waitForTimeout(250);
  await page.keyboard.type('sonnet'); await page.waitForTimeout(200);
  s = await P(page); check(`${size}: typing filters, list inside the window`, /Sonnet/.test(s.popText || '') && !/Gemini/.test(s.popText || '') && inView(s.popRect, page), s.popRect);
  await shot(page, `final-${size}-filtered.png`);
  await page.keyboard.press('Enter'); await page.waitForTimeout(250);
  await pick(page, 'effort', 'Max'); await pick(page, 'access', 'Plan');
  await shot(page, `final-${size}-new-set.png`);
  await typeAndSend(page, 'tidy the readme');
  s = await P(page); const st = last(s, 'start');
  check(`${size}: the start request carries them`, st && st.data.harness === 'opencode' && st.data.model === 'anthropic/claude-sonnet-5' && st.data.effort === 'max' && st.data.approvalPolicy === 'plan', st && st.data);
  check(`${size}: no page error (new agent)`, !page.errs.length, page.errs);
  await page.close();

  // an existing Codex agent: model, effort and access changed here go with its next message
  page = await open(size, '&tagent=1&box=1');
  await pick(page, 'model', 'GPT-6 Sol'); await pick(page, 'effort', 'Extra high'); await pick(page, 'access', 'Full Access');
  s = await P(page);
  check(`${size}: changed buttons are marked, no settings line`, s.buttons.filter(b => b.changed).length === 3 && s.line === null, s.buttons);
  check(`${size}: archive stands in the buttons' row`, s.rowButtons.includes('archive'), s.rowButtons);
  await shot(page, `final-${size}-agent-changed.png`);
  await typeAndSend(page, 'carry on');
  s = await P(page); const sd = last(s, 'send');
  check(`${size}: the next message carries them`, sd && sd.data.model === 'gpt-6-sol' && sd.data.effort === 'xhigh' && sd.data.approvalPolicy === 'never', sd && sd.data);
  check(`${size}: no page error (agent)`, !page.errs.length, page.errs);
  await page.close();
}
await browser.close();
fs.writeFileSync(`${out}/final-checks.json`, JSON.stringify(results, null, 1));
console.log(fails.length ? `${fails.length} failed` : 'all passed');
process.exit(fails.length ? 1 : 0);
