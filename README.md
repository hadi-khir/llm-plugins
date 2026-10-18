# hadi-plugins

Claude Code plugins by [Hadi Khir](https://github.com/hadi-khir).

## Install

Add the marketplace (once), then install a plugin:

```
/plugin marketplace add hadi-khir/llm-plugins
/plugin install trad-coding@hadi-plugins
/plugin install jev-planning@hadi-plugins
/plugin install session-memory@hadi-plugins
```

## Plugins

### trad-coding

If you're tired of atrophy and feel yourself becoming duller by the day, this is just the skill. Have LLM's go back to just providing you with the code and you can either type it out by hand and learn by doing, or go back to the stack overflow days and copy paste. Take a break from autopilot. 

- Triggers automatically on any code-change request while the plugin is enabled
- Reading, searching, and read-only diagnostics are still allowed, so instructions cite real files and line numbers
- If a task can't be done without Claude editing files, it asks you to confirm before breaking the rule

#### Example

**You:** add a POST /echo endpoint to my express app

**Claude** (instead of editing anything):

> Add a POST /echo route after the existing GET route.
>
> `src/app.js` — after line 8 (the closing `});` of the `app.get('/', ...)` route):
>
> ```js
> app.post('/echo', (req, res) => {
>   res.json(req.body);
> });
> ```
>
> `express.json()` is already mounted on line 4, so `req.body` will be the parsed JSON.
>
> Verify:
>
> ```bash
> curl -X POST localhost:3000/echo -H "Content-Type: application/json" -d '{"hello":"world"}'
> ```

Disable anytime with `/plugin` → uninstall, or say "disable trad-coding for this task" in a session.

### jev-planning

A hook that scores every plan-mode plan with [TypeSafe's Jev](https://docs.typesafe.ai)
before Claude may leave plan mode. Jev does not write or plan anything: it takes the
plan plus your original request and returns calibrated probabilities for narrow
questions (which candidate is smallest and safest, is each requirement covered by a
step, is each step needed, does each step name its check). Code applies the
thresholds. A weak plan is bounced back to Claude with the failing items quoted, up to
three times per session, then it goes through to you with the open findings attached.

Design and question wording: `docs/superpowers/specs/2026-09-22-jev-plan-gate-design.md`.

#### Setup

Python 3.9+ on `PATH`; no packages to install.

1. Get an API key from the [TypeSafe console](https://console.typesafe.ai). Every user
   needs their own; the plugin ships none.
2. Install the hook **one** of two ways, never both (two copies would score every plan twice
   and burn the bounce cap at double speed):

   **Marketplace install** (the plugin ships its own `hooks/hooks.json`):

   ```
   /plugin marketplace add hadi-khir/llm-plugins
   /plugin install jev-planning@hadi-plugins
   ```

   Claude Code prompts for the API key when it enables the plugin and keeps it in its
   secure storage, never in `settings.json`; the hook receives it in its environment. No
   env var to set. Rubric changes arrive with `/plugin update jev-planning`. To edit the
   rubric in place instead, add the marketplace from a local clone
   (`/plugin marketplace add /path/to/llm-plugins`): a local-directory marketplace loads
   the plugin from the checkout, so edits apply on the next plan-mode exit.

   **Wire by path** (no plugin machinery; edits apply instantly): set the key as a user
   environment variable named `TYPESAFE_API_KEY` and restart Claude Code so the hook
   process inherits it (Windows: `setx TYPESAFE_API_KEY <key>`; macOS/Linux: export it
   from your shell profile). Then clone this repo and add the hook to
   `~/.claude/settings.json`, merging into an existing `hooks` block if you have one:

   ```json
   "hooks": {
     "PreToolUse": [{
       "matcher": "ExitPlanMode",
       "hooks": [{
         "type": "command",
         "timeout": 90,
         "command": "python /absolute/path/to/llm-plugins/plugins/jev-planning/hooks/plan_gate.py"
       }]
     }]
   }
   ```

   Forward slashes work on Windows too.
3. Tell Claude the plan shape, so the first plan is scoreable. Put this in `~/.claude/CLAUDE.md`
   (global) or a project's `CLAUDE.md`:

   ```markdown
   # Plan mode

   Every plan written in plan mode has two sections, because a Jev hook scores the plan on exit and bounces it (up to 3 times) when they are missing or weak:

   - `## Candidates` — 2 to 3 approaches, each `### <name>` with what changes, files touched, new-code size, and risk. Then one line `Chosen: <name>`. To keep a candidate Jev ranks lower, add `Why not <other>: <reason>`.
   - `## Plan` — numbered steps, each naming its check (a test, a command, or an observable result).
   ```

   A plan without the two sections is bounced once with the same instructions, so this step
   only saves the first bounce.

#### Check it works

Without a key the hook fails open: every plan passes and stderr shows
`Jev plan gate: skipped (no API key: ...)`. With `TYPESAFE_API_KEY` in your shell, score
any plan file from the command line without going through Claude:

```bash
python plugins/jev-planning/hooks/plan_gate.py --dry-run my_plan.md "the request as you typed it"
```

It prints the parsed state, one line per answer, then `PASS` or `BLOCK` with the findings.
Run the unit tests (Jev is replaced by canned answers) with:

```bash
python -m unittest plugins/jev-planning/hooks/test_plan_gate.py
```

#### Tune it

- Thresholds, the bounce cap, and every question's wording live in
  `plugins/jev-planning/hooks/rubric.json`. Nothing is in code.
- A repo can add its own yes/no checks in `<repo>/.claude/jev-rubric.json`, a list of
  `{"id", "instructions", "action": "block" | "warn", "above": 0.6, "message"}`. They join
  the same request. Example: block any step that puts a test file outside `tests/`.
- Jev reads literally and cannot count or do arithmetic, so keep questions to one plain
  condition each and leave totals and thresholds to the code. The
  [jaggedness page](https://docs.typesafe.ai/model-jaggedness/jev-1.13) lists the failure
  modes to write around.
- Cost: one request per plan-mode exit, roughly 30 to 60 questions, a fraction of a cent.

#### What it never does

- Block on an outage: a missing key, a timeout, an HTTP error, or a bug in the gate lets
  the exit through with one stderr line.
- Change your approval prompt: on a pass the hook prints nothing, so Claude Code's own
  plan-approval flow is untouched.
- Loop forever: after three bounces in a session the plan goes to you with the open
  findings, and the counter resets.

### session-memory

A searchable memory of your past Claude Code sessions. When a session ends (or compacts),
a background worker reads its transcript, keeps your prompts, Claude's replies and one line
per tool call, redacts secrets, and has Haiku fold it into a short summary: goal, outcome,
decisions, artifacts, open threads, topics. Summaries land in a local SQLite full-text store
at `~/.claude/session-memory/sessions.db`. Each new session is told its project's five most
recent sessions and how to search the rest.

- **Key:** `<date>__<project>__<title-slug>`, e.g.
  `2026-10-13__lambda-argus__local-conversation-memory-store`. The row's identity is the
  session id, so a renamed or resumed session updates its own row.
- **Incremental:** a resumed or compacted session sends only its new part, merged into the
  existing summary.
- **Catch-up:** every session start sweeps transcripts that grew since their last summary
  (crashes, closed terminals) and backfills old ones on first run, newest first.
- **No loops:** the Haiku call is `claude -p` with hooks, tools and MCP off and no
  transcript written.

#### Setup

Python 3.9+ and the `claude` CLI on `PATH`; no packages to install.

```
/plugin install session-memory@hadi-plugins
```

Claude Code deletes transcripts 30 days after their last activity. To keep the raw
transcripts as long as the summaries, set `"cleanupPeriodDays": 365` in
`~/.claude/settings.json`.

#### Use it

```bash
python <plugin>/hooks/sessmem.py search echoguard tee      # ranked, any of the words
python <plugin>/hooks/sessmem.py show 2026-10-13__lambda   # key, key prefix or session id prefix
python <plugin>/hooks/sessmem.py recent --project lambda-argus
python <plugin>/hooks/sessmem.py sweep                     # summarize everything idle now
```

Claude gets the exact path in its session-start context, so asking "what did we decide
about X last week?" is enough. Worker failures go to `~/.claude/session-memory/worker.log`
and are retried by the next sweep. Tests (Haiku replaced by a fake):

```bash
python -m unittest plugins/session-memory/hooks/test_sessmem.py
```

#### What it costs

One Haiku call per session (more for sessions over ~30k tokens of conversation), billed
to whatever account `claude` is logged in to. Sessions with fewer than three prompts and
under 1,000 characters of conversation are stored by title only, with no call.

## License

MIT
