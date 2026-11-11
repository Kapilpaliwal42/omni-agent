# omni-agent

A personal agent on a **local Ollama model** (default `huihui_ai/qwen3.5-abliterated:9b`) that can:

- **learn skills**: it saves what worked as `skills/<name>/SKILL.md`, optionally with Python tool code
- **add MCP servers** to gain new tools (filesystem, GitHub, databases, anything with an MCP server)
- **search and read the web**: DuckDuckGo, or your own SearXNG
- **use a real browser**: your installed Edge, driven by numbered page elements
- **use Windows apps**: windows and controls through UI Automation, plus screenshots
- **change its own code** when you ask: the change is tested in a copy and you approve the diff
- **remember** facts about you and your setup between sessions
- **do tasks the way you say**: it lists your requirements and checks each one against real results,
  remembers the conversation for follow-ups, asks when something is missing, and learns from your feedback
- **run tasks on a schedule**: "every weekday at 9:00, ..."

It's forked from `worker-agent` (plan → step → harness check → report) and keeps its guards for a small model.

## Install

```powershell
cd D:\LocalAI\omni-agent
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
ollama pull huihui_ai/qwen3.5-abliterated:9b
```

The core loop, skills, MCP and web tools need only the standard library. Playwright, pywinauto,
pyautogui and Pillow are needed only for the browser and desktop toolsets. The browser uses your
installed Microsoft Edge, so `playwright install` isn't needed. MCP servers published on npm need Node.js
(`npx`).

## The app

```powershell
.venv\Scripts\python app.py --workspace D:\work
```

This opens omni-agent in its own window, an Edge app window with no tabs or address bar. The window shows:

- a live transcript: the plan as a checklist, a card for each tool call (click one to see its input and
  output), check results, and a final result card with the harness's verdict
- **permission cards** with Allow and Deny buttons. File contents and commands appear as readable
  blocks, and a desktop notification pops up if the window is in the background.
- **Stop** (the send button while it runs, or Esc), which also denies any pending permission
- slash commands with autocomplete (type `/`)
- a sidebar to switch workspace, model (your Ollama models), permission mode and planning, and to see
  learned skills, MCP servers and memory. Click a skill to use it.
- self-update diffs, colored, with the child agent's work indented. The window reconnects by itself
  after the agent restarts on new code.

It takes the same flags as `agent.py`, plus `--port` (default 8765) and `--no-open` (only start the
server and print the link). The server listens on 127.0.0.1 only. Each launch has a random access
token in the link, and requests with the wrong token or a non-localhost Host header are refused, so
other websites in your browser can't drive the agent. Keep the console window open while you use the app.

## Shared browser

The **Browser** button at the top of the app opens a panel on the right that shows the agent's browser
live. It opens by itself the first time the agent uses the browser. The panel has:

- an address bar, back and reload buttons, and the current page, which updates after every agent
  action and every few seconds while you watch
- the page, ready for **you to use**. Click in it, type while it's focused (`Ctrl+L` jumps to the
  address bar, paste works), and scroll with the wheel. Use it to sign in yourself when a site needs it
  (the agent never types passwords), then tell the agent to carry on. What you type goes straight to
  the page and is never written to the agent's logs or event stream.
- a switch between two browsers:
  - **Agent's**: its own Edge profile in `.omni/browser-profile`. Sign-ins you make there are kept. In
    the app it runs hidden, because the panel is the window (`--browser-window` shows it as a normal
    window as well).
  - **My Edge**: the agent attaches to an Edge window you use yourself, and works in your tabs.
    **Start shared Edge** opens that window. It has its own profile in `.omni/shared-edge`, because
    recent Edge and Chrome versions refuse remote control of your default profile, so sign in to your
    sites there once. You can also start Edge yourself with `--remote-debugging-port=9222` and pick My
    Edge, or start the app with `--browser-mode attach`. When the agent closes or switches away, it only
    disconnects; your Edge stays open. In this mode, page-changing actions ask first in `ask`
    mode, and Buy, Pay, Send and Delete clicks ask in `auto` mode too.

The agent also gets `browser_tabs` and `browser_switch_tab`, so it can work across the tabs you have
open, and `browser_set_value` for read-only fields such as date pickers that refuse typing.

## Doing tasks the way you said

Every message goes through these steps:

1. **Understand.** One model call decides whether the message is a question, which gets answered
   directly with no plan, or a task. For a task it writes the request as a complete instruction (so
   "now do the same for B" becomes a full task) and lists **every requirement you stated**, in your own
   words. The app shows these as a "What you asked for" checklist.
2. **Ask if something is missing.** If the task can't be done without information only you have, it
   asks first. During the work it can also ask with `ask_user`. In the app, that's a question card with
   a reply box.
3. **Plan and do the steps.** Your requirements and learned preferences go into every step's prompt.
4. **Check the requirements against evidence.** A separate review call compares each requirement with
   the real tool outputs and file contents, not with what the model claims. Anything unmet gets one
   focused fix round and a second check. The result shows "3/3 requirements met", with the evidence
   for each.
5. **Learn from your feedback.** Each result card has a feedback box. "Save for next time" (or
   `/feedback ...`) turns your correction into a general rule ("When making to-do lists, add a due time
   to each task") stored in `.omni/preferences.md` and used in every later plan. "Redo with this" (or
   `/redo ...`) also re-runs the task with your feedback.

The conversation is remembered, so follow-ups like "now the same for folder B" or "make it shorter"
work. `/reset` or New conversation clears it.

## Fewer permission prompts

Permission cards have a third button, **Always allow**: "Always allow file writes", "Always allow
commands", and so on. It covers that kind of action for the rest of the session, until you close the
app or press "Clear: ask again" in the sidebar (or run `/allowed clear`). In the terminal, answer `a`.
Some actions can never be set to always allow: self-updates, installing skills or MCP servers,
creating schedules, and clicks on Buy, Pay, Send, Delete and similar buttons. For a risky command like
`git push`, the allowance covers only `git push`, not all commands.

## Scheduled tasks

Say it in plain words ("every weekday at 09:00, check the BBC tech headlines and save the top 5 to
news.md") and the agent creates a schedule after asking you. Supported times: `every 30m`, `hourly`,
`daily 09:00`, `weekdays 18:30`, `mon,fri 07:00`, `once 2026-11-20 14:00`, `in 20m`. Schedules live in
`.omni/schedules.json`, and the app's sidebar lists them with run now, pause and delete buttons.
They run **while the app is open**, one at a time, when the agent is free. Each run uses the
workspace it was created in and the current permission mode. If the window is in the background, a
notification tells you a scheduled task started or needs permission.

## Terminal

```powershell
.venv\Scripts\python agent.py --workspace D:\work
```

```powershell
.venv\Scripts\python agent.py --workspace D:\work "Find the 3 newest releases of Ollama on GitHub and write them to ollama.md"
```

The first form is interactive and the second is one-shot. Interactive commands:

| Command | What it does |
|---|---|
| `/improve <what>` | Change the agent's own code (see Self-update) |
| `/learn [focus]` | Turn the last task into a reusable skill |
| `/feedback <text>` | Teach it how you want this kind of task done from now on |
| `/redo [text]` | Redo the last task, optionally with feedback (also saved for next time) |
| `/schedules` | List scheduled tasks |
| `/allowed [clear]` | Show or clear the "always allow" answers for this session |
| `/skills`, `/mcp`, `/memory` | Show learned skills, MCP servers, remembered facts |
| `/history` | Self-update and skill commits |
| `/rollback` | Revert the last self-update or skill commit, then restart |
| `/reset`, `/exit` | Clear the conversation, quit |

Useful flags: `--mode ask|auto|yolo`, `--model`, `--vision-model qwen2.5vl:3b` (describes screenshots),
`--searxng http://localhost:8888`, `--headless`, `--trust-desktop`, `--no-web`, `--no-self-update`,
`--verify "cmd"`.

## Toolsets: why the model doesn't see every tool

A 9B model picks tools worse as the list grows, so tools are grouped. Only `core` (shell, files),
`web`, `memory`, `skills` and `self` are on by default. The others switch on when the task mentions them
(a URL or "website" turns on `browser`, "window" or "notepad" turns on `desktop`, a skill's own keywords
turn it on), or when the model calls `use_toolset`. Each MCP server is its own toolset (`mcp:<name>`).

## Confirmations

| Level | ask | auto (default) | yolo | Used for |
|---|---|---|---|---|
| never | – | – | – | reading files, pages, windows |
| edit | asks | – | – | file writes, shell commands |
| act | asks | asks | – | desktop clicks/typing (unless `--trust-desktop`), launching programs |
| always | asks | asks | asks | self-update, installing a skill or MCP server |

Also: risky shell commands (push, commit, rm -r, installs, registry edits...) ask in `auto`. Clicks on
browser or desktop elements named like Buy, Pay, Send, Delete, Submit or Confirm ask in `auto`, even with
`--trust-desktop`. Typing into password fields is refused: you sign in yourself in the browser window,
and the profile (`.omni/browser-profile`) keeps you signed in. A hard-block list (format, diskpart,
shutdown...) blocks commands in every mode.

## Skills

```
skills/csv-report/
  SKILL.md        header (name, description, keywords) + step-by-step instructions
  tool.py         optional: TOOLS = [{"name", "description", "function", "parameters", "required"}]
  test_tool.py    required when tool.py exists
```

The agent creates skills with `create_skill`, by itself or after `/learn`. Each one is written to
`.omni/staging/`, compiled, imported and unit-tested in a separate process, and shown to you in full
(as a diff when it updates an existing skill). Only after you approve is it moved into `skills/` and
committed to git, so `/rollback` can undo it. You can also write skills by hand.

## MCP servers

Ask the agent ("add the MCP filesystem server for D:\notes"), or edit `.omni/mcp.json`:

```json
{"mcpServers": {
  "notes": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/notes"],
            "description": "read and search my notes"},
  "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
             "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}, "confirm": "edit"}
}}
```

`${VAR}` fills in from your environment, so keys stay out of the file. `confirm` sets the level for that
server's tools (default `edit`). Only stdio servers are supported. This client uses newline-delimited
JSON-RPC as the MCP spec requires. The old `D:\LocalAI\agent\mcp_client.py` used `Content-Length`
framing, which real MCP servers don't speak.

## Self-update

`/improve add a tool that reads PDF text` (or asking the agent to change itself) runs this:

1. A git worktree of this repo is created on branch `self/<timestamp>` under `.omni/selfupdate/`.
2. A child agent makes the change there, and the running copy isn't touched. It gets:
   - the code tools (`search_code`, `outline`, `read_symbol`, `replace_symbol`, `add_code`)
   - a context pack: the functions most related to the request (nested tool functions come with the line
     that registers them), an outline of every file, and recipes for the usual changes (a tool, a slash
     command, a setting, a test)
   - a work order: find the code, write a test, make the change, run the tests
3. **Quality gates.** Each failure goes back to the child for a focused fix round, with the functions
   from any traceback included (`--self-fix-rounds`, default 3):
   - every changed Python file compiles, and every agent module imports
   - the test suite passes
   - a code change comes with a test in `tests/` that **fails on the old code**. That proves the test
     checks the change, not just that something exists.
   - no placeholders or stubs (TODO, "implement later", NotImplementedError, `...`) in the added code
   - a model review of the diff against the request: is all of it done, and is nothing faked?
4. If an attempt still fails, a **fresh attempt** starts from a clean worktree, with notes on what went
   wrong (`--self-attempts`, default 2). If every attempt fails, nothing is applied, and you get the
   reasons.
5. You see the full diff and the gate results (also saved as `.omni/selfupdate/<id>.diff`) and approve
   or decline it.
6. If you approve, it's committed as `self-update: ...`, merged, and the agent restarts on the new code.

The repo must have no uncommitted changes to tracked files first, so every self-update stays one
revertible commit.

The agent can't edit its own source with ordinary file tools. In any workspace, writes into
`omni/`, `agent.py`, `rules.md`, `tests/` or `skills/` of the running agent are refused and redirected
to `self_improve` or `create_skill`. Its `.omni/` state folder is still writable.

**A better coding model helps most.** `--coder-model qwen3.5:9b` (or `OMNI_CODER_MODEL`) uses a
different model only for self-update coding, and `--coder-think on` turns on its thinking (slower,
often better code). The default is the main model.

### Benchmark

`bench/selfupdate_bench.py` runs four realistic self-update requests: add a tool, add a slash command,
make `web_fetch` refuse local addresses, and fix an injected bug. Each one runs in a throwaway clone,
and a hidden test the agent never sees decides whether it really worked.

```powershell
.venv\Scripts\python bench\selfupdate_bench.py --label mine --working-tree
```

`--dry` checks that every hidden test fails before any change. `--only word_count` runs one task, and
`--extra '{"cfg": {"coder_model": "qwen3.5:9b"}}'` tries another model.

## Tests

```powershell
.venv\Scripts\python -m unittest discover -s tests -t . -v
```

87 offline tests use a scripted fake model. They cover:

- the agent loop, the confirmation levels and toolset routing
- memory, and skill install, approval and rejection
- a real MCP round trip against a fake stdio server
- desktop key translation, browser guards, the browser thread and the shared-browser panel endpoints
- the code tools and the safer edits
- the own-source guard
- every self-update gate, the second attempt, and rollback, in a temp repo

They need no Ollama, network, browser or desktop.

## Limits to expect

- **Context window.** Everything the model sees in one step must fit in `--num-ctx` (default 16384
  tokens). A web page snapshot is about 2,500 tokens, so before every model call the agent shrinks older
  tool results to a short note and keeps the newest two whole. If the window still fills, it trims harder
  and retries. A stop message says which limit was hit: the context window (start a new conversation
  or split the task) or the 4,096-token reply limit (ask for less, or write long output to a file in
  parts).
- A 9B model on 6 GB VRAM handles short, specific browser and desktop tasks ("open X, find Y, copy it
  into Z"). Long open-ended ones drift. Give it one goal per task and name the site or app.
- Desktop control works best on standard Windows apps (Notepad, Explorer, Settings, Office). Apps that
  expose no UI Automation tree (games, some Electron or Java apps) need `--vision-model`.
- Self-update quality is limited by the model. The tests and your diff review are the safety net.

## Layout

```
agent.py            CLI and slash commands
rules.md            your system prompt
omni/core.py        agent loop, planner, checks, confirmations
omni/registry.py    tools and toolsets
omni/builtin.py     shell, files, web; edit safety and the own-source guard
omni/code.py        code navigation and symbol edits; symbol-aware prompt context
omni/memory.py      facts, and preferences learned from feedback
omni/schedule.py    scheduled and recurring tasks
omni/skills.py      learned skills
omni/mcp.py         MCP client and server management
omni/browser.py     Playwright browser
omni/desktop.py     Windows UI Automation
omni/selfupdate.py  self-update, rollback
omni/app.py         app server (events, permissions, stop, settings)
omni/web/index.html the app UI
omni/commands.py    slash commands (shared by CLI and app)
app.py              app launcher
tests/              offline tests
.omni/              runtime state (git-ignored): logs, memory, mcp.json, browser profile, staging
```
