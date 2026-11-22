# Driving the live page as a user

`drive.mjs` opens `agent-ide/workspace/live.html` in headless Chromium and runs a plan of actions with real pointer and
keyboard events (Playwright's `page.mouse` and `page.keyboard`). It never calls the page's functions to act. It finds
what to click by name in the page's hit registry, `window.__hits()`, and checks the outcome afterwards against
`window.__state()`.

## The rule for builders

A change to the page is verified only when a plan ran against it and the screenshots were opened, looked at and
described in words. An assert that passed is not enough: `__state()` says what the page believes, the screenshot shows
what a person would see.

## Run

`node attention-core.mjs` checks the attention registry (live.html's AttnCore, taken out of the page) on synthetic agents, no
browser and no server: the error mark's rules one by one (2026-11-09).

```sh
cd agent-ide/test
npm install                       # once; playwright 1.42.1, which uses the cached chromium-1105
python3 ../workspace/server.py --port 4321 &      # loopback only; needs Codex signed in
node drive.mjs plans/walk.json --url 'http://127.0.0.1:4321/?tick=1' --size 800x500
node drive.mjs plans/walk.json --url 'http://127.0.0.1:4321/?tick=1' --size 1440x900 --out /some/dir
```

Options: `--url` (default `http://127.0.0.1:4321/?tick=1`; `?tick=1` draws frames from a timer, so a hidden or headless
tab keeps animating), `--size WxH` (default 800x500), `--out DIR` (default `out/<plan>-<size>-<time>/`, ignored by Git),
`--var name=value` (used in the plan as `$name`). The exit code is 0 only if every assert passed, nothing was missed and
the page threw no error.

Each action prints one line: the time since start, the action, and for pointer actions the target's name, rectangle and
the point used. `<out>/run.json` holds the log, every assert with pass or fail, the page's `console` messages, its
uncaught errors, the screenshots' names and the page state at the end. Screenshots are PNGs in `<out>/`; a failed run
also leaves `failure.png`.

`walk.json` sends nothing to any agent and picks its columns by what they carry and by harness, never by title, so it runs
on whatever floor the server opens. After its first screenshot it clicks an agent that shows an error or a finished mark the
way a person does, on its column (and, with `?minimap=inline`, a marked mini-map row), and checks the mark goes and stays gone
over two canvas polls (`__state().attention.clicked`); with no such mark on the floor those steps are skipped. (It passes on this repository at 800x500 and 1440x900; its memory steps need a column
with a compaction in the part of its log the server read, its first step and its git steps a Git repository with a branch besides the current one: it waits for the trunk before its first click, because the tree's first answer refits the camera); pass
`--var folder=<a scratch folder with a README.md>` for its sheet steps, which save into that folder.
Right after loading it checks the floor's packing (2026-11-09): at most a quarter of the floor is empty room (`floor.empty`, the share no page block, open folder or closed box covers, against `floor.emptyMax`; 0.65 on the old shelf rows, 0.17 packed, on this repository) and no folder moved more than 4 floor units at any layout over 4 s (`floor.steady`, `floor.movedLast4s`).
At the fitted view (and three wheel steps out and in) it checks the group stacks and that every active agent is named
(those checks moved to the map engine's stills-and-counts harness; `crowding.stackGaps` and `crowding.unnamedActive` are gone); where no columns stand close or none is active on screen those steps are skipped.
Its closed-lane steps are the exception: they close `agent-ide/` (and `assignments/`, so the box is in view on the compact
floor), rewind the rail to the run's start and step moment by moment (→, a `key` with `until`) to the first moment when two or
more columns stand in one closed box, because on this repository its columns are now mostly finished subagents folded into their
parent; they depend on the repository's history having such a moment.
`mock-junior.json` runs on the fake scenario (`AGENT_IDE_MOCK=junior python3 ../workspace/server.py --port 4348`, see
`../workspace/fixtures/junior-swe/FIXTURE.md`) within ten minutes of the server's start: the four connector islands
with their marks, the Drive island's sheet, a courier in replay and the focused column's connector blocks.
`answer-on-card.json` runs only on a made-up floor (`?stress=20&replay=0&mix=busy&tick=1`; `&seed=4` gives it approvals as well as
questions) and sends nothing anywhere: no bottom bar, the rail's icon showing and hiding it, a card folded and brought back from its
agent's icon, cards answered by their buttons (Allow once, Decline, Type an answer and typed in the transcript's reply box) with the request resolved and the
card gone, and a message typed to a made-up agent accepted. The made-up floor answers itself (`stressWrite` in live.html).
`minimap.json` sends nothing and runs with `?minimap=inline` (its own `url`; pass `--url` for another port): the map icon in the
top-right row (usage, map, rail icon, plug, folder), nothing of the mini-map running while it is closed, the mini-map drawn into the page
at the bottom right (the canvas the floating window holds; drivers only) with a name and a line for every running or waiting
agent, no overlapping texts, the finished agents of the last hour listed (at most five, no numbers on the floor), a folder
closed and opened on the main floor followed by the mini floor, a click on an agent there flying the main floor to it, pointing
at a finished name, and the icon closing it again. Attention and plan use there too: an agent on the engine's list shows its mark in
its logo's place and before its name, never its logo; the header says no count in the page (the agent tree's head counts; the floating window has "N need you", which steps to the next agent that needs you); the header's plan bars are the harnesses the usage gauge draws; the icons and a plan bar
hovered, and an icon clicked (back to now, its column, its transcript). Those steps are skipped where the floor has nothing open or no
plan reading (the attention mock has no plan readings). The real floating window cannot be driven by this script.
`outside-click.json` runs on the made-up floor (`?stress=6&tick=1`, any server): a popup (Plan use, providers) closes on a click
outside it and on Escape; the outside click does only that (a column clicked is not focused); a click or a drag from inside keeps it;
the next top-right button switches straight to its panel. The rule lives in `live.html` under "popups" (POPUPS).
`one-window.json` runs on the made-up floor (`?stress=6&tick=1`, any server) and sends and saves nothing: the agent window and the
file window are never open together (2026-11-22; `WINDOWS` in `live.html`), an unsaved edit kept when an agent opens, Escape; its
chat-link and Plan-use steps skip there (no links, no ledger rows) and run on a real floor.
`attention.json` runs on the attention mock (`AGENT_IDE_MOCK=attention python3 ../workspace/server.py --port 4387`, see
`../workspace/fixtures/junior-swe/FIXTURE.md`), started fresh for each run (answering in the list uses up its three open
requests, and its `mock` steps happen once; the mock resolves them itself, nothing reaches an agent), at 1440x900 with
`?tick=1&minimap=inline`: the engine's one list read by every surface (one entry per agent that asks, failed, finished unread or is silent, taken by name through the agent tree's rows `tree-row:<agent>:<cond>`; counts are of agents; no icon over a column, the agent's mark takes its logo's place), no overlaps, the logos of the listed agents hidden; a Claude Code session stopped on AskUserQuestion as a question on
its column, its mini-map row and in the agent tree (no answer buttons: "answer it in Claude Code"), gone once the mock answers it;
a tree row's click (back to now, the column, its transcript; a failed one's chat shows the failed turn); the edge marks
after a flight (behind the transcript panel, and off screen once zoomed in), hovered and clicked; the agent tree's head (`tree-need`: the count of agents, a click steps to the next that needs you, the agents that ask first) against the engine's list, a
question and an approval answered on their cards (the floor shows one card at a time, the oldest, none for the agent whose transcript is open), a row click, six steps from the head reading what only tells you something, and the logos back; the error mark as "the latest turn
failed and you have not looked" (none for the receipts agent, whose failures were each followed by a turn, nor for a subagent
resumed after its failure; the Summarise row in the tree, a subagent's mini-map row and the Summarise column each clearing what
they show, the column its folded subagents' marks too, over a reload and two canvas polls); a click on the agent itself (a column, a
mini-map row) clearing its finished answer and errors once (gone from the column, the engine's list, the tree head's count and the mini-map's
"N need you", still gone after a reload) while a question or an approval stays; a subagent's hand-back never in the head's count
or the list, only a pulse to its parent gone a few seconds later; the finished mark as "returned and not looked at" (an unread
answer keeps its agent standing past ten minutes, gone while the agent works again from the Claude app, back when that turn ends, never two on one column), a turn
begun with an idle registration staying in the mini-map's running list, and a session the Claude app holds: the line above its
chat's box, "send as copy", the choice card, Cancel, the line gone once the app lets it go, and a copy as its own "copy of …" column. Its mini-map steps need `?tick=1&minimap=inline` and are skipped without it.
`inactive.json` runs on a fresh attention mock (port 4391, 1440x900, `?tick=1&minimap=inline`) and moves the page's clock with
`clock` steps: the inactive rule (2026-11-10). Two answers nobody has read stand past ten minutes as full columns with their
finished marks; the PR session, read by a click on its column, stays nine minutes after the read and folds to a stub at eleven;
the unread RCP-142 session keeps its mini-map row past the hour; past 48 hours it is inactive, its mark closed.
`usage-panel.json` sends nothing: the usage gauge at the left end of the top-right row (gauge, map, rail icon, plug, folder), the
ledger it opens at the right edge with the top 3 agents by spend, "show 3 more" until the list is complete, "top 3 only" back to
three, the ledger never past the rail, a row's hover ringing its column and its click focusing it, Esc closing it. It needs a
floor with at least four agents that have recorded calls.
`compaction.json` sends nothing: every column whose harness says where it compacts (`compaction` on the canvas, from
`../workspace/compact_points.py`) is drawn the same full height (its own scale: floor 0, top its point), its scale's top label
is the point in tokens and its half label the half (one label for a group that shares a point, left to right otherwise), no
scale label or level covers a name, a readout or a trace label, and the rim is amber from 80% and burnt orange from 95% with
the level beside an unfocused hot column; at the fitted view, focused (the tips name the point and its rule) and after a wheel
zoom. Its hot steps need a column past 80%: start the server with `AGENT_IDE_TEST_PROVIDERS=codex,claude,fake` for the fake
harness's running session at 97% of a 100k point; on a floor with none they are skipped and logged.
`floor-grows.json` sends nothing and writes nothing; it runs on a server opened on `~/projects/habit-tracker`
(`AGENT_IDE_PROJECT=... python3 ../workspace/server.py --port 4408`, at 1440x900): live, all six of the run's files and both
commits; the knob dragged to the rail's start, no page and only the "Start" knot; played back at 5x, each file rising onto its own
spot at its birth (README.md, then the streak files, then app.js, then index.html and styles.css) with the floor's size unchanged
and the commit knot still absent; the lead named while it runs, mid-replay and at 80% (however short its column); the knob at 80% and 97% of the track (a drag's `to` and `frac`, never pixels: the track's length
depends on the readout's room, which grows as the run ages), before and after the commit; back to now. Which files are
new and when each was born is `births()` in `../workspace/server.py`; the page reads them as `grows` in `__state()`.
`archive.mjs` (not a plan: `node archive.mjs [--port 4436] [--shots DIR]`) sends nothing and checks the archive stack on two
floors through one server opened on this repository: this repository's (many chats) and `~/projects/habit-tracker`'s
(few; its 48-hour line moved to 24 h with `?line=24` so a replay moment crosses it). The pile on the root lane's first desk, left
of the oldest column; its label, a one-line stack beside the pile, at the fitted view, folded, hovered and open, and at the pile
zoomed out (about 0.4x and 0.7x the fit) and in (1.5x, 4x, 10x): over nothing (the page's own list of what it keeps off, and on its
own the hit registry's targets and the texts printed before it), printed on the first clean spot whenever one exists, and printing it
moving no name or readout; open in place (oldest first, the lane gliding right with no jump, no subfolder moving, no attention icon on
an archived chat), Esc and a second click folding it; the mini-map's one "Archive · N" row; a chat either side of its line in the
replay. With `--shots DIR` it saves `many-fit.png`, `many-zoomed-out.png`, `many-a-little-out.png`, `many-zoomed.png` and the hover
and open views. The walk checks the same pile, label (over nothing, printed when it can be: at the fit, hovered, open, folded, three
wheel steps out and three in), open and Esc on whatever floor it runs.
`codex-plan.json` sends nothing and runs with `?tick=1&minimap=inline`: where a Codex column on the floor has a plan, its progress
line, a click focusing it, its checklist (at once while it runs, by ≡ once it has finished) with one row per step in the plan's order
(`progress.focused.listOk`, its statuses in `progress.focused.steps`), standing over no other column, stub, mark or printed name
(`progress.focused.listOver` empty), Esc, and the mini-map with no ring beside an attention icon;
where none has a plan, every step is skipped. With gpt-6-luna on codex-cli 0.167 the plan tool is off unless the run sets
`-c tools.update_plan.enabled=true`, and its calls stand inside the JavaScript of the `exec` calls (`../workspace/providers/plans.py`).
`live-one-message.json` starts one real agent and costs one short Codex turn: run it
only on purpose, with `--var folder=<an empty folder with a README.md>`.

## Plan actions

A plan is `{"actions": [...]}` (or a bare list), run in order.

| action | what it does |
| --- | --- |
| `{"click": "<name>"}` | moves the mouse to the target's click point and clicks |
| `{"hover": "<name>"}` | moves the mouse there and stays |
| `{"drag": "<name>", "dx": -120, "dy": 0}` | presses on the target, moves by dx, dy in 12 steps, releases (dragging the floor pans; dragging `sheet:resize` resizes the file sheet; dragging `sheet:drag` moves it) |
| `{"drag": "rail:knob", "to": "rail:track", "frac": 0.8}` | the same, released at that fraction of the `to` target's width (read just before the drag), on the dragged target's row: a share of the rail rather than pixels |
| `{"type": "text"}` | types into whatever has the keyboard focus |
| `{"key": "Enter"}` | presses one key (Playwright key names: Enter, Escape, Tab, ...) |
| `{"wheel": 500}` | turns the mouse wheel at the viewport's centre by that delta (positive zooms the floor out) |
| `{"key": "ArrowRight", "until": {"closedLane.multi": true}, "max": 300}` | presses the key until every `until` entry matches `__state()` (same matching as `assert`), at most `max` times (default 200); logs how many presses and whether it got there (follow it with an `assert`) |
| `{"wait": 500}` | waits, in ms |
| `{"wheel": 400, "at": "<name>"}` | turns the mouse wheel over that target (or the view's centre without `at`); positive zooms out (used for the git labels' size) |
| `{"mock": "answer"}` | the attention mock's next outside event (`GET /api/mock?do=…`: `answer`, `handback`, `followup`; see `../workspace/fixtures/junior-swe/FIXTURE.md`), never through the page; `after` waits |
| `{"clock": 660}` | moves the page's clock that many seconds forward (`window.__clock`, on a mock floor only; the run fails elsewhere), so a rule that waits on time (the inactive rule's ten minutes after a read, its 48 hours) is reached without waiting; `__state().clock` is the total moved, `__state().idle` each column's state under the rule |
| `{"reload": true}` | loads the page again in the same browser (its local storage, so what was read, stays) and waits for its first frame, as at the start; the way back to the fitted view after a flight |
| `{"waitFor": "<name prefix>", "timeout": 10000}` | polls the registry until a name with that prefix is on screen; fails at the timeout |
| `{"shot": "01-floor"}` | screenshot to `<out>/01-floor.png` |
| `{"assert": {...}}` | compares keys of `__state()` (see below); add `"stop": true` to end the run if it fails |
| `{"sweep": "<name prefix>"}` | moves the real mouse onto every registered target with that prefix and checks the page's own hover pick names the same object: the registry against the hit-test |
| `{"log": "text"}` | writes a line to the log |
| `{"if": "<name>", "then": [...]}` | runs the actions in `then` only when that target (with `with`, `provider`, `not` as for a click) is on screen and clickable now; otherwise logs why and skips them. Not an assert and never a miss: for things a live floor does not always show (a path strip, a stub the transcript panel covers) |

Pointer actions and `waitFor` take `"save": "x"`: the resolved name is kept as `$x`, its part after the first colon as
`$x.id`, its second segment as `$x.session` (for `label:`, `block:`). Pointer actions take `"after": ms` (pause after the
action, default 150). Pointer actions first wait for any camera flight or drag to finish and for two fresh frames, then
for the target's rectangle to hold still over two reads 120 ms apart (at most 2.5 s): the live floor glides columns,
stubs and tiles to new desks when a folder closes or a row folds back, and a click aimed mid-glide lands where the target
was.

Floor robustness (2026-11-07). `"reveal": true` on a pointer action, a `wheel` with `at`, or an `if`: a target that is
registered but not clickable (off screen, under the rail or the line) is brought toward the middle of the view by
dragging bare canvas (a drag on the floor pans it), at most five drags, each logged; still not clickable, it is a miss
(or an `if` skip) as before. `"reaim": n` on a hover: after the pause, if the target's rectangle no longer contains the
pointer (the floor moved it), aim at it again, at most n times, each logged; a target that stays put is never re-aimed,
so a hover the page gets wrong still fails its assert. `"reaim": n` with `"expect": {...}` on a click: after the pause, if
the expected state (as in an `assert`) has not come and the target is still registered but no longer under the click
point, click it again at its new place, at most n times (a live column's labels restack as its turns arrive); a click
that landed on the target is never repeated. `{"until": {...}, "timeout": 10000}` (no `key`): waits until the state
matches, at most the timeout, and logs how long it took; the assert after it decides (for results read from the server,
whose answer time depends on its load); `has`/`lacks` work there as in an assert, and in `when` and `expect` too. `"when": {...}` on an `if`: state entries (as in an `assert`,
`!` negates) that must also hold, read before the target is looked for; for toggles such as a folder's name (open it
again only if it is closed). `{"when": {...}, "then": [...]}` without `if`: the state condition alone (for example, the
rest of a chain only once its column is focused, however it was focused); a variable never saved (its step skipped)
makes either form false.

Names: an exact name, or `prefix*` for the first name that starts with the prefix and can be clicked. `"with": "notch:"`
keeps only targets `X:<id>` for which `notch:<id>` (or `notch:<id>:...`) also exists; a list of prefixes requires all.
`"provider": "codex"` (or `"claude"`) keeps only targets whose column (the name's second segment) is a session of that
harness, read from `__state().providers`. `"not": "<id>"` drops targets of that column (another column than `$c.id`); a list of ids drops each. `"grows": "<name>"` on a pointer action checks, after it, that the named target's
rectangle got wider or taller and neither smaller (recorded as an assert; used for the window grips).
A miss fails the run and prints every name that was present. A target that is registered but covered (by the chat
window, the line, another object) or off screen is also a miss, with the reason.

Assert values: a string ending in `*` is a prefix; `true` means present and non-empty, `false` absent or empty, `null`
absent; a string against a list (like `open`) means the list contains it; dotted keys reach inside (`expanded.column`).
A key starting with `!` must NOT match. `has` and `lacks` check the registry's names instead of the state; `has_html` takes a
CSS selector that must match an element in the page (used for the chat's rendered Markdown).

## The registry (`window.__hits(prefix)`)

Each frame the page records every object it hit-tests. `window.__hits()` returns
`[{name, kind, x, y, w, h, cx, cy, reachable}]` in the canvas's CSS pixels as of the last frame: `x, y, w, h` is the
target's bounding rectangle; `(cx, cy)` is a point where a real click hits exactly this target (inside it, the topmost
hit there, and no DOM element such as the transcript panel or the line over it), found from the centre outward;
`reachable` is false when no such point exists. Passing a prefix limits the point search to matching names (the others
come back as names only), which keeps the call cheap on large floors. Nothing is drawn and no hit-testing changed: the
rectangles are read from the very tests the page clicks with.

| name | what |
| --- | --- |
| `column:<session>` | a column (its whole silhouette) |
| `stub:<session>` | an inactive session's grey stub on its folder's lane (hover names the chat and its harness; a click focuses it) |
| `block:<session>:<i>` | the i-th call's block in a column (0 = first call); only columns drawn block by block; a click there hits the column |
| `notch:<session>` | the one-line label beside an unfocused column |
| `progress:<session>` | plan progress: the column's progress line under its name (or its ring alone, crowded), or, focused while it shows its plan, the line with its checklist; while the plan is unfinished it stands in the notch's place. `__state().progress.columns`: each column's count, current step, form and focused view (plan or chat) |
| `label:<session>:<turn>` | a trace label beside the focused column (right of it, or left when its right is crowded); `<turn>` is the index in that column's list of turns; |
| `expand:<session>` / `close:<session>` | ⤢ and × on the opened label |
| `colname:<session>` | the focused column's name above it |
| `provider:<session>` | the harness mark (or logo) between a column's readout and its name |
| `readout:<session>` | a column's context readout ("385k of 1M") |
| `ghost:<session>` | the dotted ghost of the focused column, up to its tier |
| `band:<session>` | a compaction band on a column |
| `follow:<session>` | "↻ follow" by the focused readout, after a drag or the wheel stopped the camera following |
| `path:<session>` | the path strip under a column standing in a closed folder: one row, cut to the column's desk ("workspace/", "…/round-4/", a dot when no letter fits); hovering it or its column shows the full "in <path>/" once as a label; none for a column standing in the closed folder itself; a click opens the folders down to it |
| `pin:<session>` | the pin on the page a column last touched |
| `flag:<session>` | the alert flag at a column's top: amber ! (no sign of work for ten minutes) or unread answers |
| `ask:<session>` | the ask flag of a column whose request waits behind another's |
| `ask` | the open request written beside its column |
| `tab:<session>:<child>`, `request:<session>` | a tab on the parent for each finished (folded) child or hand-back, named by the child's column name; a child's request sheet |
| `unfold:<parent>:<child>` | a member of a parent's row (focus chain): a finished child as its stub, a running child on the parent's lane as its column; registered over the member's own `stub:`/`column:` target while the row is out (the parent or one of its children focused, or its `children:` word hovered); a click focuses the child |
| `timeline:<session>` | ≡ beside a focused readout, on a column with children: opens or closes its timeline (the trace labels) |
| `fan:<parent>:<child>` | no longer drawn (the fan was replaced by the focus chain's row, 2026-11-06) |
| `children:<session>` | "· N children" on a column's readout line: every child of the column, folded into it, beside it or working in another folder (2026-11-09); hover unfolds the near ones into the row and draws the far ones' lines at full strength, a click focuses the column |
| `kin:<parent>:<child>` | the dot in the parent's colour where the line to a far child (a running child on another lane) lands; hover names the parent and the child's folder, a click focuses the parent |
| `tick:<session>:<i>` / `pending:<session>:<i>` | an answered approval's tick / an open one's, on the focused column |
| `allow:<request id>` / `decline:<request id>` | the approval buttons in a focused column's trace |
| `island:<connector>` | a connector's island beyond the floor's far edge (a click opens its sheet) |
| `courier:<session>:<i>` | the courier of a column's i-th MCP call while it is out (0 = the column's first MCP call on this floor) |
| `island-sheet:<connector>`, `island-sheet:close` | an island's sheet (tools called from this floor, by session) and its × |
| `tile:<folder>` | a closed folder's tile; with columns standing in it, the closed box (a tile deep, as wide as its name and lane need) |
| `closedlane:<folder>` | the lane on a closed box: from its name to past its "+" (a click opens the folder, and the folders down to each of its columns) |
| `name:<folder>` | a folder's name label (opens a closed one, closes an open one) |
| `strip:<folder>` | an open folder's top band (closes it) |
| `dir:<folder>` | an open folder's floor |
| `plus:<folder>` | the dotted "+" plinth at the end of a folder's lane |
| `page:<file>` / `plot:<file>` | a file's standing page / the plot it stands on |
| `minimap` | the map icon at the top right, between the usage gauge and the rail's icon: opens or closes the mini-map's floating window (grey without Document Picture-in-Picture) |
| `minimap-agent:<session>`, `minimap-finished:<session>` | with `?minimap=inline` only: a running or waiting agent in the mini-map (its name in the list, else its bar), a finished one's name in the list |
| `minimap-alert:<session>:<kind>`, `minimap-name-alert:<session>:<kind>` | with `?minimap=inline` only: an agent's attention icon in the mini-map, in its logo's place (a finished agent's: on top of its bar) / before its name in the list; hover words it, a click does what the main floor's icon does |
| `minimap-progress:<session>` | with `?minimap=inline` only: a row's plan progress (ring, with "3/5" while the list is short) before its name; one key icon per row, so a row with an attention icon has none (`minimap.progress.ringWithAlert` must be 0; `minimap.progress.rows` gives each row's key icon) |
| `minimap-foryou`, `minimap-plan:<harness>` | with `?minimap=inline` only: "N need you" in the mini-map's header (floating window only; a click steps to the next agent that needs you) / a harness's plan bar there (hover lists its windows with their reset times) |
| `tree-need` | the agent tree's head ("N need you"): counts the agents on the engine's one list (agents, not requests); a click, or Control+full stop, steps to the next, most pressing first |
| `attn-pulse:<child>` | a hand-back as it happens: the short pulse from that subagent to its parent (about two seconds); a click focuses the parent |
| `attn-edge:<column>` | the mark with an arrow at the view's edge (column off screen) or a window's edge (column behind it): the first listed agent of that column, +N for the others; hover words it, a click goes there and opens its transcript |
| `tree-row:<agent>:<cond>`, `tree-alerts` | the agent tree's rows for agents on the engine's list (`<cond>`: `asking`, `failed`, `finished` or `stalled`; a click goes there and opens its transcript) and its foot line's desktop-alerts opt-in |
| `pick-folder` | the folder button at the top right (opens the Mac's folder chooser; never clicked by a plan) |
| `rail`, `rail:knob`, `rail:play`, `rail:live`, `rail:speed-`, `rail:speed+` | the time machine: its strip, knob, pause/play, back to now, slower, faster |
| `rail:track` | the rail's track itself (its length the same in all four states and over a drag); for a drag's `to` |
| `card-allow:<column>:<request>`, `card-decline:…`, `card-deny:…`, `card-type:…`, `card-option:<column>:<i>` | the open request's card beside its column (one card at a time, the oldest not folded): its buttons (an approval's Allow once and Decline, a request for more access's Deny, a question's options, or Type an answer, which opens the column's transcript with the cursor in its reply box) |
| `card-fold:<column>:<request>` | the card's − at its top right: folds it away unanswered (kept for the page's life); a click on the agent's `tree-row:<column>:asking` row brings it back |
| `rail-toggle` | the time rail's icon in the top-right row (usage, map, rail, plug, folder): shows and hides the rail. Hidden by default on a live floor and on a made-up floor with `replay=0`, shown on one that replays; remembered per browser. The rail shows itself anyway while the floor is behind now or Codex is not connected; its keys work while it is hidden |
| `reply:<session>`, `reply:new:<folder>` | the transcript's reply box (for a column, for a folder's new agent); `reply-send` its send button |
| `reply:line`, `reply:change` / `reply:done`, `reply:continue` / `reply:stop`, `reply:chip` | the quiet line under the box (what the agent runs on), its change link, continue or stop, a picked command's chip |
| `reply:model`, `reply:effort`, `reply:approval`, `reply:plan` | the selects under change (a session's); `agent:model`, `agent:effort`, `agent:approval`, `agent:harness`, `agent:plan` for a new agent |
| `transcript:name`, `transcript:rename-input` | the name in the transcript's head (a click renames) and its field |
| `transcript:link:<path>[:<line>]`, `transcript:putback:<entry>`, `transcript:all` | a file link in an opened entry, a not-sent message's "put it back in the box", "show everything" at the list's foot |
| `sheet:resize`, `sheet:drag` | the file sheet's corner grip and title row (its click point is clear of the title and the controls) |
| `cmd:<name>`, `cmd:$<skill>` | an entry in the reply box's command list (/ or $ or Cmd+K in the box) |
| `sheet`, `sheet:text`, `sheet:save`, `sheet:close` | the file sheet, its text, save and × |
| `trunk` | the default branch's trunk beside the project floor |
| `lane:<branch>` | a branch's lane (and its label); a click turns its floor on or off |
| `lane-here:<branch>` | the current branch's lane: its floor is the project's, always on (a click only says so) |
| `knot:<hash>` | a commit's knot on the trunk or a lane (hover: subject, author, age) |
| `bead:<branch>` | a merge commit's bead; a click unfolds the commits it brought in |
| `floor:<branch>` | a branch's floor beside the project's (its root folder) |
| `lanestub:<branch>:<session>` | a session on a branch whose floor is off, standing beside the lane (a click turns the floor on) |
| `stratum:<session>:base`, `stratum:<session>:compact:<i>` | the § base stratum (focused column only) / a stub's § chip (its latest compaction); a click focuses the column and opens its sheet |
| `compaction:<session>` | the map engine's compaction line on the agent's floor label (shown only when the engine gives it room); a click focuses the column and opens the summary sheet |
| `seam:<session>:<path>` | a note the session wrote and read back, up the column's side |
| `memory:<session>` | "memory" on the focused readout; opens the memory sheet |
| `memsheet:<kind>:<session>`, `memsheet:close`, `memsheet:link:<path>` | the memory sheet (`memory`, `summary`, `base`), its ×, a file link in it |

The project's root folder is `.` (so `plus:.`, `dir:.`). `<session>` is the column's name as the page shows it: the
session's title, cut to 30 characters with "…", made unique with " (2)". A title can change while the page is open (Codex
names a new session after its first message), so do not hold a saved session name across a reply. If a name occurs
twice the second gets `#2`.

## The summary (`window.__state()`)

For asserts after an action, never instead of one: `frames`, `connected`, `project`, `error`, `columns`, `columnNames`,
`providers` (column → `codex` / `claude`), `stubs` (columns drawn as stubs),
`focused`, `open` (open folders' paths), `expanded` (`{column, turn}` of the opened label), `reply` (the reply box's
registry name, while its transcript shows), `replyText` (the reply box's text), `railShown`, `railOn` (the rail's own switch), `status` (`connecting…`, `Codex
disconnected` or `connected`), `cards` (`shown` request id, `column`, `kind`, `buttons`, `folded`, `foldedColumns`, `open`: the columns
with a request open, `box`), `hover` (registry name
under the mouse), `moving` (camera flight or drag in progress), `toast`, `ask`, `labels` (the focused column's shown labels,
top to bottom), `rail`, `speed`, `sheet` (the file sheet: path, read, edited, text), `controls` (the reply box's settings: `watchOnly`,
`selects`, `loading`, `settings`, `model`, `effort`, `approval`, `changed`, `chip`, `placeholder`, `menu`, `run`, `cards`), `transcript.reply`
(`to`, `text`, `focused`, `disabled`, `send`, `line`, `run`, `more`, `chip`, `menu`, `held`, `choice`, `drafts`), `notches` and `thoughts` (reasoning) and `windows` (`sheet`: the
window's `left`, `top`, `w`, `h` now, `dragged`, and `moved`: its place now less its place when the last title drag began, to
the nearest 10 px, so a drag can be checked again after a resize, a close and a reopen). `crowding`: `folded` (column names folded into a parent), `lines` (parent→child lines drawn), `fanned` (always 0 since the focus chain's row replaced the fan), `stoodUp` (the focused folded child, or null), `strayReadouts` (readouts more than one column width from their own cap outside a same-lane stack; should be empty), `clusters` (columns standing close on one lane, two or more, found again from every on-screen column that is not a stub, not from the page's own groups), `stacks` (each stack drawn: `members`, `lines` as "12k name" top to bottom), `stackGaps` (a cluster member that earns a line with no line of its own, a wrong context number or name, or lines out of left-to-right order; should be empty), `active` (on-screen agents running or waiting on you, a question or approval open counting as waiting) and `unnamedActive` (those whose name printed nowhere this frame, alone or in a stack; should be empty). `focusChain`: `column` (the focused column), `layer` (`unfolded`, `timeline`, `chat` or null), `unfolded` (its row's members), `finishedChildren` (the finished ones among them, drawn as stubs), `linesDrawn` (parent→child lines drawn from it: only to running children standing outside the row), `tip` (the stub hover's second line this frame, e.g. "handed back · 9 h ago · Claude Code"), `refused` (the last refused send to a finished subagent and where the focus went). `gitLabels`: `font` (the lane labels' size in px), `smaller` (below its full 10.5 px, after a zoom-out), `shown`, `staggered` (slid along their lane), `hidden` (shown only while their lane is hovered), `overlaps` (pairs of printed labels that overprint; should be empty). `closedLane`: `tile` (a plain tile's `w`, `h` in floor units), `boxes` (each closed box with columns: `folder`, its registered `w`, `h`, `columns`, `strips` with `text`, `w`, `desk`), `flat` (every box is exactly a tile deep), `wide` (every box holding two or more columns is wider than a tile), `multi` (some box holds two or more), `stripsFit` (each column in a subfolder has its strip within its desk, none for one in the folder itself), `label` and `labelFull` (the hovered strip's full path, and whether it is the column's whole path), `opened` (the last folder opened over a lane: `folder`, `columns`, `inner` true when each now stands on an open folder's lane). `beams`: read and write beams in flight this frame; `rebuilds`: how many times the floor was rebuilt from a changed canvas (a past call must not beam again after one). `floor`: `w`, `h`, `ratio` of the root floor, `fit` (the zoom that would fit it now), `camFit` (the zoom it was last fitted at), `tiles`. `dim`: the columns the hover or the focus greys now. `marks`: every provider mark drawn this frame as `{column, colour}`, the colour its brand's own, fixed (Claude `#D97757`, Google Drive `#4285F4`, Slack `#4A154B`, Linear `#5E6AD2`; Codex, OpenCode, GitHub and any other black or unlisted brand the ink-grey `#5b5f66`), never changed by running, idle or focus. `markOf`: `focused` (the focused column's mark colour or null), `byAgent` (every mark as `{column, colour, expected}`, `expected` the brand colour), `wrong` (columns whose mark is not in its brand colour; should be empty) and `ghostWrong` (the focused column's ghost colour when it is not the agent's own colour, else null). `ghosts`: every dotted ghost drawn this frame as `{column, colour}`. `labelPlaces`: `list` (every trace label, tick and notch drawn this frame: `name`, `kind`, `dist` in px from its anchor on its column, `colW`, `ratio` = dist / colW, `leader` `full` or `stub`, `w`), `farthest`, `farthestStub`, `near` (every one within 1.5 column widths or drawn with a full leader), `widest`, `narrow` (none wider than 3 column widths or a trace label's paper). Also `git` (`current`, `base`,
`lanes`, `on`, `floors`, `unfolded`, `stubs`; null outside Git), `memsheet` (`<kind>:<session>` of the open memory sheet) and
`memsheetRead` (its data arrived). `minimap`: `supported` (Document Picture-in-Picture here), `open`, `host` (`pip` or `inline`), `size`, `agents`, `running`, `waiting`, `header`,
`openFolders` and `closedTiles` (as the mini floor drew them), `finished` (`inHour`, `listed`, `more`, `numbers` drawn for them,
`ok`: the latest five of the last hour listed, the rest counted, no numbers), `namesOk`, `overlaps` (pairs of texts that overlap),
`crossNames`, `crossText`, `crossLines`, `crossBars` (lines through another agent's name, through any other text, name lines
crossing each other, lines over another agent's bar), `unnamed`, `lineless`, `maxText`, `minText` (px), `pointing` (the finished
agent pointed at), `flew` (the last agent clicked there), `focusInView` (the focused column's foot is on screen) and `cost`
(`ticks`, `draws`, `msPerDraw`, `drives`: this page's frames drawn by the floating window while this tab was hidden, `polls`,
`timer`: whether any timer of it is set, `iconMsPer`). `buttons` (the button row at the top right as drawn last frame: `row` and `order` (names left to right: `usage,minimap,rail-toggle,connectors,pick-folder`), `boxes` (each one's hit box), `gaps`, `inRow` (one top, one size, even gaps), `overlap` (any two boxes touch)). `connectors` (`known`: ids from
/api/connectors, `islands`: ids drawn, `calls`, `couriers` out now, `beams` (the focused column's threads to islands), `sheet`: the open island's id,
`sheetRows`, `columns`: column → its MCP calls, `times` of the focused column's calls, `blocks`: the focused column's MCP blocks by name, `ink`: the colours drawn this frame, `islands` id → colour, `alpha` id → alpha (0.7 at rest, 1 hovered or active), `glyphs` and `couriers` the colours used, `wrong` the islands and glyphs not in their brand colour (should be empty), `black` any pure black). `threads` (tool threads, one per agent and island): `drawn`, `list` ("<column> -> <island> (<why>)", why: focus, hover, island, out, recent), `bad` (a thread from a chat in the archive stack not itself focused or hovered, from a grey stub, or with no call out and none in the last ten minutes, unless its agent is focused or hovered or its island is hovered or open; should be empty), `fromArchive`, `fromStubs`, `hoveredIsland`. `kids` (far children): `printed` (every "N children" printed last frame: `column`, `n`, `where` readout or platform, `children` counted again from the sessions), `wrongCount` (should be empty), `far` (parent, child pairs of running children on another lane), `farLines` (lines drawn to them, with `colour` and `parentColour`), `wrongColour` and `farUnlined` (should be empty), `farParents`, `strong` (children whose line is at full strength).

`mock`: the server's mock scenario (`junior`, `attention`) or null. `attention`: `head` (the agent tree's head: `n` agents on the list, `asking`, `rest`, `colour` orange while any asks, `drawn`, `needShown`, `needCut`), `steps` (the steps taken from the head or its key: `how`, `to`, `cond`, `at`, `of`, `went`), `keys`, `listed` (`<column>:<cond>` in the engine's order),
`entries` (the engine's list: `id`, `cond`, `level`, `news`, `glyph`, `entry`, `rate`, `why`), `items` (the store, the engine's input:
`column`, `kind`, `waiting`, `read`, `key`), `edgeOff`, `edgeBehind`, `edges` (per column:
`column`, `side`, `behind`, `offscreen`, `ids`, `first`, `glyph`, `cond`, `more`), `pulses` (`<child>><parent>`), `overlaps` and
`overlapList` (edge marks against labels, column tops, windows and other marks), `said` (the hover word drawn), `tip`, `hoverId`,
`logosOff` (columns whose logo is hidden: the agents on the list), `logosDrawn` (logos drawn in the last frame), `clicked`, `title`.

There is no bottom text bar (2026-11-13). A key typed with the cursor nowhere goes into the open transcript's reply box; with no
agent focused, an empty transcript opens for the folder clicked last or the project's root, the key in its box. A plan opens a
project by the page's URL (`?cwd=`), never by typing a path.
