---
name: filesystem
description: Use this skill to create plain files in the repo data folder and list files already there.
---

# Filesystem

## When To Use This Skill

Use this skill when the user asks Mossy to save simple text content to a file, create a note/artifact, or inspect what files are available in the repo-local `data/` folder.

## Scope

This skill only works with the top-level `data/` folder in the Mossy repo. It does not create or read subfolders. File names must be simple names like `note.txt` or `summary.md`, not paths like `foo/note.txt`.

## Instructions

Use the available scripts:

- `scripts/create_file.py` to create or replace one file in `data/`.
- `scripts/list_files.py` to list files currently present in `data/`.

For `scripts/create_file.py`, pass:

- `filename`: the simple file name to create.
- `content`: the complete UTF-8 text content to write.

For `scripts/list_files.py`, pass no arguments.

After creating a file, summarize the saved file name and byte size from the script output. If running inside a queued task and structured task state is available, call `record_task_result` with the file name and script output.
