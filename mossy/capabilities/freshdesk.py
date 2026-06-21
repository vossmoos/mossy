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
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset


class FreshdeskError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.body is not None:
            if isinstance(self.body, dict):
                errors = self.body.get("errors")
                if isinstance(errors, list) and errors:
                    detail = "; ".join(
                        f"{err.get('field', '?')}: {err.get('message', err.get('code', '?'))}"
                        for err in errors
                        if isinstance(err, dict)
                    )
                    parts.append(detail)
                elif self.body.get("description"):
                    parts.append(str(self.body["description"]))
                else:
                    parts.append(json.dumps(self.body, ensure_ascii=False)[:500])
            else:
                parts.append(str(self.body)[:500])
        return " — ".join(parts)


def _add_source_pair(sources: dict[str, int], a: Any, b: Any) -> None:
    """Record one label → API value mapping, tolerating inverted Freshdesk shapes."""
    a_s, b_s = str(a).strip(), str(b).strip()
    try:
        if a_s.isdigit() and not b_s.isdigit():
            sources[b_s.lower()] = int(a_s)
        elif b_s.isdigit() and not a_s.isdigit():
            sources[a_s.lower()] = int(b_s)
        else:
            sources[a_s.lower()] = int(b_s)
    except (TypeError, ValueError):
        return


def _encode_form_fields(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten a ticket payload for Freshdesk multipart/form-data."""
    fields: list[tuple[str, str]] = []
    for key, value in data.items():
        if key == "custom_fields" and isinstance(value, dict):
            for cf_key, cf_val in value.items():
                if cf_val is None:
                    continue
                fields.append((f"custom_fields[{cf_key}]", str(cf_val)))
            continue
        if value is None:
            continue
        fields.append((key, str(value)))
    return fields


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

    # --------------------------------------------------------------- creates
    # Default Freshdesk source labels → numeric API values. Used as a fallback
    # when the account's ticket-field choices cannot be fetched. Accounts can
    # define custom sources (e.g. "Website"), so we resolve by label first.
    _DEFAULT_SOURCES = {
        "email": 1,
        "portal": 2,
        "phone": 3,
        "chat": 7,
        "feedback widget": 9,
        "outbound email": 10,
    }
    # English / legacy labels → account-specific labels to try in order.
    _SOURCE_ALIASES: ClassVar[dict[str, tuple[str, ...]]] = {
        "website": ("webformular", "web form", "e-commerce", "portal", "web-chat"),
    }

    def list_ticket_sources(self) -> dict[str, int]:
        """Map of available ticket-source labels → numeric API value.

        Reads the `source` ticket field's choices so custom sources (such as
        "Website") resolve to the right number for this Freshdesk account.
        Falls back to the built-in defaults if the field cannot be read.
        """
        try:
            status, data = self.request_json("GET", "/ticket_fields")
        except Exception:  # noqa: BLE001 - any read failure falls back to defaults
            status, data = 0, None
        sources: dict[str, int] = {}
        if status == 200 and isinstance(data, list):
            for field in data:
                if not isinstance(field, dict) or field.get("name") != "source":
                    continue
                choices = field.get("choices")
                # choices may be {"Website": 50} or [["Website", 50], ...]
                if isinstance(choices, dict):
                    pairs = choices.items()
                elif isinstance(choices, list):
                    pairs = [tuple(c[:2]) for c in choices if isinstance(c, (list, tuple)) and len(c) >= 2]
                else:
                    pairs = []
                for label, value in pairs:
                    _add_source_pair(sources, label, value)
        if not sources:
            sources = dict(self._DEFAULT_SOURCES)
        return sources

    def resolve_source(self, source: Any) -> int:
        """Coerce a source given as an int or label string into its API value."""
        if source is None:
            raise ValueError("source is required")
        if isinstance(source, bool):  # guard: bool is an int subclass
            raise ValueError("source must be an int or label string")
        if isinstance(source, int):
            return source
        text = str(source).strip()
        if text.isdigit():
            return int(text)
        key = text.lower()
        sources = self.list_ticket_sources()
        if key in sources:
            return sources[key]
        for alias in self._SOURCE_ALIASES.get(key, ()):
            if alias in sources:
                return sources[alias]
        if key in self._DEFAULT_SOURCES:
            return self._DEFAULT_SOURCES[key]
        available = ", ".join(sorted(sources)) or "none"
        raise FreshdeskError(
            f"Unknown ticket source {source!r}. Available sources: {available}."
        )

    def primary_email_config_id(self, product_id: int) -> int | None:
        """Primary support mailbox id for a product (matches DUSCHOLUX KI bot routing)."""
        try:
            status, data = self.request_json("GET", "/email_configs")
        except Exception:  # noqa: BLE001 - optional enrichment for repeat_ticket
            return None
        if status != 200 or not isinstance(data, list):
            return None
        for cfg in data:
            if not isinstance(cfg, dict):
                continue
            if cfg.get("product_id") == product_id and cfg.get("primary_role"):
                cfg_id = cfg.get("id")
                if isinstance(cfg_id, int):
                    return cfg_id
        return None

    def download_bytes(self, url: str) -> bytes:
        """Fetch raw bytes from a URL (Freshdesk attachment/inline links).

        Attachment URLs returned by the API are pre-signed and need no auth, but
        we send the Basic header anyway for portal-relative links; harmless on S3.
        """
        req = urllib.request.Request(
            url, headers={"Authorization": f"Basic {self.api_key}"}, method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # Retry without auth header — some pre-signed S3 links reject extra headers.
            if exc.code in (400, 401, 403):
                req2 = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req2, timeout=self.timeout_seconds) as resp:
                    return resp.read()
            raise FreshdeskError(
                "Failed to download attachment.", status=exc.code, body=url
            ) from exc

    def create_ticket(
        self,
        *,
        subject: str,
        description: str,
        email: str | None = None,
        name: str | None = None,
        requester_id: int | None = None,
        group_id: int | None = None,
        source: Any = None,
        tags: list[str] | None = None,
        status: int = 2,
        priority: int = 1,
        attachments: list[tuple[str, str, bytes]] | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new ticket. Uses multipart when attachments are supplied.

        `attachments` is a list of (filename, content_type, content_bytes).
        `source` may be an int or a label string (e.g. "Website").
        """
        if not (email or requester_id):
            raise ValueError("create_ticket requires email or requester_id")

        base: dict[str, Any] = {
            "subject": subject or "",
            "description": description or "",
            "status": status,
            "priority": priority,
        }
        if email:
            base["email"] = email
        if name:
            base["name"] = name
        if requester_id is not None:
            base["requester_id"] = requester_id
        if group_id is not None:
            base["group_id"] = group_id
        if source is not None:
            base["source"] = self.resolve_source(source)
        if extra_fields:
            base.update(extra_fields)
        tag_list = list(tags or [])

        if not attachments:
            payload = dict(base)
            if tag_list:
                payload["tags"] = tag_list
            st, data = self.request_json("POST", "/tickets", body=payload)
            if st not in (200, 201):
                raise FreshdeskError("Freshdesk ticket creation failed.", status=st, body=data)
            return {"ok": True, "http_status": st, "ticket": data}

        # Multipart: scalar fields as text, tags[]/attachments[] repeated.
        fields = _encode_form_fields(base)
        fields.extend(("tags[]", t) for t in tag_list)
        st, data = self._post_multipart("/tickets", fields, attachments)
        if st not in (200, 201):
            raise FreshdeskError("Freshdesk ticket creation failed.", status=st, body=data)
        return {"ok": True, "http_status": st, "ticket": data}

    def repeat_ticket(
        self,
        source_ticket_id: int,
        *,
        requester_email: str,
        requester_name: str | None = None,
        signature_html: str | None = None,
        group_id: int | None = None,
        source: Any = None,
        tags: list[str] | None = None,
        include_attachments: bool = True,
    ) -> dict[str, Any]:
        """Clone a source ticket into a brand-new ticket.

        Repeats the source title (subject) and text (HTML description, which keeps
        inline <img> images rendering inline), downloads and re-uploads every file
        attachment, appends the signature, and applies requester / group / tags.
        The ticket source is copied from the source ticket (numeric API value).
        Returns a small summary (no binary data) for the agent.
        """
        src = self.get_ticket(source_ticket_id)
        subject = src.get("subject") or ""
        description = src.get("description") or src.get("description_text") or ""
        if signature_html:
            description = f"{description}{signature_html}"

        files: list[tuple[str, str, bytes]] = []
        skipped: list[str] = []
        attachments = src.get("attachments") if isinstance(src.get("attachments"), list) else []
        if include_attachments:
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                url = att.get("attachment_url") or att.get("url")
                fname = att.get("name") or f"attachment-{att.get('id', 'file')}"
                ctype = att.get("content_type") or "application/octet-stream"
                if not url:
                    skipped.append(str(fname))
                    continue
                try:
                    files.append((str(fname), str(ctype), self.download_bytes(str(url))))
                except Exception:  # noqa: BLE001 - one bad attachment must not abort the repeat
                    skipped.append(str(fname))

        extra_fields: dict[str, Any] = {}
        custom_fields = src.get("custom_fields")
        if isinstance(custom_fields, dict):
            cleaned = {
                str(k): v for k, v in custom_fields.items() if v is not None and v != ""
            }
            if cleaned:
                extra_fields["custom_fields"] = cleaned
        ticket_type = src.get("type")
        if ticket_type:
            extra_fields["type"] = ticket_type
        product_id = src.get("product_id")
        if product_id is not None:
            pid = int(product_id)
            extra_fields["product_id"] = pid
            primary_ec = self.primary_email_config_id(pid)
            if primary_ec is not None:
                extra_fields["email_config_id"] = primary_ec
            elif src.get("email_config_id") is not None:
                extra_fields["email_config_id"] = src.get("email_config_id")

        ticket_source = src.get("source")
        if isinstance(ticket_source, int):
            effective_source: Any = ticket_source
        elif ticket_source is not None and str(ticket_source).isdigit():
            effective_source = int(str(ticket_source))
        elif source is not None:
            effective_source = self.resolve_source(source)
        else:
            effective_source = None

        result = self.create_ticket(
            subject=subject,
            description=description,
            email=requester_email,
            name=requester_name,
            group_id=group_id,
            source=effective_source,
            tags=tags,
            attachments=files or None,
            extra_fields=extra_fields or None,
        )
        new_ticket = result.get("ticket") if isinstance(result, dict) else None
        new_id = new_ticket.get("id") if isinstance(new_ticket, dict) else None
        inline_count = len(re.findall(r"<img[\s>]", description, flags=re.IGNORECASE))
        return {
            "ok": True,
            "source_ticket_id": source_ticket_id,
            "new_ticket_id": new_id,
            "subject": subject,
            "attachments_repeated": len(files),
            "attachments_skipped": skipped,
            "inline_images_in_body": inline_count,
        }

    def _post_multipart(
        self,
        path: str,
        fields: list[tuple[str, str]],
        files: list[tuple[str, str, bytes]],
    ) -> tuple[int, Any]:
        """POST multipart/form-data using stdlib only. files: (name, ctype, bytes)."""
        boundary = "----MossyBoundary" + uuid.uuid4().hex
        crlf = b"\r\n"
        buf = bytearray()
        for name, value in fields:
            buf += b"--" + boundary.encode() + crlf
            buf += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
            buf += str(value).encode("utf-8") + crlf
        for filename, ctype, content in files:
            safe = filename.replace('"', "")
            buf += b"--" + boundary.encode() + crlf
            buf += (
                f'Content-Disposition: form-data; name="attachments[]"; filename="{safe}"'
            ).encode("utf-8") + crlf
            buf += f"Content-Type: {ctype}".encode() + crlf + crlf
            buf += content + crlf
        buf += b"--" + boundary.encode() + b"--" + crlf

        url = self._url(path)
        headers = {
            "Authorization": f"Basic {self.api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        req = urllib.request.Request(url, data=bytes(buf), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
                code = resp.getcode() or 200
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            code = exc.code
        if not raw:
            return code, None
        try:
            return code, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return code, {"_raw": raw.decode("utf-8", errors="replace")}

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

    async def list_ticket_sources() -> dict[str, int]:
        """List available ticket-source labels mapped to their numeric API value."""
        return client.list_ticket_sources()

    async def create_ticket(
        subject: str,
        description: str,
        email: str | None = None,
        name: str | None = None,
        group_id: int | None = None,
        source: Any = None,
        tags: list[str] | None = None,
        status: int = 2,
        priority: int = 1,
    ) -> dict[str, Any]:
        """Create a new ticket (no file attachments via this tool; use repeat_ticket
        to clone a ticket with its attachments). description is HTML. source may be
        an int or a label string such as "Website"."""
        return client.create_ticket(
            subject=subject,
            description=description,
            email=email,
            name=name,
            group_id=group_id,
            source=source,
            tags=tags,
            status=status,
            priority=priority,
        )

    async def repeat_ticket(
        source_ticket_id: int,
        requester_email: str,
        requester_name: str | None = None,
        signature_html: str | None = None,
        group_id: int | None = None,
        source: Any = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Clone a source ticket into a new ticket: repeats subject, HTML body
        (keeping inline images), and all file attachments; appends signature_html;
        sets requester (email/name), group_id, source, and tags. Returns a summary
        with the new ticket id and counts."""
        return client.repeat_ticket(
            source_ticket_id,
            requester_email=requester_email,
            requester_name=requester_name,
            signature_html=signature_html,
            group_id=group_id,
            source=source,
            tags=tags,
        )

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
                list_ticket_sources,
                create_ticket,
                repeat_ticket,
            ],
            id="freshdesk",
            instructions=(
                "These tools implement the `freshdesk` skill: raw Freshdesk API access. "
                "Use reads (tickets, conversations, contacts, companies, articles) to gather "
                "context, and writes (replies, private notes, ticket updates, tags, new "
                "tickets) only when a task explicitly calls for them. Reply/note/description "
                "bodies are HTML. Use `repeat_ticket` to clone an existing ticket (subject, "
                "HTML body with inline images, and file attachments) into a new one. Follow "
                "any project-specific freshdesk policy skill for how and when to act."
            ),
        )
    )
