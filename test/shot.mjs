#!/usr/bin/env node
// Screenshot a concept page headlessly and report its console and uncaught errors.
//   node agent-ide/test/shot.mjs <url> <out.png> [--size 1440x900] [--wait 1500] [--click x,y]... [--key K]...
// Prints: WebGL renderer, frames per second measured over one second (real GPU on this Mac), console messages, page errors. Exit 1 on a page error.
import { chromium } from 'playwright';
const argv = process.argv.slice(2);
const url = argv[0], out = argv[1];
if (!url || !out) { console.error('usage: node shot.mjs <url> <out.png> [--size WxH] [--wait ms] [--click x,y] [--key K]'); process.exit(2); }
const opt = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const [W, H] = opt('--size', '1440x900').split('x').map(Number);
const wait = Number(opt('--wait', '1500'));
const steps = []; argv.forEach((a, i) => { if (a === '--click' || a === '--key') steps.push([a, argv[i + 1]]); });
const browser = await chromium.launch({ headless: true });      // no GL flags: on this Mac headless Chromium uses the real GPU (ANGLE Metal/OpenGL)
const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
const logs = [], errors = [];
page.on('console', m => logs.push(m.type() + ': ' + m.text()));
page.on('pageerror', e => errors.push(String(e)));
await page.goto(url, { waitUntil: 'load' });
await page.waitForTimeout(wait);
for (const [k, v] of steps) {
  if (k === '--click') { const [x, y] = v.split(',').map(Number); await page.mouse.click(x, y); }
  else await page.keyboard.press(v);
  await page.waitForTimeout(400);
}
const probe = await page.evaluate(() => new Promise(res => {
  const c = document.createElement('canvas'); const gl = c.getContext('webgl2') || c.getContext('webgl');
  let n = 0; const t0 = performance.now();
  const tick = () => { n++; if (performance.now() - t0 < 1000) requestAnimationFrame(tick); else res({ webgl: !!gl, renderer: gl ? (gl.getExtension('WEBGL_debug_renderer_info') ? gl.getParameter(gl.getExtension('WEBGL_debug_renderer_info').UNMASKED_RENDERER_WEBGL) : 'unknown') : null, fps: n }); };
  requestAnimationFrame(tick);
}));
await page.screenshot({ path: out });
console.log(JSON.stringify({ url, out, size: [W, H], ...probe, console: logs, errors }, null, 1));
await browser.close();
process.exit(errors.length ? 1 : 0);
