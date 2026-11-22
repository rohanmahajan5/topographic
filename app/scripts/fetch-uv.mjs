// Puts the pinned uv binary (uv-pin.json) at build/uv/<arch>/uv for each Mac architecture the app is built for.
// Downloads from Astral's GitHub release, checks the archive's SHA-256 against the pin and stops the build on a mismatch.
// A binary already in place is kept only if the archive it came from (kept beside it) still matches the pin.
//   node scripts/fetch-uv.mjs [arm64] [x64]     (default: both)
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const app = path.resolve(here, '..');
const pin = JSON.parse(fs.readFileSync(path.join(app, 'uv-pin.json'), 'utf8'));
const arches = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(pin.targets);
const sha256 = (buf) => createHash('sha256').update(buf).digest('hex');

for (const arch of arches) {
  const t = pin.targets[arch];
  if (!t) throw new Error(`uv-pin.json has no target for ${arch}`);
  const dir = path.join(app, 'build', 'uv', arch);
  const archive = path.join(app, 'build', 'uv-download', `uv-${pin.version}-${t.target}.tar.gz`);
  let buf = fs.existsSync(archive) ? fs.readFileSync(archive) : null;
  if (!buf || sha256(buf) !== t.sha256) {
    const url = pin.url.replace('{version}', pin.version).replace('{target}', t.target);
    console.log(`uv ${pin.version} (${arch}): downloading ${url}`);
    const res = await fetch(url, { redirect: 'follow' });
    if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
    buf = Buffer.from(await res.arrayBuffer());
    const got = sha256(buf);
    if (got !== t.sha256) throw new Error(`uv ${pin.version} (${arch}): SHA-256 ${got} does not match the pin ${t.sha256}; stopping`);
    fs.mkdirSync(path.dirname(archive), { recursive: true });
    fs.writeFileSync(archive, buf);
  }
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  // the archive holds uv-<target>/uv and uv-<target>/uvx; the app needs uv only
  execFileSync('/usr/bin/tar', ['-xzf', archive, '-C', dir, '--strip-components=1', `uv-${t.target}/uv`]);
  fs.chmodSync(path.join(dir, 'uv'), 0o755);
  console.log(`uv ${pin.version} (${arch}): checksum matches the pin; build/uv/${arch}/uv ready`);
}
