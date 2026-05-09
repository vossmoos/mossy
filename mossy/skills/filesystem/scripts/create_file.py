"""Create or replace a simple file in the repo-local data directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _validate_filename(filename: str) -> str:
    name = filename.strip()
    if not name:
        raise ValueError("filename is required")
    if name in {".", ".."}:
        raise ValueError("filename must be a file name, not a directory reference")
    if Path(name).name != name:
        raise ValueError("filename must not include subfolders or path separators")
    return name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filename", required=True)
    parser.add_argument("--content", required=True)
    args = parser.parse_args()

    filename = _validate_filename(args.filename)
    data_dir = _repo_root() / "data"
    data_dir.mkdir(exist_ok=True)

    path = data_dir / filename
    path.write_text(args.content, encoding="utf-8")

    print(
        json.dumps(
            {
                "filename": filename,
                "path": str(path),
                "bytes": path.stat().st_size,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
