"""Dependencies passed to Pydantic AI agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mossy.runtime.models import Task

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime


@dataclass
class RuntimeDeps:
    runtime: "Runtime"
    task: Task | None = None
    #: Who is asking, for per-user skill access. ``None`` is a trusted operator (CLI, duties,
    #: ``/run``). A set, even an empty one, is a chat caller and restricted skills are enforced.
    caller_ids: frozenset[str] | None = None
