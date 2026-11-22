// Plan-use panel look (2026-11-22): the page against a mock server, its /api/usage answer extended with fixture rows for the other
// harnesses (built by the real readers from providers/fixtures/planuse; see test_planuse.py). Headless Chrome, no window.
//   node agent-ide/test/planuse-look.mjs <base url> <providers.json | -> <out dir> [a|b|c] [name]
// Rows in providers.json replace the server's row of the same harness and are added otherwise. With a version, it shoots the
// page with the panel folded and with every harness unfolded: <out>/<name>-<v>-folded.png, <out>/<name>-<v>-open.png.
import { chromium } from 'playwright';
import fs from 'fs';
const [base, rowsFile, out, look, name = 'plan-use'] = process.argv.slice(2);
const extra = rowsFile && rowsFile !== '-' ? JSON.parse(fs.readFileSync(rowsFile, 'utf8')) : [];
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
await page.route('**/api/usage?*', async route => {
  let r, j; try { r = await route.fetch(); j = await r.json(); } catch (e) { return; }          // the page closed mid-request
  if (j.providers) j.providers = j.providers.filter(p => !extra.some(e => e.name === p.name)).concat(extra);
  await route.fulfill({ response: r, json: j });
});
await page.goto(base + '/live.html' + (look ? '?planuse=' + look : ''), { waitUntil: 'load' });
await page.waitForTimeout(6000);
const why = await page.evaluate(() => { const e = document.getElementById('usPanel'); return e && getComputedStyle(e).display; });
if (why === 'none') await page.mouse.click(1290, 20);          // the usage gauge, slot 4 of the top-right row: opens the panel
await page.waitForTimeout(800);
if (await page.evaluate(() => getComputedStyle(document.getElementById('usPanel')).display) === 'none') { await page.waitForTimeout(3000); await page.mouse.click(1290, 20); await page.waitForTimeout(800); }          // the click came too early: once more
await page.waitForSelector('#usPanel .pu, #usPanel .pu-tab, #usPanel .plans', { timeout: 90000 }).catch(() => {});          // the first reading can take a while
await page.waitForTimeout(1500);
const panel = await page.$('#usPanel');
const shown = await page.evaluate(() => { const e = document.getElementById('usPanel'); return e && getComputedStyle(e).display; });
if (look) {
  await page.screenshot({ path: `${out}/${name}-${look}-folded.png` });
  await (await page.$('#usPanel')).screenshot({ path: `${out}/${name}-${look}-folded-panel.png` });
  const n = await page.locator('#usPanel [data-prov]').count();          // each click redraws the panel: find the line afresh
  for (let i = 0; i < n; i++) { await page.locator('#usPanel [data-prov]').nth(i).click(); await page.waitForTimeout(150); }
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${out}/${name}-${look}-open.png` });
  await (await page.$('#usPanel')).screenshot({ path: `${out}/${name}-${look}-open-panel.png` });
} else {
  if (panel && shown !== 'none') await panel.screenshot({ path: out + '/plan-use-panel.png' });
  await page.screenshot({ path: out + '/plan-use-page.png' });
}
const text = await page.evaluate(() => (document.getElementById('usPanel') || {}).innerText || '');
console.log(JSON.stringify({ shown, wasOpen: why, errors, text: text.split('\n').slice(0, 60) }, null, 1));
await browser.close();
