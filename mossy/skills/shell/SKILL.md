---
name: shell
description: Use this skill to run shell commands — grep, test runners, linters, build tools, pip/npm installs, and any other bash command that needs to execute in a working directory.
---

# Shell

## When To Use This Skill

Use this skill whenever you need to execute a command in a working directory: searching
code with grep, compiling or syntax-checking files, running tests, installing
dependencies, or any other operation that requires a real shell.

## Tools

- `run_command(command, cwd, timeout)` — run a bash command and return its output.
  - `command`: any bash expression, passed to `bash -c`.
  - `cwd`: working directory — absolute path, or relative to `MOSSY_SHELL_WORKDIR`
    (which defaults to `GITHUB_WORKDIR` or the process CWD). Omit to use the default.
  - `timeout`: seconds before the command is killed (default: `MOSSY_SHELL_TIMEOUT`, 60).
  - Returns `{ok, exit_code, stdout, stderr, command, cwd}`.

## Notes

- Always check `ok` and `exit_code` in the result before acting on `stdout`.
- For large outputs, pipe through `head`, `tail`, or `grep` to keep results manageable:
  `grep -rn 'keyword' --include='*.py' | head -40`.
- Prefer `run_command` over `read_file` for directory listing and file searching —
  `find` and `grep` are faster on large codebases than listing and reading file by file.
- Commands run with the same permissions as the Mossy process. Do not run destructive
  commands (rm -rf, git reset --hard, etc.) unless the user explicitly asked for it.
- For long-running commands (test suites, builds), set `timeout` accordingly and note
  that you did so.
