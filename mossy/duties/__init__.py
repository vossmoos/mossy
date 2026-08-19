"""Auto-discovered proactive duties (package + optional repo-root `duties/`)."""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from pathlib import Path

from mossy.duties.base import Duty, EnqueueRequest, REGISTRY, register

logger = logging.getLogger(__name__)

__all__ = [
    "Duty",
    "EnqueueRequest",
    "REGISTRY",
    "discover_duties",
    "get_duty",
    "register",
]


def _load_package(pkg: object) -> None:
    paths = getattr(pkg, "__path__", None)
    name = getattr(pkg, "__name__", "")
    if not paths or not name:
        return
    for modinfo in pkgutil.iter_modules(paths):
        if modinfo.name == "base" or modinfo.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{name}.{modinfo.name}")
        except Exception:
            logger.exception("failed to import duty module %s.%s", name, modinfo.name)


def discover_duties(repo_root: Path | None = None) -> list[Duty]:
    """Import duty modules and return one instance per registered class."""
    import mossy.duties as system_pkg

    _load_package(system_pkg)

    root = repo_root
    if root is not None:
        root_s = str(root)
        if root_s not in sys.path:
            sys.path.insert(0, root_s)
        user_dir = root / "duties"
        if user_dir.is_dir() and (user_dir / "__init__.py").is_file():
            try:
                user_pkg = importlib.import_module("duties")
            except Exception:
                logger.exception("failed to import user duties package")
            else:
                _load_package(user_pkg)

    return [cls() for cls in REGISTRY.values()]


def get_duty(name: str, repo_root: Path | None = None) -> Duty:
    discover_duties(repo_root)
    cls = REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown duty: {name}")
    return cls()
