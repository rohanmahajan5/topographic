# Fixture: a junior engineer's afternoon (AGENT_IDE_MOCK=junior)

Everything here is fake: the repository, the ticket, the Slack thread, the Drive document, the pull request, the
people (named only by role: support-lead, pm-receipts, backend-oncall, "you"), the session ids and the connector
UUIDs except Google Drive's. It exists so the workspace's connector islands can be seen and tested without any
real account. Nothing is sent anywhere; no connector is called.

## Start it

```sh
cd agent-ide/workspace
AGENT_IDE_MOCK=junior python3 server.py --port 4348
```

At every start `mock.py` rebuilds `.run/` here (ignored by Git) and the server opens `.run/receipts-api`. All recorded
times are moved so the afternoon ends a minute before the start; its forty minutes keep their spacing, the clock times
do not stay in the afternoon. Ten minutes after the start the columns have folded to stubs like any idle session;
rewind the rail to replay, or restart the server.

## What is here

- `repo/` the project as it stands at the end: `receipts-api`, a small standard-library Python service (src/, tests/,
  docs/, README.md, CONTRIBUTING.md, a PR template; 16 files). Its tests pass (`python -m pytest -q`).
- `before/` the three files the fix changed, as they were on `main`.
- `sessions/git.json` the history: two commits on `main` (the service, then its HTTP endpoint), one on the branch
  `fix/rcp-142-rounding` (the fix, src/ and tests/ only); `docs/notes/` stays untracked. Author "Fixture Junior".
- `sessions/claude/projects/project/` one Claude Code transcript (`<uuid>.jsonl`) and one subagent under
  `<uuid>/subagents/` (transcript and meta.json), in the shapes Claude Code writes (one record per content block,
  usage on each assistant record, tool results as user records). Laid out under `.run/claude/projects/<encoded cwd>/`.
- `sessions/codex/` one Codex rollout and its row for the threads table (`state_5.sqlite` is built from it), in the
  shapes the Codex CLI writes (session_meta, response_item function_call with `namespace: mcp__<server>`,
  event_msg item_completed McpToolCall / CommandExecution with started_at_ms and completed_at_ms, token_count).
- `sessions/project.json` the project folder's name.

`{{PROJECT}}` in the session files stands for the project's absolute path; `mock.py` fills it in.

## The story (minutes from the start)

1. 0:00 asked to pick up RCP-142 (receipt totals a cent off for converted orders); 0:08 reads the ticket from Linear.
2. 0:40 searches Slack for it, 1:10 reads the thread; 1:50 searches Google Drive for the spec, 2:30 reads it.
3. 3:20-4:20 reads totals.py, currency.py and the tests; 5:00-8:40 a subagent lists every place money is rounded.
4. 9:20 writes the failing test, 10:00 tests fail; 11:20 and 12:40 edits totals.py and currency.py; 13:20 tests pass;
   15:00 writes docs/notes/rcp-142-rounding.md; 16:00 replies in the Slack thread.
4b. 18:00-21:52 a second Codex session makes the same fix on its own ("Second opinion on the RCP-142 fix"): four
   FileChange items whose diffs are made from `before/` and `repo/` (totals.py's import left to a `sed -i` command, which
   records nothing of what it changed) and the note added whole; it is what the change view shows for Codex.
5. 25:00-29:52 a Codex session commits the fix on fix/rcp-142-rounding, reads the PR template from GitHub and opens
   PR #57; 36:00-40:00 back in Claude Code: re-reads the spec on Drive for its acceptance criteria and comments on the
   Linear ticket.

## Connector ids and tool names: real shapes and stand-ins

- Claude Code names a claude.ai connector's tool `mcp__<server id>__<tool>` with the server id a UUID (real shape).
- Google Drive: server id `dddd0000-0000-4000-8000-000000000001` is a **stand-in** (a real connector's id differs per
  account); the tools `search_files`, `read_file_content` are the real connector's tool names (seen 2026-11-06). Its other tools there: copy_file, create_file,
  download_file_content, get_file_metadata, get_file_permissions, list_recent_files, share_file, trash_file, update_file.
- Slack (`3f1c2a9e-5b7d-4e2a-9c61-0d8e4b7a2f15`: search_messages, read_thread, send_message) and Linear
  (`a7e4c0d2-81f3-4b6e-9d25-6c0b1e9f3a48`: get_issue, add_comment): **stand-ins**, both the UUIDs and the tool names,
  until a real session with those connectors is seen.
- GitHub in Codex: namespace `mcp__github` (a configured server's name), tools get_file and create_pull_request:
  the record shape is real (as Codex writes any MCP call); the server name and tool names are **stand-ins**.
- The tool results are short invented text; file reads in the transcript carry "(file contents)" instead of the file.

Display names and marks come from `../../connectors.json`.

## The attention overlay (AGENT_IDE_MOCK=attention)

```sh
AGENT_IDE_MOCK=attention python3 server.py --port 4387
```

The same afternoon (built into `.run-attention/`, so both scenarios can run at once) plus `attention/`: seven more
sessions that between them hold one of each thing the tray and the icons above the columns show. Start it fresh before
each run of `test/plans/attention.json`: answering in the list uses up the three open requests.

- "Run the API tests in a clean venv" (Codex, running): asks to run `pip install -e '.[test]'` (an approval).
- "Write the changelog entry for RCP-142" (Codex, running): asks which release the entry goes under (a question with two
  options and "other").
- "Watch the nightly import" (Codex, running): nothing since 14:27:42, so it has been quiet for more than ten minutes.
- "Review the receipts module" (Codex, running): turn 1 failed (the model is not supported), turn 2 started two
  subagents, "Check totals.py rounding" and "Check currency.py conversions" (their own rollouts, `parent_thread_id` set),
  which handed back, then failed on the usage limit; turn 3 asks to edit a file (a file-change approval). Its failures were
  each followed by a turn, so it shows no error mark (2026-11-09), only the approval.
- "Add a currency column to the export" (Codex, running, `"tick": true` in its threads.json row): working, asking nothing. While the server runs, `mock.py` appends a read, a file change or a command to its rollout every 4 to 9 s and a message every 20 to 40 s (times now), so a running agent can be seen working; the overlay's other running threads wait on you or are silent and are never ticked. Only a server run ticks (a test's own copy, `run=` given, does not).
- "Summarise the support thread" (Claude Code): stopped on an API error (`isApiErrorMessage`). Its three subagents (2026-11-09,
  for the error mark): "Collect the support replies" retried an API call itself twice (system `api_error` records), later
  stopped on an API error, was resumed by a message and finished with an answer (no mark: work came after the failure);
  "Check the ticket history" and "Draft the reply to support" stopped on an API error and were left there (a mark each, drawn
  on the Summarise column while they are folded into it). Written by hand in the shapes of the records above, synthetic text.
- "Plan the RCP-142 release" (Claude Code, from `../../providers/fixtures/claude-code-asking/`): stopped on AskUserQuestion
  ("Ship the rounding fix as 1.4.2 now, or hold it for 1.5.0…", two options), with a background subagent, "Check the
  release notes", finished but not yet reported back. Its `later/` records are what `GET /api/mock?do=` appends, once each,
  timed now: `answer` (the person answers in Claude Code: the tool result, then the agent's answer), `handback` (the task
  notification: the subagent's report reaches its parent), `followup` (a second turn and its answer), `restart` (a third turn's
  message only) and `restart-done` (its answer; both 2026-11-09). Besides those, `held`, `busy`, `idle` and `released` (any number
  of times) write or remove a Claude Code registration for this session as the Claude app would (entrypoint claude-desktop, the
  server's own pid, status idle or busy): the app holding it, working a turn, the turn over, letting it go. Written by a script
  kept outside the repository, in the shapes read off this Mac's transcripts (AskUserQuestion's `questions`, its tool result
  with `toolUseResult.answers`, a background Agent's `async_launched` result and its `task-notification`), 2026-11-07.
- The junior afternoon's own sessions add the rest: "Open the PR for RCP-142" (Codex) and "RCP-142: receipt totals…"
  (Claude Code) finished with answers nobody has read, and the latter's subagent handed its result back.

Real shapes: the failed turns (`task_complete` with `last_agent_message` null and `error {message, codex_error_info}`),
`SubAgentActivity` started/completed and the Claude Code API error record were read off real rollouts and transcripts on
this Mac (2026-11-04 to 11-07). **Stand-ins**: `attention/live.json`, what the app-server would hold if this server drove
these threads (their running turns and the three requests), in the shapes of test_server.py's stand-ins, not a recorded
stream. Answers to those requests are resolved by the mock itself and go nowhere. The overlay is written by a script
kept outside the repository; the files here are what it wrote.
