// The shell's process work that does not need Electron: the user's PATH, a free loopback port, and starting,
// health-checking and stopping the workspace server (agent-ide/workspace/server.py, run through uv).
// test/shell-logic.test.mjs drives these functions directly.
import { spawn, execFile, ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

export type Env = Record<string, string | undefined>;

export function run(cmd: string, args: string[], env: Env, timeoutMs = 8000): Promise<{ code: number | null; out: string }> {
  return new Promise(resolve => {
    execFile(cmd, args, { env, timeout: timeoutMs, maxBuffer: 1 << 20 }, (err, stdout, stderr) => {
      const code = err ? (typeof (err as any).code === 'number' ? (err as any).code : null) : 0;
      resolve({ code, out: String(stdout || '') + String(stderr || '') });
    });
  });
}

// An app opened from Finder gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, not the user's. Ask the login shell for its PATH
// (what a terminal would have), then add the usual install folders, so `codex` and `claude` are found as in a terminal.
export async function userEnv(base: Env = process.env): Promise<Env> {
  const shell = base.SHELL || '/bin/zsh';
  const marker = '__AGENT_IDE_PATH__';
  let shellPath = '';
  const r = await run(shell, ['-ilc', `printf '${marker}%s${marker}' "$PATH"`], base, 5000);
  const m = r.out.match(new RegExp(marker + '(.*?)' + marker));
  if (m) shellPath = m[1];
  const home = os.homedir();
  const parts = [shellPath, base.PATH || '', '/opt/homebrew/bin', '/usr/local/bin', path.join(home, '.local/bin'), '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    .join(':').split(':').filter(Boolean);
  return { ...base, PATH: [...new Set(parts)].join(':') };
}

export function which(name: string, env: Env): string | null {
  for (const dir of (env.PATH || '').split(':')) {
    if (!dir) continue;
    const p = path.join(dir, name);
    try { fs.accessSync(p, fs.constants.X_OK); if (fs.statSync(p).isFile()) return p; } catch { /* next */ }
  }
  return null;
}

// Prefers the port used last time (the page keeps its layout per origin), else any free loopback port.
export function freePort(preferred?: number): Promise<number> {
  const tryPort = (port: number) => new Promise<number | null>(resolve => {
    const s = net.createServer();
    s.once('error', () => resolve(null));
    s.listen(port, '127.0.0.1', () => { const got = (s.address() as net.AddressInfo).port; s.close(() => resolve(got)); });
  });
  return (async () => (preferred && await tryPort(preferred)) || (await tryPort(0)) as number)();
}

export interface ServerHandle {
  port: number;
  child: ChildProcess;
  log: string[];
  exited: Promise<number | null>;
  stop(): Promise<void>;
}

// Starts the server from a command (logic.ts serverCommand: uv run against the bundled workspace's lock). Loopback only
// (server.py binds 127.0.0.1 itself). The page gets its mutation token from the server's own /api/bootstrap, same origin,
// so the shell never handles it. The server's output is kept (the last 200 lines for an error screen) and, when logFile
// is given, written there for Help > Show Logs.
// Stopping (2026-11-21): uv starts in a process group of its own (detached: its pid is the group id), so stop() reaches uv,
// server.py and Codex's app-server at once: SIGTERM to the whole group, then SIGKILL to whatever is left after graceMs, so
// nothing stays on the port even if Python hangs. server.py ends its Claude Code workers' groups itself on SIGTERM, within its
// own limit (STOP_LIMIT, 4 s), which graceMs stays above. The server is told the shell's pid (AGENT_IDE_PARENT_PID) and stops
// by itself if the shell is gone without stopping it (a crash).
export const STOP_GRACE_MS = 5000;
export function startServer(c: { cmd: string; args: string[]; env: Env; cwd: string }, port: number, logFile?: string, graceMs = STOP_GRACE_MS): ServerHandle {
  const env = { ...c.env, AGENT_IDE_PARENT_PID: String(process.pid) };
  const child = spawn(c.cmd, c.args, { cwd: c.cwd, env: env as NodeJS.ProcessEnv, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
  const log: string[] = [];
  let out: fs.WriteStream | null = null;
  if (logFile) { fs.mkdirSync(path.dirname(logFile), { recursive: true }); out = fs.createWriteStream(logFile, { flags: 'w' }); }
  const keep = (b: Buffer) => { out?.write(b); for (const l of b.toString().split('\n')) if (l.trim()) { log.push(l); if (log.length > 200) log.shift(); } };
  child.stdout!.on('data', keep);
  child.stderr!.on('data', keep);
  child.once('error', e => keep(Buffer.from(String(e) + '\n')));
  const exited = new Promise<number | null>(resolve => {
    child.once('exit', code => { out?.end(); resolve(code); });
    child.once('error', () => { out?.end(); resolve(-1); });
  });
  let stopping: Promise<void> | null = null;
  const stop = () => stopping ||= (async () => {
    if (child.pid === undefined) return;
    if (!groupSignal(child.pid, 'SIGTERM')) return;          // nothing of it is left
    const until = Date.now() + graceMs;
    while (Date.now() < until && groupSignal(child.pid, 0)) await new Promise(r => setTimeout(r, 50));
    groupSignal(child.pid, 'SIGKILL');
    await exited;
  })();
  return { port, child, log, exited, stop };
}

// A signal (0: is anything left?) to every process in the group `pgid`; false when the group is gone.
export function groupSignal(pgid: number, sig: NodeJS.Signals | 0): boolean {
  try { process.kill(-pgid, sig); return true; } catch (e: any) { return e && e.code === 'EPERM'; }
}

export function getJson(port: number, pathname: string, timeoutMs = 5000): Promise<any> {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path: pathname, headers: { Host: `127.0.0.1:${port}` }, timeout: timeoutMs }, res => {
      let body = '';
      res.on('data', c => body += c);
      res.on('end', () => { try { res.statusCode === 200 ? resolve(JSON.parse(body)) : reject(new Error(`${pathname}: HTTP ${res.statusCode} ${body.slice(0, 200)}`)); } catch (e) { reject(e); } });
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
  });
}

// Waits until the server accepts connections on its port. server.py binds its port only after Codex's app-server has
// initialised, so an open port means it is up. Not /api/state: on a machine with a long agent history the first
// snapshot can take longer than a probe should wait (the page itself waits for it). Fails at once, with the server's
// last output, if the process exits first (e.g. "Install a current Codex CLI or set AGENT_IDE_CODEX.").
export async function waitHealthy(h: ServerHandle, timeoutMs = 60000): Promise<void> {
  const until = Date.now() + timeoutMs;
  let gone = false;
  h.exited.then(() => { gone = true; });
  const open = () => new Promise<boolean>(resolve => {
    const s = net.connect(h.port, '127.0.0.1');
    s.setTimeout(1000, () => { s.destroy(); resolve(false); });
    s.once('connect', () => { s.destroy(); resolve(true); });
    s.once('error', () => resolve(false));
  });
  while (Date.now() < until) {
    if (gone) throw new Error('The workspace server stopped while starting:\n' + h.log.slice(-8).join('\n'));
    if (await open() && !gone) return;
    await new Promise(r => setTimeout(r, 300));
  }
  throw new Error(`The workspace server did not answer on 127.0.0.1:${h.port} within ${timeoutMs / 1000} s:\n` + h.log.slice(-8).join('\n'));
}
