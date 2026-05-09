---
name: skill-manager
description: Installs, uninstalls, lists, and inspects Mossy skills using a workspace repository folder. Use when the user wants to add or remove skills, list installed vs available skills, or read a skill's title and description from SKILL.md.
repository-root: ./repository
---

# Skill manager

## Concepts

- **Installed skills** live under `mossy/skills/<folder>/` with a `SKILL.md`. The Mossy runtime loads them automatically (`Runtime.skills_root`).
- **Repository** holds skills that are **not** installed yet: each skill is a folder at `<repository-root>/<folder>/` containing `SKILL.md` (and optional `scripts/`, assets). This folder is gitignored at the workspace root as `repository/` by default.
- **Folder name** is the stable id for install, uninstall, list, and info commands (it may match the `name` field in frontmatter).

## Repository path

Configure where to read “not installed” skills:

1. **In this file (preferred):** set `repository-root` in the YAML frontmatter above.
   - Relative paths (e.g. `./repository`) are resolved from the **workspace root** (same directory as `main.py`, parent of the `mossy/` package).
   - Use an **absolute** path when the stash should live outside the repo (paste the full path as `repository-root`).
2. **Environment override:** `MOSSY_SKILL_REPOSITORY` (absolute path to the repository directory). Wins over frontmatter when set.

## Tooling

Run the bundled script from the workspace root (or any cwd—the script resolves paths from its location):

```bash
python mossy/skills/skill-manager/scripts/manage_skills.py list
python mossy/skills/skill-manager/scripts/manage_skills.py info <folder-name>
python mossy/skills/skill-manager/scripts/manage_skills.py install <folder-name>
python mossy/skills/skill-manager/scripts/manage_skills.py install <folder-name> --force
python mossy/skills/skill-manager/scripts/manage_skills.py uninstall <folder-name>
```

Script output is JSON on stdout; errors print JSON on stderr and exit non-zero.

## Operations

### List skills

Run `manage_skills.py list`. Interpret the JSON:

- `installed`: folder names under `mossy/skills/` that have `SKILL.md`.
- `in_repository`: folder names under the configured repository root that have `SKILL.md`.
- `not_installed`: present in the repository but not yet copied into `mossy/skills/`.
- `repository_message`: when non-null, explains that the configured repository path is missing or has no skill folders with `SKILL.md` — the script **does not create** the repository directory; the user creates `repository-root` (or sets `MOSSY_SKILL_REPOSITORY`) and adds skill folders there.

Summarize these three lists clearly for the user. When `repository_message` is set, repeat that sentence to the user verbatim so they know why `in_repository` / `not_installed` are empty.

### Get details (title + description)

Run `manage_skills.py info <folder-name>`.

- **`description`**: from YAML frontmatter `description` in `SKILL.md`.
- **`title`**: first Markdown `# heading` in the body after the frontmatter (if present).
- **`name`**: frontmatter `name`, or the folder name if missing.
- **`source`**: `installed` or `repository`.

If the skill exists only in one place, `info` uses that copy.

### Install

1. Confirm the skill folder exists under the repository (`list` / filesystem).
2. Run `manage_skills.py install <folder-name>`.
3. This copies the entire folder into `mossy/skills/<folder-name>/`. The worker picks it up on reload (`auto_reload`).
4. If the skill is already installed, the script fails unless you pass `--force` (replaces the installed folder).

### Uninstall

Run `manage_skills.py uninstall <folder-name>`. This **deletes only** `mossy/skills/<folder-name>/`. The copy under the repository folder is **unchanged**.

## Notes

- Do not delete or move the repository copy on uninstall.
- Ensure new repository skills include a valid `SKILL.md` with at least `name` and `description` in frontmatter for discovery.
- For accurate `info` output, keep frontmatter `description` on a single line (the helper script does not expand YAML folded blocks).
