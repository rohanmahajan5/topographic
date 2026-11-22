# Topographic: the Mac app (developer notes)

The person-facing README is [`../README.md`](../README.md). These notes describe how the shell works.

The app is a thin Electron/TypeScript shell around the workspace server (`../workspace/server.py`) and its page
(`live.html`). It adds no features to the page.

## What happens at launch

1. The app recovers the login shell's PATH. An app opened from Finder gets only `/usr/bin:/bin:…`, and without the
   login PATH the server would not find `codex` or `claude`.
2. It opens one window: no grey title bar (`titleBarStyle: 'hidden'`); the page runs to the top, and the macOS window
   buttons are moved (`trafficLightPosition`) onto the line of the page's top-right icon row. In the app only, the page
   (`live.html`, block "the app's title band") draws the Topographic wordmark centred on that line, makes the band from
   the left edge to the icon row drag the window (a double-click does what the Mac's title-bar setting says), and starts
   the agent tree below the buttons; in full screen all of that goes. The window first shows `start.html`, which has the
   same drag band. The wordmark's style and typeface are in `workspace/brand/` (Overpass, SIL OFL 1.1). If no project is saved (or the saved folder is gone), it asks with
   Electron's folder dialog. It never falls back to a folder of its own: the server's default, two folders above its
   files, would be inside the app.
3. **Python.** The app carries `uv` (`Resources/uv/uv`, pinned in `uv-pin.json`, SHA-256 checked at build time). The
   first launch runs `uv sync --frozen --no-dev --project Resources/workspace`. That installs the Python in
   `workspace/.python-version` (3.12.16) and the packages in `workspace/uv.lock` (claude-agent-sdk 0.2.178 and its
   dependencies) into `~/Library/Application Support/Topographic/uv/`. `start.html` shows uv's lines meanwhile. A stamp
   (a hash of the lock, the Python version, pyproject.toml and the uv version) marks the setup as done, so later
   launches skip the sync. A new app version with another lock sets up again. uv is run with `UV_NO_CONFIG=1`,
   `UV_PYTHON_PREFERENCE=only-managed` and `UV_PYTHON_INSTALL_BIN=0`, and the person's own `UV_*`, `VIRTUAL_ENV` and
   `PYTHONPATH` are dropped, so their setup changes nothing and nothing lands on their PATH.
4. **Server.** `uv run --frozen --no-sync --project Resources/workspace -- python server.py --port <free port>`, with
   `AGENT_IDE_PROJECT` set to the chosen folder and `AGENT_IDE_CACHE_DIR` set to `<data>/cache`. The port is the last
   one used if it is free, so the page keeps its layout. The server counts as up once its port accepts connections
   (server.py binds only after Codex's app-server has started). Its output goes to `<data>/logs/server.log`.
5. The window loads `http://127.0.0.1:<port>/?cwd=<project>`. The page fetches its own token from `/api/bootstrap`;
   the shell never handles it.
6. On quit the app sends SIGINT to uv. uv passes it on to server.py, which closes Codex's app-server. If that does not
   finish within 5 s, the app sends SIGKILL.

The Claude Code worker (`claude_worker.py`) runs in the server's own Python, which has the Agent SDK. The
`.claude-bridge` venv that `claude_bridge_setup.sh` makes is still used first when it exists (developers).

## Menus

- **Topographic**: About; Check for Updates… (see Updates below); Hide; Quit.
- **File**: Open Project… (⌘O, Electron's folder dialog); Open Recent (the last 10, kept in `settings.json`, folders
  that are gone left out; Clear Menu); Close.
- **Edit**: the standard items.
- **View**: Reload (⌘R; on the start screen it tries the start again); Actual Size, Zoom In, Zoom Out; Full Screen.
  Developer tools have no menu item: Option-Command-I opens them.
- **Agents**: Providers… (⇧⌘P). It calls the page's `openProvidersPanel()` when the page has it. Otherwise it loads
  the page's address with `#providers`.
- **Window**, **Help** (Topographic README, Report a Problem… opens a new GitHub issue, Show Logs).

`homepage` in `package.json` is the project's GitHub page (`https://github.com/rohanmahajan5/topographic`). Every link in
the menus, and the update check, comes from it.

## Updates

The app installs nothing by itself. Electron's updater (Squirrel.Mac) works only for apps signed with an Apple Developer
ID ("Your application must be signed for automatic updates on macOS. This is a requirement of Squirrel.Mac.", Electron's
autoUpdater documentation), and this app is only ad-hoc signed. Instead (`logic.ts`, "updates"; `main.ts` `updates()`):

- at launch, at most once a day (`lastUpdateCheck` in `settings.json`), and from **Check for Updates…**, the app asks
  `https://api.github.com/repos/<owner>/<repo>/releases/latest` (no token; GitHub allows 60 such requests an hour per
  address) and compares its `tag_name` (`vX.Y.Z`; drafts, pre-releases and other tags are ignored) with the app's version;
- if the release is newer, a one-line note appears at the bottom left of the page: "Topographic X.Y.Z is out — Download",
  linking `<repo>/releases/latest/download/Topographic-arm64.dmg`, which opens in the person's browser;
- at launch, anything else (offline, a 404 while the repository is private, rate limits) is silent; from the menu, "up to
  date" gets a short message and a failed check opens the releases page.

Release files carry no version in their names (`artifactName` `${productName}-${arch}.${ext}`), so the `latest/download`
link always reaches the newest build. The version lives in `package.json`, the About box and the release tag.

## The page and the shell

`preload.ts` gives each page only its own API, and the main process checks the sender's address on every call:

- `start.html` (a local file) gets `window.agentIDEStart`: screen state, progress lines, choose, try again, set up
  Python again.
- both get `titleDoubleClick()` for their title band (the main process acts only if macOS has not already zoomed or
  minimized the window); the page also gets `fullScreen()` and `onFullScreen()`.
- the workspace page (`http://127.0.0.1:<port>`) gets `window.agentIDE.chooseFolder()`. `live.html`'s folder button
  uses it when present, in place of the server's `osascript` chooser. The shell also records the chosen folder in Open
  Recent.

## What ships

`npm run stage` (`scripts/stage_workspace.py`) builds `build/workspace` from the committed tree (HEAD). It stops if a
shipped file has uncommitted changes. What it takes:

- the Python modules `server.py` and `claude_worker.py` import (Python's modulefinder), without `mock.py` and
  `providers/fake.py`;
- what the server's static route serves: `live.html`, `engine/*.js`, `logos/*.svg`, `brand/*.css`, `brand/*.woff2`,
  `brand/favicon.svg` (the tab icon) and `vendor/*.js`, `vendor/*.wasm`, plus their credits and licences;
- `pyproject.toml`, `uv.lock`, `.python-version`;
- `connectors.json` without its UUID keys;
- a `VERSION` file (the commit).

The app's icon is `assets/icon.icns` (`build.mac.icon` in package.json), written with the tab icon by
`workspace/brand/logo-draft/make.py` (needs `rsvg-convert` and macOS's `iconutil`).

`scripts/guard.py` runs on the stage. After packaging it runs again, from `scripts/after-pack.cjs`, on
`Resources/workspace` and on the unpacked `app.asar`. The build fails on any of these:

- this machine's home path, user name or Git name or email;
- the project's folder names;
- key and token shapes;
- any UUID;
- test fixtures, `.jsonl` records, caches, the bridge venv, concept and promo pages, test folders, and the old
  `index.html`, `app.js`, `app.css` and `live-canvas.html`.

`after-pack.cjs` also checks that `app.asar` holds only `dist/*.js`, `start.html` and `package.json`, and that
`Resources/uv/uv` is a binary for the architecture being built.

## Layout

```
app/
  package.json              scripts; electron-builder: dmg + zip, arm64 only, ad-hoc signed, not notarized
  uv-pin.json               uv version and per-architecture SHA-256
  src/main.ts               Electron: window, menus, IPC, start sequence
  src/logic.ts              no Electron at run time: settings, recent projects, first run, uv commands, menu, key chord, update check
  src/server.ts             no Electron: login PATH, free port, start / wait / stop the server
  src/preload.ts            agentIDEStart (start screen) and agentIDE (page)
  start.html                first-launch progress, project choice, errors
  scripts/fetch-uv.mjs      downloads and checks uv into build/uv/<arch>/uv
  scripts/stage_workspace.py, scripts/guard.py, scripts/after-pack.cjs
  test/shell-logic.test.mjs npm test
```

`dist/`, `build/` and `release/` are build outputs, ignored by Git.

## Commands

```sh
npm ci
npm test            # compile + the logic tests (no Electron window, no real server, no agent)
npm run dev         # stage + fetch uv + open the app from source
npm run build:dir   # arm64 .app in release/mac-arm64
npm run build       # release/Topographic-arm64.dmg and release/Topographic-arm64.zip
```

`AGENT_IDE_USER_DATA=<folder>` points the app's data folder elsewhere, for a clean first launch.
