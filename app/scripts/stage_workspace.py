#!/usr/bin/env python3
"""Stages the workspace server the app ships into agent-ide/app/build/workspace, then runs the guard on it.

    python3 scripts/stage_workspace.py [--allow-dirty]

What ships is worked out from the server itself, not from a hand-kept list:
  - Python: every module under agent-ide/workspace that server.py and claude_worker.py import (Python's modulefinder,
    which also sees imports inside functions), minus the mock and test-only providers (MOCK_ONLY);
  - the page: what server.py's static route serves (live.html, engine/*.js, logos/*.svg, brand/*.css, *.woff2 and favicon.svg, vendor/*.js and
    *.wasm) plus the logos' attribution, the wordmark typeface's licence and credits and the parser's licences;
  - uv's files: pyproject.toml, uv.lock, .python-version;
  - connectors.json, rebuilt with only the entries keyed by a plain server name (the UUID keys are connector ids seen in
    one person's own sessions or the mock's invented ones; neither belongs in a stranger's copy);

Every file is taken from the committed tree (HEAD), never from the working folder, so nothing uncommitted or untracked
ships. A shipped file with uncommitted changes stops the stage (commit first) unless --allow-dirty, which still ships
the committed version. The guard (guard.py) then scans the stage and stops on any trace of the builder's machine.
"""
import json
import modulefinder
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
WS = APP.parent / 'workspace'
OUT = APP / 'build' / 'workspace'
REPO = Path(subprocess.run(['git', '-C', str(APP), 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=True).stdout.strip())

ENTRIES = ['server.py', 'claude_worker.py', 'claude_wrap.py']          # the server, the Claude worker it starts as its own process, and the wrapper the worker runs each CLI through
MOCK_ONLY = {'mock.py', 'providers/fake.py'}          # imported only under AGENT_IDE_MOCK / AGENT_IDE_TEST_PROVIDERS; their fixtures don't ship
SERVED = ['live.html', 'engine/*.js', 'logos/*.svg', 'logos/ATTRIBUTION.md',          # server.py's static route (do_GET), and the logos' credits
          'brand/*.css', 'brand/*.woff2', 'brand/OFL-*.txt', 'brand/ATTRIBUTION.md',          # the wordmark (page and start screen), its licence and credits
          'brand/favicon.svg',          # the page's tab icon (the solid logo)
          'vendor/*.js', 'vendor/*.wasm', 'vendor/LICENSES.md']          # the file window's parser (web-tree-sitter and three grammars) and their licences
UV_FILES = ['pyproject.toml', 'uv.lock', '.python-version']


def git(*args):
    return subprocess.run(['git', '-C', str(REPO), *args], capture_output=True, check=True).stdout


def imported():
    """Workspace-relative paths of the Python files the entries import, transitively."""
    found = set()
    sys.path.insert(0, str(WS))
    for entry in ENTRIES:
        mf = modulefinder.ModuleFinder(path=[str(WS)])          # only the workspace: stdlib and site packages are not ours to ship
        mf.run_script(str(WS / entry))
        found.add(entry)
        for m in mf.modules.values():
            f = getattr(m, '__file__', None)
            if f and Path(f).resolve().is_relative_to(WS):
                found.add(str(Path(f).resolve().relative_to(WS)))
    return sorted(found - MOCK_ONLY)


def served():
    return sorted({str(p.relative_to(WS)) for pat in SERVED for p in WS.glob(pat)})


def connectors(raw):
    d = json.loads(raw)
    keep = {k: v for k, v in d.items() if not k.startswith('_') and not any(c.isdigit() for c in k)}
    return json.dumps({'_about': 'Connector display names and marks: MCP server id or name -> name and the logo file in logos/ (without .svg). '
                                 'Add your own: a claude.ai connector appears in tool names as mcp__<id>__<tool>.', **keep}, indent=1) + '\n'


def main():
    allow_dirty = '--allow-dirty' in sys.argv
    files = sorted(set(imported() + served() + UV_FILES))
    tracked = set(git('ls-files', '-z', '--', str(WS)).decode().split('\0'))
    rel_ws = WS.relative_to(REPO)
    problems, dirty = [], []
    for f in files:
        if str(rel_ws / f) not in tracked:
            problems.append(f'{f}: not committed (only committed files ship)')
    dirty = [f for f in files if git('status', '--porcelain', '--', str(rel_ws / f)).strip()]
    if dirty and not allow_dirty:
        problems += [f'{f}: has uncommitted changes (commit first, or --allow-dirty to ship the committed version)' for f in dirty]
    if problems:
        sys.exit('stage stopped:\n  ' + '\n  '.join(problems))
    if OUT.exists():
        subprocess.run(['rm', '-rf', str(OUT)], check=True)
    for f in files:
        data = git('show', f'HEAD:{rel_ws / f}')
        dest = OUT / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    files.append('connectors.json')
    (OUT / 'connectors.json').write_text(connectors(git('show', f'HEAD:{rel_ws}/connectors.json')))
    sha = git('rev-parse', '--short=9', 'HEAD').decode().strip()
    (OUT / 'VERSION').write_text(sha + '\n')
    files.append('VERSION')
    print(f'workspace staged from HEAD {sha}: {len(files)} files in {OUT.relative_to(APP)}' + (f' ({len(dirty)} with uncommitted changes left out of it)' if dirty else ''))
    for f in sorted(files):
        print('  ' + f, flush=True)
    r = subprocess.run([sys.executable, str(APP / 'scripts' / 'guard.py'), str(OUT)])
    sys.exit(r.returncode)


if __name__ == '__main__':
    main()
