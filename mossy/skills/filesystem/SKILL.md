---
name: filesystem
description: Use this skill to read and write text files anywhere under the Mossy repo, author files incrementally (header then appended sections), and create/inspect/extract .zip archives.
---

# Filesystem

## When To Use This Skill

Use this skill whenever Mossy needs to save content to a file, author a file
section by section, read a file back, list a folder, or work with `.zip` archives
(create, inspect, or extract) — all under the Mossy repo.

## Scope

Paths are relative to the filesystem root (the repo, or `MOSSY_FS_ROOT` when set).
Subfolders are allowed (e.g. `data/onc-001--SIM1-SIM2.yaml`). `..` segments and
symlinks are rejected. These tools persist and read content — they never invent it.

## Tools

- `write_file(path, content, overwrite=false)` — create a file and write text. Use
  for the first write of a new file. Refuses to clobber unless `overwrite=true`.
- `append_file(path, content)` — append text, creating the file if missing. Use this
  to author large files incrementally (write the header, then append one section per
  call) so no single write is big enough to time out.
- `read_file(path, max_bytes)` — read a text file back (truncated to `max_bytes`).
- `list_dir(directory, recursive=false)` — list files/folders under a directory.
- `delete_file(path)` — delete a single file (explicit requests only; not folders).
- `zip_files(output_path, source_paths, overwrite=false, include_root=true)` — create
  a `.zip` from files/folders. For folders keep `include_root=true` unless the user
  wants only the contents.
- `unzip_file(zip_path, destination_dir, overwrite=false)` — extract a `.zip`.
- `list_zip(zip_path)` — inspect a `.zip`'s entries without extracting.

## Notes

These tools do not produce download links. To make a finished file downloadable,
hand it to the **file-sharing** skill's `share_file`. When authoring a file a skill
told you to create, never report that the file "doesn't exist yet" as a blocker —
create it with `write_file`/`append_file`. If running inside a queued task and
structured task state is available, call `record_task_result` with the result.
