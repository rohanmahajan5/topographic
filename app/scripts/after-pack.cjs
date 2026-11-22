// electron-builder afterPack hook: the last look at what ships, after the app folder is laid out and before it is signed
// and put in a dmg or zip. Stops the build unless:
//   - Resources holds the staged workspace and the uv binary for this architecture, and uv runs;
//   - the guard (scripts/guard.py) passes on Resources/workspace and on the app's own code (app.asar, unpacked here);
//   - app.asar holds only the shell: dist/*.js, start.html, package.json (the wordmark's files are in the workspace, brand/).
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

exports.default = async function afterPack(ctx) {
  const appDir = path.join(ctx.appOutDir, `${ctx.packager.appInfo.productFilename}.app`);
  const res = path.join(appDir, 'Contents', 'Resources');
  const uv = path.join(res, 'uv', 'uv');
  for (const p of [path.join(res, 'workspace', 'server.py'), path.join(res, 'workspace', 'uv.lock'), uv])
    if (!fs.existsSync(p)) throw new Error(`afterPack: ${path.relative(appDir, p)} is missing`);
  const arch = { 1: 'x64', 3: 'arm64' }[ctx.arch] || String(ctx.arch);
  const info = execFileSync('/usr/bin/file', [uv]).toString();
  if (!info.includes(arch === 'arm64' ? 'arm64' : 'x86_64')) throw new Error(`afterPack: uv is not a ${arch} binary: ${info}`);
  const asarLib = require('@electron/asar');
  const asarFile = path.join(res, 'app.asar');
  const inside = asarLib.listPackage(asarFile).map(p => p.replace(/^\//, ''));
  const allowed = p => p === 'dist' || /^dist\/[\w.-]+\.js$/.test(p) || p === 'start.html' || p === 'package.json';
  const extra = inside.filter(p => !allowed(p));
  if (extra.length) throw new Error('afterPack: app.asar holds files the app does not need:\n  ' + extra.join('\n  '));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-ide-asar-'));
  try {
    asarLib.extractAll(asarFile, path.join(tmp, 'app'));
    execFileSync('python3', [path.join(__dirname, 'guard.py'), path.join(res, 'workspace'), path.join(tmp, 'app')], { stdio: 'inherit' });
  } finally { fs.rmSync(tmp, { recursive: true, force: true }); }
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
  if (/\/OWNER\//.test(pkg.homepage || '')) console.warn('afterPack: WARNING package.json "homepage" is still the placeholder; Check for Updates, README and Report a Problem open it');
  console.log(`afterPack: ${arch} app checked: ${inside.length} entries in app.asar, workspace and uv in Resources`);
};
