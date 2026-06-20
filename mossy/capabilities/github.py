"""GitHub integration exposed as Pydantic AI capabilities.

GitHub splits cleanly into two surfaces, and we expose both under the `github` skill:

1. **Hosted GitHub MCP server** (API operations): reads, branches, commits via the
   API, pull requests, and issue/PR comments. Mounted as an MCP toolset. This is the
   "connect their MCP as a capability" piece.

2. **Local git** (working-copy operations): clone, pull, create branches, commit, and
   push. The hosted MCP cannot touch a local checkout, so these shell out to `git`.

`github_capabilities()` returns whichever of the two are available. If no GitHub token
is set, the MCP capability is omitted; local git is always available where `git` is.

Env:
    GITHUB_PERSONAL_ACCESS_TOKEN (or GITHUB_TOKEN)  PAT with repo + PR scopes.
    GITHUB_MCP_URL   optional override; defaults to GitHub's hosted MCP endpoint.
    GITHUB_WORKDIR   optional base dir for clones (default: ./repos under CWD).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

DEFAULT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"


def github_capabilities() -> list[Toolset]:
    """Return the available GitHub capabilities (MCP toolset and/or local git)."""
    caps: list[Toolset] = []
    mcp = github_mcp_capability()
    if mcp is not None:
        caps.append(mcp)
    caps.append(git_capability())
    return caps


def github_mcp_capability() -> Toolset | None:
    """Mount GitHub's hosted MCP server as a capability (API operations).

    Returns None when no GitHub token is configured.
    """
    token = (
        os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip()
    if not token:
        return None

    # Imported lazily so Mossy boots even if the MCP transport extra isn't installed.
    from pydantic_ai.mcp import MCPServerStreamableHTTP

    url = (os.environ.get("GITHUB_MCP_URL") or DEFAULT_GITHUB_MCP_URL).strip()
    server = MCPServerStreamableHTTP(
        url=url,
        headers={"Authorization": f"Bearer {token}"},
        id="github",
        tool_prefix="github",
    )
    return Toolset(server)


def git_capability() -> Toolset:
    """Local git operations (clone, pull, branch, commit, push) over a working copy."""

    def _workdir() -> Path:
        base = os.environ.get("GITHUB_WORKDIR") or str(Path.cwd() / "repos")
        path = Path(base).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _git(args: list[str], cwd: str | Path | None = None) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "command": "git " + " ".join(args),
            "output": out.decode("utf-8", errors="replace").strip(),
        }

    async def git_clone(repo_url: str, directory: str | None = None) -> dict[str, Any]:
        """Clone a repo into GITHUB_WORKDIR. Returns the local path on success."""
        target_parent = _workdir()
        name = directory or repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
        dest = target_parent / name
        result = await _git(["clone", repo_url, str(dest)])
        result["path"] = str(dest)
        return result

    async def git_pull(path: str) -> dict[str, Any]:
        """Pull the latest changes in an existing local checkout at `path`."""
        return await _git(["pull", "--ff-only"], cwd=path)

    async def git_create_branch(path: str, branch: str) -> dict[str, Any]:
        """Create and switch to a new branch in the checkout at `path`."""
        return await _git(["checkout", "-b", branch], cwd=path)

    async def git_checkout(path: str, branch: str) -> dict[str, Any]:
        """Switch to an existing branch in the checkout at `path`."""
        return await _git(["checkout", branch], cwd=path)

    async def git_commit_all(path: str, message: str) -> dict[str, Any]:
        """Stage all changes and commit them in the checkout at `path`."""
        staged = await _git(["add", "-A"], cwd=path)
        if not staged["ok"]:
            return staged
        return await _git(["commit", "-m", message], cwd=path)

    async def git_push(path: str, branch: str | None = None) -> dict[str, Any]:
        """Push the current (or named) branch, setting upstream if needed."""
        args = ["push", "--set-upstream", "origin", branch] if branch else ["push"]
        return await _git(args, cwd=path)

    async def git_status(path: str) -> dict[str, Any]:
        """Show working-tree status for the checkout at `path`."""
        return await _git(["status", "--short", "--branch"], cwd=path)

    return Toolset(
        FunctionToolset(
            [
                git_clone,
                git_pull,
                git_create_branch,
                git_checkout,
                git_commit_all,
                git_push,
                git_status,
            ],
            id="git",
            instructions=(
                "Local git tools for working copies: clone, pull, create/switch branches, "
                "commit, and push. Clones land under GITHUB_WORKDIR. Use these for changes to "
                "a checkout; use the `github` MCP tools for API-only actions (open pull "
                "requests, comment on issues/PRs, read repo metadata)."
            ),
        )
    )
