# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Which Jira issue belongs to a Wrike task, and which of its comments were already copied.

This is what makes point 2 of the Wrike → Jira flow possible: *"If there is a new comment to the ticket
which was already created in our Jira - it should be left as a comment to the existing Jira
ticket."* A notification says only that something happened on task X; answering "and does X
already have an issue?" needs something remembered.

**The table is an index, not the truth.** The table says which key to look at; Jira says whether
that key still exists. A 404 for that key does not settle it: an unauthenticated client is served
404 too, so "the row is dead, open a new issue" would open a duplicate for every task whenever the
token is bad. The decision (``decide_wrike_action``) reads it as ``link_unconfirmable`` and
recommends nothing. Nothing here can enforce that — it stores and reads — but the rule belongs
beside the storage, because the storage is what tempts a caller to skip the check.

One caveat on that rule, and it comes out of the label search's own measurements rather than from doubt:
an **unauthenticated** client is served 404 by ``GET /issue/{key}`` too. So "404 means the row is
dead" is only safe once the credentials are known good. Acting on it with a bad token would open
a duplicate issue for every task that already has one. See ``jira_label_search`` for the same
trap on the search side, where it is worse because it does not even look like a failure.

**Its own database file, deliberately not the Freshdesk flow's.** ``dedupe.duckdb`` is a cache
with an expiry: ``find_similar`` only looks back three working days, so a row there stops being
found while it still exists, and that is correct for what it does. This is a register — a link
that must still be readable a year later, and a list of comments that must never be forgotten,
because forgetting one copies it into Jira a second time. Two lifetimes, two files.

★ **A READ THAT FAILS RAISES. IT DOES NOT RETURN "NOTHING".** (the per-task lock, and the defect was here.)
``linked_issue`` returns ``None`` for "no row" and ``copied_comment_ids`` returns an empty set for
"nothing copied yet" — both are answers. A database that cannot be opened is not an answer, and
until the per-task lock it arrived as one of those two, which is the exact collapse this flow has been
naming since the label search, one level down and inside its own house. A caller acting on the difference
opens a duplicate Jira issue, or copies a comment a second time, and neither can be deleted.

It is not hypothetical. **DuckDB takes an exclusive lock on the file, so a second process is
refused outright** — measured 11 September 2026, and it is what happens when two runs
overlap. With the old behaviour a task whose row was sitting in the table came
back as "no link remembered".

Writes are deliberately the other way round: ``remember_link`` and ``remember_comment`` report
``database_error`` in their result instead of raising. A write that failed has already not
happened and the caller's next move is to report it; a read that failed is an answer the caller
is about to believe.

**Comment ids, not a high-water timestamp.** One row per comment copied. A timestamp would be
one row instead of N, and it would lose a comment that was edited or inserted with an earlier
date — silently. Whether Wrike does that cannot be measured from here, and an unmeasured risk
takes the safe option. The cost is real and is N rows per task.

Environment:

    WRIKE_JIRA_LINKS_DB   default ~/.local/share/mossy/wrike-jira-links.duckdb

Self-contained: no ``import mossy``, no ``python-dotenv``. Reads and writes one local DuckDB file
and nothing else — **it never talks to Jira or to Wrike.**
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any

import duckdb

_DEFAULT_DB = "~/.local/share/mossy/wrike-jira-links.duckdb"

LINK_TABLE = "wrike_jira_link"
COMMENT_TABLE = "copied_comment"

#: Said once, and attached wherever a caller could mistake a row for a fact about Jira.
TABLE_IS_AN_INDEX = (
    "The table says which key to look at; Jira says whether that key still exists. A 404 for the "
    "key does not mean the row is dead — an unauthenticated client is served 404 too — so it is "
    "never, on its own, a reason to open a new issue."
)

CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {LINK_TABLE} (
    -- The Wrike API id (MAAAAAEXmain), which is what the notification header carries and what
    -- the Wrike API accepts. Never the permalink number: that is a different identifier and
    -- the API refuses it (measured).
    task_api_id  VARCHAR PRIMARY KEY,
    jira_key     VARCHAR NOT NULL,
    jira_url     VARCHAR,
    linked_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS {COMMENT_TABLE} (
    -- One row per comment already copied into Jira. The e-mail notification carries no comment
    -- id at all — measured — so the only way to know what is new is to fetch the task's comments
    -- from the API and subtract what is here.
    task_api_id  VARCHAR NOT NULL,
    comment_id   VARCHAR NOT NULL,
    copied_at    TIMESTAMP NOT NULL,
    PRIMARY KEY (task_api_id, comment_id)
)
"""


class LinkStoreError(RuntimeError):
    """The register could not be read. **Never raised for "no row" — only for "I could not
    look".** Carries the original failure as ``__cause__`` so a locked file can be told from a
    corrupt one."""


def database_path() -> Path:
    return Path(os.environ.get("WRIKE_JIRA_LINKS_DB") or _DEFAULT_DB).expanduser()


def _now() -> _dt.datetime:
    """Naive UTC: the columns are TIMESTAMP, which carry no zone, so store one zone only."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _absent(db_path: Path | str | None = None) -> bool:
    """True when the register file does not exist yet.

    A read then answers "nothing recorded" and does NOT create it: ``_connect`` makes the
    directory, the file and both tables, and before the Wrike → Jira flow piece 3 a describe did exactly that —
    measured, a test-mode pass that wrote to the register. Only a write creates the file now. A
    file that does not exist holds no rows, so this is an answer, not a failure: a file that
    exists and cannot be opened still raises. If the path is simply wrong, the Jira label search
    is the rescue, as for a register that was deleted (the per-task lock).

    **Absent means "could be created", not "is not there".** A path whose nearest existing
    ancestor is a file can never exist; that is unreadable, and it still raises. A first version
    returned True for it — measured: seven per-task lock tests went red, because "I could not look" had
    turned back into "nothing recorded".
    """
    path = Path(db_path).expanduser() if db_path else database_path()
    if path.exists():
        return False
    ancestor = path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    return ancestor.is_dir()


def _connect(db_path: Path | str | None = None):
    path = Path(db_path).expanduser() if db_path else database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute(CREATE_SQL)
    return conn


def _close(conn: Any) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing must not mask the real outcome
            pass


# ------------------------------------------------------------------ the link
def remember_link(
    task_api_id: str,
    jira_key: str,
    jira_url: str | None = None,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Record that this Wrike task has this Jira issue. Re-recording the same pair is harmless.

    Keyed by ``task_api_id``: one task, one issue. A second call for the same task **replaces**
    the row, because a task whose issue was recreated should point at the new one — and because
    silently keeping the old key would be the dead row this module warns about.
    """
    task, key = str(task_api_id).strip(), str(jira_key).strip()
    result: dict[str, Any] = {"ok": False, "task_api_id": task}
    if not task or not key:
        result["reason"] = "task_api_id and jira_key are both required"
        return result
    conn = None
    try:
        conn = _connect(db_path)
        conn.execute(f"DELETE FROM {LINK_TABLE} WHERE task_api_id = ?", [task])
        conn.execute(
            f"INSERT INTO {LINK_TABLE} (task_api_id, jira_key, jira_url, linked_at) "
            "VALUES (?, ?, ?, ?)",
            [task, key, (jira_url or "").strip() or None, _now()],
        )
        result.update(ok=True, jira_key=key, jira_url=(jira_url or "").strip() or None)
        return result
    except Exception as exc:  # noqa: BLE001 - a store failure must not reach the flow
        result["reason"] = "database_error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        _close(conn)


def linked_issue(
    task_api_id: str, *, db_path: Path | str | None = None
) -> dict[str, Any] | None:
    """The Jira issue recorded for this task, or ``None``.

    ``None`` means "nothing remembered here", never "no such issue in Jira": the row could have
    been written on another machine, or this file could be new. And a row that IS here is not
    proof the issue still exists — see ``TABLE_IS_AN_INDEX``.

    **Raises ``LinkStoreError`` when the register cannot be read at all** (the per-task lock). That is not
    ``None``: ``None`` is an answer and a caller may create a Jira issue on it.
    """
    if _absent(db_path):
        return None
    conn = None
    try:
        conn = _connect(db_path)
        row = conn.execute(
            f"SELECT task_api_id, jira_key, jira_url, linked_at FROM {LINK_TABLE} "
            "WHERE task_api_id = ?",
            [str(task_api_id).strip()],
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - an unreadable register is never "nothing remembered"
        raise LinkStoreError(
            f"The register could not be read for task {str(task_api_id).strip()}. This is not "
            "the same as having no link recorded: acting on it would create a duplicate issue."
        ) from exc
    finally:
        _close(conn)
    if row is None:
        return None
    return {
        "task_api_id": row[0],
        "jira_key": row[1],
        "jira_url": row[2],
        "linked_at": row[3],
        "still_exists_in_jira": None,
        "detail": TABLE_IS_AN_INDEX,
    }


# ------------------------------------------------------------------ the comments
def remember_comment(
    task_api_id: str,
    comment_id: str,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Record that this comment was copied. Recording it twice leaves one row.

    Idempotent on purpose rather than by luck: the primary key is (task, comment), and a repeat
    is reported as ``already_recorded`` instead of raising. A pass that dies half way through
    and runs again must not be able to duplicate a comment in Jira.
    """
    task, comment = str(task_api_id).strip(), str(comment_id).strip()
    result: dict[str, Any] = {"ok": False, "task_api_id": task, "comment_id": comment}
    if not task or not comment:
        result["reason"] = "task_api_id and comment_id are both required"
        return result
    conn = None
    try:
        conn = _connect(db_path)
        existing = conn.execute(
            f"SELECT copied_at FROM {COMMENT_TABLE} WHERE task_api_id = ? AND comment_id = ?",
            [task, comment],
        ).fetchone()
        if existing is not None:
            result.update(ok=True, recorded=False, reason="already_recorded",
                          copied_at=existing[0])
            return result
        conn.execute(
            f"INSERT INTO {COMMENT_TABLE} (task_api_id, comment_id, copied_at) "
            "VALUES (?, ?, ?)",
            [task, comment, _now()],
        )
        result.update(ok=True, recorded=True)
        return result
    except Exception as exc:  # noqa: BLE001
        result["reason"] = "database_error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        _close(conn)


def copied_comment_ids(
    task_api_id: str, *, db_path: Path | str | None = None
) -> set[str]:
    """Every comment id already copied for this task. An empty set when none were.

    A set rather than a list: the only question ever asked of it is membership.

    **Raises ``LinkStoreError`` when the register cannot be read at all** (the per-task lock). An empty set
    means "nothing copied yet", which is an answer a caller copies comments on the strength of.
    """
    if _absent(db_path):
        return set()
    conn = None
    try:
        conn = _connect(db_path)
        rows = conn.execute(
            f"SELECT comment_id FROM {COMMENT_TABLE} WHERE task_api_id = ?",
            [str(task_api_id).strip()],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - an unreadable register is never "nothing copied"
        raise LinkStoreError(
            f"The register could not be read for task {str(task_api_id).strip()}. This is not "
            "the same as having copied nothing: acting on it would copy comments twice."
        ) from exc
    finally:
        _close(conn)
    return {row[0] for row in rows}


def comments_not_yet_copied(
    task_api_id: str, comments: Any, *, db_path: Path | str | None = None
) -> list[dict[str, Any]]:
    """The comments the Wrike API returned that are not already recorded, in the order given.

    ``comments`` is what ``wrike_api.get_task_comments`` puts under ``comments``. A comment with
    no ``id`` is **kept**, not dropped: it cannot be matched against the record, and losing it
    silently would be worse than the operator seeing one they may already have.

    **Propagates ``LinkStoreError``** rather than catching it (the per-task lock). This is the function a
    caller actually calls, so flattening the failure here would lose the noise made below and
    hand back "everything is new" — which is a full re-copy of the task.
    """
    already = copied_comment_ids(task_api_id, db_path=db_path)
    if not isinstance(comments, list):
        return []
    return [
        c for c in comments
        if isinstance(c, dict) and str(c.get("id") or "").strip() not in already
    ]


__all__ = [
    "COMMENT_TABLE",
    "CREATE_SQL",
    "LINK_TABLE",
    "TABLE_IS_AN_INDEX",
    "LinkStoreError",
    "comments_not_yet_copied",
    "copied_comment_ids",
    "database_path",
    "linked_issue",
    "remember_comment",
    "remember_link",
]
