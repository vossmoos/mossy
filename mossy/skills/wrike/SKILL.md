---
name: wrike
description: Use this skill to read one Wrike task by its API id, or the comments of that task. The wrike tools only read.
---

# Wrike

## When To Use This Skill

Use this skill when the user asks what a Wrike task says — its title, description, status — or
what has been said on it in its comments.

This is the generic capability skill. Which tasks matter and what to do with them belong in
separate external skills that build on these tools; the Wrike → Jira flow is one of them
(`skills/gmail-wrike-tasks`), and it carries its own client.

## Configuration

- `WRIKE_TOKEN` — Wrike permanent access token.

If the `wrike` tools are unavailable, Wrike is not configured.

## Instructions

Reads, and nothing else:

- `get_task` — one task by its **API id** (`MAAAAAEXAMPLE`). Not the number in a permalink
  (`open.htm?id=1234567890`): the API refuses it, and nothing here converts one into the other.
- `get_task_comments` — the comments of one task.

**An empty answer is not proof that there is nothing.** A task or comment list the token's licence
cannot see comes back exactly like one that does not exist. Say "not visible to this account",
never "there is none".

## "Read only" describes these tools — not what an agent can do with Wrike

Anyone reading it as a guarantee is wrong. `shell` — `run_command`, bash with no restriction — is
available on every channel, so if `WRIKE_TOKEN` is in Mossy's environment an agent can already
`curl -X POST` to Wrike today, with or without these tools. This is the lesson of VI-110: a skill
does not bound an agent that has other doors.

## Personal data — declared, not fixed

Task descriptions and comments carry the end customer's personal data: name, street, mobile,
e-mail. Reading them puts that data in the conversation, and the `jira` tools can write it into an
issue that cannot be deleted (`create_issue`, `add_comment`). The Wrike → Jira flow stopped copying
task descriptions into issues for exactly that reason; here, nothing stops it except the agent.
Do not copy a task's description or comments into Jira or anywhere else unless the task explicitly
asks for it.
