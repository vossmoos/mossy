# MCP Channel

This channel exposes Mossy over the [Model Context Protocol](https://modelcontextprotocol.io/) so Claude Desktop, Claude Code, and other MCP clients can call **`ask_mossy`**. That tool is a normal Mossy chat turn — the same agent and skills as web chat, CLI, and Slack — not a wrapper around `POST /run`.

Mossy is already an MCP *client* for GitHub. This channel makes Mossy an MCP *server*.

## Configure

Copy `.env.example` to `.env` if needed. Optional MCP settings:

```text
PLATFORMER_MCP_MODEL=          # defaults to PLATFORMER_CLI_MODEL, then PLATFORMER_SKILL_MODEL
MCP_PATH=/mcp                  # Streamable HTTP mount path
MCP_HISTORY_TTL_SECONDS=7200
MCP_HISTORY_MAX_CONVERSATIONS=500
MCP_HISTORY_MAX_MESSAGES=40
# Extra Host / Origin values for DNS rebinding protection (comma-separated).
# MCP_ALLOWED_HOSTS=
# MCP_ALLOWED_ORIGINS=
# MCP_DISABLE_DNS_REBINDING=1  # only if a client is blocked on Host/Origin checks
```

When `MOSSY_API_KEY` is set, MCP clients must send it as `Authorization: Bearer <key>` (same as `/run` and `/agui`).

## Run

Start Mossy from the repo root:

```bash
python main.py
```

The MCP endpoint is enabled with the HTTP server (default `http://127.0.0.1:8765/mcp`).

```bash
python main.py --no-mcp         # disable MCP, keep the rest of HTTP
python main.py --no-http        # disables HTTP, AG-UI, AUI, and MCP
```

## Connect Claude Code

With Mossy running:

```bash
claude mcp add mossy --transport http http://127.0.0.1:8765/mcp
```

If `MOSSY_API_KEY` is set:

```bash
claude mcp add mossy --transport http http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $MOSSY_API_KEY"
```

Ask Claude what tools it has; you should see `ask_mossy`.

## Connect Claude Desktop

Edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`) and add:

```json
{
  "mcpServers": {
    "mossy": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MOSSY_API_KEY"
      }
    }
  }
}
```

Omit `headers` when `MOSSY_API_KEY` is unset. Restart Claude Desktop, open a chat, and confirm `ask_mossy` is in the tools list.

If Desktop rejects the connection with an invalid Host/Origin error, add the value to `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`, or set `MCP_DISABLE_DNS_REBINDING=1` for local-only use.

## Tool

**`ask_mossy`**

| Argument | Required | Meaning |
|---|---|---|
| `message` | yes | Text to send to Mossy, as in web chat. |
| `conversation_id` | no | Named thread. If omitted, history is kept for this MCP session. |

The call waits for Mossy's reply. Long-running work may be queued by Mossy itself; the reply then includes a task id.

## Behavior

- Same Mossy agent as web/CLI/Slack: skills, system-queue, and personality from `MOSSY.md`.
- Each user turn gets a `[System UTC now: …]` prefix (for scheduling, same as other channels).
- Conversation history is in-memory per `conversation_id` or MCP session, with a TTL (default 2 hours).
- This is not a second Mossy process: it shares the runtime, queue, and skills of the running server.
