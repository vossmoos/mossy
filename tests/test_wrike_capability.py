"""The read-only Wrike capability in the core, offline.

Four properties carry the weight, each with a mutation:

* **It answers exactly as the skill's client does.** The client is ported, not imported — the core
  cannot depend on a skill that can be removed — so there are two. ``TestTheTwoClientsAgree`` runs
  both against the same fake Wrike in five situations and requires the same envelope, the same URL,
  the same bearer header and the same verb. Nothing like it exists for Gmail or Jira.
* **Without ``WRIKE_TOKEN`` there is nothing, and no traffic.**
* **It exposes no write**, checked against the functions the toolset really registers and by
  calling every one of them — never by reading a docstring.
* **It is registered in ``core.py``.**

Nothing here touches the network: ``urlopen`` is replaced for every test by a fixture that fails.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import io
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mossy.capabilities import wrike as core_wrike
from mossy.runtime.core import Runtime

_ROOT = Path(__file__).resolve().parents[1]
_SKILL_CLIENT = _ROOT / "skills" / "gmail-wrike-tasks" / "scripts" / "wrike_api.py"
_spec = importlib.util.spec_from_file_location("wrike_api", _SKILL_CLIENT)
skill_wrike = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill_wrike)

TASK_ID = "MAAAAAEXmain"
READS = ("get_task", "get_task_comments")


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def getcode(self):
        return 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Transport:
    """Records every request, and answers with a payload or raises what the test decided."""

    def __init__(self, payload=None, status=None):
        self.requests: list[tuple[str, str, str]] = []
        self.payload = payload
        self.status = status

    def __call__(self, request, timeout=None):
        self.requests.append((request.get_method(), request.full_url,
                              request.headers.get("Authorization", "")))
        if self.status is not None:
            raise urllib.error.HTTPError(request.full_url, self.status, "refused", {},
                                         io.BytesIO(b'{"error": "refused"}'))
        return _Response(self.payload)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("this test reached the network; install a _Transport instead")
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def _install(monkeypatch, transport):
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    return transport


def _outcome(client, read, task_id):
    """The envelope, or — for a refusal — what the error says about itself."""
    try:
        return ("ok", getattr(client, read)(task_id))
    except Exception as exc:  # noqa: BLE001 - the comparison is the point
        return ("raised", type(exc).__name__, getattr(exc, "status", None), str(exc))


# ------------------------------------------------------------------------------ parity
CASES = {
    "a task with rows": dict(payload={"kind": "tasks", "data": [
        {"id": TASK_ID, "title": "t", "description": "<p>d</p>"}]}, status=None, task=TASK_ID),
    "an empty answer": dict(payload={"kind": "tasks", "data": []}, status=None, task=TASK_ID),
    "a refused token (401)": dict(payload=None, status=401, task=TASK_ID),
    "a forbidden read (403)": dict(payload=None, status=403, task=TASK_ID),
    "an empty task id": dict(payload=None, status=None, task="   "),
}


class TestTheTwoClientsAgree:
    """The skill's client and the core's, same fake Wrike, same answer — in the five situations
    where they could come apart without anybody noticing."""

    @pytest.mark.parametrize("read", READS)
    @pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
    def test_same_envelope_same_url_same_header_same_verb(self, monkeypatch, case, read):
        spec = CASES[case]
        seen = {}
        for name, module in (("skill", skill_wrike), ("core", core_wrike)):
            transport = _install(monkeypatch, _Transport(spec["payload"], spec["status"]))
            seen[name] = (_outcome(module.WrikeClient("tok"), read, spec["task"]),
                          transport.requests)
        assert seen["core"] == seen["skill"], case

    def test_the_cases_really_differ(self, monkeypatch):
        """Guards the guard: five situations that produced the same result would prove nothing."""
        outcomes = set()
        for spec in CASES.values():
            _install(monkeypatch, _Transport(spec["payload"], spec["status"]))
            outcomes.add(repr(_outcome(core_wrike.WrikeClient("tok"), "get_task", spec["task"])))
        assert len(outcomes) == len(CASES)

    def test_every_request_is_one_get_with_the_bearer_token(self, monkeypatch):
        transport = _install(monkeypatch, _Transport({"kind": "comments", "data": []}))
        core_wrike.WrikeClient("tok").get_task_comments(TASK_ID)
        assert transport.requests == [
            ("GET", f"https://www.wrike.com/api/v4/tasks/{TASK_ID}/comments", "bearer tok")]


# ------------------------------------------------------------------------------ no token
class TestWithoutAToken:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_nothing_is_built_and_nothing_is_sent(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("WRIKE_TOKEN", raising=False)
        else:
            monkeypatch.setenv("WRIKE_TOKEN", value)
        transport = _install(monkeypatch, _Transport({"kind": "tasks", "data": []}))
        assert core_wrike.WrikeClient.from_env() is None
        assert core_wrike.wrike_capability() is None
        assert transport.requests == [], "zero traffic"


# ------------------------------------------------------------------------------ no write
def _toolset(monkeypatch):
    monkeypatch.setenv("WRIKE_TOKEN", "tok")
    capability = core_wrike.wrike_capability()
    assert capability is not None
    return capability.toolset


class TestItExposesNoWrite:
    def test_the_registered_tools_are_exactly_the_two_reads(self, monkeypatch):
        assert set(_toolset(monkeypatch).tools) == set(READS)

    def test_calling_every_registered_tool_sends_only_gets(self, monkeypatch):
        """Every function the toolset really holds, whatever it is called, is invoked; a write
        slipped in under an innocent name still has to send its verb."""
        tools = _toolset(monkeypatch).tools
        transport = _install(monkeypatch, _Transport({"kind": "tasks", "data": []}))
        for tool in tools.values():
            params = inspect.signature(tool.function).parameters
            args = {name: "x" for name, p in params.items() if p.default is inspect.Parameter.empty}
            asyncio.run(tool.function(**args))
        assert transport.requests, "no tool made a request; the check below would be vacuous"
        assert {method for method, _, _ in transport.requests} == {"GET"}

    def test_the_client_has_no_other_public_operation(self):
        public = {name for name, value in vars(core_wrike.WrikeClient).items()
                  if not name.startswith("_") and callable(getattr(core_wrike.WrikeClient, name))}
        assert public == {"from_env", *READS}

    def test_the_only_verb_in_the_module_is_get(self):
        source = Path(core_wrike.__file__).read_text(encoding="utf-8")
        assert re.findall(r'method="(\w+)"', source) == ["GET"]


# ------------------------------------------------------------------------------ registration
class TestItIsRegisteredInTheCore:
    @staticmethod
    def _ids(capabilities):
        return [getattr(getattr(c, "toolset", None), "id", None) for c in capabilities]

    def test_with_a_token_every_channel_gets_it(self, monkeypatch):
        monkeypatch.setenv("WRIKE_TOKEN", "tok")
        ids = self._ids(Runtime(enable_duties=False).shared_capabilities(allow_skills={"wrike"}))
        assert "wrike" in ids

    def test_without_a_token_it_is_absent(self, monkeypatch):
        monkeypatch.delenv("WRIKE_TOKEN", raising=False)
        ids = self._ids(Runtime(enable_duties=False).shared_capabilities(allow_skills={"wrike"}))
        assert "wrike" not in ids


# ------------------------------------------------------------------------------ declarations
class TestWhatReadOnlyDoesNotMeanStaysWritten:
    """"Read only" describes the capability, not what the agent can do with Wrike — and the
    personal-data path is declared, not fixed. Both in the module and in the SKILL.md."""

    SKILL_MD = _ROOT / "mossy" / "skills" / "wrike" / "SKILL.md"

    def test_the_module_says_the_shell_already_reaches_wrike(self):
        doc = " ".join(core_wrike.__doc__.split())
        assert "curl -X POST" in doc and "a skill does not bound an agent that has another way to reach the same service" in doc
        assert "NOT WHAT THE AGENT CAN DO WITH WRIKE" in doc
        assert "personal data" in doc and "create_issue" in doc

    def test_the_skill_says_it_too(self):
        text = " ".join(self.SKILL_MD.read_text(encoding="utf-8").split())
        assert "curl -X POST" in text and "A skill does not bound an agent that has another way to reach the same service" in text
        assert "not what an agent can do with Wrike" in text
        assert "personal data" in text and "create_issue" in text
