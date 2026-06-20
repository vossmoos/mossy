"""Zip archive operations exposed as a Pydantic AI capability."""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

ARCHIVE_ROOT_ENV = "MOSSY_ARCHIVE_ROOT"
ARCHIVE_MAX_STORAGE_ENV = "ARCHIVE_MAX_STORAGE"
DEFAULT_ARCHIVE_MAX_STORAGE_BYTES = 100 * 1024 * 1024
ARCHIVE_DOWNLOAD_PREFIX = "/archive/files"


def archives_capability(base_dir: str | Path | None = None) -> Toolset:
    """Expose safe zip/unzip operations for the `archives` skill.

    Paths are resolved under MOSSY_ARCHIVE_ROOT when set, otherwise under `base_dir`
    or the current working directory. Archive extraction rejects unsafe member names
    and verifies every target stays inside the destination directory.
    """

    root = archive_root(base_dir)

    def _resolve_path(path: str, *, must_exist: bool = False) -> Path:
        if not path.strip():
            raise ValueError("path is required")
        resolved = resolve_archive_path(path, root=root)
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"path does not exist: {path}")
        return resolved

    async def create_zip_archive(
        output_path: str,
        source_paths: list[str],
        overwrite: bool = False,
        include_root: bool = True,
    ) -> dict[str, Any]:
        """Create a zip archive from one or more files/folders under the archive root."""
        if not source_paths:
            raise ValueError("source_paths must include at least one file or folder")

        output = _resolve_path(output_path)
        ensure_zip_file_path(output, label="output_path")
        if output.exists() and not overwrite:
            raise FileExistsError(f"archive already exists: {output_path}")
        output.parent.mkdir(parents=True, exist_ok=True)

        sources = [_resolve_path(path, must_exist=True) for path in source_paths]
        entries, skipped = _collect_zip_entries(sources, output, include_root=include_root)
        names = [entry[1] for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError("source paths produce duplicate archive entries")

        excluded_output = output.resolve(strict=False)
        max_storage = archive_max_storage_bytes()
        current_size = archive_storage_bytes(root, exclude_paths={excluded_output})
        if current_size >= max_storage:
            raise ValueError(
                "archive storage limit exceeded: "
                f"current={current_size} bytes, candidate=unknown, max={max_storage} bytes"
            )

        temp_path = _temporary_zip_path(output)
        try:
            _write_zip_archive(temp_path, entries)
            candidate_size = temp_path.stat().st_size
            current_size = archive_storage_bytes(root, exclude_paths={excluded_output, temp_path})
            projected_size = current_size + candidate_size
            if projected_size > max_storage:
                raise ValueError(
                    "archive storage limit exceeded: "
                    f"current={current_size} bytes, candidate={candidate_size} bytes, "
                    f"max={max_storage} bytes"
                )
            temp_path.replace(output)
        finally:
            temp_path.unlink(missing_ok=True)

        return {
            "ok": True,
            "archive_path": str(output),
            "sources": [str(path) for path in sources],
            "files_added": sum(1 for path, _, is_dir in entries if path.is_file() and not is_dir),
            "directories_added": sum(1 for _, _, is_dir in entries if is_dir),
            "skipped": skipped,
            "bytes": output.stat().st_size,
            "storage": archive_storage_summary(root),
        }

    async def extract_zip_archive(
        zip_path: str,
        destination_dir: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Extract a zip archive into a destination directory under the archive root."""
        archive_path = _resolve_path(zip_path, must_exist=True)
        ensure_zip_file_path(archive_path, label="zip_path")
        destination = _resolve_path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)

        files: list[str] = []
        directories: list[str] = []
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                target = _safe_member_target(destination, info.filename)
                if target is None:
                    continue
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories.append(str(target))
                    continue
                if target.exists() and not overwrite:
                    raise FileExistsError(f"target already exists: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
                files.append(str(target))

        return {
            "ok": True,
            "archive_path": str(archive_path),
            "destination_dir": str(destination),
            "files_extracted": files,
            "directories_created": directories,
        }

    async def list_zip_archive(zip_path: str, limit: int = 200) -> dict[str, Any]:
        """List entries in a zip archive without extracting it."""
        archive_path = _resolve_path(zip_path, must_exist=True)
        ensure_zip_file_path(archive_path, label="zip_path")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            entries = [
                {
                    "name": info.filename,
                    "bytes": info.file_size,
                    "compressed_bytes": info.compress_size,
                    "is_dir": info.is_dir(),
                }
                for info in infos[:limit]
            ]

        return {
            "archive_path": str(archive_path),
            "entries": entries,
            "total_entries": len(infos),
            "truncated": len(infos) > limit,
        }

    async def list_downloadable_files(
        directory: str = "",
        recursive: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        """List files under the archive root that can be downloaded over HTTP."""
        return list_archive_files(directory, root=root, recursive=recursive, limit=limit)

    async def get_download_info(path: str) -> dict[str, Any]:
        """Return metadata and the protected HTTP download path for one file."""
        file_path = _resolve_path(path, must_exist=True)
        if not file_path.is_file():
            raise ValueError(f"path is not a file: {path}")
        ensure_zip_file_path(file_path, label="path")
        stat = file_path.stat()
        relative_path = archive_relative_path(file_path, root=root)
        return {
            "path": relative_path,
            "download_url": archive_download_url(relative_path),
            "bytes": stat.st_size,
            "modified_at": stat.st_mtime,
        }

    async def delete_archive_file(path: str) -> dict[str, Any]:
        """Delete one file under the archive root using a relative, non-traversing path."""
        file_path = resolve_relative_archive_file_path(path, root=root, must_exist=True)
        ensure_zip_file_path(file_path, label="path")
        stat = file_path.stat()
        file_path.unlink()
        return {
            "ok": True,
            "path": archive_relative_path(file_path, root=root),
            "bytes_deleted": stat.st_size,
        }

    return Toolset(
        FunctionToolset(
            [
                create_zip_archive,
                extract_zip_archive,
                list_zip_archive,
                list_downloadable_files,
                get_download_info,
                delete_archive_file,
            ],
            id="archives",
            instructions=(
                "Zip archive tools for local files under the configured archive root. "
                "Use create_zip_archive to zip files or folders, extract_zip_archive to "
                "unzip archives, list_zip_archive to inspect an archive before extracting, "
                "and list_downloadable_files/get_download_info when the user wants files "
                "available through the protected web download endpoint. Use delete_archive_file "
                "only for explicit requests to delete a file from the archive root."
            ),
        )
    )


def archive_root(base_dir: str | Path | None = None) -> Path:
    configured = os.environ.get(ARCHIVE_ROOT_ENV)
    root = Path(configured).expanduser() if configured else Path(base_dir or Path.cwd())
    root = root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_archive_path(path: str, *, root: Path) -> Path:
    raw = path.strip()
    candidate = Path(raw).expanduser() if raw else root
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    _ensure_within_root(resolved, root)
    return resolved


def resolve_relative_archive_file_path(
    path: str,
    *,
    root: Path,
    must_exist: bool = False,
) -> Path:
    raw = path.strip()
    if not raw:
        raise ValueError("path is required")
    if Path(raw).expanduser().is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError("path must be relative to the archive root")

    raw_parts = raw.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("path must not be absolute or contain '.'/'..' segments")

    member = PurePosixPath(*raw_parts)
    candidate = root / Path(*member.parts)
    if candidate.is_symlink():
        raise ValueError("refusing to delete symlinks")
    resolved = candidate.resolve(strict=False)
    _ensure_within_root(resolved, root)

    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"path does not exist: {path}")
    if resolved.exists() and not resolved.is_file():
        raise ValueError(f"path is not a file: {path}")
    return resolved


def archive_relative_path(path: Path, *, root: Path) -> str:
    return _zip_name(path.relative_to(root))


def archive_download_url(relative_path: str) -> str:
    return f"{ARCHIVE_DOWNLOAD_PREFIX}/{quote(relative_path.lstrip('/'), safe='/')}"


def is_zip_file_path(path: Path) -> bool:
    return path.name.lower().endswith(".zip")


def ensure_zip_file_path(path: Path, *, label: str = "path") -> None:
    if not is_zip_file_path(path):
        raise ValueError(f"{label} must point to a .zip file")


def archive_max_storage_bytes() -> int:
    raw = (os.environ.get(ARCHIVE_MAX_STORAGE_ENV) or "").strip()
    if not raw:
        return DEFAULT_ARCHIVE_MAX_STORAGE_BYTES
    return _parse_storage_size(raw)


def archive_storage_bytes(root: Path, *, exclude_paths: set[Path] | None = None) -> int:
    excluded = {path.resolve(strict=False) for path in exclude_paths or set()}
    total = 0
    for path in root.rglob("*"):
        resolved = path.resolve(strict=False)
        if resolved in excluded:
            continue
        try:
            _ensure_within_root(resolved, root)
        except ValueError:
            continue
        if path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
    return total


def archive_storage_summary(root: Path) -> dict[str, int]:
    used = archive_storage_bytes(root)
    max_storage = archive_max_storage_bytes()
    return {
        "used_bytes": used,
        "max_bytes": max_storage,
        "remaining_bytes": max(max_storage - used, 0),
    }


def list_archive_files(
    directory: str = "",
    *,
    root: Path,
    recursive: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be at least 1")

    base = resolve_archive_path(directory, root=root)
    if not base.exists():
        raise FileNotFoundError(f"directory does not exist: {directory}")
    if not base.is_dir():
        raise ValueError(f"path is not a directory: {directory}")

    iterator = base.rglob("*") if recursive else base.iterdir()
    items = sorted(iterator, key=lambda item: str(item.relative_to(root)))
    visible_items: list[Path] = []
    for item in items:
        try:
            _ensure_within_root(item.resolve(strict=False), root)
        except ValueError:
            continue
        if not item.is_file() or not is_zip_file_path(item):
            continue
        visible_items.append(item)

    entries = []
    for item in visible_items[:limit]:
        relative_path = archive_relative_path(item, root=root)
        is_file = item.is_file()
        stat = item.stat()
        entries.append(
            {
                "path": relative_path,
                "name": item.name,
                "is_dir": item.is_dir(),
                "is_file": is_file,
                "bytes": stat.st_size if is_file else None,
                "modified_at": stat.st_mtime,
                "download_url": archive_download_url(relative_path) if is_file else None,
            }
        )

    return {
        "root": str(root),
        "directory": archive_relative_path(base, root=root) if base != root else "",
        "entries": entries,
        "total_entries": len(visible_items),
        "truncated": len(visible_items) > limit,
        "storage": archive_storage_summary(root),
    }


def _ensure_within_root(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path must be inside {root}") from exc


def _collect_zip_entries(
    sources: list[Path],
    output: Path,
    *,
    include_root: bool,
) -> tuple[list[tuple[Path, str, bool]], list[str]]:
    entries: list[tuple[Path, str, bool]] = []
    skipped: list[str] = []

    for source in sources:
        if source.is_symlink():
            skipped.append(str(source))
            continue
        if source.is_file():
            if source.resolve(strict=False) != output.resolve(strict=False):
                entries.append((source, source.name, False))
            continue

        if not source.is_dir():
            skipped.append(str(source))
            continue

        base = source.parent if include_root else source
        children = sorted(source.rglob("*"), key=lambda item: str(item.relative_to(source)))
        if not children:
            if include_root:
                entries.append((source, _zip_name(source.relative_to(base)), True))
            continue

        for child in children:
            if child.is_symlink():
                skipped.append(str(child))
                continue
            if child.resolve(strict=False) == output.resolve(strict=False):
                skipped.append(str(child))
                continue
            arcname = _zip_name(child.relative_to(base))
            entries.append((child, arcname, child.is_dir()))

    return entries, skipped


def _temporary_zip_path(output: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp:
        return Path(temp.name)


def _write_zip_archive(
    output: Path,
    entries: list[tuple[Path, str, bool]],
) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, arcname, is_dir in entries:
            if is_dir:
                info = zipfile.ZipInfo(arcname.rstrip("/") + "/")
                archive.writestr(info, b"")
            else:
                archive.write(path, arcname)


def _parse_storage_size(raw: str) -> int:
    normalized = raw.strip().lower().replace("_", "").replace(" ", "")
    units = {
        "b": 1,
        "kb": 1024,
        "k": 1024,
        "mb": 1024 * 1024,
        "m": 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
        "g": 1024 * 1024 * 1024,
    }
    for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)]
            break
    else:
        number = normalized
        multiplier = 1

    try:
        value = float(number)
    except ValueError as exc:
        raise ValueError(f"{ARCHIVE_MAX_STORAGE_ENV} must be a byte count or size like 100MB") from exc
    if value <= 0:
        raise ValueError(f"{ARCHIVE_MAX_STORAGE_ENV} must be greater than zero")
    return int(value * multiplier)


def _zip_name(path: Path) -> str:
    return PurePosixPath(*path.parts).as_posix()


def _safe_member_target(destination: Path, member_name: str) -> Path | None:
    if not member_name:
        return None

    member = PurePosixPath(member_name.replace("\\", "/"))
    if member.is_absolute() or any(part in {"", ".", ".."} for part in member.parts):
        raise ValueError(f"unsafe archive member path: {member_name}")

    target = (destination / Path(*member.parts)).resolve(strict=False)
    try:
        target.relative_to(destination)
    except ValueError as exc:
        raise ValueError(f"archive member escapes destination: {member_name}") from exc
    return target
