"""A Freshdesk account in memory, faithful to the behaviours that have actually bitten us.

Not a stub that returns whatever a test wants: a small model of the account, so a test drives
the real ``apply_link.main`` end to end and the assertions are about what the account holds
afterwards. Every write is recorded in order, because several of our rules are about ordering —
a company created and then abandoned is a permanent record nobody asked for.

Modelled deliberately, each one measured against the real API during this project:

* ``description`` is HTML and ``description_text`` may arrive with **no newlines at all** for a
  ticket written in Freshdesk's own UI.
* Note bodies come back wrapped in Freshdesk's own markup, and **HTML comments are stripped**.
* Custom fields live under their bare name; a ``cf_`` prefix is rejected.
* A contact is linked to a company by ``company_id``; deleting the company leaves the contact.
"""

from __future__ import annotations

import re
from typing import Any


class FakeFreshdeskError(Exception):
    """Stands in for FreshdeskError so failure paths can be driven."""


class FakeFreshdesk:
    def __init__(self) -> None:
        self.tickets: dict[int, dict[str, Any]] = {}
        self.contacts: dict[int, dict[str, Any]] = {}
        self.companies: dict[int, dict[str, Any]] = {}
        self.conversations: dict[int, list[dict[str, Any]]] = {}
        self.writes: list[tuple[str, Any]] = []
        self.fail_on: dict[str, Exception] = {}
        self._next_id = 900000
        self.listing_notes: list[str] = []

    # ----------------------------------------------------------------- setup
    def add_company(self, name: str, domains: list[str] | None = None, **fields: Any) -> int:
        cid = self._new_id()
        self.companies[cid] = {"id": cid, "name": name, "domains": list(domains or []),
                               "custom_fields": dict(fields)}
        return cid

    def add_contact(self, email: str, company_id: int | None = None, **fields: Any) -> int:
        kid = self._new_id()
        self.contacts[kid] = {"id": kid, "email": email, "company_id": company_id,
                              "custom_fields": dict(fields)}
        return kid

    def add_ticket(self, description_html: str, requester_id: int, *, group_id: int = 1,
                   tags: list[str] | None = None, flatten: bool = True, **extra: Any) -> int:
        tid = self._new_id()
        text = re.sub(r"<[^>]+>", " ", description_html)
        text = re.sub(r"[ \t]+", " ", text).strip()
        self.tickets[tid] = {
            "id": tid, "description": description_html,
            # Flattened by default: that is what a UI-written ticket really looks like, and a
            # fixture with tidy newlines tests a shape Freshdesk does not send.
            "description_text": text if flatten else description_html,
            "requester_id": requester_id, "group_id": group_id,
            "tags": list(tags or []), **extra,
        }
        self.conversations.setdefault(tid, [])
        return tid

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _maybe_fail(self, operation: str) -> None:
        if operation in self.fail_on:
            raise self.fail_on[operation]

    # ------------------------------------------------------------ read paths
    def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        self._maybe_fail("get_ticket")
        if int(ticket_id) not in self.tickets:
            raise FakeFreshdeskError(f"Ticket {ticket_id} does not exist.")
        return dict(self.tickets[int(ticket_id)])

    def get_contact(self, contact_id: int) -> dict[str, Any]:
        self._maybe_fail("get_contact")
        contact = self.contacts[int(contact_id)]
        return {**contact, "custom_fields": dict(contact["custom_fields"])}

    def get_company(self, company_id: int) -> dict[str, Any]:
        self._maybe_fail("get_company")
        company = self.companies[int(company_id)]
        return {**company, "custom_fields": dict(company["custom_fields"])}

    def list_companies(self) -> tuple[list[dict[str, Any]], list[str]]:
        self._maybe_fail("list_companies")
        return [dict(c) for c in self.companies.values()], list(self.listing_notes)

    def search_companies_by_domain(self, domain: str) -> tuple[list[dict[str, Any]], list[str]]:
        hits = [dict(c) for c in self.companies.values() if domain in (c.get("domains") or [])]
        return hits, []

    def list_ticket_notes(self, ticket_id: int) -> list[dict[str, Any]]:
        self._maybe_fail("list_ticket_notes")
        return [dict(n) for n in self.conversations.get(int(ticket_id), [])]

    # ----------------------------------------------------------- write paths
    def create_company(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail("create_company")
        cid = self._new_id()
        company = {"id": cid, "name": payload.get("name"),
                   "domains": list(payload.get("domains") or []),
                   "custom_fields": dict(payload.get("custom_fields") or {})}
        self.companies[cid] = company
        self.writes.append(("create_company", company["name"]))
        return dict(company)

    def link_contact_to_company(self, contact_id: int, company_id: int) -> dict[str, Any]:
        self._maybe_fail("link_contact_to_company")
        self.contacts[int(contact_id)]["company_id"] = int(company_id)
        self.writes.append(("link_contact", (int(contact_id), int(company_id))))
        return dict(self.contacts[int(contact_id)])

    def update_contact_custom_field(self, contact_id: int, field: str, value: Any) -> dict[str, Any]:
        self._maybe_fail("update_contact_custom_field")
        if field.startswith("cf_"):
            raise FakeFreshdeskError("A cf_ prefixed field name is rejected by Freshdesk.")
        self.contacts[int(contact_id)]["custom_fields"][field] = value
        self.writes.append(("contact_field", (field, value)))
        return dict(self.contacts[int(contact_id)])

    def add_ticket_tags(self, ticket_id: int, tags: list[str]) -> dict[str, Any]:
        self._maybe_fail("add_ticket_tags")
        existing = self.tickets[int(ticket_id)]["tags"]
        merged = list(dict.fromkeys([*existing, *tags]))
        self.tickets[int(ticket_id)]["tags"] = merged
        self.writes.append(("tags", list(tags)))
        return {}

    def add_private_note(self, ticket_id: int, body_html: str) -> dict[str, Any]:
        self._maybe_fail("add_private_note")
        # Freshdesk strips HTML comments out of note bodies and returns its own wrapper. Both
        # are modelled so no test can pass by relying on a marker that would not survive.
        body = re.sub(r"<!--.*?-->", "", body_html, flags=re.S)
        entry = {"id": self._new_id(), "body": f"<div>{body}</div>", "private": True,
                 "incoming": False}
        self.conversations.setdefault(int(ticket_id), []).append(entry)
        self.writes.append(("note", body))
        return dict(entry)
