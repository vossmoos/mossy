---
name: system-queue
description: Use this skill to create, inspect, schedule, or cancel Mossy's queued tasks.
---

# System Queue

## When To Use This Skill

Use this skill when the user asks to create background work, inspect the queue, check task status, schedule future work, or cancel a task.

## Instructions

Use the `system-queue` tools:

- `enqueue_task` to create queued work.
- `queue_depth` for approximate pending queue depth.
- `list_tasks` for known tasks, optionally filtered by status.
- `get_task_status` for one task id.
- `cancel_task` when the user explicitly asks to cancel a task.

For future work, pass `scheduled_for` as an absolute UTC datetime. The chat turn includes a line
`[System UTC now: …]` — use it as the clock for relative times ("in 1 minute", "tomorrow at 9").
Convert to UTC before calling `enqueue_task`.

When cancelling, trust the tool response: if `cancelled` is false and `reason` is `already_completed`,
the task ran to completion and was not cancelled.

Summarize the result plainly. If a task id is unknown, say so.
