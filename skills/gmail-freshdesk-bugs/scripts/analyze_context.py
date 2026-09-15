# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Context analysis of a Freshdesk mention -> a problem summary ready for Jira.

Given a ticket and **the text of the mention itself**, this reads the ticket's whole thread as
background and asks the model to describe **what is being asked of us** — not the end customer's
incident.

The mention text is passed in, not looked up, and that is the change measurement forced. The original
version took a ``--conversation-id`` and selected "the mentioned conversation" from the API. That
hypothesis is measured false: of four thread messages that produced a notification, exactly one
came back from ``get_ticket_conversations``, and only because it happened to be the note the
thread hangs from. The other three exist in no conversation at all. ``parse_mention_email.py``
reads the text from the notification e-mail, which carries it whole — 2005 characters over three
paragraphs, measured, without truncating.

``--conversation-id`` is **gone**, not deprecated. The e-mail's ``X-FD-Note-Id`` is the thread's
anchor and repeats across every notification of that thread while the text changes; while a
conversation flag existed, somebody would pass the anchor to it and file a Jira issue about the
wrong content. The anchor is accepted separately as ``--thread-anchor-note-id``, used only to mark
that note in the context.

The client's own words frame the whole task: "we don't work with end-customer problem, we work
with our customer references about the quality of our triage agents." So the problem to solve is
the request/complaint/correction directed at us; the customer's incident is only background. The
prompt says this explicitly.

Self-contained (like ``dedupe_cache.py``): no ``import mossy``, no ``python-dotenv``; its own
urllib clients for Freshdesk and OpenAI. Config from the environment:

    ANALYSIS_MODEL     default "gpt-5.4-mini"
    OPENAI_API_KEY     required for the model call
    FRESHDESK_DOMAIN, FRESHDESK_API_KEY   the Basic-auth pair (key is the pre-encoded value)

CLI (named flags only): analyze_context.py --ticket-id 102 --mention-text "…" \
    [--thread-anchor-note-id 900000000003]
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

_DEFAULT_MODEL = "gpt-5.4-mini"
_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


def _model() -> str:
    return (os.environ.get("ANALYSIS_MODEL") or _DEFAULT_MODEL).strip()


class FreshdeskError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# --------------------------------------------------------------------------- Freshdesk (urllib)
@dataclass
class AnalyzeFreshdeskClient:
    domain: str
    api_key: str
    timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> "AnalyzeFreshdeskClient | None":
        domain = (os.environ.get("FRESHDESK_DOMAIN") or "").strip()
        api_key = (os.environ.get("FRESHDESK_API_KEY") or "").strip()
        if not domain or not api_key:
            return None
        return cls(domain=domain, api_key=api_key)

    def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        status, data = self.request_json("GET", f"/tickets/{int(ticket_id)}", query={"include": "requester"})
        if status != 200 or not isinstance(data, dict):
            raise FreshdeskError(f"Ticket {ticket_id} fetch failed.", status=status, body=data)
        return data

    def get_ticket_conversations(self, ticket_id: int, *, max_pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            status, data = self.request_json(
                "GET", f"/tickets/{int(ticket_id)}/conversations",
                query={"page": str(page), "per_page": "100"},
            )
            if status != 200:
                raise FreshdeskError("Conversations fetch failed.", status=status, body=data)
            if not isinstance(data, list) or not data:
                break
            out.extend(data)
            if len(data) < 100:
                break
        return out

    def request_json(self, method, path, *, query=None, body=None) -> tuple[int, Any]:
        url = self._url(path, query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Basic {self.api_key}", "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw, code = resp.read(), (resp.getcode() or 200)
        except urllib.error.HTTPError as exc:
            raw, code = (exc.read() if exc.fp else b""), exc.code
        if not raw:
            return code, None
        try:
            return code, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return code, {"_raw": raw.decode("utf-8", errors="replace")}

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        clean = path if path.startswith("/") else f"/{path}"
        base = f"https://{self.domain.rstrip('/')}/api/v2{clean}"
        return base + ("?" + urllib.parse.urlencode(query) if query else "")


# --------------------------------------------------------------------------- model (urllib)
def _chat(system_prompt: str, user_prompt: str) -> str:
    """One chat completion via the OpenAI API using urllib (no ``openai`` library)."""
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot analyze.")
    payload = json.dumps({
        "model": _model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        _OPENAI_CHAT_URL, data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500] if exc.fp else ""
        raise RuntimeError(f"OpenAI chat failed (HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI chat unreachable: {exc.reason}") from exc
    return str(data["choices"][0]["message"]["content"] or "")


# --------------------------------------------------------------------------- prompt
_SYSTEM_PROMPT = (
    "You are triaging an internal support thread for one of our customers and writing a Jira issue for our own "
    "engineering team.\n\n"
    "Read this rule carefully, it decides everything:\n\n"
    "  We do NOT work on the end customer's problem. We work on our OWN customers' feedback about "
    "the quality of our triage agents. The problem to solve is what is being asked OF US in the "
    "mentioned thread — the request, complaint, question, or correction directed at our team or at "
    "our triage agent. It is NOT the end customer's incident.\n\n"
    "  Treat the end customer's incident only as background context. Never turn the customer's "
    "incident into the Jira issue. If the mentioned thread asks us to fix, review, explain, or "
    "improve something on our side, THAT — and only that — is the problem you describe.\n\n"
    "Return ONE JSON object and nothing else:\n"
    '  {"summary": "<one line, imperative, suitable as a Jira issue title>",\n'
    '   "problem": "<several short paragraphs describing what WE are being asked to do, suitable as '
    'the Jira description>"}\n\n'
    "Write in English."
)


def _conversation_body(conv: dict[str, Any]) -> str:
    text = conv.get("body_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return str(conv.get("body") or "").strip()


def build_user_prompt(subject: str, mention_text: str, thread_bodies: list[str]) -> str:
    """Assemble the user message: the mention to act on, then the whole ticket as context.

    The mention is passed in rather than looked up. Measured: of four thread messages
    that triggered a notification, only one came back from ``get_ticket_conversations`` — the one
    that happened to be the note the thread hangs from. The other three exist nowhere in the API,
    so a version of this function that fetched "the mentioned conversation" would analyse the
    wrong text, or nothing at all.
    """
    context = "\n\n---\n\n".join(thread_bodies) if thread_bodies else "(no messages in the ticket)"
    return (
        f"Ticket subject: {subject}\n\n"
        "=== MENTIONED THREAD — this is what you must act on ===\n"
        f"{mention_text}\n\n"
        "=== THE TICKET THREAD (context only — the end customer's incident) ===\n"
        f"{context}\n"
    )


# --------------------------------------------------------------------------- public API
def analyze(
    ticket_id: int,
    mention_text: str,
    *,
    thread_anchor_note_id: int | None = None,
    freshdesk_url: str | None = None,
    fd_client: AnalyzeFreshdeskClient | None = None,
) -> dict[str, Any]:
    """Summarize what the mention asks of us, ready for a Jira issue.

    ``mention_text`` is the message we were mentioned in, taken from the notification e-mail by
    ``parse_mention_email.py``. It is **not** looked up in Freshdesk: it was measured that a thread
    message is usually not a conversation at all.

    ``thread_anchor_note_id`` is the note the thread hangs from. It is context, never the thing to
    analyse — the same anchor is repeated across every notification of a thread while the text
    changes, so treating it as the mention produces an issue about the wrong content.

    ``freshdesk_url`` is the link out of the notification e-mail, which points at the thread we were
    mentioned in (``?thread=N``). It is passed through untouched. Rebuilding it from the ticket id
    is only the fallback for a caller that has no e-mail to hand: that form lands on the ticket and
    leaves the reader to find the thread.
    """
    text = (mention_text or "").strip()
    if not text:
        # Refused before the model is called: an empty mention would become a Jira issue with no
        # description, which reads exactly like a real one.
        return {"ok": False, "reason": "empty_mention_text", "ticket_id": ticket_id,
                "thread_anchor_note_id": thread_anchor_note_id}

    fd_client = fd_client or AnalyzeFreshdeskClient.from_env()
    if fd_client is None:
        return {"ok": False, "reason": "freshdesk_not_configured", "ticket_id": ticket_id,
                "thread_anchor_note_id": thread_anchor_note_id}

    ticket = fd_client.get_ticket(ticket_id)
    subject = str(ticket.get("subject") or "")
    conversations = fd_client.get_ticket_conversations(ticket_id)

    # The whole thread is context — every conversation, private notes included. Nothing is
    # selected: there is no "the mentioned conversation" to find.
    thread_bodies: list[str] = []
    anchor_found = False
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        body = _conversation_body(conv)
        if (thread_anchor_note_id is not None and conv.get("id") is not None
                and int(conv["id"]) == int(thread_anchor_note_id)):
            anchor_found = True
            body = f"[the note this thread hangs from]\n{body}"
        thread_bodies.append(body)

    user_prompt = build_user_prompt(subject, text, thread_bodies)
    raw = _chat(_SYSTEM_PROMPT, user_prompt)

    body = raw.strip()
    if body.startswith("```"):
        body = body.split("```")[1]
        body = body[4:] if body.lower().startswith("json") else body
    try:
        parsed = json.loads(body.strip())
    except json.JSONDecodeError:
        return {"ok": False, "reason": "model_output_unparseable", "ticket_id": ticket_id,
                "thread_anchor_note_id": thread_anchor_note_id, "raw": raw[:500]}

    # A url from the notification carries ?thread=N. Rebuilding it would drop the thread and
    # send whoever opens the Jira issue to the ticket instead.
    link = (freshdesk_url or "").strip()
    if not link:
        link = f"https://{fd_client.domain.rstrip('/')}/a/tickets/{int(ticket_id)}"
    return {
        "ok": True,
        "summary": str(parsed.get("summary") or "").strip(),
        "problem": str(parsed.get("problem") or "").strip(),
        "ticket_id": ticket_id,
        "thread_anchor_note_id": thread_anchor_note_id,
        "anchor_note_in_thread": anchor_found,
        "ticket_subject": subject,
        "freshdesk_url": link,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Analyze a Freshdesk mention into a Jira-ready problem.")
    p.add_argument("--ticket-id", type=int, required=True)
    p.add_argument("--mention-text", required=True,
                   help="The mention itself, verbatim from parse_mention_email.py.")
    # ``--conversation-id`` is deliberately absent, not deprecated. While it existed somebody would
    # pass the thread anchor note id to it and file an issue about the wrong text.
    p.add_argument("--thread-anchor-note-id", type=int, default=None,
                   help="Context only: the note the thread hangs from. Never the text to analyse.")
    p.add_argument("--freshdesk-url", default=None,
                   help="The link from the notification e-mail, verbatim. Kept as-is so the issue "
                        "points at the thread; without it the ticket url is rebuilt instead.")
    return p


def main() -> None:  # pragma: no cover - exercised end to end, not in the offline suite
    args = _build_parser().parse_args()
    out = analyze(args.ticket_id, args.mention_text,
                  thread_anchor_note_id=args.thread_anchor_note_id,
                  freshdesk_url=args.freshdesk_url)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
