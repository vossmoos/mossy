---
name: mcp
description: Use this skill to work with tools from connected MCP servers, such as Myco (permissioned company SaaS tools and memory). Tool names start with their source, e.g. jira_get_issue or memory_recall.
---

# Connected MCP servers

## When To Use This Skill

Use this skill when the task needs a tool that comes from an external MCP server rather than
from Mossy itself. Behind **Myco**, those are the company's SaaS tools (Jira, Zendesk, Wrike,
...) and its memory, each curated and permission-checked.

## Configuration

- `MOSSY_MCP_SERVERS` — comma-separated server names, e.g. `myco`.
- `MOSSY_MCP__<NAME>__URL` — the server's MCP endpoint, e.g. `http://myco:8080/mcp`.
- `MOSSY_MCP__<NAME>__TOKEN` — sent as a bearer token (for Myco: this agent's key).
- Optional: `MOSSY_MCP__<NAME>__HEADERS` (JSON), `__TOOL_PREFIX`, `__SKILL`, `__TIMEOUT`.

If no such tools are available, no server is configured or it is unreachable right now.

## Instructions

- Tool names start with their source (`jira_`, `zendesk_`, `memory_`). Read each tool's
  description: Myco appends what you are allowed to do there — the data scope, redacted
  fields and arguments fixed for you. Which memory you may read is decided by Myco.
- **A refusal is a decision, not an error.** "Myco denied …" means this agent may not do it.
  Do not retry it in another way or with another tool; tell the user what was refused.
- `[redacted]` fields and withheld rows were removed on purpose. Never guess what they said.
- These tools may be the same services as Mossy's own `jira`/`wrike`/`freshdesk` tools. When
  both exist, prefer the Myco ones: they carry the company's permissions and audit log.
