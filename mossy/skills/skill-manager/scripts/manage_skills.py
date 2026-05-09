"""List, inspect, install, and uninstall Mossy skills from the repository folder."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path


def _workspace_root() -> Path:
    # mossy/skills/<skill>/scripts/this_file.py -> parents[4] = git workspace root
    return Path(__file__).resolve().parents[4]


def _skills_install_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_skill_manager_frontmatter(skill_md: Path) -> dict[str, str]:
    text = skill_md.read_text(encoding="utf-8")
    return _parse_frontmatter_block(text)


def _parse_frontmatter_block(text: str) -> dict[str, str]:
    m = re.match(r"^---\s*\r?\n(.*?)\r?\n---", text, re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        val = rest.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        out[key] = val
    return out


def _repository_root() -> Path:
    env = os.environ.get("MOSSY_SKILL_REPOSITORY")
    if env:
        return Path(env).expanduser().resolve()
    skill_md = Path(__file__).resolve().parents[1] / "SKILL.md"
    fm = _parse_skill_manager_frontmatter(skill_md)
    raw = fm.get("repository-root", "./repository").strip()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (_workspace_root() / path).resolve()


def _skill_dirs(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    names: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and (child / "SKILL.md").is_file():
            names.append(child.name)
    return names


def _title_from_body(skill_md: Path) -> str | None:
    text = skill_md.read_text(encoding="utf-8")
    after = re.split(r"^---\s*\r?\n.*?\r?\n---\s*\r?\n", text, maxsplit=1, flags=re.DOTALL)
    body = after[-1] if after else text
    for line in body.splitlines():
        heading = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if heading:
            return heading.group(1).strip()
    return None


def _skill_details(skill_dir: Path) -> dict[str, str | None]:
    md = skill_dir / "SKILL.md"
    fm = _parse_frontmatter_block(md.read_text(encoding="utf-8"))
    name = fm.get("name") or skill_dir.name
    description = fm.get("description")
    title = _title_from_body(md)
    return {
        "folder": skill_dir.name,
        "name": name,
        "description": description,
        "title": title,
        "path": str(skill_dir.resolve()),
    }


def cmd_list() -> None:
    install_dir = _skills_install_dir()
    repo_root = _repository_root()
    installed = set(_skill_dirs(install_dir))
    in_repo = set(_skill_dirs(repo_root))
    not_installed = sorted(in_repo - installed)
    repo_empty_or_missing = not repo_root.is_dir() or not in_repo
    payload = {
        "install_dir": str(install_dir),
        "repository_root": str(repo_root),
        "installed": sorted(installed),
        "in_repository": sorted(in_repo),
        "not_installed": not_installed,
        "repository_message": (
            "The configured repository is empty or does not exist."
            if repo_empty_or_missing
            else None
        ),
    }
    print(json.dumps(payload, indent=2))


def cmd_info(name: str) -> None:
    install_dir = _skills_install_dir()
    repo_root = _repository_root()
    installed_path = install_dir / name
    repo_path = repo_root / name
    if installed_path.is_dir() and (installed_path / "SKILL.md").is_file():
        details = _skill_details(installed_path)
        details["source"] = "installed"
        print(json.dumps(details, indent=2))
        return
    if repo_path.is_dir() and (repo_path / "SKILL.md").is_file():
        details = _skill_details(repo_path)
        details["source"] = "repository"
        print(json.dumps(details, indent=2))
        return
    print(
        json.dumps(
            {"error": f"Skill {name!r} not found under installed skills or repository."},
            indent=2,
        ),
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_install(name: str, *, force: bool) -> None:
    repo_root = _repository_root()
    if not repo_root.is_dir():
        print(
            json.dumps(
                {"error": "The configured repository does not exist.", "repository_root": str(repo_root)},
                indent=2,
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    src = repo_root / name
    if not src.is_dir() or not (src / "SKILL.md").is_file():
        print(json.dumps({"error": f"No skill {name!r} in repository at {repo_root}."}, indent=2), file=sys.stderr)
        sys.exit(1)
    dest = _skills_install_dir() / name
    if dest.exists():
        if not force:
            print(
                json.dumps(
                    {"error": f"Already installed at {dest}. Use --force to replace."},
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print(json.dumps({"status": "installed", "from": str(src), "to": str(dest)}, indent=2))


def cmd_uninstall(name: str) -> None:
    dest = _skills_install_dir() / name
    if not dest.is_dir():
        print(json.dumps({"error": f"Skill {name!r} is not installed at {dest}."}, indent=2), file=sys.stderr)
        sys.exit(1)
    shutil.rmtree(dest)
    print(json.dumps({"status": "uninstalled", "removed": str(dest)}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Mossy skills from the repository folder.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List installed, repository, and not-yet-installed skill names.")

    p_info = sub.add_parser("info", help="Show title (first # heading), name, and description from SKILL.md.")
    p_info.add_argument("name", help="Skill folder name.")

    p_in = sub.add_parser("install", help="Copy a skill from the repository into mossy/skills/.")
    p_in.add_argument("name", help="Skill folder name under the repository.")
    p_in.add_argument("--force", action="store_true", help="Replace an existing installed folder.")

    p_un = sub.add_parser("uninstall", help="Remove an installed skill folder (repository copy is kept).")
    p_un.add_argument("name", help="Skill folder name under mossy/skills/.")

    args = parser.parse_args()
    if args.command == "list":
        cmd_list()
    elif args.command == "info":
        cmd_info(args.name)
    elif args.command == "install":
        cmd_install(args.name, force=args.force)
    elif args.command == "uninstall":
        cmd_uninstall(args.name)


if __name__ == "__main__":
    main()
