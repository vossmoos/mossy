"""Shell execution capability — lets agents run commands in a working directory.

Exposes a single `run_command` tool that shells out to bash with a configurable
timeout and working directory. Intended for code-writing workflows: grep, test
runners, linters, build commands, and similar.

Env:
    MOSSY_SHELL_WORKDIR   Base directory for relative `cwd` values (default: CWD).
    MOSSY_SHELL_TIMEOUT   Default timeout in seconds (default: 60).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.capabilities.toolset import Toolset


def shell_capability() -> Toolset:
    """Return a toolset with a single `run_command` tool."""

    def _base_dir() -> Path:
        base = os.environ.get("MOSSY_SHELL_WORKDIR") or os.environ.get("GITHUB_WORKDIR") or str(Path.cwd())
        return Path(base).expanduser().resolve()

    def _default_timeout() -> int:
        try:
            return int(os.environ.get("MOSSY_SHELL_TIMEOUT", "60"))
        except ValueError:
            return 60

    async def run_command(
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
    ) -> dict:
        """Run a shell command and return its output.

        Args:
            command: The bash command to run (passed to `bash -c`).
            cwd: Working directory. Absolute path, or relative to MOSSY_SHELL_WORKDIR.
                 Defaults to MOSSY_SHELL_WORKDIR (or GITHUB_WORKDIR).
            timeout: Seconds before the command is killed. Defaults to MOSSY_SHELL_TIMEOUT (60).

        Returns a dict with keys:
            ok (bool), exit_code (int), stdout (str), stderr (str), command (str), cwd (str).
        """
        base = _base_dir()
        if cwd:
            resolved = Path(cwd)
            if not resolved.is_absolute():
                resolved = base / resolved
        else:
            resolved = base

        limit = timeout if timeout is not None else _default_timeout()

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", command,
                cwd=str(resolved),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=limit
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {
                    "ok": False,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Command timed out after {limit}s.",
                    "command": command,
                    "cwd": str(resolved),
                }
        except FileNotFoundError:
            return {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Working directory not found: {resolved}",
                "command": command,
                "cwd": str(resolved),
            }

        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout_bytes.decode("utf-8", errors="replace").strip(),
            "stderr": stderr_bytes.decode("utf-8", errors="replace").strip(),
            "command": command,
            "cwd": str(resolved),
        }

    return Toolset(
        FunctionToolset(
            [run_command],
            id="shell",
            instructions=(
                "Shell execution tool. Use `run_command` to run bash commands in a working "
                "directory — grep, test runners, linters, build commands, pip/npm installs, etc. "
                "`cwd` can be an absolute path or relative to MOSSY_SHELL_WORKDIR. "
                "Always check `ok` and `exit_code` in the result. "
                "For large outputs, pipe through `head` or `grep` to keep results concise."
            ),
        )
    )
