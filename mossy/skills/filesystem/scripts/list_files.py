"""List simple files in the repo-local data directory."""

from __future__ import annotations

import json
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main() -> None:
    data_dir = _repo_root() / "data"
    data_dir.mkdir(exist_ok=True)

    files = []
    for path in sorted(data_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        if path.name == ".gitkeep":
            continue
        files.append(
            {
                "filename": path.name,
                "bytes": path.stat().st_size,
                "modified_at": path.stat().st_mtime,
            }
        )

    print(json.dumps({"data_dir": str(data_dir), "files": files}, indent=2))


if __name__ == "__main__":
    main()
