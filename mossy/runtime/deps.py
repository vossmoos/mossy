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
