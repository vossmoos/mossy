# A2A Channel

Lets other agents discover this Mossy and ask it questions over the
[A2A protocol](https://a2a-protocol.org).

- Agent card: `GET /a2a/.well-known/agent-card.json` (public)
- Tasks: `POST /a2a/` with `Authorization: Bearer <MOSSY_API_KEY>`

With no API key set, every task request is refused. Disable the channel with `--no-a2a`.

## What the agent can do

It does not get Mossy's skills or capabilities, only these read-only tools:

- `agent_status`: up, start time, tasks by status, queue size
- `task_status`: status, result and error of one queued task

## Add your own tools

List plain Python functions as `package.module:function`:

```bash
MOSSY_A2A_TOOLS=my_agent.a2a_tools:order_status,my_agent.a2a_tools:list_reports
```

The function's signature and docstring become the tool definition. Anyone holding the key can
reach what you list, so keep these read-only unless you mean otherwise. A name that cannot be
loaded is reported on stderr and skipped.

## Configure

```text
PLATFORMER_A2A_MODEL=          # default: PLATFORMER_SKILL_MODEL
MOSSY_A2A_NAME=mossy
MOSSY_A2A_DESCRIPTION=
MOSSY_PUBLIC_BASE_URL=         # the URL the card advertises
```
