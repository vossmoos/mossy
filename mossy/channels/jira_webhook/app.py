"""Jira webhook channel: accept Jira webhooks and enqueue Jira test tasks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from mossy.runtime.models import Envelope, Priority

if TYPE_CHECKING:
    from mossy.runtime import Runtime

_DEFAULT_PATH = "/webhooks/jira"
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def configured_webhook_secret() -> str:
    return (os.getenv("JIRA_WEBHOOK_SECRET") or "").strip()


def configured_webhook_path() -> str:
    value = (os.getenv("JIRA_WEBHOOK_PATH") or _DEFAULT_PATH).strip()
    return value or _DEFAULT_PATH


def normalize_webhook_path(path: str) -> str:
    return path.rstrip("/") or "/"


def verify_hub_signature(raw_body: bytes, secret: str, signature_header: str) -> bool:
    """Verify Jira Cloud X-Hub-Signature (WebSub: sha256=<hex_hmac_of_raw_body>)."""
    if not secret or not signature_header.strip():
        return False

    header = signature_header.strip()
    if "=" not in header:
        return False

    method, _, received = header.partition("=")
    if method.lower() != "sha256" or not received:
        return False

    computed = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, received.lower())


def configured_trigger_status() -> str:
    return (os.getenv("JIRA_WEBHOOK_TRIGGER_STATUS") or "For Test").strip()


def is_trigger_status_transition(payload: Any) -> bool:
    """True when changelog contains status -> configured trigger status (default For Test)."""
    if not isinstance(payload, dict):
        return False

    trigger = configured_trigger_status()
    changelog = payload.get("changelog")
    if not isinstance(changelog, dict):
        return False

    items = changelog.get("items")
    if not isinstance(items, list):
        return False

    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("field") or "").lower() != "status":
            continue
        to_status = str(item.get("toString") or "").strip()
        if to_status == trigger:
            return True
    return False


def _normalize_issue_key(raw: str, *, project_key: str) -> str | None:
    value = raw.strip().upper()
    if not value:
        return None
    if _ISSUE_KEY_RE.fullmatch(value):
        if project_key and not value.startswith(f"{project_key.upper()}-"):
            return None
        return value
    if project_key and re.fullmatch(r"\d+", value):
        return f"{project_key.upper()}-{value}"
    return None


def extract_issue_key(payload: Any, *, project_key: str = "") -> str | None:
    """Best-effort issue key from Jira native webhook or Automation custom JSON."""
    if isinstance(payload, str):
        text = payload.strip()
        if _ISSUE_KEY_RE.fullmatch(text.upper()):
            return _normalize_issue_key(text, project_key=project_key)
        return None

    if not isinstance(payload, dict):
        return None

    for key in ("issue_key", "issueKey", "key"):
        direct = payload.get(key)
        if isinstance(direct, str):
            normalized = _normalize_issue_key(direct, project_key=project_key)
            if normalized:
                return normalized

    issue = payload.get("issue")
    if isinstance(issue, dict):
        issue_key = issue.get("key")
        if isinstance(issue_key, str):
            normalized = _normalize_issue_key(issue_key, project_key=project_key)
            if normalized:
                return normalized

    return None


def register_jira_webhook_routes(app: FastAPI, runtime: "Runtime") -> str | None:
    """Mount the Jira webhook POST endpoint when JIRA_WEBHOOK_SECRET is configured."""
    expected_secret = configured_webhook_secret()
    if not expected_secret:
        return None

    path = normalize_webhook_path(configured_webhook_path())
    project_key = (os.getenv("JIRA_SPACE_NAME") or "").strip()

    async def handle_jira_webhook(request: Request) -> dict[str, str | bool]:
        raw_body = await request.body()
        signature = request.headers.get("X-Hub-Signature") or ""
        if not verify_hub_signature(raw_body, expected_secret, signature):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing X-Hub-Signature.",
            )

        payload: Any = {}
        if raw_body:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Request body must be JSON when present.",
                ) from exc

        if not is_trigger_status_transition(payload):
            return {
                "ok": True,
                "accepted": False,
                "ignored": True,
                "reason": "status_not_for_test",
            }

        issue_key = extract_issue_key(payload, project_key=project_key)
        if issue_key is None:
            raise HTTPException(
                status_code=400,
                detail="Could not determine Jira issue key from webhook payload.",
            )

        task = await runtime.submit(
            Envelope(
                payload=f"test jira ticket {issue_key}",
                priority=int(Priority.USER_INPUT),
                source="jira-webhook",
                raw={
                    "issue_key": issue_key,
                    "webhook_payload": payload if isinstance(payload, dict) else {},
                },
            )
        )
        return {
            "ok": True,
            "accepted": True,
            "task_id": task.id,
            "issue_key": issue_key,
        }

    app.post(path)(handle_jira_webhook)
    if path != "/":
        app.post(f"{path}/")(handle_jira_webhook)
    print(
        f"Jira webhook channel enabled at POST {path} "
        "(X-Hub-Signature + JIRA_WEBHOOK_SECRET; not MOSSY_API_KEY).",
        file=sys.stderr,
        flush=True,
    )
    return path
