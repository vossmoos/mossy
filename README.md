# Mossy

> Ship agents by writing skills, not framework code.

Mossy is a ready-to-run agent with a tiny core and a powerful skill engine inside. You don't learn a Mossy API — you write **skills** in the open agentic `SKILL.md` format and the agent picks them up. Install it, run it, and extend it by dropping a Markdown file.

- **Skill-first.** Every new behavior is a skill folder — a `SKILL.md` in the open agentic skills format, plus any scripts or assets the skill needs. No bespoke API to memorize.
- **Tiny core.** A few hundred lines of Python on top of [`pydantic-ai`](https://github.com/pydantic/pydantic-ai). You can ignore it and just write skills.
- **Works out of the box.** Worker, queue, CLI chat, and HTTP API are already wired up. Run `python main.py` and you have an agent.
- **Extensible channels.** CLI, HTTP, AG-UI (SSE web chat), web chat (`/ui`), an adaptive UI (`/aui`, chat + skill-driven widget panel), and Slack (Socket Mode) ship in the box. Add Telegram or any other connector as a module under `mossy/channels/` — anything that produces an `Envelope` plugs into the same inbox.
- **Adaptive UI.** Skills can render interactive widget boxes next to the chat — cards, tables, progress, logs, documents — with zero frontend changes per skill. See below.
- **Team-ready.** Agents enqueue work for each other, set priorities, and chain tasks across any channel.

---

## Core concepts

A handful of small pieces, each doing one thing.

- **Runtime** (`mossy/runtime/core.py`) — the heart. Owns the inbox, the queue, the worker agent, the duty sentinel, and the task lifecycle.
- **Task & Envelope** (`mossy/runtime/models.py`) — typed units of work, with `Priority` (`INTERRUPT → IDLE`), `depends_on`, `dedupe_key`, and a structured `result`.
- **Skills** — packaged system skills live in `mossy/skills/<name>/`; downloadable or user-provided skills live in `skills/<name>/`. Each skill is a `SKILL.md` with YAML frontmatter, plus any helper scripts (e.g. `scripts/*.py`) or assets the skill calls. The worker discovers both roots, picks the relevant skill, loads its instructions, and runs the bundled scripts when told to.
- **Duties** (`mossy/duties/`) — a clock that can run a bit of code, then put work on the same queue. A 1s sentinel calls `check(now)` (cadence only). That enqueues `evaluate_duty`; `evaluate()` does programmatic work (no LLM) and may enqueue a **goal**. The worker resolves goals with skills, same as chat or HTTP. `dedupe_key` makes each window fire once. User duties live in repo-root `duties/`.
- **Capabilities** (`mossy/capabilities/`) — toolsets exposed to agents through skills: `system-queue` (enqueue, cancel, inspect tasks), `worker-state` (record results, follow-ups), `mossy-personality` (always-on identity and tone instructions loaded from root `MOSSY.md`), `skill-manager` (install/remove skills from a repository, CLI only), and the dynamic `skills` capability.
- **Channels** (`mossy/channels/`) — input/output surfaces:
  - `cli/chat.py` — interactive terminal agent with conversation history.
  - `http/app.py` — FastAPI endpoints (`/run`, `/status/{id}`, `/queue`, `/health`).
  - `agui/app.py` — AG-UI protocol over SSE for web chat clients (`POST /agui`). See `mossy/channels/agui/README.md`.
  - `web/app.py` — self-contained browser chat page (`GET /ui`) streaming from the AG-UI endpoint.
  - `aui/app.py` — adaptive UI (`GET /aui`): chat plus a skill-driven widget panel. See `mossy/channels/aui/README.md`.
  - `slack/app.py` — Slack Socket Mode bot that replies to `@`-mentions in channels and DMs, with per-thread in-memory history. See `mossy/channels/slack/README.md` for setup.
- **Autonomous follow-ups** — when a task finishes, `think_next` can chain a follow-up goal or run an idle housekeeping task. Disable with `PLATFORMER_DISABLE_AUTONOMOUS=1`.

That's the whole platform. Everything else is a skill. Duties check the clock, run code, then enqueue work.

---

## Duties

A **skill** is how a task is resolved. A **duty** is how Mossy puts a task on the queue by itself (chat, HTTP, and Slack do the same from the outside).

The sentinel ticks every second. That tick is not an LLM call.

1. **`check(now)`** — cadence only. If it is time, enqueue `kind="evaluate_duty"`.
2. **`evaluate(payload, runtime)`** — Python only (count, filter, fetch). No skill, no model. Return `[]` to stop, or enqueue `kind="goal"` tasks.
3. **Worker** — each goal is a normal queue item. The worker picks a skill and runs it, same as any other task.

`dedupe_key` (for example `ops_digest:2026-08-19`) means that window is queued at most once per process. The queue is in-memory, so a restart can fire again.

Framework: `mossy/duties/`. User duties: a Python module under repo-root `duties/` with `@register`. Restart after adding a file.

### Example: ops digest

Ships **inert**. When enabled, once per day:

- `duties/ops_digest.py` — `check()` at `OPS_DIGEST_HOUR_UTC`; `evaluate()` counts pending / running / failed / done and enqueues a goal that already contains the line.
- `skills/ops-digest/` — the worker prints that line to stderr.

```text
Mossy ops 2026-08-19: 2 pending, 1 running, 0 failed, 14 done
```

```bash
# .env
OPS_DIGEST_ENABLED=true
OPS_DIGEST_HOUR_UTC=13    # 0–23 UTC, default 13
```

Turn the sentinel off with `--no-duties` or `PLATFORMER_DISABLE_DUTIES=1`.

### Write a duty

```python
from mossy.duties.base import Duty, EnqueueRequest, register

@register
class Ping(Duty):
    name = "ping"

    async def check(self, now):
        if now.minute != 0 or now.second >= 8:
            return []
        hour = now.strftime("%Y-%m-%dT%H")
        return [EnqueueRequest(
            kind="evaluate_duty",
            payload={"duty": self.name, "hour": hour},
            dedupe_key=f"ping:{hour}",
        )]

    async def evaluate(self, payload, runtime):
        hour = payload.get("hour", "")
        # hardcoded work here, then enqueue a goal (or return [])
        return [EnqueueRequest(
            kind="goal",
            goal=f"Print a one-line ping to stderr for {hour}.",
            dedupe_key=f"ping:print:{hour}",
        )]
```

The goal text should match a skill description so the worker selects that skill.

---

## Install

Requires Python 3.11+. Mossy uses OpenAI by default, but model names are passed to Pydantic AI, so you can use any supported provider in Pydantic AI's `provider:model` format.

```bash
git clone <your-fork-or-this-repo> mossy
cd mossy

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# then edit .env and set OPENAI_API_KEY=sk-... for the default OpenAI model
```

To use another Pydantic AI-supported provider, set `PLATFORMER_SKILL_MODEL` and `PLATFORMER_CLI_MODEL` in `.env`, for example `anthropic:claude-...` or another provider/model string supported by Pydantic AI.

## Run

From the repo root:

```bash
python main.py
```

This starts everything at once:

- the **runtime** (inbox + worker loop + duty sentinel),
- the **HTTP API** on `http://127.0.0.1:8765`,
- the **CLI chat** on stdin.

Useful flags:

```bash
python main.py --no-http        # just the CLI + runtime
python main.py --no-cli         # headless: HTTP only
python main.py --no-slack       # disable the Slack channel
python main.py --no-agui        # disable the AG-UI web chat endpoint
python main.py --no-aui         # disable the adaptive UI channel
python main.py --no-duties      # disable the duty sentinel loop
python main.py --port 9000      # change HTTP port
```

Slack starts automatically when both `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set in `.env`. Setup steps (creating the Slack app, scopes, tokens) live in `mossy/channels/slack/README.md`.

Submit work over HTTP:

```bash
curl -X POST http://127.0.0.1:8765/run \
  -H 'content-type: application/json' \
  -d '{"payload": "Summarize today's queue and tell me what's pending."}'
```

---

## Adaptive UI: skill-driven widgets (`/aui`)

Open `http://127.0.0.1:8765/aui` for a chat with an **adaptive side panel**. Skills render interactive widget boxes next to the conversation — no frontend work per skill: the skill's `SKILL.md` tells the agent what to show, the agent calls the generic `panel_*` tools, and the panel updates live over the same SSE stream. Buttons send messages back into the chat, so every action also works by typing it — the same skills run unchanged on `/ui`, CLI, and Slack, where the panel simply doesn't exist.

Six widget types cover the vocabulary:

1. **`card_grid`** — entity teasers with a detail popup and action buttons per card.
2. **`table`** — compact tabular data: columns and rows.
3. **`markdown`** — free-form formatted text, summaries, notes.
4. **`progress`** — status line with a progress bar for long-running work, updated in place.
5. **`log`** — a streaming console for live output, appendable line by line.
6. **`document`** — a shared file such as a generated PDF report, with Open/Download buttons.

Skill-authoring contract, JSON schemas, and worked examples: `mossy/channels/aui/README.md`.

---

## Quick example: chat with Mossy from the CLI

Start it:

```bash
python main.py
```

You'll see:

```text
Mossy CLI — chat mode. Use /quit to exit.
```

Now ask a simple question:

```text
> what skills can you use?
I can load system skills from mossy/skills, plus any extended skills from skills/.
System skills include echo, planner, system-queue, and filesystem.

> /quit
bye.
```

The CLI chat is the fastest way to try Mossy. For background work, the **worker** picks tasks off the queue and resolves them with skills. The same tasks are visible at `GET /queue` and `GET /status/{task_id}`.

---

## Add your own skill

A downloadable or user-provided skill is a folder under the repo-root `skills/` directory containing a `SKILL.md` and, optionally, any helper scripts or assets it needs:

```text
skills/weather/
├── SKILL.md
└── scripts/
    └── fetch_forecast.py
```

`SKILL.md` describes when to use the skill and how to use the scripts:

```markdown
---
name: weather
description: Use this skill when the user asks about the weather.
---

# Weather

## When To Use This Skill
Use whenever the user asks about current or forecast weather.

## Instructions
1. Ask for a city if none is given.
2. Run `scripts/fetch_forecast.py` with the city to get the forecast.
3. Return a one-sentence summary based on the script output.
```

See the built-in `mossy/skills/filesystem/` system skill for a working example that bundles `SKILL.md` with a `scripts/` folder.

Restart (or rely on auto-reload) and the worker will discover the skill on the next task. That's the whole extension model.

---

## Install skills from a repository

Besides hand-authoring skills, Mossy can pull them from a Git repository straight from the CLI chat. The **skill-manager** capability adds two verbs to the interactive CLI:

```text
> install skill weather
Skill 'weather' installed at skills/weather. It is wired to the agent and
available from the next message.

> delete skill weather
Skill 'weather' deleted. It is un-wired from the agent and gone from the next message onward.
```

- **`install skill <name>`** clones the configured skills repository, takes the top-level folder named `<name>` (which must contain a `SKILL.md`), and (re)creates it at `skills/<name>`. Install is always a clean replace: an existing `skills/<name>` is removed first, then copied fresh.
- **`delete skill <name>`** removes `skills/<name>`.
- **`list skills available for install`** clones the repository and lists every installable skill (each top-level folder with a `SKILL.md`), with its description and whether it's already installed.
- **`list skills enabled`** lists the skills currently installed under `skills/` (the ones wired to the agent).

```text
> list skills available for install
weather  — Use this skill when the user asks about the weather.  (installed)
jira      — Triage and comment on Jira issues.

> list skills enabled
weather  — Use this skill when the user asks about the weather.
```

Both take effect on the **next** message — the `skills/` directory is re-scanned before every run, so installing wires the skill to the agent and deleting un-wires it. No code changes or restart needed.

### Point it at your own repository

By default Mossy installs from a sample repository. Set your own in `.env` so your team installs from your catalog:

```bash
# .env
MOSSY_SKILLS_REPO=https://github.com/your-org/your-skills   # public or private
MOSSY_SKILLS_REPO_REF=main                                  # optional branch/tag/commit
```

Private repositories authenticate with the same `GITHUB_PERSONAL_ACCESS_TOKEN` used by the GitHub capability. Each top-level folder in the repo is an installable skill (a folder plus its `SKILL.md`), addressed by its folder name.
