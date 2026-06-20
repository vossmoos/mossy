---
name: archives
description: Use this skill to create zip archives, inspect zip archive contents, or extract zip files.
---

# Archives

## When To Use This Skill

Use this skill when the user asks to zip files or folders, unzip a `.zip` archive, or
check what is inside a zip archive before extracting it.

## Instructions

Use the `archives` tools:

- `create_zip_archive` to create a `.zip` from one or more files or folders.
- `list_zip_archive` to inspect archive entries without extracting.
- `extract_zip_archive` to unzip an archive into a destination folder.
- `list_downloadable_files` to list files visible through Mossy's protected download endpoint.
- `get_download_info` to get metadata and the protected download path for one file.
- `delete_archive_file` to delete one file from the archive root.

Paths are local paths under Mossy's configured archive root. By default, pass relative
paths from the repo root. Set `overwrite=true` only when the user explicitly wants to
replace an existing archive or extracted file.

Archive artifacts are zip-only. `create_zip_archive` outputs, downloadable files, and
deletable files must be `.zip` files; do not use this skill to create, expose, or delete
other file types as archive artifacts.

Before saving a new zip, Mossy checks total storage under the archive root plus the
candidate zip size against `ARCHIVE_MAX_STORAGE` (default 100MB). If the limit would be
exceeded, the zip is not saved; tell the user the storage limit was reached and suggest
deleting an older zip.

For folders, keep `include_root=true` unless the user asks for only the folder's
contents. After creating or extracting an archive, summarize the archive path,
destination path, and file counts from the tool result. If running inside a queued task
and structured task state is available, call `record_task_result` with the tool result.

When the user asks to download or retrieve a generated file, call `get_download_info`
or `list_downloadable_files` and return the `download_url`. The HTTP endpoint is
protected by Mossy's API key, so browser clients must fetch it with the same bearer key
used for chat/API calls.

Delete files only when the user explicitly asks. Pass only relative paths under the
archive root to `delete_archive_file`; do not pass absolute paths, `..`, or `.` path
segments. The tool deletes `.zip` files only, not folders.
