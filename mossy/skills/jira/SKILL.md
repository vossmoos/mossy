---
name: jira
description: Use this skill to read Jira issues in the configured project, add comments, change status, assign issues, and log work time.
---

# Jira

## When To Use This Skill

Use this skill when the user asks to look up a Jira task/issue, comment on it,
change its status, assign it to someone, or log time against it.

This is the generic capability skill. Project-specific rules (which issues to
touch, status naming, time-logging conventions) belong in separate external
skills that build on top of these tools.

## Configuration

Credentials and scope come from environment variables:

- `JIRA_SITE` — Atlassian site hostname, e.g. `vossmoos.atlassian.net`
- `JIRA_SPACE_NAME` — project key, e.g. `VM`
- `JIRA_USER_EMAIL` — account email for API auth
- `JIRA_API_KEY` — Atlassian API token

If the `jira` tools are unavailable, Jira is not configured.

## Instructions

Task ids are scoped to `JIRA_SPACE_NAME`. Accept either a bare number (`190`)
or a full key (`VM-190`). Reject ids from other projects.

Reads:

- `get_issue` — summary, status, assignee, reporter, timestamps, comment count.
- `list_transitions` — workflow moves available right now for an issue.

Writes (only when the task explicitly asks):

- `add_comment` — plain-text comment on the issue.
- `transition_issue` — change status via transition name, target status name,
  or transition id. Use `list_transitions` first when the target status is unclear.
- `assign_issue` — set assignee by display name, email, or account id.
- `log_work` — log time with Jira duration syntax (`30m`, `1h`, `1h 30m`).

Gather context with reads before any write. Summarize results plainly. If an
issue id is unknown or outside the configured project, say so rather than guessing.
