"""Freshdesk integration exposed as a Pydantic AI capability (toolset).

This is the *capability* layer: raw, reusable Freshdesk API operations. It carries
no business logic — how/when to use these tools for a given project lives in skills
(the thin internal `freshdesk` skill, plus any project-specific policy skills).

The client is stdlib-only (urllib) and uses the same auth/env contract as the rest
of the Mossy/Freshdesk infra:

    FRESHDESK_DOMAIN   e.g. "yourcompany.freshdesk.com"
    FRESHDESK_API_KEY  the Basic-auth value sent verbatim as `Authorization: Basic <value>`
                       (i.e. base64 of "<api_key>:X"), matching existing deployments.

If either env var is missing, `freshdesk_capability()` returns None so Mossy still
boots without Freshdesk configured.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset


class FreshdeskError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class FreshdeskClient:
    """Minimal Freshdesk v2 client. Reads + the writes Mossy needs for triage."""

    domain: str
    api_key: str
    timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> "FreshdeskClient | None":
        domain = (os.environ.get("FRESHDESK_DOMAIN") or "").strip()
        api_key = (os.environ.get("FRESHDESK_API_KEY") or "").strip()
        if not domain or not api_key:
            return None
        return cls(domain=domain, api_key=api_key)

    # ----------------------------------------------------------------- reads
    def list_recent_tickets(self, *, limit: int = 10, max_pages: int = 20) -> list[dict[str, Any]]:
        """Most recently created tickets, newest first."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        collected: list[dict[str, Any]] = []
        page = 1
        while len(collected) < limit and page <= max_pages:
            need = limit - len(collected)
            status, data = self.request_json(
                "GET", "/tickets", query={"page": str(page), "per_page": str(need)}
            )
            if status != 200:
                raise FreshdeskError("Freshdesk ticket listing failed.", status=status, body=data)
            if not isinstance(data, list) or not data:
                break
            collected.extend(item for item in data if isinstance(item, dict))
            if len(data) < need:
                break
            page += 1
        collected.sort(key=lambda t: str(t.get("created_at") or "")[:30], reverse=True)
        return collected[:limit]

    def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Ticket details including the requester contact."""
        status, data = self.request_json(
            "GET", f"/tickets/{ticket_id}", query={"include": "requester"}
        )
        if status != 200 or not isinstance(data, dict):
            raise FreshdeskError("Freshdesk ticket fetch failed.", status=status, body=data)
        return data

    def get_ticket_conversations(
        self, ticket_id: int, *, max_pages: int = 10
    ) -> list[dict[str, Any]]:
        """All conversation entries (public replies + private notes), chronological."""
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            status, data = self.request_json(
                "GET",
                f"/tickets/{ticket_id}/conversations",
                query={"page": str(page), "per_page": "100"},
            )
            if status != 200:
                raise FreshdeskError(
                    "Freshdesk conversations fetch failed.", status=status, body=data
                )
            if not isinstance(data, list) or not data:
                break
            out.extend(data)
            if len(data) < 100:
                break
        return out

    def get_contact(self, contact_id: int) -> dict[str, Any]:
        status, data = self.request_json("GET", f"/contacts/{contact_id}")
        if status != 200 or not isinstance(data, dict):
            raise FreshdeskError("Freshdesk contact fetch failed.", status=status, body=data)
        return data

    def get_company(self, company_id: int) -> dict[str, Any]:
        status, data = self.request_json("GET", f"/companies/{company_id}")
        if status != 200 or not isinstance(data, dict):
            raise FreshdeskError("Freshdesk company fetch failed.", status=status, body=data)
        return data

    def get_article(self, article_id: str, language: str = "de") -> dict[str, Any]:
        """Solution article in an explicit locale (de / fr / nl).

        Articles are used as reply templates. The unsuffixed endpoint returns the
        portal default (often English) — always request the explicit locale.
        """
        lang = (language or "de").strip().lower()
        if lang not in ("de", "fr", "nl"):
            lang = "de"
        status, data = self.request_json("GET", f"/solutions/articles/{article_id}/{lang}")
        if status != 200 or not isinstance(data, dict):
            raise FreshdeskError(
                f"Freshdesk article {article_id} ({lang}) fetch failed.",
                status=status,
                body=data,
            )
        return data

    # ----------------------------------------------------------------- writes
    def post_public_reply(self, ticket_id: int, body: str) -> dict[str, Any]:
        """Post a customer-visible reply on the ticket thread."""
        status, data = self.request_json(
            "POST", f"/tickets/{ticket_id}/reply", body={"body": body}
        )
        if status not in (200, 201):
            raise FreshdeskError("Freshdesk public reply failed.", status=status, body=data)
        return {"ok": True, "http_status": status, "response": data}

    def post_private_note(self, ticket_id: int, body: str) -> dict[str, Any]:
        """Post a private (internal) note on the ticket."""
        status, data = self.request_json(
            "POST", f"/tickets/{ticket_id}/notes", body={"body": body, "private": True}
        )
        if status not in (200, 201):
            raise FreshdeskError("Freshdesk private note failed.", status=status, body=data)
        return {"ok": True, "http_status": status, "response": data}

    def update_ticket(self, ticket_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Update ticket fields (status, type, tags, custom fields, …)."""
        status, data = self.request_json("PUT", f"/tickets/{ticket_id}", body=payload)
        if status != 200:
            raise FreshdeskError("Freshdesk ticket update failed.", status=status, body=data)
        return {"ok": True, "http_status": status, "response": data}

    def add_ticket_tags(self, ticket_id: int, tags: list[str]) -> dict[str, Any]:
        """Append tags without replacing existing ones."""
        ticket = self.get_ticket(ticket_id)
        existing = ticket.get("tags")
        if not isinstance(existing, list):
            existing = []
        merged = list(dict.fromkeys([*existing, *tags]))
        if merged == existing:
            return {"ok": True, "skipped": True, "reason": "tags_already_present", "tags": merged}
        return self.update_ticket(ticket_id, {"tags": merged})

    # ------------------------------------------------------------- transport
    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: Any = None,
    ) -> tuple[int, Any]:
        url = self._url(path, query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Basic {self.api_key}",
            "Content-Type": "application/json",
        }

        def do_call() -> tuple[int, bytes]:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    return resp.getcode() or 200, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read() if exc.fp else b""

        status, raw = do_call()
        if status == 429:  # rate limited — back off once and retry
            time.sleep(60)
            status, raw = do_call()
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return status, {"_raw": raw.decode("utf-8", errors="replace")}

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        clean_path = path if path.startswith("/") else f"/{path}"
        base = f"https://{self.domain.rstrip('/')}/api/v2{clean_path}"
        return base + ("?" + urllib.parse.urlencode(query) if query else "")


def freshdesk_capability() -> Toolset | None:
    """Expose Freshdesk operations for the `freshdesk` skill.

    Returns None when FRESHDESK_DOMAIN / FRESHDESK_API_KEY are not configured, so the
    capability is simply absent rather than failing at runtime.
    """
    client = FreshdeskClient.from_env()
    if client is None:
        return None

    async def list_recent_tickets(limit: int = 10) -> list[dict[str, Any]]:
        """List the most recently created tickets (newest first)."""
        return client.list_recent_tickets(limit=limit)

    async def get_ticket(ticket_id: int) -> dict[str, Any]:
        """Get one ticket with its requester contact embedded."""
        return client.get_ticket(ticket_id)

    async def get_ticket_conversations(ticket_id: int) -> list[dict[str, Any]]:
        """Get the full conversation thread for a ticket (replies + private notes)."""
        return client.get_ticket_conversations(ticket_id)

    async def get_contact(contact_id: int) -> dict[str, Any]:
        """Get a customer contact by id."""
        return client.get_contact(contact_id)

    async def get_company(company_id: int) -> dict[str, Any]:
        """Get a company by id."""
        return client.get_company(company_id)

    async def get_article(article_id: str, language: str = "de") -> dict[str, Any]:
        """Get a solution article (reply template) in a locale: de, fr, or nl."""
        return client.get_article(article_id, language)

    async def post_public_reply(ticket_id: int, body: str) -> dict[str, Any]:
        """Post a customer-visible reply on a ticket. body is HTML."""
        return client.post_public_reply(ticket_id, body)

    async def post_private_note(ticket_id: int, body: str) -> dict[str, Any]:
        """Post an internal private note on a ticket. body is HTML."""
        return client.post_private_note(ticket_id, body)

    async def update_ticket(ticket_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Update ticket fields, e.g. {"status": 4, "type": "Question"}."""
        return client.update_ticket(ticket_id, payload)

    async def add_ticket_tags(ticket_id: int, tags: list[str]) -> dict[str, Any]:
        """Append tags to a ticket without dropping existing ones."""
        return client.add_ticket_tags(ticket_id, tags)

    return Toolset(
        FunctionToolset(
            [
                list_recent_tickets,
                get_ticket,
                get_ticket_conversations,
                get_contact,
                get_company,
                get_article,
                post_public_reply,
                post_private_note,
                update_ticket,
                add_ticket_tags,
            ],
            id="freshdesk",
            instructions=(
                "These tools implement the `freshdesk` skill: raw Freshdesk API access. "
                "Use reads (tickets, conversations, contacts, companies, articles) to gather "
                "context, and writes (replies, private notes, ticket updates, tags) only when a "
                "task explicitly calls for them. Reply/note bodies are HTML. Follow any "
                "project-specific freshdesk policy skill for how and when to act."
            ),
        )
    )
