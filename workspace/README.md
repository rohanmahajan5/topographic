# Topographic: the workspace server (developer notes)

`server.py` is the part of Topographic that does the work: it finds the agents' sessions on this Mac, starts and talks to
agents through each provider's own tool, and serves the page (`live.html`) that draws them. The Mac app in `../app` only
starts this server and shows its page; the server also runs on its own.

## Run

```sh
python3 server.py                     # Python 3.11+, standard library only; open http://127.0.0.1:4321
uv run --frozen python server.py      # with uv: also installs claude-agent-sdk, needed to drive Claude Code
```

Options (environment variables):

| Variable | What it does |
| --- | --- |
| `AGENT_IDE_PROJECT=/path` | the project folder the page opens on |
| `AGENT_IDE_CACHE_DIR=/path` | where the server keeps what it has read from transcripts (default: the app data folder) |
| `AGENT_IDE_CODEX=/path/to/codex` | which Codex binary to use |
| `CODEX_HOME`, `CLAUDE_CONFIG_DIR` | where Codex and Claude Code keep their sessions (their own defaults otherwise) |
| `AGENT_IDE_NO_WINDOWS=1` | never open a browser or a folder chooser (tests, headless runs) |
| `AGENT_IDE_MOCK=junior` or `attention` | an invented scenario from `fixtures/junior-swe/` instead of this Mac's sessions |

`python3 server.py --port N` picks the port; `--shared` connects to a Codex app-server you already run.

The page also has a made-up floor for trying it without any agent: `http://127.0.0.1:4321/?stress=6&tick=1`.

## Files

- `server.py`: the HTTP server, the Codex app-server connection and the page's API (`/api/state`, `/api/canvas`,
  `/api/history`, `/api/events`, ...).
- `providers/`: one module per agent tool behind `providers/base.py`: `codex.py`, `claude_code.py`, `acp.py` with
  `acp_flavors.py` (Antigravity and Cursor Agent over the Agent Client Protocol), `opencode.py`; `fake.py` for tests.
- `provider_setup.py`: the Providers panel's checks (installed, signed in, ready) and its sign-in steps.
- `claude_worker.py`, `claude_wrap.py`, `claude_bridge.py`: drive Claude Code through the Claude Agent SDK.
  `claude_bridge_setup.sh` makes a separate venv for it when the server runs without uv.
- `log_watch.py`, `usage.py`, `compact_points.py`, `edits.py`: transcript reading, plan usage, compaction points, file edits.
- `live.html` and `engine/*.js`: the page. `logos/` and `brand/`: provider marks and the wordmark's typeface.
- `connectors.json`: display names and marks for MCP connectors, by server id. Add your own.
- `mock.py` and `fixtures/`: the invented scenarios. `test_*.py`: the tests.

## Security

The server listens only on 127.0.0.1, checks the Host and Origin of every request, needs a per-process token for anything
that changes something, serves only its own page and files, refuses paths outside the project, and escapes model text
before the page shows it. A web page cannot reach it; a program running on the same Mac can ask it for its token. Do not
expose its port to a network.

## Tests

Run single test files, for example:

```sh
python3 -m unittest test_server test_providers
```

`../test/` drives the page in a headless browser with Playwright (see its README).
