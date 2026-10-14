"""Shared fixtures for the duty tests: no real lock directory, no OpenAI, a fake Wrike model."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _never_the_real_task_locks(tmp_path_factory, monkeypatch):
    """Every test takes its per-task locks in a throwaway directory, never the operator's."""
    monkeypatch.setenv("WRIKE_TASK_LOCK_DIR", str(tmp_path_factory.mktemp("task-locks")))


@pytest.fixture(autouse=True)
def _never_openai(monkeypatch):
    """No test reaches OpenAI: without a key the real ``_chat`` refuses before it builds a request,
    so a forgotten fake fails loudly instead of spending money."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


class _FakeWrikeModel:
    """Stands in for ``analyze_wrike_task._chat``. Records what it was sent.

    The summary it returns is deliberately NOT the Wrike title — ``Model summary of: <title>`` —
    so no test can pass by confusing the model's title with the task's own.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.raises = None
        self.reply = None

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.raises is not None:
            raise self.raises
        if self.reply is not None:
            return self.reply
        title = next((line[len("Task title: "):] for line in user.splitlines()
                      if line.startswith("Task title: ")), "")
        return json.dumps({"summary": f"Model summary of: {title}",
                           "problem": f"Model problem for: {title}"})


@pytest.fixture()
def fake_wrike_model(monkeypatch):
    """The model, faked, for every test that creates a Wrike issue."""
    name = "analyze_wrike_task"
    module = sys.modules.get(name)
    if module is None:
        path = _REPO_ROOT / "skills" / "gmail-wrike-tasks" / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    fake = _FakeWrikeModel()
    monkeypatch.setattr(module, "_chat", fake)
    return fake
