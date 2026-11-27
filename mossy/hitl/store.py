"""Approval requests on disk, in DuckDB: what waits for a person survives a restart.

The file is ``MOSSY_DUCKDB_PATH`` when set, otherwise ``<repo>/data/mossy.duckdb``. A skill that
keeps its own tables in DuckDB can use the same file: one repo, one file.

Every operation opens the file, does one thing and closes it: nothing holds the file between
operations, and a new process sees what the last one wrote. A read never creates the file or the
table — a repo that never asks for an approval never gets either.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from pydantic import BaseModel

from mossy.hitl.base import PENDING

if TYPE_CHECKING:
    # Not at import time: the runtime package imports this one.
    from mossy.runtime.models import Envelope

DEFAULT_DB_RELATIVE = Path("data") / "mossy.duckdb"
TABLE = "hitl_requests"

_COLUMNS = (
    "id",
    "envelope",
    "medium",
    "external_id",
    "status",
    "summary",
    "requester",
    "origin_channel",
    "origin_conversation",
    "dedupe_key",
    "created_at",
    "updated_at",
    "decided_at",
    "decision_by",
    "decision_comment",
)
_TIMESTAMPS = frozenset({"created_at", "updated_at", "decided_at"})

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id                   VARCHAR    PRIMARY KEY,
    envelope             VARCHAR    NOT NULL,
    medium               VARCHAR    NOT NULL,
    external_id          VARCHAR,
    status               VARCHAR    NOT NULL,
    summary              VARCHAR,
    requester            VARCHAR,
    origin_channel       VARCHAR,
    origin_conversation  VARCHAR,
    dedupe_key           VARCHAR,
    created_at           TIMESTAMP  NOT NULL,
    updated_at           TIMESTAMP  NOT NULL,
    decided_at           TIMESTAMP,
    decision_by          VARCHAR,
    decision_comment     VARCHAR
)
"""


def database_path(repo_root: Path) -> Path:
    """``MOSSY_DUCKDB_PATH`` when set, otherwise ``<repo>/data/mossy.duckdb``."""
    configured = (os.environ.get("MOSSY_DUCKDB_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return repo_root / DEFAULT_DB_RELATIVE


class ApprovalRequest(BaseModel):
    """One task waiting for, or decided by, a person."""

    id: str
    #: The task, as ``Envelope.model_dump_json()`` wrote it. Never edited after it is stored.
    envelope: str
    medium: str
    external_id: str | None = None
    status: str = PENDING
    #: A few words on what is asked. It is all a rejection hands back of the task itself.
    summary: str | None = None
    requester: str | None = None
    origin_channel: str | None = None
    #: Whatever the channel gave to find the conversation again. Opaque here: stored as JSON,
    #: returned as it was given, never read.
    origin_conversation: Any = None
    dedupe_key: str | None = None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None
    decision_by: str | None = None
    decision_comment: str | None = None

    def task_envelope(self) -> Envelope:
        """The stored task, read back."""
        from mossy.runtime.models import Envelope

        return Envelope.model_validate_json(self.envelope)

    @property
    def task_text(self) -> str:
        """The words of the stored task, exactly as they will be queued: what an approver must see."""
        return self.task_envelope().payload


def _naive_utc(value: datetime | None) -> datetime | None:
    """The columns are TIMESTAMP, which carries no zone, so one zone only is stored."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _request(row: tuple[Any, ...]) -> ApprovalRequest:
    data = dict(zip(_COLUMNS, row))
    for name in _TIMESTAMPS:
        if data[name] is not None:
            data[name] = data[name].replace(tzinfo=UTC)
    if data["origin_conversation"] is not None:
        data["origin_conversation"] = json.loads(data["origin_conversation"])
    return ApprovalRequest(**data)


class ApprovalStore:
    """The ``hitl_requests`` table of one DuckDB file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ connections
    @contextmanager
    def _writing(self) -> Iterator[Any]:
        import duckdb

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(str(self.path))
        try:
            connection.execute(CREATE_TABLE_SQL)
            yield connection
        finally:
            connection.close()

    def _read(self, where: str, parameters: list[Any]) -> list[ApprovalRequest]:
        """Rows matching ``where``, oldest first. No file or no table means no rows."""
        if not self.path.is_file():
            return []
        import duckdb

        connection = duckdb.connect(str(self.path), read_only=True)
        try:
            rows = connection.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM {TABLE} WHERE {where} ORDER BY created_at, id",
                parameters,
            ).fetchall()
        except duckdb.CatalogException:
            return []
        finally:
            connection.close()
        return [_request(row) for row in rows]

    # ------------------------------------------------------------------ reads
    def get(self, request_id: str) -> ApprovalRequest | None:
        rows = self._read("id = ?", [request_id])
        return rows[0] if rows else None

    def pending(self) -> list[ApprovalRequest]:
        return self._read("status = ?", [PENDING])

    def has_pending(self) -> bool:
        return bool(self.pending())

    def find_pending(self, dedupe_key: str) -> ApprovalRequest | None:
        rows = self._read("status = ? AND dedupe_key = ?", [PENDING, dedupe_key])
        return rows[0] if rows else None

    # ------------------------------------------------------------------ writes
    def add(self, request: ApprovalRequest) -> None:
        values = request.model_dump()
        for name in _TIMESTAMPS:
            values[name] = _naive_utc(values[name])
        if values["origin_conversation"] is not None:
            values["origin_conversation"] = json.dumps(values["origin_conversation"])
        with self._writing() as connection:
            connection.execute(
                f"INSERT INTO {TABLE} ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                [values[name] for name in _COLUMNS],
            )

    def set_external_id(self, request_id: str, external_id: str) -> None:
        with self._writing() as connection:
            connection.execute(
                f"UPDATE {TABLE} SET external_id = ?, updated_at = ? WHERE id = ?",
                [external_id, _now(), request_id],
            )

    def decide(self, request_id: str, status: str, by: str | None, comment: str | None) -> bool:
        """Record a decision on a pending request. False when it was no longer pending."""
        now = _now()
        with self._writing() as connection:
            changed = connection.execute(
                f"UPDATE {TABLE} SET status = ?, decision_by = ?, decision_comment = ?, "
                "decided_at = ?, updated_at = ? WHERE id = ? AND status = ? RETURNING id",
                [status, by, comment, now, now, request_id, PENDING],
            ).fetchall()
        return bool(changed)

    def delete(self, request_id: str) -> None:
        with self._writing() as connection:
            connection.execute(f"DELETE FROM {TABLE} WHERE id = ?", [request_id])
