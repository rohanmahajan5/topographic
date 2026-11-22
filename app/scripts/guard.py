#!/usr/bin/env python3
"""The shipping guard: stops the build when anything of the builder's machine or of real sessions would ship.

    python3 scripts/guard.py DIR [DIR...]

Carried over from the friends' package build (agent-ide/package/build.py on branch ide-package-2026-11-09) and tightened.
Any of these in any file under the given folders fails the build (exit 1, each hit listed with its file and line):
  - personal traces: this machine's home path and user name, the Git author's name (and each part of it) and email, and
    the names of the folders this checkout sits in under the home folder, plus GUARD_DENY (case-insensitive);
  - secrets: API-key, token and private-key shapes;
  - any UUID at all, unless listed in ALLOW_UUIDS. The old build compared UUIDs with the session ids in the builder's
    Claude Code and Codex stores; this guard does not read those stores, and instead refuses every UUID, which covers
    real session ids and the connector ids a person's own sessions show;
  - files and folders that must never ship: NEVER_PATHS (caches, the bridge venv, test fixtures, recorded sessions,
    concept and promo pages, test run folders, the three old unused pages).
The patterns are built at run time from the machine, so this file names nobody.
"""
import os
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

COMMON_FOLDERS = {'downloads', 'documents', 'desktop', 'projects', 'code', 'src', 'dev', 'git', 'repos', 'work', 'library'}
SECRETS = [
    r'sk-[A-Za-z0-9_-]{20,}', r'sk-ant-[A-Za-z0-9_-]{20,}', r'ghp_[A-Za-z0-9]{20,}', r'github_pat_[A-Za-z0-9_]{20,}',
    r'xox[abprs]-[A-Za-z0-9-]{10,}', r'AKIA[0-9A-Z]{16}', r'crsr_[A-Za-z0-9]{16,}', r'key_[A-Za-z0-9]{24,}',
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----', r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.',
]
UUID = re.compile(rb'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
ALLOW_UUIDS = set()
NEVER_PATHS = ['.cache', '.cache/*', '*/.cache/*', '.claude-bridge*', '*/.claude-bridge*', 'fixtures*', '*/fixtures*', 'concepts*', '*/concepts*',
               'promo*', '*/promo*', 'test', 'test/*', '*/test/*', 'test_*.py', '*/test_*.py', '*.jsonl', 'index.html', 'app.js', 'app.css',
               'live-canvas.html', 'mock.py', '*.db', '*.db-*', '.DS_Store', '*/.DS_Store', '__pycache__*', '*/__pycache__*', '.venv*', '*/.venv*']


def deny_folders():
    """The folder names between the home folder and this checkout (and its main checkout, for a Git worktree), plus any in
    GUARD_DENY (comma-separated): names of the builder's own folders, which have no business in a stranger's copy."""
    home, names = Path.home().resolve(), set()
    here = Path(__file__).resolve().parent
    for arg in ('--show-toplevel', '--git-common-dir'):
        r = subprocess.run(['git', '-C', str(here), 'rev-parse', arg], capture_output=True, text=True)
        top = (here / r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
        if top and top.name == '.git':
            top = top.parent
        if top and top.is_relative_to(home):
            names |= set(top.relative_to(home).parts)
    names = {n for n in names if 'topographic' not in n.lower()}          # the project's own checkout folder is its name, not a trace
    names |= {n.strip() for n in os.environ.get('GUARD_DENY', '').split(',') if n.strip()}
    return sorted(n for n in names if len(n) >= 4 and n.lower() not in COMMON_FOLDERS and not n.startswith('.'))


def personal_patterns():
    pats = [(re.sub(r'\\?[-_]', '[-_]', re.escape(p)), 'folder name') for p in deny_folders()]          # - and _ alike
    home, user = str(Path.home()), os.environ.get('USER') or Path.home().name
    pats.append((re.escape(home), 'home path'))
    if len(user) >= 4:
        # the GitHub handle in the project's own address (github.com/<handle>/..., io.github.<handle>.topographic) is meant to ship
        pats.append((r'(?<![a-z])(?<!github\.com/)(?<!github\.)' + re.escape(user) + r'(?![a-z])', 'user name'))
    for key in ('user.email', 'user.name'):
        v = subprocess.run(['git', 'config', key], capture_output=True, text=True).stdout.strip()
        if v:
            pats.append((re.escape(v), 'Git ' + key))
            if key == 'user.name':
                pats += [(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', 'Git user.name part') for w in v.split() if len(w) >= 4]
    return [(re.compile(p.encode(), re.I), why) for p, why in pats] + [(re.compile(p.encode()), 'secret shape') for p in SECRETS]


def scan(root, pats):
    problems, count = [], 0
    for f in sorted(root.rglob('*')):
        rel = str(f.relative_to(root))
        if any(fnmatch(rel, p) for p in NEVER_PATHS):
            problems.append(f'{root.name}/{rel}: must never ship')
        if not f.is_file():
            continue
        count += 1
        data = f.read_bytes()
        for rx, why in pats:
            for m in rx.finditer(data):
                line = data[:m.start()].count(b'\n') + 1
                problems.append(f'{root.name}/{rel}:{line}: {why}')          # the hit itself is not printed: it may be the secret
        for m in UUID.finditer(data):
            if m.group().decode().lower() not in ALLOW_UUIDS:
                line = data[:m.start()].count(b'\n') + 1
                problems.append(f'{root.name}/{rel}:{line}: a UUID (could be a real session or connector id)')
    return count, problems


def main(dirs):
    if not dirs:
        sys.exit(__doc__)
    pats, total, problems = personal_patterns(), 0, []
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            sys.exit(f'guard: {d} is not a folder')
        n, p = scan(root, pats)
        total += n
        problems += p
    if problems:
        print(f'guard: FAILED, {len(problems)} problem(s) in {total} files:')
        for p in problems:
            print('  ' + p)
        sys.exit(1)
    print(f'guard: passed ({total} files in {len(dirs)} folder(s): no personal traces, secret shapes, UUIDs or never-ship files)')


if __name__ == '__main__':
    main(sys.argv[1:])
