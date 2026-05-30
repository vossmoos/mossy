# AG-UI Channel

This channel exposes Mossy over the [AG-UI (Agent-User Interaction) protocol](https://docs.ag-ui.com/introduction) with **Server-Sent Events (SSE)** streaming. Use it to connect web chat UIs, CopilotKit, or any AG-UI-compatible frontend.

Mossy uses Pydantic AI's [`AGUIAdapter`](https://ai.pydantic.dev/ui/ag-ui/) for full protocol support: lifecycle events, streamed text, tool calls, frontend tools, and shared state.

## Install

From the repo root (the `ag-ui` extra is included in `requirements.txt`):

```bash
pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` if needed. Optional AG-UI settings:

```text
PLATFORMER_AGUI_MODEL=          # defaults to PLATFORMER_CLI_MODEL, then PLATFORMER_SKILL_MODEL
AGUI_PATH=/agui                 # POST endpoint path
AGUI_CORS_ORIGINS=              # comma-separated origins for browser clients, e.g. http://localhost:5173
```

Set `AGUI_CORS_ORIGINS` when your frontend runs on a different origin than Mossy.

## Run

Start Mossy from the repo root:

```bash
python main.py
```

The AG-UI endpoint is enabled with the HTTP server (default `http://127.0.0.1:8765/agui`).

```bash
python main.py --no-agui        # disable AG-UI, keep task HTTP API
python main.py --no-http        # disables HTTP and AG-UI
```

## Try it with curl

```bash
curl -N -X POST http://127.0.0.1:8765/agui \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "threadId": "thread-1",
    "runId": "run-1",
    "messages": [{"id": "m1", "role": "user", "content": "Say hello in one sentence."}],
    "state": {},
    "context": [],
    "tools": [],
    "forwardedProps": {}
  }'
```

You should see SSE `data:` lines with AG-UI events such as `RUN_STARTED`, `TEXT_MESSAGE_CONTENT`, and `RUN_FINISHED`.

## Frontend integration

Point your AG-UI client at:

```text
POST http://<host>:<port>/agui
Accept: text/event-stream
Content-Type: application/json
```

Body shape follows [`RunAgentInput`](https://docs.ag-ui.com/sdk/python/core/types#runagentinput): `threadId`, `runId`, `messages`, optional `state`, `context`, and frontend `tools`.

For interactive debugging, see the [AG-UI Dojo](https://docs.ag-ui.com/tutorials/debugging#the-ag-ui-dojo).

## Behavior

- Same Mossy agent as CLI/Slack: skills, system-queue, and personality from `MOSSY.md`.
- Each user turn gets a `[System UTC now: …]` prefix (for scheduling, same as other channels).
- Conversation history is client-managed via `messages` on each request (AG-UI standard).
- Queued background tasks do not stream back to the web client automatically yet; the agent is instructed to return task ids in chat.
