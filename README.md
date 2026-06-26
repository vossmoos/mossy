# Mossy

> Ship agents by writing skills, not framework code.

Mossy is a ready-to-run agent with a tiny core and a powerful skill engine inside. You don't learn a Mossy API — you write **skills** in the open agentic `SKILL.md` format and the agent picks them up. Install it, run it, and extend it by dropping a Markdown file.

- **Skill-first.** Every new behavior is a skill folder — a `SKILL.md` in the open agentic skills format, plus any scripts or assets the skill needs. No bespoke API to memorize.
- **Tiny core.** A few hundred lines of Python on top of [`pydantic-ai`](https://github.com/pydantic/pydantic-ai). You can ignore it and just write skills.
- **Works out of the box.** Worker, queue, CLI chat, and HTTP API are already wired up. Run `python main.py` and you have an agent.
- **Extensible channels.** CLI, HTTP, AG-UI (SSE web chat), and Slack (Socket Mode) ship in the box. Add Telegram or any other connector as a module under `mossy/channels/` — anything that produces an `Envelope` plugs into the same inbox.
- **Team-ready.** Agents enqueue work for each other, set priorities, and chain tasks across any channel.

---

## Core concepts

A handful of small pieces, each doing one thing.

- **Runtime** (`mossy/runtime/core.py`) — the heart. Owns the inbox, the queue, the worker agent, and the task lifecycle.
- **Task & Envelope** (`mossy/runtime/models.py`) — typed units of work, with `Priority` (`INTERRUPT → IDLE`), `depends_on`, and a structured `result`.
- **Skills** — packaged system skills live in `mossy/skills/<name>/`; downloadable or user-provided skills live in `skills/<name>/`. Each skill is a `SKILL.md` with YAML frontmatter, plus any helper scripts (e.g. `scripts/*.py`) or assets the skill calls. The worker discovers both roots, picks the relevant skill, loads its instructions, and runs the bundled scripts when told to.
- **Capabilities** (`mossy/capabilities/`) — toolsets exposed to agents through skills: `system-queue` (enqueue, cancel, inspect tasks), `worker-state` (record results, follow-ups), `mossy-personality` (always-on identity and tone instructions loaded from root `MOSSY.md`), `skill-manager` (install/remove skills from a repository, CLI only), and the dynamic `skills` capability.
- **Channels** (`mossy/channels/`) — input/output surfaces:
  - `cli/chat.py` — interactive terminal agent with conversation history.
  - `http/app.py` — FastAPI endpoints (`/run`, `/status/{id}`, `/queue`, `/health`).
  - `agui/app.py` — AG-UI protocol over SSE for web chat clients (`POST /agui`). See `mossy/channels/agui/README.md`.
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
python main.py --no-agui        # disable the AG-UI web chat endpoint
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
