"""File sharing exposed as a Pydantic AI capability.

Working files live wherever a skill puts them (typically the repo ``data/``
folder). Those are not, and should not be, directly downloadable. This capability
provides the one explicit step that makes a file available to the user: it copies a
finished deliverable out of the repo into a single, isolated *share root*, and hands
back a protected ``download_url`` served by the HTTP channel.

Two roots, two jobs:
  - source root  (the repo) — where files are *read from* (e.g. ``data/<x>.zip``);
  - share root   (isolated) — where shared copies *live*, and the only place the
    download endpoint will serve from.

Keeping the share root separate from the working tree means "what is downloadable"
is an explicit allow-list (you shared it) rather than "anything Mossy ever wrote".
Zipping/among files is the filesystem capability's job; this capability only copies
already-existing files out for download.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

SHARE_ROOT_ENV = "MOSSY_SHARE_ROOT"
SHARE_MAX_STORAGE_ENV = "MOSSY_SHARE_MAX_STORAGE"
DEFAULT_SHARE_MAX_STORAGE_BYTES = 100 * 1024 * 1024
SHARE_DOWNLOAD_PREFIX = "/files"
DEFAULT_SHARE_DIRNAME = "shared"


def file_sharing_capability(repo_root: str | Path | None = None) -> Toolset:
    """Expose copy-to-share + download-link tools.

    Files are *read* from under ``repo_root`` (the working tree) and *written* into
    the isolated share root (``MOSSY_SHARE_ROOT`` when set, otherwise
    ``<repo_root>/shared``). Only files under the share root are downloadable.
    """
    source_root = _source_root(repo_root)
    root = share_root(repo_root)

    async def share_file(
        path: str,
        name: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Copy a finished file into the share root and return its download link.

        `path` is a file under the repo (e.g. `data/archive/20260620T....zip`).
        `name` optionally renames the shared copy (a simple file name, no folders).
        Returns the shared path, byte size, and the protected `download_url` to give
        the user.
        """
        src = _resolve_source(path, source_root)
        if not src.is_file():
            raise ValueError(f"path is not a file: {path}")
        dest_name = _validate_share_name(name) if name.strip() else src.name
        dest = (root / dest_name).resolve(strict=False)
        _ensure_within_root(dest, root)
        if dest.is_symlink():
            raise ValueError("refusing to overwrite a symlink in the share root")
        if dest.exists() and not overwrite:
            raise FileExistsError(
                f"a shared file named {dest_name} already exists; pass overwrite=true "
                f"or choose another name."
            )

        candidate_size = src.stat().st_size
        _enforce_storage(root, candidate_size, replacing=dest if dest.exists() else None)

        root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        relative = _relpath(dest, root)
        return {
            "ok": True,
            "shared_path": relative,
            "bytes": dest.stat().st_size,
            "download_url": share_download_url(relative),
            "note": "Give the user the download_url. It is served by the protected "
                    "HTTP endpoint and needs the same API key as chat.",
        }

    async def list_shared_files(recursive: bool = False, limit: int = 200) -> dict[str, Any]:
        """List files currently in the share root, each with its download link."""
        return list_shared(root=root, recursive=recursive, limit=limit)

    async def get_download_info(path: str) -> dict[str, Any]:
        """Return metadata and the protected download URL for one shared file."""
        file_path = resolve_relative_shared_file_path(path, root=root, must_exist=True)
        stat = file_path.stat()
        relative = _relpath(file_path, root)
        return {
            "shared_path": relative,
            "download_url": share_download_url(relative),
            "bytes": stat.st_size,
            "modified_at": stat.st_mtime,
        }

    async def unshare_file(path: str) -> dict[str, Any]:
        """Remove one file from the share root (revokes its download link)."""
        file_path = resolve_relative_shared_file_path(path, root=root, must_exist=True)
        if file_path.is_symlink():
            raise ValueError("refusing to delete symlinks")
        if not file_path.is_file():
            raise ValueError(f"path is not a file: {path}")
        size = file_path.stat().st_size
        relative = _relpath(file_path, root)
        file_path.unlink()
        return {"ok": True, "shared_path": relative, "bytes_deleted": size}

    return Toolset(
        FunctionToolset(
            [share_file, list_shared_files, get_download_info, unshare_file],
            id="file-sharing",
            instructions=(
                "Make finished files downloadable. share_file copies a file from the "
                "repo (e.g. a zip you built under data/) into the isolated share root "
                "and returns a protected download_url — give that URL to the user. "
                "list_shared_files / get_download_info report what is currently shared; "
                "unshare_file revokes a link. This capability never zips or edits files "
                "(use the filesystem capability for that) — it only copies existing "
                "files out for download. The download endpoint is API-key protected, so "
                "browser clients must fetch it with the same bearer key used for chat."
            ),
        )
    )


def _source_root(repo_root: str | Path | None = None) -> Path:
    return Path(repo_root or Path.cwd()).resolve(strict=False)


def share_root(repo_root: str | Path | None = None) -> Path:
    configured = os.environ.get(SHARE_ROOT_ENV)
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            root = _source_root(repo_root) / root
    else:
        root = _source_root(repo_root) / DEFAULT_SHARE_DIRNAME
    root = root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_source(path: str, source_root: Path) -> Path:
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = source_root / candidate
    resolved = candidate.resolve(strict=False)
    _ensure_within_root(resolved, source_root)
    if not resolved.exists():
        raise FileNotFoundError(f"path does not exist: {path}")
    return resolved


def resolve_shared_path(path: str, *, root: Path) -> Path:
    raw = (path or "").strip()
    candidate = Path(raw).expanduser() if raw else root
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    _ensure_within_root(resolved, root)
    return resolved


def resolve_relative_shared_file_path(
    path: str,
    *,
    root: Path,
    must_exist: bool = False,
) -> Path:
    raw = (path or "").strip()
    if not raw:
        raise ValueError("path is required")
    if Path(raw).expanduser().is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError("path must be relative to the share root")
    parts = raw.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path must not be absolute or contain '.'/'..' segments")
    candidate = root / Path(*PurePosixPath(*parts).parts)
    if candidate.is_symlink():
        raise ValueError("refusing to resolve symlinks")
    resolved = candidate.resolve(strict=False)
    _ensure_within_root(resolved, root)
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"path does not exist: {path}")
    if resolved.exists() and not resolved.is_file():
        raise ValueError(f"path is not a file: {path}")
    return resolved


def share_relative_path(path: Path, *, root: Path) -> str:
    return _relpath(path, root)


def share_download_url(relative_path: str) -> str:
    return f"{SHARE_DOWNLOAD_PREFIX}/{quote(relative_path.lstrip('/'), safe='/')}"


def list_shared(
    directory: str = "",
    *,
    root: Path,
    recursive: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    base = resolve_shared_path(directory, root=root)
    if not base.exists():
        raise FileNotFoundError(f"directory does not exist: {directory}")
    if not base.is_dir():
        raise ValueError(f"path is not a directory: {directory}")

    iterator = base.rglob("*") if recursive else base.iterdir()
    items = sorted(iterator, key=lambda item: str(item))
    visible: list[Path] = []
    for item in items:
        try:
            _ensure_within_root(item.resolve(strict=False), root)
        except ValueError:
            continue
        if not item.is_file():
            continue
        visible.append(item)

    entries = []
    for item in visible[:limit]:
        relative = _relpath(item, root)
        stat = item.stat()
        entries.append(
            {
                "path": relative,
                "name": item.name,
                "is_dir": False,
                "is_file": True,
                "bytes": stat.st_size,
                "modified_at": stat.st_mtime,
                "download_url": share_download_url(relative),
            }
        )
    return {
        "root": str(root),
        "directory": _relpath(base, root) if base != root else "",
        "entries": entries,
        "total_entries": len(visible),
        "truncated": len(visible) > limit,
        "storage": _storage_summary(root),
    }


def share_max_storage_bytes() -> int:
    raw = (os.environ.get(SHARE_MAX_STORAGE_ENV) or "").strip()
    if not raw:
        return DEFAULT_SHARE_MAX_STORAGE_BYTES
    return _parse_storage_size(raw)


def _enforce_storage(root: Path, candidate: int, *, replacing: Path | None) -> None:
    max_storage = share_max_storage_bytes()
    exclude = {replacing.resolve(strict=False)} if replacing else set()
    current = _storage_bytes(root, exclude_paths=exclude)
    if current + candidate > max_storage:
        raise ValueError(
            "share storage limit exceeded: "
            f"current={current} bytes, candidate={candidate} bytes, max={max_storage} bytes. "
            f"Unshare an older file first."
        )


def _storage_bytes(root: Path, *, exclude_paths: set[Path] | None = None) -> int:
    excluded = {p.resolve(strict=False) for p in (exclude_paths or set())}
    total = 0
    for path in root.rglob("*"):
        resolved = path.resolve(strict=False)
        if resolved in excluded:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
    return total


def _storage_summary(root: Path) -> dict[str, int]:
    used = _storage_bytes(root)
    max_storage = share_max_storage_bytes()
    return {
        "used_bytes": used,
        "max_bytes": max_storage,
        "remaining_bytes": max(max_storage - used, 0),
    }


def _validate_share_name(name: str) -> str:
    n = name.strip()
    if not n or n in {".", ".."} or Path(n).name != n:
        raise ValueError("name must be a simple file name without path separators")
    return n


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path)


def _ensure_within_root(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path must be inside {root}") from exc


def _parse_storage_size(raw: str) -> int:
    normalized = raw.strip().lower().replace("_", "").replace(" ", "")
    units = {"b": 1, "kb": 1024, "k": 1024, "mb": 1024**2, "m": 1024**2,
             "gb": 1024**3, "g": 1024**3}
    for suffix, multiplier in sorted(units.items(), key=lambda i: len(i[0]), reverse=True):
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)]
            break
    else:
        number, multiplier = normalized, 1
    try:
        value = float(number)
    except ValueError as exc:
        raise ValueError(f"{SHARE_MAX_STORAGE_ENV} must be a byte count or size like 100MB") from exc
    if value <= 0:
        raise ValueError(f"{SHARE_MAX_STORAGE_ENV} must be greater than zero")
    return int(value * multiplier)
