# Mossy

> A lightweight, skill-first platform for building autonomous agents that work in teams — across any channel.

Mossy is a tiny runtime for autonomous agents that **think with skills**, **talk through channels**, and **collaborate through a shared task queue**. No graphs, no DAGs, no framework lock-in. Just a worker, a queue, and a folder of Markdown skills your agents discover and load on demand.

- **Lightweight.** A few hundred lines of Python on top of [`pydantic-ai`](https://github.com/pydantic/pydantic-ai).
- **Skill-oriented.** Every capability is a `SKILL.md` the agent reads at runtime. Drop a folder, get a new skill.
- **Multichannel.** The same runtime serves a CLI chat, an HTTP API, and anything else you wire to its inbox.
- **Team-ready.** Agents enqueue work for each other, set priorities, and chain tasks with dependencies.

---

## What Mossy is for

Mossy is for builders who want **autonomous agents in production** without adopting a heavyweight framework.

You get one `Runtime` that:

1. Accepts work from any channel (CLI, HTTP, your own).
2. Picks tasks off a priority queue.
3. Hands them to a worker agent that loads only the skills it needs.
4. Lets that agent enqueue follow-up work, spawn teammates, or hand off to another channel.

If you've ever wanted "a small Slack-bot-shaped thing that can also run a background queue, expose an HTTP endpoint, and grow new abilities by dropping a Markdown file" — that's Mossy.

---

## Core concepts

A handful of small pieces, each doing one thing.

- **Runtime** (`mossy/runtime/core.py`) — the heart. Owns the inbox, the queue, the worker agent, and the task lifecycle.
- **Task & Envelope** (`mossy/runtime/models.py`) — typed units of work, with `Priority` (`INTERRUPT → IDLE`), `depends_on`, and a structured `result`.
- **Skills** (`mossy/skills/<name>/SKILL.md`) — Markdown files with YAML frontmatter. The worker discovers them, picks the relevant one, loads its instructions, and acts. Add one by creating a folder.
- **Capabilities** (`mossy/capabilities/`) — toolsets exposed to agents: `runtime-control` (enqueue, cancel, inspect tasks), `worker-state` (record results, follow-ups), and the dynamic `skills` capability.
- **Channels** (`mossy/channels/`) — input/output surfaces:
  - `cli/chat.py` — interactive terminal agent with conversation history.
  - `http/app.py` — FastAPI endpoints (`/run`, `/status/{id}`, `/queue`, `/health`).
- **Autonomous follow-ups** — when a task finishes, `think_next` can chain a follow-up goal or run an idle housekeeping task. Disable with `PLATFORMER_DISABLE_AUTONOMOUS=1`.

That's the whole platform. Everything else is a skill.

---

## Install

Requires Python 3.11+ and an OpenAI API key (for the default model).

```bash
git clone <your-fork-or-this-repo> mossy
cd mossy

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# then edit .env and set OPENAI_API_KEY=sk-...
```

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
python main.py --port 9000      # change HTTP port
```

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

Now talk to it. The CLI agent has access to Mossy's skills and runtime-control tools, so it can answer directly, queue background work, or inspect the queue.

```text
> hi mossy, what can you do?
I'm Mossy. I can answer directly, run skills (echo, planner, queue-status, …),
or queue background tasks for the worker. Try asking me to plan something or
to show the queue.

> plan a 3-step research task about local mushrooms and queue it
Queued 3 tasks:
  1. abc123 — gather common species in the region
  2. def456 — collect identification tips (depends on abc123)
  3. ghi789 — draft a beginner-friendly summary (depends on def456)

> what's in the queue?
3 pending tasks. Worker is currently running abc123.

> /quit
bye.
```

While you chat, the **worker** is independently picking tasks off the queue and resolving them with skills — that's the multichannel, team-of-agents loop in action. The same tasks are visible at `GET /queue` and `GET /status/{task_id}`.

---

## Add your own skill

Create a folder under `mossy/skills/` with a `SKILL.md`:

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
2. Return a one-sentence summary.
```

Restart (or rely on auto-reload) and the worker will discover it on the next task. That's the whole extension model.

---

## License

MIT (or your choice — update this section to match your repo).
