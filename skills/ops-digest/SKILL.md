---
name: ops-digest
description: Use this skill to print a one-line ops digest to stderr when the goal already contains the line.
---

# Ops Digest

## When To Use This Skill

Use when the task is to print an ops digest line to stderr.

## Instructions

The goal already contains the exact line (it starts with `Mossy ops `).

1. Copy that line verbatim (one line, no extra text).
2. If `print_stderr` is available, call it with that line. Otherwise reply with that line only.
3. If `record_task_result` is available, save `{"digest": "<the line>"}`.
4. Do not enqueue follow-up work. Do not recount the queue.
