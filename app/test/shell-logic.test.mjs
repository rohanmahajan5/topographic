// The shell's main-process logic without Electron and without the real server: recent projects, the first-run choice,
// the uv commands, the menu's wiring, the developer-tools chord, the providers hook, the server lifecycle (against a
// stand-in server), the pinned uv, and the shipping guard. Run: npm test (compiles, then runs this). Starts no agent,
// opens no window, touches no port but the free ones it is given.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import L from '../dist/logic.js';
import S from '../dist/server.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const app = path.resolve(here, '..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-ide-test-'));
const step = (m) => console.log('ok  ' + m);
const ws = '/Applications/Agent IDE.app/Contents/Resources/workspace', data = '/Users/x/Library/Application Support/Agent IDE';

// ---------- recent projects and settings ----------
assert.deepEqual(L.addRecent(['/a', '/b', '/c'], '/b'), ['/b', '/a', '/c']);
assert.deepEqual(L.addRecent(undefined, '/a'), ['/a']);
assert.equal(L.addRecent(Array.from({ length: 20 }, (_, i) => '/p' + i), '/new').length, L.RECENT_MAX);
assert.deepEqual(L.liveRecent(['/a', '/gone', 7, '/b'], p => p !== '/gone'), ['/a', '/b']);
assert.deepEqual(L.opened({ port: 5, recent: ['/x'] }, '/y'), { port: 5, project: '/y', recent: ['/y', '/x'] });
const sfile = path.join(tmp, 'data', 'settings.json');
assert.deepEqual(L.loadSettings(sfile), {}, 'no settings yet reads as empty');
L.saveSettings(sfile, { project: tmp, recent: [tmp] });
assert.deepEqual(L.loadSettings(sfile), { project: tmp, recent: [tmp] });
fs.writeFileSync(sfile, '{broken'); assert.deepEqual(L.loadSettings(sfile), {}, 'a broken settings file reads as empty');
step('recent projects: newest first, no repeats, at most ' + L.RECENT_MAX + ', missing folders dropped; settings round-trip in the data folder');

// ---------- first run ----------
assert.deepEqual(L.firstRunAction({}), { kind: 'ask' });
assert.deepEqual(L.firstRunAction({ project: path.join(tmp, 'gone') }), { kind: 'ask' }, 'a saved project that is gone asks again');
assert.deepEqual(L.firstRunAction({ project: tmp }), { kind: 'open', project: tmp });
step('first run asks with the folder dialog when no project (or a gone one) is saved; never a guessed folder');

// ---------- uv: set up Python, start the server ----------
const base = { PATH: '/usr/bin:/bin', HOME: '/Users/x', UV_INDEX_URL: 'https://elsewhere', VIRTUAL_ENV: '/venv', PYTHONPATH: '/p' };
const paths = { uv: '/R/uv/uv', workspace: ws, data };
const sync = L.uvSyncCommand(paths, base);
assert.equal(sync.cmd, '/R/uv/uv');
assert.deepEqual(sync.args, ['sync', '--frozen', '--no-dev', '--project', ws]);
assert.equal(sync.cwd, ws);
const run = L.serverCommand(paths, base, { port: 4567, project: '/Users/x/code/proj' });
assert.deepEqual(run.args, ['run', '--frozen', '--no-sync', '--project', ws, '--', 'python', 'server.py', '--port', '4567']);
assert.equal(run.cwd, ws);
for (const e of [sync.env, run.env]) {
  assert.equal(e.UV_PROJECT_ENVIRONMENT, path.join(data, 'uv', 'env'));
  assert.equal(e.UV_PYTHON_INSTALL_DIR, path.join(data, 'uv', 'python'));
  assert.equal(e.UV_CACHE_DIR, path.join(data, 'uv', 'cache'));
  assert.equal(e.UV_PYTHON_PREFERENCE, 'only-managed');
  assert.equal(e.UV_NO_CONFIG, '1');
  assert.equal(e.UV_PYTHON_INSTALL_BIN, '0');
  assert.equal(e.UV_INDEX_URL, undefined, "the person's own UV_ settings are dropped");
  assert.equal(e.VIRTUAL_ENV, undefined); assert.equal(e.PYTHONPATH, undefined);
  assert.equal(e.PATH, base.PATH);
}
assert.equal(run.env.AGENT_IDE_PROJECT, '/Users/x/code/proj');
assert.equal(run.env.AGENT_IDE_CACHE_DIR, path.join(data, 'cache'));
assert.equal(sync.env.AGENT_IDE_PROJECT, undefined);
step('uv: `uv sync --frozen` into the app data folder, then `uv run --frozen --no-sync -- python server.py --port N` with the chosen project');

const fakeWs = path.join(tmp, 'ws'); fs.mkdirSync(fakeWs);
fs.writeFileSync(path.join(fakeWs, 'uv.lock'), 'lock-1'); fs.writeFileSync(path.join(fakeWs, '.python-version'), '3.12.16\n');
const d2 = path.join(tmp, 'appdata');
const stamp = L.envStamp(fakeWs, 'uv 0.13.7');
assert.equal(L.envReady(d2, stamp), false, 'nothing set up yet');
fs.mkdirSync(path.join(d2, 'uv', 'env'), { recursive: true }); fs.writeFileSync(path.join(d2, 'uv', 'env', 'pyvenv.cfg'), '');
assert.equal(L.envReady(d2, stamp), false, 'an environment without the stamp is not trusted');
L.markEnvReady(d2, stamp);
assert.equal(L.envReady(d2, stamp), true, 'later launches skip the set-up');
fs.writeFileSync(path.join(fakeWs, 'uv.lock'), 'lock-2');
assert.equal(L.envReady(d2, L.envStamp(fakeWs, 'uv 0.13.7')), false, 'a new lock (app update) sets up again');
assert.notEqual(L.envStamp(fakeWs, 'uv 0.13.7'), L.envStamp(fakeWs, 'uv 0.14.0'), 'so does a new uv');
assert.equal(L.progressLine('\x1b[1mResolved\x1b[0m 33 packages  '), 'Resolved 33 packages');
assert.equal(L.progressLine('   '), null);
step('first-launch screen only when the environment is missing or made from another lock, Python or uv');

// ---------- the menu ----------
const calls = [];
const h = new Proxy({}, { get: (_t, k) => (...a) => calls.push([k, ...a]) });
const all = (items) => items.flatMap(i => [i, ...(Array.isArray(i.submenu) ? all(i.submenu) : [])]);
const byLabel = (items, l) => all(items).find(i => i.label === l);
const tpl = L.menuTemplate(h, { appName: 'Agent IDE', recent: ['/p/one', '/p/two'], serverUp: true });
assert.deepEqual(tpl.map(m => m.label || m.role), ['Agent IDE', 'File', 'editMenu', 'View', 'Agents', 'windowMenu', 'help']);
for (const [label, handler, args] of [
  ['About Agent IDE', 'about', []], ['Check for Updates…', 'checkForUpdates', []], ['Open Project…', 'openProject', []],
  ['/p/two', 'openRecent', ['/p/two']], ['Clear Menu', 'clearRecent', []], ['Reload', 'reload', []], ['Providers…', 'providers', []],
  ['Agent IDE README', 'readme', []], ['Report a Problem…', 'reportProblem', []], ['Show Logs', 'showLogs', []],
]) {
  calls.length = 0; byLabel(tpl, label).click();
  assert.deepEqual(calls, [[handler, ...args]], label);
}
assert.equal(byLabel(tpl, 'Open Project…').accelerator, 'CmdOrCtrl+O');
assert.ok(all(tpl).some(i => i.role === 'quit'), 'Quit');
assert.ok(!all(tpl).some(i => /devtools/i.test(String(i.role)) || /developer/i.test(String(i.label))), 'no developer tools item in the menu');
assert.deepEqual(byLabel(tpl, 'Open Recent').submenu.slice(0, 2).map(i => i.label), ['/p/one', '/p/two']);
const empty = L.menuTemplate(h, { appName: 'Agent IDE', recent: [], serverUp: false });
assert.deepEqual(byLabel(empty, 'Open Recent').submenu.map(i => [i.label, i.enabled]), [['No Recent Projects', false]]);
assert.equal(byLabel(empty, 'Providers…').enabled, false, 'Providers… waits for the page');
step('menu: App (About, Check for Updates, Quit), File (Open Project ⌘O, Open Recent, Clear), Edit, View, Agents (Providers), Window, Help; every item reaches its handler');

const k = (o) => ({ type: 'keyDown', key: 'i', meta: false, alt: false, control: false, shift: false, ...o });
assert.equal(L.isDevToolsChord(k({ meta: true, alt: true, key: 'ˆ', code: 'KeyI' })), true, 'Option-Command-I (Option turns the key into ˆ)');
assert.equal(L.isDevToolsChord(k({ meta: true, alt: true })), true);
assert.equal(L.isDevToolsChord(k({ meta: true })), false, 'Command-I alone is the page\'s');
assert.equal(L.isDevToolsChord(k({ meta: true, alt: true, type: 'keyUp' })), false);
step('developer tools: only Option-Command-I');

assert.deepEqual(L.links('https://github.com/someone/agent-ide/'), { releases: 'https://github.com/someone/agent-ide/releases', readme: 'https://github.com/someone/agent-ide#readme', issues: 'https://github.com/someone/agent-ide/issues/new' });
assert.equal(L.withHash('http://127.0.0.1:5000/?cwd=%2Fp#old', 'providers'), 'http://127.0.0.1:5000/?cwd=%2Fp#providers');
let openedPanel = 0;
assert.equal(vm.runInNewContext(L.PROVIDERS_SCRIPT, { window: { openProvidersPanel: () => openedPanel++ } }), true);
assert.equal(openedPanel, 1);
assert.equal(vm.runInNewContext(L.PROVIDERS_SCRIPT, { window: {} }), false, 'no panel function: the shell loads #providers instead');
step('Providers… calls openProvidersPanel() or falls back to #providers');

// ---------- updates: GitHub's latest release, at most once a day at launch, a quiet note when newer ----------
{
  assert.deepEqual(L.parseVersion('v0.2.0'), [0, 2, 0]); assert.deepEqual(L.parseVersion('1.10.3'), [1, 10, 3]);
  for (const bad of ['v0.2', '0.2.0-beta.1', 'latest', '', null, 7, '0.2.0; alert(1)']) assert.equal(L.parseVersion(bad), null, String(bad));
  assert.equal(L.isNewer('v0.2.0', '0.1.0'), true);
  assert.equal(L.isNewer('0.1.10', '0.1.9'), true, 'numbers, not text: 10 > 9');
  assert.equal(L.isNewer('1.0.0', '0.99.99'), true);
  assert.equal(L.isNewer('v0.1.0', '0.1.0'), false, 'the same version');
  assert.equal(L.isNewer('0.0.9', '0.1.0'), false, 'older');
  assert.equal(L.isNewer('0.3.0-rc.1', '0.1.0'), false, 'a pre-release tag is never offered');
  assert.equal(L.isNewer('0.2.0', 'garbage'), false);
  const day = L.UPDATE_EVERY, now = Date.UTC(2026, 10, 22, 12);
  assert.equal(L.updateCheckDue(undefined, now), true, 'never checked');
  assert.equal(L.updateCheckDue(now - day + 60000, now), false, 'checked 23 h 59 min ago: not again');
  assert.equal(L.updateCheckDue(now - day, now), true, 'a day ago: again');
  assert.equal(L.updateCheckDue(now + 3600000, now), true, 'the clock went backwards: again');
  assert.equal(L.updateCheckDue(NaN, now), true);
  // the once-a-day rule through the saved settings, as main.ts keeps it
  const sf = path.join(tmp, 'upd', 'settings.json');
  L.saveSettings(sf, { ...L.loadSettings(sf), lastUpdateCheck: now });
  assert.equal(L.updateCheckDue(L.loadSettings(sf).lastUpdateCheck, now + 5 * 3600000), false, 'a relaunch five hours later does not ask');
  assert.equal(L.updateCheckDue(L.loadSettings(sf).lastUpdateCheck, now + 25 * 3600000), true, 'the next day does');
  const u = L.updateLinks('https://github.com/someone/topographic');
  assert.deepEqual(u, { api: 'https://api.github.com/repos/someone/topographic/releases/latest', dmg: 'https://github.com/someone/topographic/releases/latest/download/Topographic-arm64.dmg', page: 'https://github.com/someone/topographic/releases/latest' });
  assert.equal(L.updateLinks('https://example.com/x').api, null, 'not GitHub: no check');
  const pkg = JSON.parse(fs.readFileSync(path.join(app, 'package.json'), 'utf8'));
  assert.equal(pkg.build.artifactName, '${productName}-${arch}.${ext}', 'release files carry no version, so latest/download always finds them');
  assert.equal(`${pkg.build.productName}-arm64.dmg`, L.DMG_NAME);
  assert.deepEqual(pkg.build.mac.target.map(t => [t.target, t.arch]), [['dmg', ['arm64']], ['zip', ['arm64']]], 'Apple silicon only');
  assert.equal(L.updateLinks(pkg.homepage).api, 'https://api.github.com/repos/rohanmahajan5/topographic/releases/latest');
  const fake = (o) => async (url, init) => { if (o.throw) throw o.throw; assert.equal(init.headers.Accept, 'application/vnd.github+json'); return { ok: o.status === 200, status: o.status, json: async () => o.body }; };
  assert.deepEqual(await L.checkForUpdate(fake({ status: 200, body: { tag_name: 'v0.2.0' } }), u.api, '0.1.0'), { kind: 'newer', version: '0.2.0' });
  assert.deepEqual(await L.checkForUpdate(fake({ status: 200, body: { tag_name: 'v0.1.0' } }), u.api, '0.1.0'), { kind: 'current', version: '0.1.0' });
  assert.equal((await L.checkForUpdate(fake({ status: 404 }), u.api, '0.1.0')).kind, 'unknown', 'a private repository (404) is silent');
  assert.equal((await L.checkForUpdate(fake({ status: 403 }), u.api, '0.1.0')).kind, 'unknown', 'rate limited');
  assert.equal((await L.checkForUpdate(fake({ throw: new TypeError('fetch failed') }), u.api, '0.1.0')).kind, 'unknown', 'offline');
  assert.equal((await L.checkForUpdate(fake({ status: 200, body: { tag_name: 'v9.0.0', prerelease: true } }), u.api, '0.1.0')).kind, 'unknown');
  assert.equal((await L.checkForUpdate(fake({ status: 200, body: { tag_name: '<img onerror=x>' } }), u.api, '0.1.0')).kind, 'unknown', 'a tag that is not a version');
  assert.equal((await L.checkForUpdate(fake({ status: 200, body: null }), u.api, '0.1.0')).kind, 'unknown');
  assert.equal((await L.checkForUpdate(fake({ status: 200 }), null, '0.1.0')).kind, 'unknown');
  const hang = (_u, init) => new Promise((_r, rej) => { const keep = setTimeout(() => {}, 5000); init.signal.addEventListener('abort', () => { clearTimeout(keep); rej(init.signal.reason); }); });          // keep: AbortSignal.timeout's timer alone does not hold Node open
  assert.deepEqual(await L.checkForUpdate(hang, u.api, '0.1.0', 50), { kind: 'unknown', why: 'no answer in time' });
  // the note: one line in the page, the link built here; run twice, still one
  assert.throws(() => L.updateNoteScript('0.2.0"<x>', u.dmg));
  const script = L.updateNoteScript('0.2.0', u.dmg);
  const made = [];
  const el = (tag) => { const e = { tag, style: {}, children: [], attrs: {}, listeners: {}, setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.children.push(c); }, addEventListener(k) { this.listeners[k] = 1; }, remove() { const i = made.indexOf(this); if (i >= 0) made.splice(i, 1); } }; return e; };
  const doc = { body: { appendChild: (n) => made.push(n) }, createElement: el, getElementById: (id) => made.find(n => n.id === id) || null };
  assert.equal(vm.runInNewContext(script, { document: doc }), true);
  vm.runInNewContext(script, { document: doc });
  assert.equal(made.length, 1, 'shown again, it replaces itself');
  const [note] = made, link = note.children[0].children[0];
  assert.equal(note.id, L.UPDATE_NOTE_ID);
  assert.equal(note.children[0].textContent + link.textContent, 'Topographic 0.2.0 is out — Download');
  assert.deepEqual([link.href, link.target], [u.dmg, '_blank'], 'the dmg, in the browser (the open handler sends outside links there)');
  assert.deepEqual(L.windowOpenDecision({ url: u.dmg, frameName: '', opener: 'http://127.0.0.1:5123/', origin: 'http://127.0.0.1:5123' }), { kind: 'external', url: u.dmg });
  assert.ok(note.listeners.pointerdown && note.listeners.click, 'clicks on the note stay off the page');
  const main = fs.readFileSync(path.join(app, 'src', 'main.ts'), 'utf8');
  assert.ok(main.includes('if (!fromMenu && !updateCheckDue(settings().lastUpdateCheck, Date.now())) return;') && main.includes("if (!fromMenu) return;          // at launch: the note or nothing; never a window"), 'launch: once a day, never a window');
  assert.ok(main.includes('checkForUpdates: () => { updates(true); },') && main.includes('updates(false);'), 'the menu always asks; launch asks when due');
}
step('updates: versions compared as numbers; launch asks GitHub at most once a day; a newer release gives a quiet note linking the arm64 dmg; offline, 404 and odd answers are silent');

// ---------- windows the page opens: the mini-map's small window on top, links to the system browser, nothing else ----------
{
  const o = 'http://127.0.0.1:5123', page = o + '/?cwd=%2Fp', d = (x) => L.windowOpenDecision({ url: '', frameName: '', opener: page, origin: o, ...x });
  assert.deepEqual(d({ frameName: L.MINIMAP_FRAME }), { kind: 'minimap' }, 'the page opens the mini-map empty');
  assert.deepEqual(d({ frameName: L.MINIMAP_FRAME, url: 'about:blank' }), { kind: 'minimap' });
  assert.deepEqual(d({ frameName: L.MINIMAP_FRAME, url: 'https://example.com/' }), { kind: 'deny' }, 'never a page from elsewhere under that name');
  assert.deepEqual(d({ frameName: L.MINIMAP_FRAME, opener: 'file:///start.html' }), { kind: 'deny' }, 'only the server\'s page');
  assert.deepEqual(d({ frameName: L.MINIMAP_FRAME, origin: null }), { kind: 'deny' }, 'no server: no mini-map');
  assert.deepEqual(d({ frameName: L.MINIMAP_FRAME, opener: 'http://127.0.0.1:51234/' }), { kind: 'deny' }, 'another port is not the server');
  assert.deepEqual(d({ url: 'https://github.com/x' }), { kind: 'external', url: 'https://github.com/x' });
  assert.deepEqual(d({ url: o + '/live.html' }), { kind: 'deny' }, 'the server\'s own pages open no window');
  assert.deepEqual(d({ url: 'about:blank' }), { kind: 'deny' }, 'an empty window under any other name');
  assert.deepEqual(d({ url: 'file:///etc/hosts' }), { kind: 'deny' });
  const w = L.MINIMAP_WINDOW;
  assert.equal(w.alwaysOnTop, true); assert.deepEqual([w.width, w.height], [360, 240], 'the browser\'s picture-in-picture size');
  const pre = fs.readFileSync(path.join(app, 'src', 'preload.ts'), 'utf8');
  assert.ok(pre.includes(`const MINIMAP_FRAME = '${L.MINIMAP_FRAME}'`) && pre.includes('miniMap: MINIMAP_FRAME'), 'the preload hands the page the same name');
  const main = fs.readFileSync(path.join(app, 'src', 'main.ts'), 'utf8');
  assert.ok(main.includes('windowOpenDecision(') && main.includes('overrideBrowserWindowOptions: MINIMAP_WINDOW'), 'the window handler uses the decision');
  const livePage = fs.readFileSync(path.join(app, '..', 'workspace', 'live.html'), 'utf8');
  assert.ok(livePage.includes('agentIDE.miniMap'), 'the page opens the named window in the app');
}
step('windows: the server page\'s empty mini-map window opens on top at 360 x 240; links go out; nothing else opens');

// ---------- the server lifecycle, against a stand-in that answers /api/state and stops on SIGTERM ----------
const blocker = net.createServer(); await new Promise(r => blocker.listen(0, '127.0.0.1', r));
const taken = blocker.address().port;
const port = await S.freePort(taken);
assert.notEqual(port, taken); blocker.close();
const standIn = `const http=require('http');const s=http.createServer((q,r)=>{r.setHeader('content-type','application/json');r.end(JSON.stringify({connected:true,path:q.url}))});
setTimeout(()=>s.listen(${port},'127.0.0.1',()=>console.log('listening, parent '+process.env.AGENT_IDE_PARENT_PID)),400);process.on('SIGTERM',()=>{console.log('stopping on SIGTERM');s.close();process.exit(0)});`;
const logFile = path.join(tmp, 'logs', 'server.log');
const handle = S.startServer({ cmd: process.execPath, args: ['-e', standIn], env: process.env, cwd: tmp }, port, logFile);
await S.waitHealthy(handle, 10000);
assert.deepEqual(await S.getJson(port, '/api/state'), { connected: true, path: '/api/state' });
await handle.stop();
assert.equal(handle.child.exitCode, 0, 'stopped by SIGTERM, cleanly');
assert.match(fs.readFileSync(logFile, 'utf8'), new RegExp('listening, parent ' + process.pid + '[\\s\\S]*stopping on SIGTERM'), 'the server log is kept for Help > Show Logs; the server is told the shell\'s pid');
assert.equal(S.groupSignal(handle.child.pid, 0), false, 'nothing of its group is left');
const dead = S.startServer({ cmd: process.execPath, args: ['-e', 'console.error("Install a current Codex CLI");process.exit(3)'], env: process.env, cwd: tmp }, port);
await assert.rejects(S.waitHealthy(dead, 10000), /stopped while starting[\s\S]*Install a current Codex CLI/);
const missing = S.startServer({ cmd: path.join(tmp, 'no-such-uv'), args: [], env: process.env, cwd: tmp }, port);
await assert.rejects(S.waitHealthy(missing, 5000), /stopped while starting/);
step(`server lifecycle: healthy on a free port (${taken} was taken), SIGTERM stop, log file; a server that exits or cannot start is reported with its last lines`);

// A server that hangs: it ignores SIGTERM and has a child of its own (as uv has server.py, and server.py Codex's app-server).
// stop() ends the whole group: SIGTERM, then SIGKILL after the grace; nothing is left on the port.
const kidFile = path.join(tmp, 'kid.pid');
const hung = `const http=require('http');const {spawn}=require('child_process');process.on('SIGTERM',()=>console.log('ignoring SIGTERM'));
const kid=spawn(process.execPath,['-e','process.on("SIGTERM",()=>{});setInterval(()=>{},1000)'],{stdio:'ignore'});require('fs').writeFileSync(${JSON.stringify(kidFile)},String(kid.pid));
http.createServer((q,r)=>r.end('{}')).listen(${port},'127.0.0.1',()=>console.log('listening'));`;
const h2 = S.startServer({ cmd: process.execPath, args: ['-e', hung], env: process.env, cwd: tmp }, port, undefined, 800);
await S.waitHealthy(h2, 10000);
const kid = Number(fs.readFileSync(kidFile, 'utf8'));
const alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } };
assert.ok(alive(kid));
const t0 = Date.now();
await h2.stop();
assert.ok(Date.now() - t0 >= 700 && Date.now() - t0 < 4000, 'SIGKILL after the grace: ' + (Date.now() - t0) + ' ms');
assert.equal(h2.child.signalCode, 'SIGKILL');
for (let i = 0; i < 40 && alive(kid); i++) await new Promise(r => setTimeout(r, 50));
assert.ok(!alive(kid), 'the hung server\'s child is gone with its group');
assert.ok(await new Promise(r => { const c = net.connect(port, '127.0.0.1'); c.once('connect', () => { c.destroy(); r(false); }); c.once('error', () => r(true)); }), 'the port is closed');
step('server stop: a server that ignores SIGTERM is killed with its whole group after the grace');

// ---------- the pinned uv ----------
const pin = JSON.parse(fs.readFileSync(path.join(app, 'uv-pin.json'), 'utf8'));
for (const t of Object.values(pin.targets)) assert.match(t.sha256, /^[0-9a-f]{64}$/);
const uvBin = path.join(app, 'build', 'uv', process.arch, 'uv');
if (fs.existsSync(uvBin)) {
  assert.match(execFileSync(uvBin, ['--version']).toString(), new RegExp('^uv ' + pin.version.replace(/\./g, '\\.') + ' '));
  step(`uv ${pin.version} in build/uv/${process.arch} runs and is the pinned version`);
} else step('uv not fetched yet (npm run fetch-uv): pin format checked only');

// ---------- the shipping guard ----------
const guard = (dir) => spawnSync('python3', [path.join(app, 'scripts', 'guard.py'), dir], { encoding: 'utf8' });
const clean = path.join(tmp, 'clean'); fs.mkdirSync(path.join(clean, 'engine'), { recursive: true });
fs.writeFileSync(path.join(clean, 'server.py'), 'print("hello")\n'); fs.writeFileSync(path.join(clean, 'engine', 'testctl.js'), '//\n');
const ok = guard(clean);
assert.equal(ok.status, 0, ok.stdout + ok.stderr);
const dirty = path.join(tmp, 'dirty'); fs.cpSync(clean, dirty, { recursive: true });
fs.writeFileSync(path.join(dirty, 'a.py'), `HOME = "${os.homedir()}/x"\n`);
fs.writeFileSync(path.join(dirty, 'b.json'), '{"id": "0f1e2d3c-4b5a-6978-8877-665544332211"}\n');
fs.writeFileSync(path.join(dirty, 'c.txt'), 'token sk-ant-' + 'a'.repeat(30) + '\n');
fs.mkdirSync(path.join(dirty, 'fixtures')); fs.writeFileSync(path.join(dirty, 'fixtures', 's.jsonl'), '{}\n');
fs.writeFileSync(path.join(dirty, 'index.html'), '<p>\n');
const bad = guard(dirty);
assert.equal(bad.status, 1);
for (const why of ['a.py:1: home path', 'b.json:1: a UUID', 'c.txt:1: secret shape', 'fixtures: must never ship', 'index.html: must never ship'])
  assert.ok(bad.stdout.includes(why), why + ' in:\n' + bad.stdout);
assert.ok(!bad.stdout.includes('sk-ant-'), 'the guard never prints what it found');
const named = path.join(tmp, 'named'); fs.cpSync(clean, named, { recursive: true });
fs.writeFileSync(path.join(named, 'd.md'), 'built in ~/my_private-folder/x\n');
const byName = spawnSync('python3', [path.join(app, 'scripts', 'guard.py'), named], { encoding: 'utf8', env: { ...process.env, GUARD_DENY: 'my-private_folder' } });
assert.equal(byName.status, 1, 'a folder name of the builder\'s (GUARD_DENY, or the folders the checkout sits in) fails, written with - or _');
assert.ok(byName.stdout.includes('d.md:1: folder name'), byName.stdout);
step('guard: a clean folder passes; a home path, a UUID, a key shape, fixtures and an old page each fail the build');

// ---------- the main window: no grey title bar; the window buttons, the wordmark and the icon row on one line (2026-11-21) ----------
{
  const o = L.mainWindowOptions({ title: 'Topographic', preload: '/x/preload.js' });
  assert.equal(o.titleBarStyle, 'hidden', 'the grey bar hidden, the macOS window buttons kept');
  assert.equal(o.title, 'Topographic', 'the real title (Dock, Mission Control, Window menu) stays Topographic');
  assert.equal(o.backgroundColor, L.BG);
  assert.deepEqual([o.webPreferences.preload, o.webPreferences.contextIsolation, o.webPreferences.nodeIntegration, o.webPreferences.sandbox], ['/x/preload.js', true, false, true]);
  const live = fs.readFileSync(path.join(app, '..', 'workspace', 'live.html'), 'utf8'), start = fs.readFileSync(path.join(app, 'start.html'), 'utf8');
  assert.ok(live.includes(`--bg:${L.BG}`) && start.includes(`--bg: ${L.BG}`), 'the window, the page and the start screen share one background');
  // the buttons' centre on the icon row's line: the row is drawn at y 12, PICK px tall; the buttons are about 14 px tall
  // the top bar's one geometry (live.html TOPBAR, 2026-11-22: a taller bar, bigger icons); the icons are drawn at its Y and size
  const tb = live.match(/const TOPBAR = \{ Y: (\d+), ICON: (\d+), H: (\d+) \};/).slice(1).map(Number);
  assert.ok(/function drawPickFolder\(\) \{\n  const sx = btnX\(0\), sy = TOPBAR\.Y,/.test(live) && live.includes('const PICK = 16, ICON = TOPBAR.ICON,'));
  assert.equal(L.ICON_ROW_MID, tb[0] + tb[1] / 2, 'the icon row\'s centre as live.html draws it');
  assert.deepEqual(o.trafficLightPosition, L.TRAFFIC_LIGHTS);
  assert.ok(Math.abs(L.TRAFFIC_LIGHTS.y + 7 - L.ICON_ROW_MID) <= 1, 'the window buttons centred on the icon row\'s line');
  // the double-click: the Mac's own setting
  assert.equal(L.titleDoubleClick(undefined), 'zoom', 'unset: zoom, as macOS does');
  assert.equal(L.titleDoubleClick('Maximize'), 'zoom'); assert.equal(L.titleDoubleClick('Fill'), 'zoom');
  assert.equal(L.titleDoubleClick('Minimize'), 'minimize'); assert.equal(L.titleDoubleClick('None'), 'none');
  assert.equal(L.titleDoubleClick('', true), 'minimize', 'the older minimize setting');
  assert.equal(L.titleDoubleClick('None', true), 'none', 'the newer setting wins');
  assert.ok(L.sameBounds({ x: 1, y: 2, width: 3, height: 4 }, { x: 1, y: 2, width: 3, height: 4 }) && !L.sameBounds({ x: 1, y: 2, width: 3, height: 4 }, { x: 1, y: 2, width: 3, height: 5 }));
  // the page's band: app only, in one marked block; drags; stops at the icon row; the page's own things stay clickable;
  // the tree and the notes start below the buttons; full screen drops it all
  const blk = live.slice(live.indexOf("/* ================= the app's title band"), live.indexOf("/* ================= end of the app's title band"));
  assert.ok(blk.length > 500, 'one marked block');
  assert.ok(blk.includes("const A = window.agentIDE || (qs.get('appband') === '1' ? { app: true } : null); if (!A || !A.app) return null;"), 'nothing of it in a browser, unless previewed with ?appband=1');
  assert.ok(blk.includes("const MARK_SVG = '<svg xmlns=\"http://www.w3.org/2000/svg\" class=\"mark\"") && blk.includes("' + MARK_SVG + '<span class=\"topo\">topo</span><span class=\"graphic\">graphic</span>"), 'the band: the logo, then the lowercase wordmark (2026-11-22)');
  assert.ok(blk.includes('-webkit-app-region:drag') && blk.includes("html.appband body > :not(canvas):not(#appband){-webkit-app-region:no-drag}"), 'the band drags; the page\'s windows and panels over it do not');
  assert.ok(blk.includes('(W - BTN_L())'), 'the band stops where the icon row begins');
  assert.ok(blk.includes('ENG_TREE.y = S.treeY + (S.on ? S.DROP : 0)') && blk.includes('#msg{top:${20 + S.DROP}px}'), 'tree and notes below the window buttons');
  assert.ok(blk.includes('H: TOPBAR.H, DROP: TOPBAR.H - 6, MID: TOPBAR.MID'));
  const H = tb[2], DROP = tb[2] - 6;
  assert.ok(start.includes('height: ' + H + 'px; -webkit-app-region: drag'), 'the start screen\'s band as tall as the page\'s');
  assert.ok(L.TRAFFIC_LIGHTS.y + 14 < H && 16 + DROP - 10 >= H, 'the buttons inside the band; the tree\'s box (its top at y - pad 10) starts below it');
  assert.ok(blk.includes('A.onFullScreen(') && blk.includes('A.titleDoubleClick()') && blk.includes("link.href = 'brand/wordmark.css'"));
  // the start screen: a drag band and the wordmark, from the workspace's brand/ (no network)
  assert.ok(/#band \{[^}]*-webkit-app-region: drag/.test(start) && start.includes('api.titleDoubleClick()'));
  assert.ok(start.includes('href="../workspace/brand/wordmark.css"') && start.includes('</svg><span class="topo">topo</span><span class="graphic">graphic</span>'));
  assert.ok(start.includes('<span class="wordmark"><svg xmlns="http://www.w3.org/2000/svg" class="mark"') && !/<img|url\(data:/.test(start), 'the logo inline, left of the word (the CSP allows no image files)');
  assert.ok(/font-src 'self' file:/.test(start) && !/https?:\/\/(?!www\.w3)/.test(start.replace(/<!--[\s\S]*?-->/g, '')), 'the typeface from the app, never the network');
  assert.ok(start.includes('aria-label="Topographic | Agent IDE"') && start.includes('<span class="sep" aria-hidden="true"></span><span class="rest">Agent IDE</span>') && start.includes('<title>Topographic | Agent IDE</title>'), 'the start screen keeps "Topographic | Agent IDE", the divider a drawn hairline (2026-11-22)');
  assert.ok(/h1 \.sep \{[^}]*flex: none; width: 1px;/.test(start) && /h1 \.rest \{[^}]*font-weight: 300;/.test(start), 'a 1 px divider; the rest of the name lighter (300, in the bundled variable Overpass)');
  const brand = path.join(app, '..', 'workspace', 'brand'), css = fs.readFileSync(path.join(brand, 'wordmark.css'), 'utf8');
  assert.ok(/\.topo \{ color: #000; \}/.test(css) && /src: url\('Overpass\.woff2'\)/.test(css) && !/https?:/.test(css));
  const stops = css.match(/linear-gradient\(90deg, ([^)]*)\)/)[1].match(/#[0-9a-f]{6}/gi);
  const lum = h => { const c = [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255).map(x => x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
  const ratio = h => (lum(L.BG) + 0.05) / (lum(h) + 0.05);
  // "topo" is text in black: it keeps strong contrast (WCAG AAA, 7:1, measured about 18:1 on the page).
  assert.ok(ratio('#000000') >= 7, '"topo" (black) keeps 7:1 on the background');
  // "graphic" is a logotype: its gradient (red #ff2a1a -> orange #ff7a00 -> yellow #ffc800, commit a8f639b56) measures 3.32, 2.32 and
  // 1.38 to 1 on the page's #f3f1ec, below the 3:1 this block used to ask. On 2026-11-22 these colours were kept at that
  // contrast for now; WCAG exempts text that is part of a logo or brand name (1.4.3). The ratios are still computed and pinned,
  // so any change to the colours, or to the background, shows up here and is decided again.
  assert.deepEqual(stops.map(h => h.toLowerCase()), ['#ff2a1a', '#ff7a00', '#ffc800'], 'the shipped "graphic" stops are the brand colours (brand/wordmark.css)');
  assert.deepEqual(stops.map(h => +ratio(h).toFixed(2)), [3.32, 2.32, 1.38], '"graphic" keeps the contrast accepted on 2026-11-22 (logotype)');
  const markStops = s => (s.match(/<linearGradient id="tpg-mark"[^>]*>(.*?)<\/linearGradient>/) || [, ''])[1].match(/#[0-9a-f]{6}/gi) || [];
  assert.deepEqual(markStops(start), stops, 'the start screen\'s logo carries the same stops as the word');
  assert.ok(stops.every(h => { const [r, g, b] = [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)); return r >= g && r > b; }), 'red to yellow: no green stop');
  assert.ok(fs.statSync(path.join(brand, 'Overpass.woff2')).size > 5000 && fs.existsSync(path.join(brand, 'OFL-Overpass.txt')) && fs.existsSync(path.join(brand, 'ATTRIBUTION.md')), 'the font, its licence and its credits');
  const stage = fs.readFileSync(path.join(app, 'scripts', 'stage_workspace.py'), 'utf8'), srv = fs.readFileSync(path.join(app, '..', 'workspace', 'server.py'), 'utf8');
  assert.ok(stage.includes("'brand/*.woff2'") && stage.includes("'brand/*.css'") && srv.includes("allowed |= {'brand/' + f.name"), 'the wordmark ships with the workspace and the server serves it');
  assert.ok(stage.includes("'brand/favicon.svg'") && srv.includes("allowed.add('brand/favicon.svg')") && fs.existsSync(path.join(brand, 'favicon.svg')), 'the tab icon ships with the workspace and the server serves it');
  const pkg = JSON.parse(fs.readFileSync(path.join(app, 'package.json'), 'utf8'));
  assert.ok(pkg.build.mac.icon === 'assets/icon.icns' && fs.readFileSync(path.join(app, pkg.build.mac.icon)).subarray(0, 4).toString() === 'icns', 'the app icon: an .icns the build points at');
  // the preload: the start screen and the page get the title band's calls (run the compiled file)
  const preload = fs.readFileSync(path.join(app, 'dist', 'preload.js'), 'utf8');
  const exposed = (href) => {
    const u = new URL(href), out = {};
    const electron = { contextBridge: { exposeInMainWorld: (k, v) => { out[k] = Object.keys(v).sort(); } }, ipcRenderer: { invoke: () => {}, on: () => {} } };
    vm.runInNewContext(preload, { require: m => { assert.equal(m, 'electron'); return electron; }, exports: {}, module: { exports: {} }, location: u });
    return out;
  };
  assert.deepEqual(exposed('file:///Applications/Topographic.app/Contents/Resources/app.asar/start.html').agentIDEStart, ['choose', 'onProgress', 'onScreen', 'repair', 'retry', 'screen', 'titleDoubleClick']);
  assert.deepEqual(exposed('http://127.0.0.1:4519/?cwd=%2Fx').agentIDE, ['app', 'chooseFolder', 'fullScreen', 'miniMap', 'onFullScreen', 'titleDoubleClick']);
  assert.deepEqual(exposed('http://example.com/'), {});
  const main = fs.readFileSync(path.join(app, 'src', 'main.ts'), 'utf8');
  assert.ok(main.includes("ipcMain.handle('app:title-double-click', e => {\n  from(e, 'start', 'page');") && main.includes("ipcMain.handle('app:full-screen', e => { from(e, 'start', 'page');"), 'each call checked by sender');
  assert.ok(main.includes("pc.send('app:full-screen', on)") && main.includes("w.on('enter-full-screen', full(true)); w.on('leave-full-screen', full(false));"));
}
step('main window: hidden title bar; window buttons, wordmark and icon row centred on one line; the page\'s app-only band drags and zooms per the Mac\'s setting, the tree below the buttons, none of it in full screen or a browser; red-to-gold wordmark from a bundled font');

fs.rmSync(tmp, { recursive: true, force: true });
console.log('PASS');
