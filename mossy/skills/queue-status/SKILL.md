---
name: queue-status
description: Use this skill to inspect Mossy's queue, task statuses, pending work, cancellations, and backlog depth.
---

# Queue Status

## When To Use This Skill

Use this skill when the user asks what is queued, how many tasks exist, whether a specific task is done, or wants to cancel or inspect background work.

## Instructions

Use the runtime-control tools:

- `queue_depth` for approximate pending queue depth.
- `list_tasks` for known tasks, optionally filtered by status.
- `get_task_status` for one task id.
- `cancel_task` when the user explicitly asks to cancel a task.

Summarize the result plainly. If a task id is unknown, say so.
