// The shell's decisions that need no Electron at run time, so test/shell-logic.test.mjs can drive them directly: the
// settings and the recent projects, what the first run does, the uv commands that set up Python and start the server,
// the menu, the developer-tools key chord, and the links the menu opens.
import type { MenuItemConstructorOptions } from 'electron';
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

export type Env = Record<string, string | undefined>;

// ---------- settings and recent projects (kept in the app's data folder, not in any project) ----------
export type Settings = { project?: string; recent?: string[]; port?: number; lastUpdateCheck?: number };
export const RECENT_MAX = 10;

export function loadSettings(file: string): Settings {
  try { const s = JSON.parse(fs.readFileSync(file, 'utf8')); return s && typeof s === 'object' ? s : {}; } catch { return {}; }
}
export function saveSettings(file: string, s: Settings) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(s, null, 2));
}
export const isFolder = (p?: string | null): p is string => { try { return !!p && fs.statSync(p).isDirectory(); } catch { return false; } };

// The project just opened goes first; a folder appears once; the list keeps RECENT_MAX.
export function addRecent(recent: string[] | undefined, project: string, max = RECENT_MAX): string[] {
  return [project, ...(recent || []).filter(p => p !== project)].slice(0, max);
}
// Folders that are gone (deleted, an unplugged disk) are not offered.
export function liveRecent(recent: string[] | undefined, exists: (p: string) => boolean = isFolder): string[] {
  return (recent || []).filter(p => typeof p === 'string' && exists(p));
}
export function opened(s: Settings, project: string): Settings {
  return { ...s, project, recent: addRecent(s.recent, project) };
}

// First run: no project chosen yet (or the saved one is gone) means the folder dialog, never a folder guessed for the
// person (the server's own default is the folder two levels above its files, which inside the app is the app itself).
export function firstRunAction(s: Settings, exists: (p: string) => boolean = isFolder): { kind: 'open'; project: string } | { kind: 'ask' } {
  return s.project && exists(s.project) ? { kind: 'open', project: s.project } : { kind: 'ask' };
}

// ---------- Python and the server, through uv ----------
// Everything uv fetches or makes lives in the app's data folder: the Python build, the package cache, the environment.
// UV_NO_CONFIG: a uv.toml of the person's does not change what the app runs. only-managed: uv's own Python build of the
// version in .python-version, never whatever python3 the Mac has. UV_PYTHON_INSTALL_BIN=0: nothing is put on the PATH.
// uv's default link mode on macOS (clone) is kept: the environment and the cache share the disk space of the packages.
export type UvPaths = { uv: string; workspace: string; data: string };
export function uvEnv(base: Env, p: UvPaths): Env {
  const env: Env = { ...base };
  for (const k of Object.keys(env)) if (k.startsWith('UV_') || k === 'VIRTUAL_ENV' || k === 'PYTHONHOME' || k === 'PYTHONPATH') delete env[k];
  return {
    ...env,
    UV_CACHE_DIR: path.join(p.data, 'uv', 'cache'),
    UV_PYTHON_INSTALL_DIR: path.join(p.data, 'uv', 'python'),
    UV_PROJECT_ENVIRONMENT: path.join(p.data, 'uv', 'env'),
    UV_PYTHON_PREFERENCE: 'only-managed',
    UV_PYTHON_INSTALL_BIN: '0',
    UV_NO_CONFIG: '1',
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONUNBUFFERED: '1',
  };
}

export type Command = { cmd: string; args: string[]; env: Env; cwd: string };

// Installs Python and the locked packages (first launch, or after an app update changed the lock). --frozen: exactly
// uv.lock, never re-resolved.
export function uvSyncCommand(p: UvPaths, base: Env): Command {
  return { cmd: p.uv, args: ['sync', '--frozen', '--no-dev', '--project', p.workspace], env: uvEnv(base, p), cwd: p.workspace };
}

// Starts server.py in that environment. --no-sync: the environment is already set up (uvSyncCommand), so a launch
// needs no network. The cache folder and the chosen project reach the server through its own variables.
export function serverCommand(p: UvPaths, base: Env, o: { port: number; project: string }): Command {
  const env = uvEnv(base, p);
  env.AGENT_IDE_PROJECT = o.project;
  env.AGENT_IDE_CACHE_DIR = path.join(p.data, 'cache');
  return { cmd: p.uv, args: ['run', '--frozen', '--no-sync', '--project', p.workspace, '--', 'python', 'server.py', '--port', String(o.port)], env, cwd: p.workspace };
}

// What the set-up environment was made from. When it differs from what is on disk (no environment yet, or a new app
// version with another lock, Python or uv), the first-launch screen runs uvSyncCommand again.
export function envStamp(workspace: string, uvVersion: string): string {
  const h = createHash('sha256');
  for (const f of ['uv.lock', '.python-version', 'pyproject.toml']) { try { h.update(fs.readFileSync(path.join(workspace, f))); } catch { h.update('-'); } }
  h.update(uvVersion);
  return h.digest('hex');
}
export function envReady(data: string, stamp: string): boolean {
  try {
    return fs.existsSync(path.join(data, 'uv', 'env', 'pyvenv.cfg')) && fs.readFileSync(path.join(data, 'uv', 'stamp'), 'utf8').trim() === stamp;
  } catch { return false; }
}
export function markEnvReady(data: string, stamp: string) {
  fs.mkdirSync(path.join(data, 'uv'), { recursive: true });
  fs.writeFileSync(path.join(data, 'uv', 'stamp'), stamp + '\n');
}

// uv's own lines (it prints no progress bars when its output is not a terminal), as one plain line for the screen.
export function progressLine(raw: string): string | null {
  const s = raw.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').trim();
  return s ? s.slice(0, 200) : null;
}

// ---------- links (package.json "homepage" is the project's GitHub page) ----------
export function links(homepage: string) {
  const repo = homepage.replace(/[#?].*$/, '').replace(/\/+$/, '');
  return { releases: repo + '/releases', readme: repo + '#readme', issues: repo + '/issues/new' };
}

// ---------- updates: a quiet note when GitHub has a newer release ----------
// The app is not signed with an Apple Developer ID, and macOS's updater framework (Squirrel.Mac, which Electron's
// autoUpdater and electron-updater use) installs updates only into signed apps. So the app does not install anything:
// at launch, at most once a day, and from the menu, it asks GitHub for the latest release, and when that is newer than
// the app it shows a note with a link to the newest dmg. The file names carry no version (Topographic-arm64.dmg), so
// <repo>/releases/latest/download/<name> is always the newest build. Any failure (offline, a private repository's 404,
// GitHub's rate limit) is silent at launch.
export const UPDATE_EVERY = 24 * 60 * 60 * 1000;          // ms between automatic checks
export const UPDATE_TIMEOUT = 10000;          // ms for GitHub's answer
export const DMG_NAME = 'Topographic-arm64.dmg';

// "0.2.0" or "v0.2.0" -> [0, 2, 0]; anything else (a pre-release tag, words) -> null, and is never offered.
export function parseVersion(v: unknown): number[] | null {
  const m = typeof v === 'string' ? /^v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})$/.exec(v.trim()) : null;
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}
export function isNewer(latest: unknown, current: unknown): boolean {
  const a = parseVersion(latest), b = parseVersion(current);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}
// The automatic check runs when none has run yet, when the last one is a day old, or when the clock went backwards.
export function updateCheckDue(last: number | undefined, now: number, every = UPDATE_EVERY): boolean {
  return typeof last !== 'number' || !Number.isFinite(last) || now - last >= every || now < last;
}
export function updateLinks(homepage: string) {
  const repo = homepage.replace(/[#?].*$/, '').replace(/\/+$/, '');
  const m = /^https:\/\/github\.com\/([\w.-]+)\/([\w.-]+)$/.exec(repo);
  return {
    api: m ? `https://api.github.com/repos/${m[1]}/${m[2]}/releases/latest` : null,
    dmg: `${repo}/releases/latest/download/${DMG_NAME}`,
    page: `${repo}/releases/latest`,
  };
}
export type UpdateResult = { kind: 'newer'; version: string } | { kind: 'current'; version: string } | { kind: 'unknown'; why: string };
export type FetchLike = (url: string, init: { headers: Record<string, string>; signal?: AbortSignal }) => Promise<{ ok: boolean; status: number; json(): Promise<any> }>;
export async function checkForUpdate(fetchFn: FetchLike, api: string | null, current: string, timeout = UPDATE_TIMEOUT): Promise<UpdateResult> {
  if (!api) return { kind: 'unknown', why: 'no GitHub repository in package.json' };
  try {
    const r = await fetchFn(api, { headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'Topographic' }, signal: AbortSignal.timeout(timeout) });
    if (!r.ok) return { kind: 'unknown', why: 'GitHub answered ' + r.status };
    const rel = await r.json();
    if (!rel || rel.draft || rel.prerelease) return { kind: 'unknown', why: 'no published release' };
    const v = parseVersion(rel.tag_name);
    if (!v) return { kind: 'unknown', why: 'the latest release has no version tag' };
    const version = v.join('.');
    return isNewer(version, current) ? { kind: 'newer', version } : { kind: 'current', version };
  } catch (e: any) {
    return { kind: 'unknown', why: String(e?.name === 'TimeoutError' ? 'no answer in time' : e?.message || e) };
  }
}
// The note, run in the server's page: one small line at the bottom left, "Topographic 0.2.0 is out — Download", and a ×.
// The link opens in the person's browser (the window's open handler sends outside links there). Run again, it replaces
// itself. The version is one parseVersion accepted and the link is built here, never taken from GitHub's answer.
export const UPDATE_NOTE_ID = 'topographic-update-note';
export function updateNoteScript(version: string, dmg: string): string {
  if (!parseVersion(version)) throw new Error('not a version: ' + version);
  return `(function(){
  var id = ${JSON.stringify(UPDATE_NOTE_ID)}, old = document.getElementById(id); if (old) old.remove();
  var n = document.createElement('div'); n.id = id; n.setAttribute('role', 'status');
  n.style.cssText = 'position:fixed;left:12px;bottom:10px;z-index:2147483000;font:12px/1.4 -apple-system,system-ui,sans-serif;color:#3d3a35;background:rgba(243,241,236,.94);border:1px solid #d8d3c8;border-radius:6px;padding:4px 6px 4px 10px;display:flex;gap:8px;align-items:center;-webkit-app-region:no-drag';
  var t = document.createElement('span'); t.textContent = ${JSON.stringify(`Topographic ${version} is out`)} + ' \\u2014 ';
  var a = document.createElement('a'); a.textContent = 'Download'; a.href = ${JSON.stringify(dmg)}; a.target = '_blank'; a.rel = 'noopener'; a.style.color = '#8a4b14';
  t.appendChild(a);
  var x = document.createElement('button'); x.textContent = '\\u00d7'; x.title = 'Hide'; x.setAttribute('aria-label', 'Hide');
  x.style.cssText = 'border:0;background:none;color:#8a857b;font:14px/1 system-ui;cursor:pointer;padding:0 2px';
  x.onclick = function(){ n.remove(); };
  ['pointerdown', 'mousedown', 'click', 'wheel', 'keydown'].forEach(function(k){ n.addEventListener(k, function(e){ e.stopPropagation(); }); });
  n.appendChild(t); n.appendChild(x); document.body.appendChild(n); return true;
})()`;
}

// ---------- the providers panel (built in the page: openProvidersPanel(), or the address ending in #providers) ----------
// Run in the page; true when the page's own function opened the panel, false when the page has none (the shell then
// loads the page's address with #providers).
export const PROVIDERS_SCRIPT = `(function(){ if (typeof window.openProvidersPanel === 'function') { window.openProvidersPanel(); return true; } return false; })()`;
export function withHash(url: string, hash: string): string { return url.replace(/#.*$/, '') + '#' + hash; }

// ---------- the main window: no grey title bar (2026-11-21) ----------
// The window's title bar is hidden and the page runs to the top of the window. Round two (2026-11-21: the
// window buttons, the title and the icon row stand roughly on one line): the macOS window buttons are
// moved down onto the line of the page's top-right icon row (live.html TOPBAR: icons 20 px tall at y 14, so centred at y 24);
// the page's app-only block (live.html "the app's title band") draws the wordmark centred on that line and makes the band
// from the window's left edge to the icon row drag the window (a double-click there does what the Mac's title-bar setting
// says), and starts the agent tree below the buttons. The start screen has the same band. In full screen macOS hides the
// buttons and the page drops the band and the offsets (it hears of full screen through the preload).
export const BG = '#f3f1ec';          // the page's background (live.html --bg, start.html --bg)
export const ICON_ROW_MID = 24;          // px: the vertical centre of the page's top-right icon row (live.html TOPBAR.MID: icons 20 px at y 14; was 20 before 2026-11-22: a taller bar, bigger icons)
// Electron places the buttons' top edge at y and centres them in a band of their height plus 2y; the buttons are about 14 px
// tall, so y 17 puts their centre at about 24. x 14: a little more room from the edge than the standard 7, as a toolbar
// window has.
export const TRAFFIC_LIGHTS = { x: 14, y: ICON_ROW_MID - 7 };
export function mainWindowOptions(o: { title: string; preload: string }) {
  return {
    width: 1440, height: 900, minWidth: 800, minHeight: 500, title: o.title, backgroundColor: BG,
    titleBarStyle: 'hidden' as const,          // the bar's grey gone, the buttons kept
    trafficLightPosition: { ...TRAFFIC_LIGHTS },          // the buttons on the icon row's line
    webPreferences: { preload: o.preload, contextIsolation: true, nodeIntegration: false, sandbox: true },
  };
}
// A double-click on the title band does what the Mac's setting for double-clicking a title bar says (System Settings, Desktop
// & Dock): AppleActionOnDoubleClick is Maximize (zoom; also when unset), Minimize, or None; the older AppleMiniaturizeOnDoubleClick
// minimizes. The page reports the double-click; the main process acts only if macOS has not already (the window did not
// move, resize or minimize within TITLE_DOUBLE_CLICK_WAIT), so the action never happens twice.
export const TITLE_DOUBLE_CLICK_WAIT = 250;          // ms
export function titleDoubleClick(action: string | undefined | null, legacyMinimize = false): 'zoom' | 'minimize' | 'none' {
  const a = (action || '').trim().toLowerCase();
  if (a === 'minimize') return 'minimize';
  if (a === 'none') return 'none';
  if (a === 'maximize' || a === 'fill' || a === 'zoom') return 'zoom';
  return legacyMinimize ? 'minimize' : 'zoom';
}
export type Bounds = { x: number; y: number; width: number; height: number };
export const sameBounds = (a: Bounds, b: Bounds) => a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;

// ---------- windows the page opens ----------
// The page's mini-map floats in a small window that stays on top. In a browser that is Document Picture-in-Picture; Electron
// does not provide that window, so in the app the page opens a plain child window under this name (preload.ts hands the page
// the name) and draws into it as it would into the picture-in-picture one. Only the server's page may open it, and only
// empty (the page fills it); an http(s) link to anywhere but the server goes to the system browser; nothing else opens.
export const MINIMAP_FRAME = 'topographic-minimap';
export const MINIMAP_WINDOW = {
  width: 360, height: 240, minWidth: 200, minHeight: 140, title: 'Mini-map', backgroundColor: '#f3f1ec',
  alwaysOnTop: true, fullscreenable: false, minimizable: false, maximizable: false, autoHideMenuBar: true,
};
export type WindowOpen = { kind: 'minimap' } | { kind: 'external'; url: string } | { kind: 'deny' };
export function windowOpenDecision(o: { url: string; frameName: string; opener: string; origin: string | null }): WindowOpen {
  const fromPage = !!o.origin && o.opener.startsWith(o.origin + '/');
  if (o.frameName === MINIMAP_FRAME) return fromPage && (o.url === '' || o.url === 'about:blank') ? { kind: 'minimap' } : { kind: 'deny' };
  if (/^https?:\/\//.test(o.url) && !(o.origin && (o.url === o.origin || o.url.startsWith(o.origin + '/')))) return { kind: 'external', url: o.url };
  return { kind: 'deny' };
}

// ---------- developer tools: no menu item; Option-Command-I opens them ----------
export type KeyInput = { type: string; key: string; code?: string; meta: boolean; alt: boolean; control: boolean; shift: boolean };
export function isDevToolsChord(i: KeyInput): boolean {
  return i.type === 'keyDown' && i.meta && i.alt && !i.control && (i.code === 'KeyI' || i.key.toLowerCase() === 'i');
}

// ---------- the menu bar ----------
export interface MenuHandlers {
  about(): void;
  checkForUpdates(): void;
  openProject(): void;
  openRecent(project: string): void;
  clearRecent(): void;
  reload(): void;
  providers(): void;
  readme(): void;
  reportProblem(): void;
  showLogs(): void;
}

export function menuTemplate(h: MenuHandlers, o: { appName: string; recent: string[]; serverUp: boolean }): MenuItemConstructorOptions[] {
  const recent: MenuItemConstructorOptions[] = o.recent.length
    ? [...o.recent.map(p => ({ label: p, click: () => h.openRecent(p) })), { type: 'separator' }, { label: 'Clear Menu', click: () => h.clearRecent() }]
    : [{ label: 'No Recent Projects', enabled: false }];
  return [
    { label: o.appName, submenu: [
      { label: `About ${o.appName}`, click: () => h.about() },
      { label: 'Check for Updates…', click: () => h.checkForUpdates() },
      { type: 'separator' }, { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
      { type: 'separator' }, { role: 'quit' },
    ] },
    { label: 'File', submenu: [
      { label: 'Open Project…', accelerator: 'CmdOrCtrl+O', click: () => h.openProject() },
      { label: 'Open Recent', submenu: recent },
      { type: 'separator' }, { role: 'close' },
    ] },
    { role: 'editMenu' },
    { label: 'View', submenu: [
      { label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => h.reload() },
      { type: 'separator' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
      { type: 'separator' }, { role: 'togglefullscreen' },
    ] },
    { label: 'Agents', submenu: [
      { label: 'Providers…', accelerator: 'CmdOrCtrl+Shift+P', enabled: o.serverUp, click: () => h.providers() },
    ] },
    { role: 'windowMenu' },
    { role: 'help', submenu: [
      { label: `${o.appName} README`, click: () => h.readme() },
      { label: 'Report a Problem…', click: () => h.reportProblem() },
      { type: 'separator' },
      { label: 'Show Logs', click: () => h.showLogs() },
    ] },
  ];
}
