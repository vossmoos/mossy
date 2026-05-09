---
name: planner
description: Use this skill to decompose complex, multi-step goals into ordered queued tasks.
---

# Planner

## When To Use This Skill

Use this skill when a request has several distinct steps, should continue in the background, or benefits from checkpointed queued work.

## Instructions

1. Break the goal into small, executable natural-language tasks.
2. Keep each task independently understandable.
3. Use `enqueue_task` for each step.
4. When steps must run in order, pass the previous task id in `depends_on` for the next task.
5. Report the queued task ids and a concise summary of the plan.

Do not use this skill for simple chat answers that can be resolved immediately.
