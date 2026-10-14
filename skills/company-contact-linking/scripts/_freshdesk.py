"""Freshdesk client for this skill. Standard library only, no imports outside this folder.

Reads for every step; the two writes this skill needs — linking a contact to a company, and
creating a company — live here too, and nowhere else. ``apply_link.py`` is their only caller.

The skill has to survive being copied into another Mossy repository, so it does not reach for
a shared client. What it needs is small: read a ticket, a contact and a company, and read the
company directory two different ways.

**Why two ways.** Neither is reliable alone. Freshdesk's search index lags a minute or two and
flaps while it catches up — measured on a sibling skill, an entity visible 15s after creation
was gone three minutes later and back two minutes after that — and the listing is not instant
either: a company created during this work appeared in ``/companies`` only on the second read,
seconds apart. The search also accepts **only** a domain query; ``name`` comes back HTTP 400.
So ``/companies`` is read every time, the domain search adds what it may know first, and the
two are merged by company id.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from _config import ConfigError, directory_limits, freshdesk_credentials


class FreshdeskError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} — HTTP {self.status}" if self.status else base


class FreshdeskClient:
    """Freshdesk access for this skill: reads everywhere, two writes used by apply_link.py."""

    def __init__(self, domain: str, api_key: str, timeout_seconds: int = 60) -> None:
        self.domain = domain.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "FreshdeskClient":
        domain, api_key = freshdesk_credentials()
        return cls(domain, api_key)

    # ------------------------------------------------------------------ reads
    def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        status, data = self.request_json("GET", f"/tickets/{int(ticket_id)}")
        if status != 200 or not isinstance(data, dict):
            raise FreshdeskError(f"Ticket {ticket_id} could not be read.", status=status, body=data)
        return data

    def get_contact(self, contact_id: int) -> dict[str, Any]:
        status, data = self.request_json("GET", f"/contacts/{int(contact_id)}")
        if status != 200 or not isinstance(data, dict):
            raise FreshdeskError(
                f"Contact {contact_id} could not be read.", status=status, body=data
            )
        return data

    def get_company(self, company_id: int) -> dict[str, Any]:
        status, data = self.request_json("GET", f"/companies/{int(company_id)}")
        if status != 200 or not isinstance(data, dict):
            raise FreshdeskError(
                f"Company {company_id} could not be read.", status=status, body=data
            )
        return data

    def list_companies(self) -> tuple[list[dict[str, Any]], list[str]]:
        """The company directory, straight from the API. Returns (companies, notes)."""
        max_pages, page_size = directory_limits()
        companies: list[dict[str, Any]] = []
        notes: list[str] = []
        page = 1
        while page <= max_pages:
            status, data = self.request_json(
                "GET", "/companies", query={"page": str(page), "per_page": str(page_size)}
            )
            if status != 200:
                raise FreshdeskError("Company listing failed.", status=status, body=data)
            if not isinstance(data, list) or not data:
                break
            companies.extend(item for item in data if isinstance(item, dict))
            if len(data) < page_size:
                break
            page += 1
        if page > max_pages:
            notes.append(
                f"Directory listing stopped after {max_pages} page(s) of {page_size}; a company "
                "beyond that was not considered."
            )
        return companies, notes

    def search_companies_by_domain(self, domain: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Search the company index by domain. Never raises — returns (results, notes).

        Only ``domain`` works here. A ``name`` query is rejected with HTTP 400
        ("Unexpected/invalid field in request"), measured against a live account, so there is
        no search path for a company name at all: names are matched against the listing.
        """
        domain = (domain or "").strip().lower()
        if not domain:
            return [], []
        status, data = self.request_json(
            "GET", "/search/companies", query={"query": f"\"domain:'{domain}'\""}
        )
        if status != 200 or not isinstance(data, dict):
            # A failing search must not sink the run: the directory listing still covers it.
            return [], [
                f"Company search for domain {domain!r} failed (HTTP {status}); the directory "
                "listing was used on its own."
            ]
        results = data.get("results")
        return ([r for r in results if isinstance(r, dict)] if isinstance(results, list) else []), []

    # ----------------------------------------------------------------- writes
    def link_contact_to_company(self, contact_id: int, company_id: int) -> dict[str, Any]:
        """Attach a contact to a company. The only field this skill ever sets on a contact."""
        status, data = self.request_json(
            "PUT", f"/contacts/{int(contact_id)}", body={"company_id": int(company_id)}
        )
        if status != 200:
            raise FreshdeskError(
                f"Contact {contact_id} could not be linked.", status=status, body=data
            )
        return data if isinstance(data, dict) else {}

    def update_contact_custom_field(
        self, contact_id: int, field: str, value: str
    ) -> dict[str, Any]:
        """Set one custom field on a contact, leaving every other field alone.

        Contact and company custom fields are addressed by their bare name inside
        ``custom_fields`` — measured against a live account, a ``cf_`` prefixed name is rejected
        with HTTP 400. The ``cf_`` form belongs to ticket fields.
        """
        status, data = self.request_json(
            "PUT", f"/contacts/{int(contact_id)}", body={"custom_fields": {field: value}}
        )
        if status != 200:
            raise FreshdeskError(
                f"Contact {contact_id} field {field!r} could not be written.",
                status=status,
                body=data,
            )
        return data if isinstance(data, dict) else {}

    def create_company(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a company from the fields the caller assembled."""
        status, data = self.request_json("POST", "/companies", body=payload)
        if status not in (200, 201) or not isinstance(data, dict):
            raise FreshdeskError("The company could not be created.", status=status, body=data)
        return data

    def add_ticket_tags(self, ticket_id: int, tags: list[str]) -> dict[str, Any]:
        """Append tags without replacing the ones already there."""
        ticket = self.get_ticket(ticket_id)
        existing = ticket.get("tags") if isinstance(ticket.get("tags"), list) else []
        merged = list(dict.fromkeys([*[str(t) for t in existing], *tags]))
        if merged == existing:
            return {"ok": True, "skipped": True, "tags": merged}
        status, data = self.request_json("PUT", f"/tickets/{int(ticket_id)}", body={"tags": merged})
        if status != 200:
            raise FreshdeskError(f"Ticket {ticket_id} could not be tagged.", status=status, body=data)
        return data if isinstance(data, dict) else {}

    def list_ticket_notes(self, ticket_id: int) -> list[dict[str, Any]]:
        """Every conversation entry on the ticket: notes, replies and forwards alike.

        Read before writing a note and again after, because the only way to know a note is on
        a ticket is to find it there.
        """
        status, data = self.request_json(
            "GET", f"/tickets/{int(ticket_id)}/conversations", query={"per_page": "100"}
        )
        if status != 200 or not isinstance(data, list):
            raise FreshdeskError(
                f"The conversations of ticket {ticket_id} could not be read.",
                status=status,
                body=data,
            )
        return data

    def add_private_note(self, ticket_id: int, body_html: str) -> dict[str, Any]:
        """Add a note only agents can see.

        ``private`` is sent explicitly rather than relying on the endpoint's default: this note
        says out loud that an automation touched the customer's record, and the one reader it
        must never reach is the customer.
        """
        status, data = self.request_json(
            "POST",
            f"/tickets/{int(ticket_id)}/notes",
            body={"body": body_html, "private": True},
        )
        if status not in (200, 201) or not isinstance(data, dict):
            raise FreshdeskError(
                f"The private note could not be added to ticket {ticket_id}.",
                status=status,
                body=data,
            )
        return data

    # ------------------------------------------------------------- transport
    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: Any = None,
    ) -> tuple[int, Any]:
        url = f"https://{self.domain}/api/v2{path if path.startswith('/') else '/' + path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            # The stored key is already the base64 of "<api_key>:X" and is sent verbatim.
            "Authorization": f"Basic {self.api_key}",
            "Content-Type": "application/json",
        }

        data = json.dumps(body).encode("utf-8") if body is not None else None

        def call() -> tuple[int, bytes]:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return response.getcode() or 200, response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read() if exc.fp else b""
            except urllib.error.URLError as exc:
                # Wrapped, so callers only ever have to catch FreshdeskError.
                raise FreshdeskError(f"Freshdesk is unreachable: {exc.reason}") from exc

        status, raw = call()
        if status == 429:  # rate limited — back off once, then give up honestly
            time.sleep(60)
            status, raw = call()
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, {"_raw": raw.decode("utf-8", errors="replace")}


__all__ = ["ConfigError", "FreshdeskClient", "FreshdeskError"]
