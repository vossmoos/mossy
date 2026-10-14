"""The Wrike read layer: three reads, no writes, and an empty answer that admits what it is.

the Wrike reader. Loaded by file path, like the other skill-script tests: it lives under a skill's
``scripts/`` and is not on the import path.

**Nothing here touches the network.** ``urlopen`` is replaced for every test by an autouse
fixture that fails loudly, so a test that forgets to install a transport does not quietly make a
live call — which is how eighteen offline tests in another repository spent 112 seconds talking
to a real API while staying green.

The class that carries the weight is ``TestAnEmptyAnswerIsNotAnAbsence``. The account this was
measured on holds a Collaborator licence, and Wrike serves a licence-restricted read as ``200``
with an empty ``data`` list — byte for byte what a task with no comments looks like. A caller
that reads absence into that silence will report a task as having no discussion when it has
twenty-three, and nothing will contradict it.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "wrike_api.py"
)
_spec = importlib.util.spec_from_file_location("wrike_api", _MODULE_PATH)
wrike_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrike_api)

#: A real id, from the notification e-mail's X-Wrike-Metadata header.
TASK_ID = "MAAAAAEXmain"
#: The number from the same task's permalink. Never accepted by the API — measured, 400.
PERMALINK_NUMBER = "1234567890"


class _Response:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self._status = status

    def getcode(self):
        return self._status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Transport:
    """Records every URL asked for, and answers with what the test decided."""

    def __init__(self, payload=None, error=None):
        self.urls: list[str] = []
        self.headers: list[dict] = []
        self.methods: list[str] = []
        self.payload = payload
        self.error = error

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        self.headers.append(dict(request.headers))
        self.methods.append(request.get_method())
        if self.error is not None:
            raise self.error
        return _Response(self.payload)

    @property
    def url(self) -> str:
        assert self.urls, "no request was made at all"
        return self.urls[-1]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any unplanned call is an error, not a live request."""
    def blocked(*args, **kwargs):
        raise AssertionError(
            "this test reached the network; install a _Transport instead"
        )
    monkeypatch.setattr(wrike_api.urllib.request, "urlopen", blocked)


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setenv("WRIKE_TOKEN", "test-token-value")


def _install(monkeypatch, transport):
    monkeypatch.setattr(wrike_api.urllib.request, "urlopen", transport)
    return transport


def _comments(*ids):
    return {"kind": "comments", "data": [{"id": i, "text": "x", "taskId": TASK_ID} for i in ids]}


def _task():
    return {"kind": "tasks", "data": [{"id": TASK_ID, "title": "t", "description": "<p>d</p>"}]}


class TestWithoutAToken:
    def test_from_env_returns_none(self, monkeypatch):
        """``None``, not an exception: a deployment without Wrike configured is an ordinary
        state of the world, and it is what jira.py and gmail.py both do."""
        monkeypatch.delenv("WRIKE_TOKEN", raising=False)
        assert wrike_api.WrikeClient.from_env() is None

    def test_an_empty_or_blank_token_is_no_token(self, monkeypatch):
        for value in ("", "   ", "\t"):
            monkeypatch.setenv("WRIKE_TOKEN", value)
            assert wrike_api.WrikeClient.from_env() is None

    def test_it_makes_no_request_at_all(self, monkeypatch):
        """Zero traffic. The autouse guard turns any call into a failure, so reaching the end
        of this test is the assertion."""
        monkeypatch.delenv("WRIKE_TOKEN", raising=False)
        assert wrike_api.WrikeClient.from_env() is None

    def test_a_configured_token_does_produce_a_client(self, token):
        """The other direction, so the pair pins the sign rather than one end of it."""
        assert wrike_api.WrikeClient.from_env() is not None


class TestTheTwoOperationsAndTheirPaths:
    def test_the_task_read_uses_the_task_path(self, monkeypatch, token):
        t = _install(monkeypatch, _Transport(_task()))
        wrike_api.WrikeClient.from_env().get_task(TASK_ID)
        assert t.url == f"https://www.wrike.com/api/v4/tasks/{TASK_ID}"

    def test_the_comments_read_uses_the_sub_resource(self, monkeypatch, token):
        """The measured shape: ``/tasks/{id}/comments``.

        This is the assertion that goes red if somebody moves to the flat ``/comments``, which
        refuses ``?taskId=`` with a 400 and would mean fetching every comment in the account
        and filtering here.
        """
        t = _install(monkeypatch, _Transport(_comments("c1", "c2")))
        wrike_api.WrikeClient.from_env().get_task_comments(TASK_ID)
        assert t.url == f"https://www.wrike.com/api/v4/tasks/{TASK_ID}/comments"

    def test_the_comments_read_never_calls_the_flat_endpoint(self, monkeypatch, token):
        """Said as its own fact, because the flat endpoint is the tempting one: it is the call
        that already returned 200 when the token was first measured."""
        t = _install(monkeypatch, _Transport(_comments("c1")))
        wrike_api.WrikeClient.from_env().get_task_comments(TASK_ID)
        assert "/api/v4/comments" not in t.url
        assert t.url.endswith("/comments")
        assert f"/tasks/{TASK_ID}/" in t.url

    def test_no_taskid_query_parameter_is_ever_sent(self, monkeypatch, token):
        """``?taskId=`` is a 400 — "Parameter 'taskId' is not allowed". A caller that added it
        would get a refusal, not a filter."""
        t = _install(monkeypatch, _Transport(_comments("c1")))
        wrike_api.WrikeClient.from_env().get_task_comments(TASK_ID)
        assert "taskId=" not in t.url and "?" not in t.url

    def test_the_paths_are_declared_and_are_the_ones_used(self):
        assert wrike_api.TASK_PATH == "/tasks/{task_id}"
        assert wrike_api.TASK_COMMENTS_PATH == "/tasks/{task_id}/comments"
        assert wrike_api.CONTACTS_ME_PATH == "/contacts?me=true"

    def test_the_token_travels_as_a_bearer_header(self, monkeypatch, token):
        t = _install(monkeypatch, _Transport(_task()))
        wrike_api.WrikeClient.from_env().get_task(TASK_ID)
        assert t.headers[-1].get("Authorization") == "bearer test-token-value"

    def test_the_description_is_not_requested(self, monkeypatch, token):
        """It arrives by default, and asking for it is a 400 — measured. A ``fields`` parameter
        here would turn every task read into a refusal."""
        t = _install(monkeypatch, _Transport(_task()))
        result = wrike_api.WrikeClient.from_env().get_task(TASK_ID)
        assert "fields" not in t.url
        assert result["task"]["description"] == "<p>d</p>"


class TestItNeverWrites:
    def test_every_request_is_a_get(self, monkeypatch, token):
        t = _install(monkeypatch, _Transport(_task()))
        client = wrike_api.WrikeClient.from_env()
        client.get_task(TASK_ID)
        _install(monkeypatch, t)
        client.get_task_comments(TASK_ID)
        assert set(t.methods) == {"GET"}

    def test_the_transport_has_no_method_parameter(self):
        """Structural, not promised: adding a write means adding a function, not passing an
        argument. A rule somebody has to break on purpose beats one they can forget."""
        import inspect

        assert "method" not in inspect.signature(wrike_api.WrikeClient._get).parameters

    def test_the_module_names_no_other_verb(self):
        source = _MODULE_PATH.read_text(encoding="utf-8")
        for verb in ("POST", "PUT", "DELETE", "PATCH"):
            assert verb not in source, f"{verb} appears in a module that only reads"

    def test_it_exposes_only_the_three_reads(self):
        """Three since the Wrike → Jira flow piece 2: ``get_me`` (``GET /contacts?me=true``) joined the two task
        reads so the flow can check ``responsibleIds`` before creating. Still reads only — a
        fourth public name here is a decision to review, not an addition to wave through."""
        public = {n for n in dir(wrike_api.WrikeClient) if not n.startswith("_")}
        assert public == {"from_env", "get_task", "get_task_comments", "get_me"}


class TestARefusalIsNotAnEmptyAnswer:
    """401 and 403 must never arrive at a caller shaped like "there is nothing here"."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refusal_raises_instead_of_returning_rows(self, monkeypatch, token, status):
        error = urllib.error.HTTPError("u", status, "refused", {}, None)
        _install(monkeypatch, _Transport(error=error))
        with pytest.raises(wrike_api.WrikeError) as caught:
            wrike_api.WrikeClient.from_env().get_task_comments(TASK_ID)
        assert caught.value.status == status

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refusal_is_marked_refused(self, monkeypatch, token, status):
        error = urllib.error.HTTPError("u", status, "refused", {}, None)
        _install(monkeypatch, _Transport(error=error))
        with pytest.raises(wrike_api.WrikeError) as caught:
            wrike_api.WrikeClient.from_env().get_task(TASK_ID)
        assert caught.value.refused is True

    @pytest.mark.parametrize("status", [400, 404, 500])
    def test_other_failures_are_not_marked_refused(self, monkeypatch, token, status):
        """The other direction. If everything were "refused", the flag would say nothing —
        and a 400 (the answer to a permalink number) is a caller's mistake, not a licence."""
        error = urllib.error.HTTPError("u", status, "boom", {}, None)
        _install(monkeypatch, _Transport(error=error))
        with pytest.raises(wrike_api.WrikeError) as caught:
            wrike_api.WrikeClient.from_env().get_task(TASK_ID)
        assert caught.value.refused is False

    def test_the_refusal_message_says_it_is_not_an_empty_read(self, monkeypatch, token):
        error = urllib.error.HTTPError("u", 403, "refused", {}, None)
        _install(monkeypatch, _Transport(error=error))
        with pytest.raises(wrike_api.WrikeError) as caught:
            wrike_api.WrikeClient.from_env().get_task_comments(TASK_ID)
        assert "not an empty one" in str(caught.value)

    def test_an_unreachable_host_is_reported_and_not_swallowed(self, monkeypatch, token):
        _install(monkeypatch, _Transport(error=urllib.error.URLError("down")))
        with pytest.raises(wrike_api.WrikeError):
            wrike_api.WrikeClient.from_env().get_task(TASK_ID)


class TestAnEmptyAnswerIsNotAnAbsence:
    """A 200 with no rows is not an error — and it is not proof either."""

    def test_no_comments_is_not_an_error(self, monkeypatch, token):
        _install(monkeypatch, _Transport({"kind": "comments", "data": []}))
        result = wrike_api.WrikeClient.from_env().get_task_comments(TASK_ID)
        assert result["ok"] is True
        assert result["comments"] == [] and result["count"] == 0

    def test_it_is_marked_empty_and_says_what_that_does_not_mean(self, monkeypatch, token):
        _install(monkeypatch, _Transport({"kind": "comments", "data": []}))
        result = wrike_api.WrikeClient.from_env().get_task_comments(TASK_ID)
        assert result["empty"] is True
        assert result["empty_means"] == wrike_api.EMPTY_IS_NOT_ABSENCE
        assert "not evidence" in result["empty_means"]

    def test_a_non_empty_answer_carries_no_such_caveat(self, monkeypatch, token):
        """The caveat is about silence. Attaching it to every answer would make it noise that
        nobody reads, which is how a real warning stops working."""
        _install(monkeypatch, _Transport(_comments("c1", "c2")))
        result = wrike_api.WrikeClient.from_env().get_task_comments(TASK_ID)
        assert result["empty"] is False
        assert "empty_means" not in result
        assert result["count"] == 2

    def test_a_task_that_comes_back_with_no_rows_is_not_visible_rather_than_absent(
        self, monkeypatch, token
    ):
        """Same shape for the task read: "not there" and "not allowed to see it" are the same
        bytes, so the result says the weaker of the two."""
        _install(monkeypatch, _Transport({"kind": "tasks", "data": []}))
        result = wrike_api.WrikeClient.from_env().get_task(TASK_ID)
        assert result["ok"] is False
        assert result["reason"] == "not_visible"
        assert result["detail"] == wrike_api.EMPTY_IS_NOT_ABSENCE

    def test_the_caveat_is_one_sentence_in_one_place(self):
        """One fact, one place: the module constant, so the reads cannot drift into
        saying different things about the same silence."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        assert source.count("EMPTY_IS_NOT_ABSENCE") >= 3
        assert "Collaborator" in wrike_api.EMPTY_IS_NOT_ABSENCE


class TestThePermalinkNumberIsNotAnApiId:
    """Measured: ``GET /tasks/1234567890`` is a 400, "Invalid Task ID"."""

    def test_this_module_converts_nothing(self):
        source = _MODULE_PATH.read_text(encoding="utf-8")
        assert "open.htm" not in source.split('"""', 2)[2], (
            "the body of the module should not build or parse permalinks; the header explains "
            "why the conversion does not exist here"
        )

    def test_the_number_is_passed_through_untouched_so_the_api_can_refuse_it(
        self, monkeypatch, token
    ):
        """No silent rescue. If a caller passes the permalink number, the request goes out with
        it and Wrike answers 400 — which is the honest outcome, because this module genuinely
        cannot turn one into the other."""
        t = _install(monkeypatch, _Transport({"kind": "tasks", "data": []}))
        wrike_api.WrikeClient.from_env().get_task(PERMALINK_NUMBER)
        assert t.url.endswith(f"/tasks/{PERMALINK_NUMBER}")

    def test_the_module_documents_where_the_api_id_comes_from(self):
        header = _MODULE_PATH.read_text(encoding="utf-8").split('"""')[1]
        assert "X-Wrike-Metadata" in header and "taskApiId" in header


class TestAnEmptyTaskIdNeverBecomesARequest:
    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_it_refuses_before_the_transport(self, monkeypatch, token, value):
        """The autouse guard makes any call a failure, so this also proves nothing was sent —
        an empty id in the path would be ``GET /tasks/``, which is the listing this module is
        forbidden to make."""
        client = wrike_api.WrikeClient.from_env()
        assert client.get_task(value or "")["reason"] == "no_task_id"
        assert client.get_task_comments(value or "")["reason"] == "no_task_id"


class TestItFollowsTheRepositoryConvention:
    def test_it_does_not_import_mossy_or_dotenv(self):
        """The rule the whole design rests on: this could not have been a
        ``mossy/capabilities/`` module, because the script runtime has no ``mossy``."""
        import re

        source = _MODULE_PATH.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import|from)\s+(mossy|dotenv)\b", source, re.MULTILINE)

    def test_it_uses_only_the_standard_library(self):
        import re

        source = _MODULE_PATH.read_text(encoding="utf-8")
        imported = set(re.findall(r"^\s*import\s+([a-z_.0-9]+)", source, re.MULTILINE))
        assert imported <= {"json", "os", "urllib.error", "urllib.parse", "urllib.request"}

    def test_the_env_var_is_named_and_documented(self):
        header = _MODULE_PATH.read_text(encoding="utf-8").split('"""')[1]
        assert "WRIKE_TOKEN" in header

    def test_the_docstring_warns_that_the_env_file_wins(self):
        """``mossy/env.py`` calls ``load_dotenv(override=True)``, so a token in the repo's
        ``.env`` beats one exported in the shell. Somebody debugging a wrong token needs that
        sentence in front of them."""
        header = _MODULE_PATH.read_text(encoding="utf-8").split('"""')[1]
        assert "override=True" in header


class TestWhoTheTokenIsIsNeverGuessed:
    """``get_me`` — the Wrike → Jira flow, piece 2. Wrike must COMMIT to one contact, and the row is checked.

    Measured on 14 September 2026: ``/contacts`` and ``/contacts?me=true`` both returned one
    contact, KUEXAMP2, ``me: True``. The difference only shows on a licence that can see the
    directory, which is exactly the case the class below builds.
    """

    @staticmethod
    def _contacts(*rows):
        return {"kind": "contacts", "data": list(rows)}

    class _ByUrl:
        """Answers ``?me=true`` with the token's own contact and plain ``/contacts`` with the
        whole directory — what a fuller licence would do."""

        def __init__(self, me_rows, directory_rows):
            self.urls: list[str] = []
            self.me_rows, self.directory_rows = me_rows, directory_rows

        def __call__(self, request, timeout=None):
            self.urls.append(request.full_url)
            rows = self.me_rows if request.full_url.endswith("?me=true") else self.directory_rows
            return _Response({"kind": "contacts", "data": rows})

    def test_it_asks_with_me_true(self, monkeypatch, token):
        t = _install(monkeypatch, _Transport(self._contacts({"id": "KUEXAMP2", "me": True})))
        wrike_api.WrikeClient.from_env().get_me()
        assert t.url.endswith("/contacts?me=true") and t.methods == ["GET"]

    def test_the_committed_answer_is_accepted(self, monkeypatch, token):
        _install(monkeypatch, _Transport(self._contacts({"id": "KUEXAMP2", "me": True})))
        assert wrike_api.WrikeClient.from_env().get_me() == {"ok": True, "me_id": "KUEXAMP2"}

    def test_on_a_licence_that_sees_the_directory_it_still_names_exactly_one(self, monkeypatch,
                                                                             token):
        me = {"id": "KUEXAMP2", "me": True}
        directory = [{"id": "KUEXAMP1", "me": False}, me, {"id": "KUEXAMP4", "me": False}]
        _install(monkeypatch, self._ByUrl([me], directory))
        assert wrike_api.WrikeClient.from_env().get_me() == {"ok": True, "me_id": "KUEXAMP2"}

    def test_zero_rows_is_unresolved(self, monkeypatch, token):
        _install(monkeypatch, _Transport(self._contacts()))
        out = wrike_api.WrikeClient.from_env().get_me()
        assert out["ok"] is False and out["reason"] == wrike_api.ME_UNRESOLVED and out["rows"] == 0

    def test_two_rows_is_unresolved_even_when_one_says_me(self, monkeypatch, token):
        _install(monkeypatch, _Transport(self._contacts(
            {"id": "KUEXAMP2", "me": True}, {"id": "KUEXAMP1", "me": False})))
        out = wrike_api.WrikeClient.from_env().get_me()
        assert out["ok"] is False and out["reason"] == wrike_api.ME_UNRESOLVED and out["rows"] == 2

    @pytest.mark.parametrize("row", [
        {"id": "KUEXAMP2", "me": False},
        {"id": "KUEXAMP2"},
        {"id": "KUEXAMP2", "me": "true"},
    ])
    def test_one_row_without_me_true_is_not_accepted_as_mine(self, monkeypatch, token, row):
        _install(monkeypatch, _Transport(self._contacts(row)))
        out = wrike_api.WrikeClient.from_env().get_me()
        assert out["ok"] is False and out["reason"] == wrike_api.ME_UNRESOLVED

    def test_a_row_without_an_id_is_unresolved(self, monkeypatch, token):
        _install(monkeypatch, _Transport(self._contacts({"me": True})))
        assert wrike_api.WrikeClient.from_env().get_me()["ok"] is False

    def test_a_refused_read_raises_rather_than_answering(self, monkeypatch, token):
        error = urllib.error.HTTPError("https://www.wrike.com/api/v4/contacts?me=true", 401,
                                       "Unauthorized", {}, None)
        _install(monkeypatch, _Transport(error=error))
        with pytest.raises(wrike_api.WrikeError) as raised:
            wrike_api.WrikeClient.from_env().get_me()
        assert raised.value.refused is True
