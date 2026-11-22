// Topographic: a thin desktop shell around the workspace server. It sets up Python and the server's packages with the uv it
// carries (first launch only, on a plain progress screen), starts server.py through `uv run` on a free loopback port,
// opens one window on the page the server serves, and stops the server on quit. The menu bar holds what belongs to no
// one project: open a project, recent projects, the providers panel, updates, help.
import { app, BrowserWindow, dialog, ipcMain, Menu, net, shell, IpcMainInvokeEvent, systemPreferences, WebContents } from 'electron';
import { ChildProcess, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { Env, freePort, run, ServerHandle, startServer, userEnv, waitHealthy } from './server.js';
import {
  checkForUpdate, FetchLike, firstRunAction, envReady, envStamp, isDevToolsChord, isFolder, links, liveRecent, loadSettings, mainWindowOptions, markEnvReady, menuTemplate,
  updateCheckDue, updateLinks, updateNoteScript,
  MINIMAP_WINDOW, sameBounds, TITLE_DOUBLE_CLICK_WAIT, titleDoubleClick, opened, progressLine, PROVIDERS_SCRIPT, saveSettings, serverCommand, Settings, UvPaths, uvSyncCommand, windowOpenDecision, withHash,
} from './logic.js';

app.setName('Topographic');
// AGENT_IDE_USER_DATA: where settings, Python and the cache live (default ~/Library/Application Support/Topographic)
if (process.env.AGENT_IDE_USER_DATA) app.setPath('userData', path.resolve(process.env.AGENT_IDE_USER_DATA));
const TITLE = 'Topographic';
const APP_DIR = path.join(__dirname, '..');
const RES = app.isPackaged ? process.resourcesPath : path.join(APP_DIR, 'build');
const WORKSPACE = path.join(RES, 'workspace');
const UV = app.isPackaged ? path.join(RES, 'uv', 'uv') : path.join(APP_DIR, 'build', 'uv', process.arch, 'uv');
const START = path.join(APP_DIR, 'start.html');
const DATA = () => app.getPath('userData');
const SETTINGS = () => path.join(DATA(), 'settings.json');
const LOGS = () => path.join(DATA(), 'logs');
const PKG = (() => { try { return JSON.parse(fs.readFileSync(path.join(APP_DIR, 'package.json'), 'utf8')); } catch { return {}; } })();
const LINKS = links(PKG.homepage || '');
const UPDATE = updateLinks(PKG.homepage || '');

let env: Env = process.env;
let win: BrowserWindow | null = null;
const page = (): WebContents | null => win && !win.isDestroyed() && !win.webContents.isDestroyed() ? win.webContents : null;          // the start screen, then the server's page
let server: ServerHandle | null = null;
let starting: Promise<void> | null = null;
let syncing: ChildProcess | null = null;          // uv sync on the first launch; stopped if the app quits meanwhile

const settings = () => loadSettings(SETTINGS());
const save = (s: Settings) => saveSettings(SETTINGS(), s);
const serverOrigin = () => server ? `http://127.0.0.1:${server.port}` : null;
const uvPaths = (): UvPaths => ({ uv: UV, workspace: WORKSPACE, data: DATA() });

// ---------- the start screen (start.html: first-launch progress, choosing a project, errors) ----------
type Screen = { kind: 'starting' | 'installing' | 'choose' | 'error'; text?: string; detail?: string };
let screen: Screen = { kind: 'starting' };
function show(s: Screen) {
  screen = s;
  const p = page();
  if (p && p.getURL().startsWith('file:')) p.send('start:screen', s);
}
function progress(line: string) { page()?.send('start:progress', line); }
async function showStart() { const p = page(); if (p && !p.getURL().startsWith('file:')) await p.loadFile(START); }

// ---------- Python and packages: uv sync into the app's data folder, once ----------
async function uvVersion(): Promise<string> { return (await run(UV, ['--version'], env, 5000)).out.trim(); }

async function ensurePython() {
  if (!fs.existsSync(UV)) throw new Error(`The uv binary is missing from the app (${UV}). Reinstall Topographic.`);
  const stamp = envStamp(WORKSPACE, await uvVersion());
  if (envReady(DATA(), stamp)) return;
  show({ kind: 'installing', text: 'Setting up Python for Topographic (first launch only). This downloads about 170 MB (Python and the Claude Agent SDK) and takes a few minutes.' });
  const c = uvSyncCommand(uvPaths(), env);
  const tail: string[] = [];
  const code = await new Promise<number | null>(resolve => {
    const child = spawn(c.cmd, c.args, { cwd: c.cwd, env: c.env as NodeJS.ProcessEnv, stdio: ['ignore', 'pipe', 'pipe'] });
    syncing = child;
    const each = (b: Buffer) => { for (const raw of b.toString().split('\n')) { const l = progressLine(raw); if (l) { progress(l); tail.push(l); if (tail.length > 12) tail.shift(); } } };
    child.stdout.on('data', each); child.stderr.on('data', each);
    child.once('error', e => { tail.push(String(e)); resolve(-1); });
    child.once('exit', resolve);
  });
  syncing = null;
  if (code !== 0) throw new Error('Setting up Python did not finish. Check the internet connection and try again.\n\n' + tail.join('\n'));
  markEnvReady(DATA(), stamp);
}

// ---------- the server ----------
async function ensureServer(project: string): Promise<ServerHandle> {
  if (server && server.child.exitCode === null && server.child.signalCode === null) return server;
  server = null;
  await ensurePython();
  show({ kind: 'starting', text: 'Starting the workspace server…' });
  const s = settings();
  const port = await freePort(s.port);
  save({ ...s, port });
  const h = startServer(serverCommand(uvPaths(), env, { port, project }), port, path.join(LOGS(), 'server.log'));
  server = h;
  h.exited.then(code => { if (server === h) { server = null; buildMenu(); console.log(`[agent-ide] server exited (${code})`); } });
  try { await waitHealthy(h, 90000); } catch (e) { await h.stop(); if (server === h) server = null; throw e; }
  console.log(`[agent-ide] server on port ${port}`);
  return h;
}

// Opens a project: remembered as the current one and first in Open Recent; the page is loaded with ?cwd= so it opens
// there (the running server and its agents carry on; the page switches folders on its own).
async function openProject(project: string) {
  if (!isFolder(project)) { dialog.showErrorBox('Not a folder', `${project} is not a folder any more.`); return; }
  save(opened(settings(), project));
  buildMenu();
  if (starting) return;          // the start in progress picks up the saved project
  starting = (async () => {
    try {
      await showStart();
      const h = await ensureServer(project);
      const target = settings().project || project;          // a project picked while this start ran wins
      await page()!.loadURL(`http://127.0.0.1:${h.port}/?cwd=${encodeURIComponent(target)}`);
    } catch (e: any) {
      await showStart();
      show({ kind: 'error', text: 'Topographic could not start.', detail: String(e?.message || e) });
    } finally { starting = null; buildMenu(); }
  })();
  await starting;
}

async function chooseFolder(): Promise<string | null> {
  const s = settings();
  const r = win
    ? await dialog.showOpenDialog(win, { title: 'Open a project', buttonLabel: 'Open', defaultPath: s.project, properties: ['openDirectory', 'createDirectory'] })
    : await dialog.showOpenDialog({ title: 'Open a project', buttonLabel: 'Open', defaultPath: s.project, properties: ['openDirectory', 'createDirectory'] });
  return r.canceled || !r.filePaths[0] ? null : r.filePaths[0];
}

async function askForProject() {
  if (!win) createWindow();
  const p = await chooseFolder();
  if (p) return openProject(p);
  if (!server) { await showStart(); show({ kind: 'choose', text: 'Choose the folder of a project to work in.' }); }
}

async function openProviders() {
  const p = page();
  if (!p || !server) return;
  const opened = await p.executeJavaScript(PROVIDERS_SCRIPT).catch(() => false);
  if (!opened) await p.loadURL(withHash(p.getURL(), 'providers'));
}

// ---------- IPC: the start screen (a local file) and the page (the server's origin) ----------
function from(e: IpcMainInvokeEvent, ...kinds: ('start' | 'page')[]) {
  const url = e.senderFrame?.url || '';
  const o = serverOrigin();
  const ok = (kinds.includes('start') && url.startsWith('file:')) || (kinds.includes('page') && !!o && url.startsWith(o + '/'));
  if (!ok) throw new Error('not allowed');
}
ipcMain.handle('start:screen', e => { from(e, 'start'); return screen; });
ipcMain.handle('start:choose', async e => { from(e, 'start'); await askForProject(); });
ipcMain.handle('start:retry', async e => { from(e, 'start'); reload(); });
ipcMain.handle('start:repair', async e => {          // the environment is made again from scratch on the next start
  from(e, 'start');
  fs.rmSync(path.join(DATA(), 'uv', 'stamp'), { force: true });
  fs.rmSync(path.join(DATA(), 'uv', 'env'), { recursive: true, force: true });
  const s = settings();
  return s.project ? openProject(s.project) : askForProject();
});
// The page's folder button: Electron's own folder dialog instead of the server's osascript chooser. The page then
// switches to the folder itself; the shell remembers it for Open Recent and the next launch.
ipcMain.handle('app:choose-folder', async e => {
  from(e, 'page');
  const p = await chooseFolder();
  if (!p) return { cancelled: true };
  save(opened(settings(), p));
  buildMenu();
  return { path: p };
});

// The title band's double-click (the page's or the start screen's, logic.ts titleDoubleClick): what the Mac's setting says,
// unless macOS already zoomed or minimized the window itself. Full screen: the page asks once and then hears of each change.
ipcMain.handle('app:title-double-click', e => {
  from(e, 'start', 'page');
  const w = win;
  if (!w) return;
  const before = w.getBounds(), wasMin = w.isMinimized();
  let moved = false; const mark = () => { moved = true; };
  w.once('resize', mark); w.once('move', mark); w.once('minimize', mark);
  setTimeout(() => {
    if (w.isDestroyed()) return;
    w.off('resize', mark); w.off('move', mark); w.off('minimize', mark);
    if (moved || w.isMinimized() !== wasMin || !sameBounds(before, w.getBounds())) return;
    const legacy = systemPreferences.getUserDefault('AppleMiniaturizeOnDoubleClick', 'boolean');
    const act = titleDoubleClick(systemPreferences.getUserDefault('AppleActionOnDoubleClick', 'string'), legacy);
    if (act === 'minimize') w.minimize();
    else if (act === 'zoom') w.isMaximized() ? w.unmaximize() : w.maximize();
  }, TITLE_DOUBLE_CLICK_WAIT);
});
ipcMain.handle('app:full-screen', e => { from(e, 'start', 'page'); return !!win?.isFullScreen(); });

// View > Reload: the page reloads; on the start screen, the start is tried again.
function reload() {
  const p = page();
  if (!win || !p) return;
  if (server && !p.getURL().startsWith('file:')) return p.reload();
  const first = firstRunAction(settings());
  first.kind === 'open' ? openProject(first.project) : askForProject();
}

// ---------- updates (logic.ts checkForUpdate: GitHub's latest release against this version; the app installs nothing) ----------
let updateNote: string | null = null;          // the note's script once a newer release is known; shown on every load of the page
function showUpdateNote() {
  const p = page(), o = serverOrigin();
  if (updateNote && p && o && p.getURL().startsWith(o + '/')) p.executeJavaScript(updateNote).catch(() => {});
}
async function updates(fromMenu: boolean) {
  if (!fromMenu && !updateCheckDue(settings().lastUpdateCheck, Date.now())) return;
  save({ ...settings(), lastUpdateCheck: Date.now() });
  const r = await checkForUpdate(net.fetch as unknown as FetchLike, UPDATE.api, app.getVersion());
  console.log(`[agent-ide] update check: ${r.kind}${'version' in r ? ' ' + r.version : ''}${'why' in r ? ' (' + r.why + ')' : ''}`);
  if (r.kind === 'newer') { updateNote = updateNoteScript(r.version, UPDATE.dmg); showUpdateNote(); }
  if (!fromMenu) return;          // at launch: the note or nothing; never a window
  if (r.kind === 'newer') {
    const b = await dialog.showMessageBox({ type: 'info', message: `Topographic ${r.version} is out`, detail: `You have ${app.getVersion()}. Download the new dmg, open it and drag Topographic into Applications to replace this one.`, buttons: ['Download', 'Later'], defaultId: 0, cancelId: 1 });
    if (b.response === 0) shell.openExternal(UPDATE.dmg);
  } else if (r.kind === 'current') {
    dialog.showMessageBox({ type: 'info', message: 'Topographic is up to date', detail: `${app.getVersion()} is the latest version.`, buttons: ['OK'] });
  } else shell.openExternal(LINKS.releases);          // GitHub could not be asked (offline, or the repository is private): its releases page
}

// ---------- the menu bar ----------
function buildMenu() {
  const s = settings();
  Menu.setApplicationMenu(Menu.buildFromTemplate(menuTemplate({
    about: () => app.showAboutPanel(),
    checkForUpdates: () => { updates(true); },
    openProject: () => { askForProject(); },
    openRecent: p => { openProject(p); },
    clearRecent: () => { save({ ...settings(), recent: [] }); buildMenu(); },
    reload: () => { reload(); },
    providers: () => { openProviders(); },
    readme: () => shell.openExternal(LINKS.readme),
    reportProblem: () => shell.openExternal(LINKS.issues),
    showLogs: () => { fs.mkdirSync(LOGS(), { recursive: true }); shell.openPath(LOGS()); },
  }, { appName: TITLE, recent: liveRecent(s.recent), serverUp: !!server })));
}

function createWindow() {
  const o = mainWindowOptions({ title: TITLE, preload: path.join(__dirname, 'preload.js') });
  const w = win = new BrowserWindow(o);
  w.on('page-title-updated', e => e.preventDefault());          // the window's title stays Topographic (Dock, Mission Control, Window menu)
  const pc = w.webContents;
  const full = (on: boolean) => () => { if (!pc.isDestroyed()) pc.send('app:full-screen', on); };          // the page drops its title band and offsets
  w.on('enter-full-screen', full(true)); w.on('leave-full-screen', full(false));
  pc.setWindowOpenHandler(({ url, frameName }) => {
    const d = windowOpenDecision({ url, frameName, opener: pc.isDestroyed() ? '' : pc.getURL(), origin: serverOrigin() });
    if (d.kind === 'minimap') return { action: 'allow', overrideBrowserWindowOptions: MINIMAP_WINDOW };
    if (d.kind === 'external') shell.openExternal(d.url);
    return { action: 'deny' };
  });
  // the mini-map window closes with the main window (else it alone would keep the app open) and when the page goes elsewhere
  let mini: BrowserWindow | null = null;
  pc.on('did-create-window', child => {
    mini = child;
    child.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    child.webContents.on('will-navigate', e => e.preventDefault());
    child.on('closed', () => { if (mini === child) mini = null; });
  });
  const closeMini = () => { if (mini && !mini.isDestroyed()) mini.close(); mini = null; };
  pc.on('did-start-navigation', d => { if (d.isMainFrame && !d.isSameDocument) closeMini(); });
  pc.on('did-finish-load', () => showUpdateNote());          // a newer release's note, on the page after each load
  pc.on('will-navigate', (e, url) => {
    const o = serverOrigin();
    if (url.startsWith('file:') || (o && url.startsWith(o + '/'))) return;
    e.preventDefault();
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
  });
  // developer tools have no menu item: Option-Command-I
  pc.on('before-input-event', (e, input) => { if (isDevToolsChord(input)) { e.preventDefault(); pc.toggleDevTools(); } });
  w.on('closed', () => { closeMini(); if (win === w) win = null; });
}

async function boot() {
  env = await userEnv();
  app.setAboutPanelOptions({ applicationName: TITLE, applicationVersion: app.getVersion() });
  buildMenu();
  createWindow();
  await page()!.loadFile(START);
  updates(false);          // at most once a day; silent unless a newer release exists
  const first = firstRunAction(settings());
  if (first.kind === 'open') return openProject(first.project);
  await askForProject();
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.whenReady().then(boot);
  app.on('activate', () => {
    if (win) return;
    createWindow();
    const s = settings();
    if (s.project && isFolder(s.project)) openProject(s.project); else page()!.loadFile(START).then(() => show({ kind: 'choose', text: 'Choose the folder of a project to work in.' }));
  });
  app.on('window-all-closed', () => app.quit());
  let quitting = false;
  app.on('before-quit', e => {
    if (quitting) return;
    e.preventDefault(); quitting = true;
    syncing?.kill('SIGINT');
    (server ? server.stop() : Promise.resolve()).finally(() => { console.log('[agent-ide] server stopped'); app.quit(); });
  });
}
