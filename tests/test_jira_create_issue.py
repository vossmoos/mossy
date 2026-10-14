"""JiraClient.create_issue — payload shape, ADF conversion, and best-effort assignee.

The HTTP *transport* is mocked (``request_json``), not the whole client, so the ADF conversion
and the assignee resolution run for real: only the network I/O is canned. This lets the tests
capture the exact POST body and drive the assignee-resolution branches with real search results.
"""

from __future__ import annotations

from unittest.mock import patch

from mossy.capabilities.jira import JiraClient


def _client() -> JiraClient:
    return JiraClient(
        site="example.atlassian.net", space_name="PROJ", email="me@x.com", api_token="tok"
    )


class _Transport:
    """Stand-in for JiraClient.request_json: records calls, cans responses by (method, path)."""

    def __init__(self, users: list[dict]) -> None:
        self.users = users
        self.calls: list[tuple[str, str, dict | None, object]] = []
        self.create_body: dict | None = None

    def __call__(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, query, body))
        if method == "GET" and path == "/user/assignable/search":
            return 200, self.users
        if method == "POST" and path == "/issue":
            self.create_body = body
            return 201, {"id": "10001", "key": "PROJ-123"}
        raise AssertionError(f"unexpected transport call: {method} {path}")

    def searched(self) -> bool:
        return any(p == "/user/assignable/search" for _, p, _, _ in self.calls)


_ADF = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "It rattles."}]}],
}


def test_happy_path_builds_the_exact_payload_and_returns_key_and_url():
    client = _client()
    users = [{"accountId": "5b:abc", "displayName": "Ana Beispiel", "emailAddress": "ana@x.com"}]
    transport = _Transport(users)

    with patch.object(JiraClient, "request_json", transport):
        result = client.create_issue(
            "Fix the door", "It rattles.", issue_type="Bug", assignee_query="Ana Beispiel"
        )

    # The exact POST /issue payload — project key, summary, issuetype, ADF description, assignee.
    assert transport.create_body == {
        "fields": {
            "project": {"key": "PROJ"},
            "summary": "Fix the door",
            "issuetype": {"name": "Bug"},
            "description": _ADF,
            "assignee": {"accountId": "5b:abc"},
        }
    }
    # MUTATION GUARD: the assignee must be in the payload when resolution was unique. Remove the
    # `fields["assignee"] = ...` line in create_issue and this assertion (and the dict above) fail.
    assert transport.create_body["fields"]["assignee"] == {"accountId": "5b:abc"}

    assert result["issue_key"] == "PROJ-123"
    assert result["url"] == "https://example.atlassian.net/browse/PROJ-123"
    assert "warnings" not in result


def test_ambiguous_assignee_creates_issue_unassigned_with_a_warning():
    client = _client()
    users = [
        {"accountId": "1", "displayName": "Ana Eins", "emailAddress": "a1@x.com"},
        {"accountId": "2", "displayName": "Ana Zwei", "emailAddress": "a2@x.com"},
    ]
    transport = _Transport(users)

    with patch.object(JiraClient, "request_json", transport):
        result = client.create_issue("Task X", "body", assignee_query="Ana")

    assert "assignee" not in transport.create_body["fields"]  # created without assignee
    assert result["issue_key"] == "PROJ-123"
    assert result.get("warnings"), "an ambiguous assignee must surface a warning"
    assert any("assignee" in w.lower() for w in result["warnings"])


def test_unknown_assignee_creates_issue_unassigned_with_a_warning():
    client = _client()
    transport = _Transport([])  # search returns no candidates

    with patch.object(JiraClient, "request_json", transport):
        result = client.create_issue("Task Y", "body", assignee_query="Nobody Here")

    assert "assignee" not in transport.create_body["fields"]
    assert result.get("warnings")
    assert any("no assignable user" in w.lower() for w in result["warnings"])


def test_no_assignee_query_means_no_search_no_assignee_no_warning():
    client = _client()
    transport = _Transport([])

    with patch.object(JiraClient, "request_json", transport):
        result = client.create_issue("Just a task", "desc")

    assert not transport.searched()  # never hits /user/assignable/search
    assert "assignee" not in transport.create_body["fields"]
    assert "warnings" not in result
    assert result["issue_key"] == "PROJ-123"
    assert result["url"] == "https://example.atlassian.net/browse/PROJ-123"


def test_email_exact_match_resolves_to_that_account():
    client = _client()
    users = [
        {"accountId": "1", "displayName": "Ana Eins", "emailAddress": "ana@x.com"},
        {"accountId": "2", "displayName": "Ana Zwei", "emailAddress": "ana2@x.com"},
    ]
    transport = _Transport(users)

    with patch.object(JiraClient, "request_json", transport):
        client.create_issue("Task Z", "body", assignee_query="ana2@x.com")

    # An exact email match disambiguates even when several candidates come back.
    assert transport.create_body["fields"]["assignee"] == {"accountId": "2"}
