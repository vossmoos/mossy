# Mossy

> Ship agents by writing skills, not framework code.

Mossy is a ready-to-run agent with a tiny core and a powerful skill engine inside. You don't learn a Mossy API — you write **skills** in the open agentic `SKILL.md` format and the agent picks them up. Install it, run it, and extend it by dropping a Markdown file.

- **Skill-first.** Every new behavior is a skill folder — a `SKILL.md` in the open agentic skills format, plus any scripts or assets the skill needs. No bespoke API to memorize.
- **Tiny core.** A few hundred lines of Python on top of [`pydantic-ai`](https://github.com/pydantic/pydantic-ai). You can ignore it and just write skills.
- **Works out of the box.** Worker, queue, CLI chat, and HTTP API are already wired up. Run `python main.py` and you have an agent.
- **Extensible channels.** CLI, HTTP, and Slack (Socket Mode) ship in the box. Add Telegram or any other connector as a module under `mossy/channels/` — anything that produces an `Envelope` plugs into the same inbox.
- **Team-ready.** Agents enqueue work for each other, set priorities, and chain tasks across any channel.

---

## Core concepts

A handful of small pieces, each doing one thing.

- **Runtime** (`mossy/runtime/core.py`) — the heart. Owns the inbox, the queue, the worker agent, and the task lifecycle.
- **Task & Envelope** (`mossy/runtime/models.py`) — typed units of work, with `Priority` (`INTERRUPT → IDLE`), `depends_on`, and a structured `result`.
- **Skills** (`mossy/skills/<name>/`) — a `SKILL.md` with YAML frontmatter, plus any helper scripts (e.g. `scripts/*.py`) or assets the skill calls. The worker discovers the folder, picks the relevant skill, loads its instructions, and runs the bundled scripts when told to. Add one by creating a folder.
- **Capabilities** (`mossy/capabilities/`) — toolsets exposed to agents through skills: `system-queue` (enqueue, cancel, inspect tasks), `worker-state` (record results, follow-ups), `mossy-personality` (always-on identity and tone instructions loaded from root `MOSSY.md`), and the dynamic `skills` capability.
- **Channels** (`mossy/channels/`) — input/output surfaces:
  - `cli/chat.py` — interactive terminal agent with conversation history.
  - `http/app.py` — FastAPI endpoints (`/run`, `/status/{id}`, `/queue`, `/health`).
  - `slack/app.py` — Slack Socket Mode bot that replies to `@`-mentions in channels and DMs, with per-thread in-memory history. See `mossy/channels/slack/README.md` for setup.
- **Autonomous follow-ups** — when a task finishes, `think_next` can chain a follow-up goal or run an idle housekeeping task. Disable with `PLATFORMER_DISABLE_AUTONOMOUS=1`.

That's the whole platform. Everything else is a skill.

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

- the **runtime** (inbox + worker loop),
- the **HTTP API** on `http://127.0.0.1:8765`,
- the **CLI chat** on stdin.

Useful flags:

```bash
python main.py --no-http        # just the CLI + runtime
python main.py --no-cli         # headless: HTTP only
python main.py --no-slack       # disable the Slack channel
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
I can load skills from mossy/skills, such as echo, planner, system-queue,
filesystem, and skill-manager. Ask me a question or tell me what task to run.

> /quit
bye.
```

The CLI chat is the fastest way to try Mossy. For background work, the **worker** picks tasks off the queue and resolves them with skills. The same tasks are visible at `GET /queue` and `GET /status/{task_id}`.

---

## Add your own skill

A skill is a folder under `mossy/skills/` containing a `SKILL.md` and, optionally, any helper scripts or assets it needs:

```text
mossy/skills/weather/
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

See `mossy/skills/filesystem/` for a working example that bundles `SKILL.md` with a `scripts/` folder.

Restart (or rely on auto-reload) and the worker will discover the skill on the next task. That's the whole extension model.

## Managing skills from CLI chat

Mossy has a **`skill-manager`** skill, so you can manage skills by talking to it. Ask naturally to install, remove, list, or explain skills.

> List what's installed and what's available to install.  
> What does the `weather` skill do?  
> Install the `calendar` skill.  
> Let's remove the `old-notes` skill.
