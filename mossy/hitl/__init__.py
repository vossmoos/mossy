"""Human in the loop: a task waits for a person's approval before it enters the queue.

Media are auto-discovered like duties: modules of this package, plus an optional repo-root
``hitl/`` package. The core ships no medium, so nothing here does anything until a repo adds one.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from pathlib import Path

from mossy.hitl.base import (
    APPROVED,
    CANCELLED,
    PENDING,
    REGISTRY,
    REJECTED,
    Decision,
    Medium,
    register,
)

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVED",
    "CANCELLED",
    "Decision",
    "Medium",
    "PENDING",
    "REGISTRY",
    "REJECTED",
    "discover_media",
    "get_medium",
    "register",
]

#: Modules of this package that are the mechanism, not a medium.
_MECHANISM = frozenset({"base", "store", "flow"})


def _load_package(pkg: object) -> None:
    paths = getattr(pkg, "__path__", None)
    name = getattr(pkg, "__name__", "")
    if not paths or not name:
        return
    for modinfo in pkgutil.iter_modules(paths):
        if modinfo.name in _MECHANISM or modinfo.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{name}.{modinfo.name}")
        except Exception:
            logger.exception("failed to import HITL medium module %s.%s", name, modinfo.name)


def _is_configured(cls: type[Medium]) -> bool:
    try:
        return bool(cls.configured())
    except Exception:
        logger.exception("HITL medium %s failed while checking its configuration", cls.name)
        return False


def discover_media(repo_root: Path | None = None) -> dict[str, type[Medium]]:
    """Import medium modules and return the configured media by name, sorted."""
    import mossy.hitl as system_pkg

    _load_package(system_pkg)

    root = repo_root
    if root is not None:
        root_s = str(root)
        if root_s not in sys.path:
            sys.path.insert(0, root_s)
        user_dir = root / "hitl"
        if user_dir.is_dir() and (user_dir / "__init__.py").is_file():
            try:
                user_pkg = importlib.import_module("hitl")
            except Exception:
                logger.exception("failed to import user hitl package")
            else:
                _load_package(user_pkg)

    return {name: cls for name, cls in sorted(REGISTRY.items()) if _is_configured(cls)}


def get_medium(name: str, repo_root: Path | None = None) -> Medium:
    cls = discover_media(repo_root).get(name)
    if cls is None:
        raise ValueError(f"Unknown HITL medium: {name}")
    return cls()
