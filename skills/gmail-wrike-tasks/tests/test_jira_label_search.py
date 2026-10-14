"""Finding a Wrike task's Jira issue by label, and refusing to read absence into silence.

the label search. Loaded by file path, like the other skill-script tests. **Nothing here touches the
network**: ``urlopen`` is replaced for every test by an autouse fixture that fails loudly, so a
test that forgets to install a transport cannot quietly make a live call.

``TestAnEmptyPageIsNotAnAnswer`` is the class this module exists for, and it comes out of a real
hour lost. Measured against the live Jira: **a client whose token does not authenticate is served
200 with an empty ``issues`` list, not a 401.** So "this task has no issue" and "these credentials
are no good" arrive as the same bytes. A caller that treats the first as settled will open a new
Jira issue per task, every pass, for as long as the token stays broken — and every one of those
runs would look successful.

``TestTheRemovedEndpoint`` pins the other measurement: ``/rest/api/3/search`` is **gone**, not
deprecated — it answers 410 telling you to migrate. The mutation that matters is a search that
falls back to it.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "jira_label_search.py"
)
_spec = importlib.util.spec_from_file_location("jira_label_search", _MODULE_PATH)
search = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(search)

TASK_ID = "MAAAAAEXmain"
LABEL = "wrike-MAAAAAEXmain"


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Transport:
    def __init__(self, payload=None, error=None):
        self.urls: list[str] = []
        self.methods: list[str] = []
        self.payload = payload
        self.error = error

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
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
    def blocked(*args, **kwargs):
        raise AssertionError("this test reached the network; install a _Transport instead")
    monkeypatch.setattr(search.urllib.request, "urlopen", blocked)


@pytest.fixture()
def client():
    return search.JiraLabelSearch("example.atlassian.net", "a@b.test", "token")


def _install(monkeypatch, transport):
    monkeypatch.setattr(search.urllib.request, "urlopen", transport)
    return transport


def _issue(key="PROJ-99"):
    return {"issues": [{"key": key, "fields": {"labels": [LABEL]}}], "isLast": True}


class TestAnEmptyPageIsNotAnAnswer:
    """★ The measurement this module was built around: unauthenticated == 200 + empty list."""

    EMPTY = {"issues": [], "isLast": True}

    def test_an_empty_page_is_not_reported_as_no_issue(self, monkeypatch, client):
        result = _install(monkeypatch, _Transport(self.EMPTY)) and client.find_issue_for_task(TASK_ID)
        assert result["matched"] is False
        assert result["empty"] is True
        # The thing that must never appear: a settled negative.
        assert "linked" not in result
        assert result.get("reason") != "no_issue"

    def test_an_empty_page_never_claims_the_credentials_work(self, monkeypatch, client):
        """``authenticated`` stays None. It is not False either — nothing here can tell a bad
        token from an empty project, and False would be a claim as wrong as True."""
        _install(monkeypatch, _Transport(self.EMPTY))
        result = client.find_issue_for_task(TASK_ID)
        assert result["authenticated"] is None

    def test_an_empty_page_says_what_else_it_could_be(self, monkeypatch, client):
        _install(monkeypatch, _Transport(self.EMPTY))
        result = client.find_issue_for_task(TASK_ID)
        assert result["empty_means"] == search.EMPTY_IS_NOT_ABSENCE
        assert "not a 401" in result["empty_means"]
        assert "unauthenticated" in result["empty_means"]

    def test_rows_coming_back_are_the_only_proof_of_authentication(self, monkeypatch, client):
        """The asymmetry, stated as a test: a non-empty page cannot be served to an
        unauthenticated client, so it is the one thing that settles the question."""
        _install(monkeypatch, _Transport(_issue()))
        result = client.find_issue_for_task(TASK_ID)
        assert result["authenticated"] is True
        assert result["matched"] is True

    def test_the_two_cases_are_distinguishable_by_the_caller(self, monkeypatch, client):
        """The pair, so a caller branching on ``matched`` alone cannot be right by accident."""
        _install(monkeypatch, _Transport(self.EMPTY))
        empty = client.find_issue_for_task(TASK_ID)
        _install(monkeypatch, _Transport(_issue()))
        found = client.find_issue_for_task(TASK_ID)
        assert (empty["matched"], empty["authenticated"]) == (False, None)
        assert (found["matched"], found["authenticated"]) == (True, True)

    def test_a_page_with_no_issues_key_at_all_is_treated_as_empty(self, monkeypatch, client):
        """A shape we have not seen must not become a match by accident."""
        _install(monkeypatch, _Transport({"isLast": True}))
        result = client.find_issue_for_task(TASK_ID)
        assert result["matched"] is False and result["authenticated"] is None


class TestABadQueryIsNotAnEmptyResult:
    def test_a_400_raises_instead_of_returning_empty(self, monkeypatch, client):
        _install(monkeypatch, _Transport(
            error=urllib.error.HTTPError("u", 400, "bad jql", {}, None)))
        with pytest.raises(search.JiraSearchError) as caught:
            client.find_issue_for_task(TASK_ID)
        assert caught.value.status == 400

    def test_a_400_is_marked_as_a_bad_query(self, monkeypatch, client):
        _install(monkeypatch, _Transport(
            error=urllib.error.HTTPError("u", 400, "bad jql", {}, None)))
        with pytest.raises(search.JiraSearchError) as caught:
            client.find_issue_for_task(TASK_ID)
        assert caught.value.bad_query is True
        assert "not an empty result" in str(caught.value)

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    def test_other_failures_are_not_marked_as_a_bad_query(self, monkeypatch, client, status):
        """The other direction. If everything were ``bad_query`` the flag would say nothing."""
        _install(monkeypatch, _Transport(
            error=urllib.error.HTTPError("u", status, "x", {}, None)))
        with pytest.raises(search.JiraSearchError) as caught:
            client.find_issue_for_task(TASK_ID)
        assert caught.value.bad_query is False

    def test_an_unreachable_host_is_reported_not_swallowed(self, monkeypatch, client):
        _install(monkeypatch, _Transport(error=urllib.error.URLError("down")))
        with pytest.raises(search.JiraSearchError):
            client.find_issue_for_task(TASK_ID)


class TestTheRemovedEndpoint:
    """``/rest/api/3/search`` answers 410. It is removed, not deprecated."""

    def test_the_search_goes_to_the_jql_endpoint(self, monkeypatch, client):
        t = _install(monkeypatch, _Transport(_issue()))
        client.find_issue_for_task(TASK_ID)
        assert "/rest/api/3/search/jql?" in t.url

    def test_the_search_never_calls_the_removed_endpoint(self, monkeypatch, client):
        """The mutation this exists for: a search that falls back to ``/search``.

        Asserted on the path, not on the string ``/search`` — which is a prefix of
        ``/search/jql`` and would make this test unfailable.
        """
        t = _install(monkeypatch, _Transport(_issue()))
        client.find_issue_for_task(TASK_ID)
        path = t.url.split("?", 1)[0]
        assert path.endswith(search.SEARCH_PATH)
        assert not path.endswith(search.REMOVED_SEARCH_PATH)

    def test_a_410_is_reported_as_the_removed_endpoint(self, monkeypatch, client):
        _install(monkeypatch, _Transport(
            error=urllib.error.HTTPError("u", 410, "gone", {}, None)))
        with pytest.raises(search.JiraSearchError) as caught:
            client.find_issue_for_task(TASK_ID)
        assert caught.value.status == 410
        assert "removed" in str(caught.value)

    def test_the_two_paths_are_declared_and_different(self):
        assert search.SEARCH_PATH == "/search/jql"
        assert search.REMOVED_SEARCH_PATH == "/search"


class TestTheLabelFormat:
    def test_it_is_the_prefix_plus_the_api_id(self):
        assert search.task_label(TASK_ID) == LABEL

    def test_the_jql_asks_for_that_exact_label(self, monkeypatch, client):
        t = _install(monkeypatch, _Transport(_issue()))
        client.find_issue_for_task(TASK_ID)
        import urllib.parse

        query = urllib.parse.parse_qs(t.url.split("?", 1)[1])
        assert query["jql"] == [f'labels = "{LABEL}"']

    def test_the_format_is_spelled_in_one_place(self):
        """The search reads it and the create step will write it. A format spelled twice drifts."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        assert body.count('"wrike-"') == 1

    def test_a_blank_task_id_is_refused_before_the_transport(self, monkeypatch, client):
        """The autouse guard makes any call a failure, so this proves nothing was sent."""
        result = client.find_issue_for_task("   ")
        assert result["ok"] is False and result["reason"] == "no_task_api_id"


class TestMoreThanOneIssueIsNotHidden:
    def test_two_matches_are_reported_as_such(self, monkeypatch, client):
        _install(monkeypatch, _Transport(
            {"issues": [{"key": "PROJ-1"}, {"key": "PROJ-2"}], "isLast": True}))
        result = client.find_issue_for_task(TASK_ID)
        assert result["matched"] is True
        assert result["issue_keys"] == ["PROJ-1", "PROJ-2"]
        assert result["reason"] == "more_than_one_issue"

    def test_one_match_carries_no_such_warning(self, monkeypatch, client):
        _install(monkeypatch, _Transport(_issue()))
        assert "reason" not in client.find_issue_for_task(TASK_ID)


class TestTheLabelIsConfirmedAgainstTheData:
    """``labels`` comes back when asked for — measured — so a match is checked, not trusted.

    The filter saying an issue carries the label and the issue carrying it are two different
    claims. They agree almost always; this class is about the "almost", because a disagreement
    means the query and the data have come apart and acting on it would comment on somebody
    else's issue.
    """

    def test_a_row_carrying_the_label_is_confirmed(self, monkeypatch, client):
        _install(monkeypatch, _Transport(_issue()))
        result = client.find_issue_for_task(TASK_ID)
        assert result["label_confirmed"] is True
        assert result["confirmed_keys"] == ["PROJ-99"]
        assert result["matched"] is True

    def test_a_row_without_the_label_is_not_treated_as_the_match(self, monkeypatch, client):
        """The filter returned it; the data says it is not ours. It does not become issue_key."""
        _install(monkeypatch, _Transport(
            {"issues": [{"key": "PROJ-77", "fields": {"labels": ["something-else"]}}],
             "isLast": True}))
        result = client.find_issue_for_task(TASK_ID)
        assert result["label_confirmed"] is False
        assert result["issues_without_the_label"] == ["PROJ-77"]
        assert result["matched"] is False
        assert result["issue_key"] is None
        assert "label_not_on_issue" in result["anomalies"]

    def test_a_mixed_page_keeps_only_the_confirmed_one(self, monkeypatch, client):
        _install(monkeypatch, _Transport({"issues": [
            {"key": "PROJ-77", "fields": {"labels": ["other"]}},
            {"key": "PROJ-99", "fields": {"labels": [LABEL]}},
        ], "isLast": True}))
        result = client.find_issue_for_task(TASK_ID)
        assert result["issue_key"] == "PROJ-99"
        assert result["issues_without_the_label"] == ["PROJ-77"]

    def test_a_response_with_no_labels_at_all_is_unchecked_not_unconfirmed(
        self, monkeypatch, client
    ):
        """``None``, not False. Nothing was checked, and saying False would be a claim about
        data that never arrived."""
        _install(monkeypatch, _Transport({"issues": [{"key": "PROJ-99"}], "isLast": True}))
        result = client.find_issue_for_task(TASK_ID)
        assert result["label_confirmed"] is None
        assert result["unchecked_keys"] == ["PROJ-99"]
        assert result["matched"] is True
        assert result["issue_key"] == "PROJ-99"

    def test_the_request_asks_for_labels(self, monkeypatch, client):
        """The confirmation is only possible because the field is requested. If somebody trims
        the ``fields`` parameter to save bytes, the check silently becomes unavailable."""
        import urllib.parse

        t = _install(monkeypatch, _Transport(_issue()))
        client.find_issue_for_task(TASK_ID)
        query = urllib.parse.parse_qs(t.url.split("?", 1)[1])
        assert "labels" in query["fields"][0].split(",")


class TestMoreThanOnePageIsAnAnomaly:
    """The new endpoint pages by token. This module does not follow it, and says so.

    A label encodes one Wrike task, so zero or one issue is the whole of what is expected. A
    second page is not a long list; it is a sign that the label was reused or something ran in
    bulk. Following it would gather that quietly and hand it back looking ordinary — and the
    caller would comment on whichever came first.
    """

    def test_is_last_false_is_reported(self, monkeypatch, client):
        _install(monkeypatch, _Transport({
            "issues": [{"key": "PROJ-99", "fields": {"labels": [LABEL]}}],
            "isLast": False, "nextPageToken": "tok-2",
        }))
        result = client.find_issue_for_task(TASK_ID)
        assert "more_than_one_page" in result["anomalies"]
        assert result["next_page_token"] == "tok-2"

    def test_is_last_true_carries_no_anomaly(self, monkeypatch, client):
        _install(monkeypatch, _Transport(_issue()))
        result = client.find_issue_for_task(TASK_ID)
        assert "anomalies" not in result
        assert "next_page_token" not in result

    def test_it_does_not_follow_the_token(self, monkeypatch, client):
        """One request, whatever the page says. Counted, because a loop would be invisible in
        the result and visible only in the call log."""
        t = _install(monkeypatch, _Transport({
            "issues": [{"key": "PROJ-99", "fields": {"labels": [LABEL]}}],
            "isLast": False, "nextPageToken": "tok-2",
        }))
        client.find_issue_for_task(TASK_ID)
        assert len(t.urls) == 1
        assert "nextPageToken" not in t.urls[0]

    def test_the_result_still_carries_what_came_back(self, monkeypatch, client):
        """Reporting the anomaly is not the same as throwing the page away."""
        result_transport = _Transport({
            "issues": [{"key": "PROJ-99", "fields": {"labels": [LABEL]}}],
            "isLast": False, "nextPageToken": "tok-2",
        })
        _install(monkeypatch, result_transport)
        result = client.find_issue_for_task(TASK_ID)
        assert result["matched"] is True and result["issue_key"] == "PROJ-99"

    def test_two_anomalies_are_both_reported(self, monkeypatch, client):
        _install(monkeypatch, _Transport({
            "issues": [{"key": "PROJ-1", "fields": {"labels": [LABEL]}},
                       {"key": "PROJ-2", "fields": {"labels": [LABEL]}}],
            "isLast": False, "nextPageToken": "tok-2",
        }))
        result = client.find_issue_for_task(TASK_ID)
        assert set(result["anomalies"]) == {"more_than_one_issue", "more_than_one_page"}


class TestItNeverWrites:
    def test_every_request_is_a_get(self, monkeypatch, client):
        t = _install(monkeypatch, _Transport(_issue()))
        client.find_issue_for_task(TASK_ID)
        assert set(t.methods) == {"GET"}

    def test_the_transport_has_no_method_parameter(self):
        import inspect

        assert "method" not in inspect.signature(search.JiraLabelSearch._get).parameters

    def test_it_exposes_only_reads(self):
        """Three reads now — the decision step added the key lookup, the create step the canary — and still nothing
        that writes. Creating belongs to ``create_jira_for_task``, which has its own client."""
        public = {n for n in dir(search.JiraLabelSearch) if not n.startswith("_")}
        assert public == {"from_env", "find_issue_for_task", "issue_exists",
                          "credentials_proven"}

    def test_it_imports_nothing_that_could_write_elsewhere(self):
        import re

        imported = set(re.findall(
            r"^\s*import\s+([a-z_.0-9]+)", _MODULE_PATH.read_text(encoding="utf-8"), re.MULTILINE))
        assert imported == {"base64", "json", "os", "urllib.error", "urllib.parse",
                            "urllib.request"}


class TestIssueExists:
    """``GET /issue/{key}``. A 200 settles two questions; a 404 settles neither."""

    def test_a_200_says_the_issue_is_there(self, monkeypatch, client):
        _install(monkeypatch, _Transport({"key": "PROJ-193"}))
        result = client.issue_exists("PROJ-193")
        assert result["exists"] is True
        assert result["issue_key"] == "PROJ-193"

    def test_a_200_is_the_positive_proof_of_authentication(self, monkeypatch, client):
        """Rows cannot be served to an unauthenticated client, so this is the one place besides
        a non-empty search that can settle it."""
        _install(monkeypatch, _Transport({"key": "PROJ-193"}))
        assert client.issue_exists("PROJ-193")["authenticated"] is True

    def test_a_404_does_not_say_the_issue_is_gone(self, monkeypatch, client):
        """``exists`` is None, never False. An unauthenticated client is served 404 here too, so
        "deleted" and "bad credentials" arrive as the same status."""
        _install(monkeypatch, _Transport(
            error=urllib.error.HTTPError("u", 404, "nope", {}, None)))
        result = client.issue_exists("PROJ-193")
        assert result["exists"] is None
        assert result["exists"] is not False
        assert result["reason"] == "not_found_or_unauthenticated"

    def test_a_404_does_not_claim_the_credentials_are_bad_either(self, monkeypatch, client):
        _install(monkeypatch, _Transport(
            error=urllib.error.HTTPError("u", 404, "nope", {}, None)))
        assert client.issue_exists("PROJ-193")["authenticated"] is None

    def test_a_404_says_what_else_it_could_be(self, monkeypatch, client):
        _install(monkeypatch, _Transport(
            error=urllib.error.HTTPError("u", 404, "nope", {}, None)))
        assert "unauthenticated" in client.issue_exists("PROJ-193")["detail"]

    @pytest.mark.parametrize("status", [400, 403, 500])
    def test_other_failures_still_raise(self, monkeypatch, client, status):
        """Only 404 is ambiguous. Everything else is a failure and must not become a verdict."""
        _install(monkeypatch, _Transport(
            error=urllib.error.HTTPError("u", status, "x", {}, None)))
        with pytest.raises(search.JiraSearchError):
            client.issue_exists("PROJ-193")

    def test_it_asks_for_that_exact_key(self, monkeypatch, client):
        t = _install(monkeypatch, _Transport({"key": "PROJ-193"}))
        client.issue_exists("PROJ-193")
        assert "/rest/api/3/issue/PROJ-193?" in t.url

    def test_it_is_a_get(self, monkeypatch, client):
        t = _install(monkeypatch, _Transport({"key": "PROJ-193"}))
        client.issue_exists("PROJ-193")
        assert t.methods == ["GET"]

    def test_a_blank_key_is_refused_before_the_transport(self, client):
        assert client.issue_exists("  ")["ok"] is False


class TestTheCanary:
    """A question that MUST return rows if the credentials work (the create step).

    Needed because every search is served an empty 200 to an unauthenticated client, and a
    caller about to create a Jira issue needs better than "nothing came back" — this account
    cannot delete, so a create on a bad token is permanent.
    """

    @pytest.fixture()
    def client(self):
        return search.JiraLabelSearch("example.atlassian.net", "a@b.test", "token",
                                      space_name="PROJ")

    def test_rows_prove_the_credentials(self, monkeypatch, client):
        _install(monkeypatch, _Transport({"issues": [{"key": "PROJ-193"}], "isLast": True}))
        assert client.credentials_proven()["proven"] is True

    def test_an_empty_answer_does_not_prove_the_opposite(self, monkeypatch, client):
        """``proven`` is True or None, never False: the project could genuinely be empty."""
        _install(monkeypatch, _Transport({"issues": [], "isLast": True}))
        result = client.credentials_proven()
        assert result["proven"] is None
        assert result["proven"] is not False
        assert result["reason"] == "empty_project_or_unauthenticated"

    def test_the_empty_answer_says_what_it_does_and_does_not_show(self, monkeypatch, client):
        _install(monkeypatch, _Transport({"issues": [], "isLast": True}))
        detail = client.credentials_proven()["detail"]
        assert "fails to prove they are good" in detail
        assert "refuse to create" in detail

    def test_it_asks_about_the_configured_project(self, monkeypatch, client):
        import urllib.parse

        t = _install(monkeypatch, _Transport({"issues": [{"key": "PROJ-1"}]}))
        client.credentials_proven()
        query = urllib.parse.parse_qs(t.url.split("?", 1)[1])
        assert query["jql"] == ["project = PROJ ORDER BY created DESC"]
        assert query["maxResults"] == ["1"]

    def test_it_goes_to_the_jql_endpoint(self, monkeypatch, client):
        t = _install(monkeypatch, _Transport({"issues": [{"key": "PROJ-1"}]}))
        client.credentials_proven()
        assert t.url.split("?", 1)[0].endswith(search.SEARCH_PATH)

    def test_it_is_a_get(self, monkeypatch, client):
        t = _install(monkeypatch, _Transport({"issues": [{"key": "PROJ-1"}]}))
        client.credentials_proven()
        assert t.methods == ["GET"]

    def test_without_a_project_there_is_no_question_to_ask(self, monkeypatch):
        """And it does not reach the network to find that out."""
        bare = search.JiraLabelSearch("x.atlassian.net", "a@b.test", "tok")
        result = bare.credentials_proven()
        assert result["ok"] is False and result["reason"] == "no_project"

    def test_the_project_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("JIRA_SITE", "x.atlassian.net")
        monkeypatch.setenv("JIRA_USER_EMAIL", "a@b.test")
        monkeypatch.setenv("JIRA_API_KEY", "tok")
        monkeypatch.setenv("JIRA_SPACE_NAME", "PROJ")
        assert search.JiraLabelSearch.from_env().space_name == "PROJ"


class TestConfiguration:
    def test_without_credentials_from_env_is_none_and_makes_no_request(self, monkeypatch):
        for name in ("JIRA_SITE", "JIRA_USER_EMAIL", "JIRA_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        assert search.JiraLabelSearch.from_env() is None

    @pytest.mark.parametrize("missing", ["JIRA_SITE", "JIRA_USER_EMAIL", "JIRA_API_KEY"])
    def test_any_one_missing_is_enough(self, monkeypatch, missing):
        monkeypatch.setenv("JIRA_SITE", "x.atlassian.net")
        monkeypatch.setenv("JIRA_USER_EMAIL", "a@b.test")
        monkeypatch.setenv("JIRA_API_KEY", "tok")
        monkeypatch.delenv(missing, raising=False)
        assert search.JiraLabelSearch.from_env() is None

    def test_all_three_present_gives_a_client(self, monkeypatch):
        monkeypatch.setenv("JIRA_SITE", "https://x.atlassian.net/")
        monkeypatch.setenv("JIRA_USER_EMAIL", "a@b.test")
        monkeypatch.setenv("JIRA_API_KEY", "tok")
        client = search.JiraLabelSearch.from_env()
        assert client is not None and client.site == "x.atlassian.net"

    def test_it_does_not_import_the_repositorys_jira_client(self):
        """A skill script cannot import mossy, and the Freshdesk flow's client is not ours to
        reuse either — it lives in another skill."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        assert "create_jira_issue" not in body
        import re

        assert not re.search(r"^\s*(?:import|from)\s+(mossy|dotenv)\b", source, re.MULTILINE)

    def test_the_docstring_records_what_was_measured_not_a_stale_limitation(self):
        """It used to say the module had never met a live Jira. It has now, so that sentence is
        gone and the measurements are in its place — including the one that is still true."""
        header = _MODULE_PATH.read_text(encoding="utf-8").split('"""')[1]
        assert "NOT VERIFIED AGAINST A LIVE JIRA" not in header
        for phrase in ("nextPageToken", "isLast", "labels", "the create step"):
            assert phrase in header, phrase
