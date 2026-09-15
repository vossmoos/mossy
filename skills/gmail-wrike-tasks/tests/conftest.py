"""Shared fixtures for the gmail-wrike-tasks tests: no real lock directory, no OpenAI, a fake model."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _never_the_real_task_locks(tmp_path_factory, monkeypatch):
    """Every test takes its per-task locks in a throwaway directory (the per-task lock).

    ``run(apply=True)`` in the create step and the comment copy takes a lock under
    ``~/.local/share/mossy/task-locks`` by default. Before this fixture the
    suite created a real lock file there — the operator's directory, touched by tests. Autouse
    and in the conftest, so a new test file cannot forget it.
    """
    monkeypatch.setenv("WRIKE_TASK_LOCK_DIR", str(tmp_path_factory.mktemp("task-locks")))


import json  # noqa: E402
import importlib.util  # noqa: E402


@pytest.fixture(autouse=True)
def _never_openai(monkeypatch):
    """No test reaches OpenAI. The Wrike summary (the Wrike → Jira flow) is evaluated by a model, and the real
    ``_chat`` refuses before it builds a request when there is no key — so removing the key for
    every test makes a forgotten fake fail loudly instead of spending money or reaching the
    network. Tests of the real client put a key back and replace ``urlopen``."""
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
        path = _SKILL / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    fake = _FakeWrikeModel()
    monkeypatch.setattr(module, "_chat", fake)
    return fake
