"""GitHub integration exposed as Pydantic AI capabilities.

GitHub splits cleanly into three surfaces, and we expose all under the `github` skill:

1. **Hosted GitHub MCP server** (API operations): reads, branches, commits via the
   API, pull requests, and issue/PR comments. Mounted as an MCP toolset. This is the
   "connect their MCP as a capability" piece.

2. **Local git** (working-copy operations): clone, pull, create branches, commit, and
   push. The hosted MCP cannot touch a local checkout, so these shell out to `git`.

3. **GitHub REST API** (repository discovery): list repositories the token can access.
   Uses the same PAT as the MCP tools.

`github_capabilities()` returns whichever surfaces are available. If no GitHub token
is set, the MCP and REST API capabilities are omitted; local git is always available
where `git` is.

Env:
    GITHUB_PERSONAL_ACCESS_TOKEN (or GITHUB_TOKEN)  PAT with repo + PR scopes.
    GITHUB_API_URL   optional override; defaults to https://api.github.com.
    GITHUB_MCP_URL   optional override; defaults to GitHub's hosted MCP endpoint.
    GITHUB_WORKDIR   optional base dir for clones (default: ./repos under CWD).
    MOSSY_GIT_USER_NAME   commit author/committer name (default: mossy-mossy).
    MOSSY_GIT_USER_EMAIL  commit author/committer email (default: services@vossmoos.com).
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

DEFAULT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
DEFAULT_GITHUB_API_URL = "https://api.github.com"


def _github_token() -> str:
    return (
        os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip()


def _authed_repo_url(repo: str) -> str:
    """Inject a GitHub token into an https repo URL so private clones/pushes work."""
    token = _github_token()
    if not token:
        return repo
    parts = urlsplit(repo)
    if parts.scheme != "https" or "@" in parts.netloc:
        return repo  # ssh URL, or credentials already present — leave as-is
    netloc = f"x-access-token:{token}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _redact(text: str) -> str:
    token = _github_token()
    return text.replace(token, "***") if token else text


def _git_commit_identity() -> tuple[str, str]:
    name = (os.environ.get("MOSSY_GIT_USER_NAME") or "mossy-mossy").strip()
    email = (os.environ.get("MOSSY_GIT_USER_EMAIL") or "services@vossmoos.com").strip()
    return name, email


def _github_api_request(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
) -> tuple[int, Any]:
    token = _github_token()
    base = (os.environ.get("GITHUB_API_URL") or DEFAULT_GITHUB_API_URL).strip().rstrip("/")
    clean_path = path if path.startswith("/") else f"/{path}"
    url = f"{base}{clean_path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode() or 200
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read() if exc.fp else b""
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return status, {"_raw": raw.decode("utf-8", errors="replace")}


def _repo_summary(repo: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_name": repo.get("full_name"),
        "clone_url": repo.get("clone_url"),
        "html_url": repo.get("html_url"),
        "private": repo.get("private"),
        "default_branch": repo.get("default_branch"),
        "description": repo.get("description") or "",
    }


def _list_repositories_sync(org: str | None, limit: int) -> dict[str, Any]:
    repos: list[dict[str, Any]] = []
    page = 1
    per_page = min(100, limit)

    while len(repos) < limit:
        if org:
            path = f"/orgs/{org.strip()}/repos"
            query = {
                "per_page": str(per_page),
                "page": str(page),
                "type": "all",
                "sort": "updated",
            }
        else:
            path = "/user/repos"
            query = {
                "affiliation": "owner,collaborator,organization_member",
                "visibility": "all",
                "sort": "updated",
                "per_page": str(per_page),
                "page": str(page),
            }
        status, data = _github_api_request("GET", path, query=query)
        if status != 200:
            message = data.get("message") if isinstance(data, dict) else str(data)
            return {"ok": False, "status": status, "error": message or f"HTTP {status}"}
        if not isinstance(data, list) or not data:
            break
        for repo in data:
            if len(repos) >= limit:
                break
            repos.append(_repo_summary(repo))
        if len(data) < per_page:
            break
        page += 1

    return {"ok": True, "count": len(repos), "repositories": repos}


def github_capabilities() -> list[Toolset]:
    """Return the available GitHub capabilities (MCP toolset and/or local git)."""
    caps: list[Toolset] = []
    mcp = github_mcp_capability()
    if mcp is not None:
        caps.append(mcp)
    api = github_api_capability()
    if api is not None:
        caps.append(api)
    caps.append(git_capability())
    return caps


def github_mcp_capability() -> Toolset | None:
    """Mount GitHub's hosted MCP server as a capability (API operations).

    Returns None when no GitHub token is configured.
    """
    token = _github_token()
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


def github_api_capability() -> Toolset | None:
    """Direct GitHub REST API helpers (no MCP). Returns None when no token is set."""
    if not _github_token():
        return None

    async def github_list_repositories(
        org: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List GitHub repositories available to the configured token.

        Without `org`: repos you own, collaborate on, or access via org membership.
        With `org`: repos in that organization (requires access). Returns `clone_url`
        values suitable for `git_clone`.
        """
        limit = max(1, min(limit, 500))
        return await asyncio.to_thread(_list_repositories_sync, org, limit)

    return Toolset(
        FunctionToolset(
            [github_list_repositories],
            id="github-api",
            instructions=(
                "GitHub REST API helpers. Use github_list_repositories to discover repos "
                "the token can access before choosing a URL for git_clone."
            ),
        )
    )


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
        command = "git " + " ".join(args)
        output = out.decode("utf-8", errors="replace").strip()
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "command": _redact(command),
            "output": _redact(output),
        }

    async def _ensure_authed_origin(path: str) -> None:
        remote = await _git(["remote", "get-url", "origin"], cwd=path)
        if not remote["ok"]:
            return
        url = remote["output"].strip()
        authed = _authed_repo_url(url)
        if authed != url:
            await _git(["remote", "set-url", "origin", authed], cwd=path)

    async def git_clone(repo_url: str, directory: str | None = None) -> dict[str, Any]:
        """Clone a repo into GITHUB_WORKDIR. Returns the local path on success."""
        target_parent = _workdir()
        name = directory or repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
        dest = target_parent / name
        result = await _git(["clone", _authed_repo_url(repo_url), str(dest)])
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
        name, email = _git_commit_identity()
        return await _git(
            ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message],
            cwd=path,
        )

    async def git_push(path: str, branch: str | None = None) -> dict[str, Any]:
        """Push the current (or named) branch, setting upstream if needed."""
        await _ensure_authed_origin(path)
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
