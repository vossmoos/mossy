---
name: github
description: Use this skill to work with GitHub — clone/pull repos, create branches, commit and push, open pull requests, and comment on issues or PRs.
---

# GitHub

## When To Use This Skill

Use this skill when the user asks to work with a GitHub repository: clone or pull it,
create a branch, commit and push changes, open a pull request, or comment on an issue
or PR.

This is the generic capability skill. Project-specific rules (e.g. "we only work via
PRs", "never push to main", branch naming, comment templates) belong in a separate
project policy skill built on top of these tools.

## Instructions

GitHub is split across two tool groups:

**Local git** (`git_*` tools) — for changes to a working copy:

- `git_clone` — clone a repo (lands under `GITHUB_WORKDIR`); returns the local path.
- `git_pull` — fast-forward an existing checkout.
- `git_create_branch` / `git_checkout` — create or switch branches.
- `git_commit_all` — stage everything and commit with a message.
- `git_push` — push the branch (sets upstream when a branch is named).
- `git_status` — inspect the working tree.

**GitHub API** (`github` MCP tools) — for server-side actions that don't need a
checkout: open pull requests, comment on issues/PRs, and read repo/PR/issue data.
Requires `GITHUB_PERSONAL_ACCESS_TOKEN`; if those tools are absent, GitHub is not
configured.

Typical flow for a change: `git_clone` (or `git_pull`) → `git_create_branch` → make
edits → `git_commit_all` → `git_push` → open a PR via the GitHub MCP tools → comment if
asked. Report the branch name, pushed commit, and PR URL. If a command fails, surface
its `output` rather than retrying blindly.
