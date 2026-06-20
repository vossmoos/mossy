---
name: file-sharing
description: Use this skill to make a finished file downloadable by the user — copy it from the repo into the isolated share folder and return a protected download link.
---

# File Sharing

## When To Use This Skill

Use this skill when the user should be able to download a file Mossy produced — a
generated report, a bundle, a `.zip`, an export. Sharing is an explicit step: only
files copied into the share folder are downloadable.

## Tools

- `share_file(path, name="", overwrite=false)` — copy a file from under the repo
  (e.g. `data/archive/20260620T...Z.zip`) into the isolated share root and return its
  `download_url`. Optionally rename the shared copy with `name` (a simple file name).
- `list_shared_files(recursive=false)` — list everything currently shared, each with
  its download link.
- `get_download_info(path)` — metadata + download URL for one shared file (path
  relative to the share root).
- `unshare_file(path)` — remove a shared file, revoking its link.

## Instructions

This skill does not zip or edit files — build the file first (the **filesystem**
skill zips and writes), then `share_file` the finished artifact. Give the user the
`download_url` from the result. The download endpoint is protected by Mossy's API
key, so browser clients must fetch it with the same bearer key used for chat.

The share root is `MOSSY_SHARE_ROOT` (default `<repo>/shared`), kept separate from
the working tree so "downloadable" is an explicit allow-list. Before copying, the
skill checks total share storage against `MOSSY_SHARE_MAX_STORAGE` (default 100MB);
if the limit would be exceeded the copy is refused — tell the user and suggest
`unshare_file` on an older file. After sharing, summarize the shared file name, byte
size, and the download URL. If running inside a queued task and structured task
state is available, call `record_task_result` with the result.
