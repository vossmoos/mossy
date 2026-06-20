"""Filesystem read/write operations exposed as a Pydantic AI capability.

The Mossy worker agent can run skill scripts and zip/unzip archives, but it had
no general way to *create* a file. Skills like `simxp-ehr` ask the model to author
content (e.g. a patient YAML) and persist it incrementally — header first, then one
section appended per write. Without a write tool the model stalls ("the YAML
doesn't exist yet") because it has no way to put its authored text on disk.

This capability restores that path safely: every path is resolved under a single
root (``MOSSY_FS_ROOT`` when set, otherwise the repo root passed in). Traversal
outside the root, absolute escapes, and symlinks are rejected. The tools do not
invent content — they only persist, read back, list, and archive what the model
(or a script) produced.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

FS_ROOT_ENV = "MOSSY_FS_ROOT"
FS_MAX_WRITE_ENV = "MOSSY_FS_MAX_WRITE"
DEFAULT_MAX_WRITE_BYTES = 5 * 1024 * 1024  # 5 MiB per call — generous for text records
READ_PREVIEW_LIMIT = 200_000  # cap read_file payloads so a huge file can't blow context


def filesystem_capability(base_dir: str | Path | None = None) -> Toolset:
    """Expose safe read/write/append/list operations under a single root.

    Paths are resolved under ``MOSSY_FS_ROOT`` when set, otherwise under ``base_dir``
    (typically the repo root) or the current working directory. Every resolved path
    must stay inside that root; symlinks and ``..`` escapes are refused.
    """

    root = fs_root(base_dir)

    def _resolve(path: str, *, must_exist: bool = False) -> Path:
        resolved = resolve_fs_path(path, root=root)
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"path does not exist: {path}")
        return resolved

    def _guard_write_target(target: Path) -> None:
        if target.is_symlink():
            raise ValueError(f"refusing to write through a symlink: {target}")
        if target.exists() and target.is_dir():
            raise IsADirectoryError(f"path is a directory, not a file: {target}")

    async def write_file(
        path: str,
        content: str,
        overwrite: bool = False,
        make_parents: bool = True,
    ) -> dict[str, Any]:
        """Create a file (under the root) and write `content` to it as UTF-8 text.

        Use this for the first write of a new file — e.g. the header of a patient
        YAML you are authoring. By default it refuses to clobber an existing file;
        pass overwrite=True to replace one, or use `append_file` to add to it.
        """
        _check_size(content)
        target = _resolve(path)
        _guard_write_target(target)
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"file already exists: {path}. Pass overwrite=true to replace it, "
                f"or use append_file to add to it."
            )
        if make_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _stat_summary(target, root, action="wrote")

    async def append_file(
        path: str,
        content: str,
        make_parents: bool = True,
    ) -> dict[str, Any]:
        """Append `content` to a file under the root, creating it if missing.

        This is the workhorse for incremental authoring: write the header with
        write_file, then append one section per call so no single write is large
        enough to time out.
        """
        _check_size(content)
        target = _resolve(path)
        _guard_write_target(target)
        if make_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        with target.open("a", encoding="utf-8") as fh:
            fh.write(content)
        summary = _stat_summary(target, root, action="appended")
        summary["created"] = not existed
        return summary

    async def read_file(path: str, max_bytes: int = READ_PREVIEW_LIMIT) -> dict[str, Any]:
        """Read a UTF-8 text file under the root (truncated to max_bytes)."""
        target = _resolve(path, must_exist=True)
        if not target.is_file():
            raise ValueError(f"path is not a file: {path}")
        limit = max(1, min(int(max_bytes), READ_PREVIEW_LIMIT))
        data = target.read_bytes()
        truncated = len(data) > limit
        text = data[:limit].decode("utf-8", errors="replace")
        return {
            "path": _relpath(target, root),
            "bytes": len(data),
            "truncated": truncated,
            "content": text,
        }

    async def list_dir(directory: str = "", recursive: bool = False) -> dict[str, Any]:
        """List files and folders under a directory within the root."""
        base = _resolve(directory, must_exist=True)
        if not base.is_dir():
            raise ValueError(f"path is not a directory: {directory}")
        iterator = base.rglob("*") if recursive else base.iterdir()
        entries: list[dict[str, Any]] = []
        for item in sorted(iterator, key=lambda p: str(p)):
            try:
                _ensure_within_root(item.resolve(strict=False), root)
            except ValueError:
                continue
            is_file = item.is_file()
            entries.append(
                {
                    "path": _relpath(item, root),
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "is_file": is_file,
                    "bytes": item.stat().st_size if is_file else None,
                }
            )
        return {"root": str(root), "directory": _relpath(base, root), "entries": entries}

    async def delete_file(path: str) -> dict[str, Any]:
        """Delete a single file under the root (not directories, not symlinks)."""
        target = _resolve(path, must_exist=True)
        if target.is_symlink():
            raise ValueError("refusing to delete symlinks")
        if not target.is_file():
            raise ValueError(f"path is not a file: {path}")
        size = target.stat().st_size
        target.unlink()
        return {"ok": True, "path": _relpath(target, root), "bytes_deleted": size}

    async def zip_files(
        output_path: str,
        source_paths: list[str],
        overwrite: bool = False,
        include_root: bool = True,
    ) -> dict[str, Any]:
        """Create a `.zip` from one or more files/folders under the root.

        `output_path` must end in `.zip`. For folders, keep include_root=true to
        preserve the top folder name inside the archive. This only packages files
        that already exist on disk — it does not produce download links (use the
        file-sharing capability's share_file for that).
        """
        if not source_paths:
            raise ValueError("source_paths must include at least one file or folder")
        output = _resolve(output_path)
        if not is_zip_file_path(output):
            raise ValueError("output_path must end in .zip")
        if output.exists() and not overwrite:
            raise FileExistsError(f"archive already exists: {output_path}")
        output.parent.mkdir(parents=True, exist_ok=True)

        sources = [_resolve(p, must_exist=True) for p in source_paths]
        entries, skipped = _collect_zip_entries(sources, output, include_root=include_root)
        names = [name for _, name, _ in entries]
        if len(names) != len(set(names)):
            raise ValueError("source paths produce duplicate archive entries")
        _write_zip_archive(output, entries)
        return {
            "ok": True,
            "path": _relpath(output, root),
            "files_added": sum(1 for p, _, is_dir in entries if p.is_file() and not is_dir),
            "directories_added": sum(1 for _, _, is_dir in entries if is_dir),
            "skipped": skipped,
            "bytes": output.stat().st_size,
        }

    async def unzip_file(
        zip_path: str,
        destination_dir: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Extract a `.zip` into a destination directory under the root."""
        archive_path = _resolve(zip_path, must_exist=True)
        if not is_zip_file_path(archive_path):
            raise ValueError("zip_path must end in .zip")
        destination = _resolve(destination_dir)
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
                    directories.append(_relpath(target, root))
                    continue
                if target.exists() and not overwrite:
                    raise FileExistsError(f"target already exists: {_relpath(target, root)}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                files.append(_relpath(target, root))
        return {
            "ok": True,
            "path": _relpath(archive_path, root),
            "destination_dir": _relpath(destination, root),
            "files_extracted": files,
            "directories_created": directories,
        }

    async def list_zip(zip_path: str, limit: int = 200) -> dict[str, Any]:
        """List entries in a `.zip` without extracting it."""
        archive_path = _resolve(zip_path, must_exist=True)
        if not is_zip_file_path(archive_path):
            raise ValueError("zip_path must end in .zip")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            entries = [
                {
                    "name": info.filename,
                    "bytes": info.file_size,
                    "is_dir": info.is_dir(),
                }
                for info in infos[:limit]
            ]
        return {
            "path": _relpath(archive_path, root),
            "entries": entries,
            "total_entries": len(infos),
            "truncated": len(infos) > limit,
        }

    return Toolset(
        FunctionToolset(
            [
                write_file,
                append_file,
                read_file,
                list_dir,
                delete_file,
                zip_files,
                unzip_file,
                list_zip,
            ],
            id="filesystem",
            instructions=(
                "Read, write, and archive files under the configured filesystem root "
                "(the repo). Use write_file for the first write of a new file and "
                "append_file to add sections incrementally; read_file and list_dir to "
                "inspect what exists. When a skill tells you to author a file (e.g. a "
                "patient YAML), create it with these tools — never report that the file "
                "'does not exist yet' as a blocker. zip_files/unzip_file/list_zip handle "
                "`.zip` archives in place; they do NOT create download links — for that, "
                "copy the file out with the file-sharing capability's share_file. "
                "delete_file is only for explicit delete requests. All paths are relative "
                "to the root; '..' and symlinks are rejected."
            ),
        )
    )


def fs_root(base_dir: str | Path | None = None) -> Path:
    configured = os.environ.get(FS_ROOT_ENV)
    root = Path(configured).expanduser() if configured else Path(base_dir or Path.cwd())
    root = root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_fs_path(path: str, *, root: Path) -> Path:
    raw = (path or "").strip()
    candidate = Path(raw).expanduser() if raw else root
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    _ensure_within_root(resolved, root)
    return resolved


def _check_size(content: str) -> None:
    max_bytes = _max_write_bytes()
    size = len(content.encode("utf-8"))
    if size > max_bytes:
        raise ValueError(
            f"write rejected: {size} bytes exceeds limit {max_bytes}. "
            f"Author the file in smaller sections with append_file."
        )


def _max_write_bytes() -> int:
    raw = (os.environ.get(FS_MAX_WRITE_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_WRITE_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{FS_MAX_WRITE_ENV} must be an integer byte count") from exc
    if value <= 0:
        raise ValueError(f"{FS_MAX_WRITE_ENV} must be greater than zero")
    return value


def _stat_summary(target: Path, root: Path, *, action: str) -> dict[str, Any]:
    stat = target.stat()
    return {
        "ok": True,
        "action": action,
        "path": _relpath(target, root),
        "bytes": stat.st_size,
    }


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


def is_zip_file_path(path: Path) -> bool:
    return path.name.lower().endswith(".zip")


def _zip_name(path: Path) -> str:
    return PurePosixPath(*path.parts).as_posix()


def _collect_zip_entries(
    sources: list[Path],
    output: Path,
    *,
    include_root: bool,
) -> tuple[list[tuple[Path, str, bool]], list[str]]:
    entries: list[tuple[Path, str, bool]] = []
    skipped: list[str] = []
    out_resolved = output.resolve(strict=False)

    for source in sources:
        if source.is_symlink():
            skipped.append(str(source))
            continue
        if source.is_file():
            if source.resolve(strict=False) != out_resolved:
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
            if child.resolve(strict=False) == out_resolved:
                skipped.append(str(child))
                continue
            entries.append((child, _zip_name(child.relative_to(base)), child.is_dir()))

    return entries, skipped


def _write_zip_archive(output: Path, entries: list[tuple[Path, str, bool]]) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, arcname, is_dir in entries:
            if is_dir:
                archive.writestr(zipfile.ZipInfo(arcname.rstrip("/") + "/"), b"")
            else:
                archive.write(path, arcname)


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
