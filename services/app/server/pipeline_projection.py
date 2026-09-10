"""Durable, rebuildable PostgreSQL projection of per-video pipeline state.

Canonical annotation, SAM3 and review files remain authoritative.  This module
only stores ordered intents and metadata snapshots that make library reads
cheap.  A failed apply deliberately remains pending so a reconciler can retry
it without inventing ordering from timestamps.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias


ProjectionStatus: TypeAlias = Literal["pending", "applied", "superseded"]

_CONNECT_TIMEOUT_SECONDS = 5
_MAX_ERROR_LENGTH = 1000
_URI_CREDENTIALS = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s/@]+)(@)"
)
_BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)([^\s,;]+)")
_ASSIGNED_SECRET = re.compile(
    r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)([^\s,;&]+)"
)
_JSON_SECRET = re.compile(
    r'(?i)(["\'](?:password|passwd|token|secret|api[_-]?key)["\']\s*:\s*["\'])(.*?)(["\'])'
)


class _ConnectionFactory(Protocol):
    def __call__(self) -> Any: ...


ConnectArg: TypeAlias = _ConnectionFactory | None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class ProjectionIntent:
    event_seq: int
    object_id: str
    video_id: str
    event_kind: str
    source_identity: dict[str, Any]
    status: ProjectionStatus
    error: str | None
    created_at: datetime | None
    updated_at: datetime | None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_seq": self.event_seq,
            "object_id": self.object_id,
            "video_id": self.video_id,
            "event_kind": self.event_kind,
            "source_identity": copy.deepcopy(self.source_identity),
            "status": self.status,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


@dataclass(frozen=True)
class ProjectionRecord:
    object_id: str
    video_id: str
    event_seq: int
    source_identity: dict[str, Any]
    snapshot: dict[str, Any]
    projected_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "video_id": self.video_id,
            "event_seq": self.event_seq,
            "source_identity": copy.deepcopy(self.source_identity),
            "snapshot": copy.deepcopy(self.snapshot),
            "projected_at": _iso(self.projected_at),
        }


def sanitize_error(error: object) -> str:
    """Return a bounded diagnostic safe to persist and expose operationally."""

    value = str(error).replace("\x00", "").replace("\r", " ").replace("\n", " ")
    value = _URI_CREDENTIALS.sub(r"\1[REDACTED]\3", value)
    value = _BEARER_TOKEN.sub(r"\1[REDACTED]", value)
    value = _ASSIGNED_SECRET.sub(r"\1[REDACTED]", value)
    value = _JSON_SECRET.sub(r"\1[REDACTED]\3", value)
    return value[:_MAX_ERROR_LENGTH]


def _canonical_json(value: Mapping[str, Any], *, field: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} deve ser um objeto JSON")
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} deve conter JSON valido") from exc


def _source_digest(serialized_identity: str) -> str:
    return hashlib.sha256(serialized_identity.encode("utf-8")).hexdigest()


def _validate_identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} nao pode ser vazio")
    return value.strip()


def _open_connection(
    database_url: str | None,
    connect: ConnectArg,
):
    if connect is not None:
        return connect()
    url = database_url if database_url is not None else os.environ.get("DATABASE_URL")
    if not url:
        return None
    import psycopg

    return psycopg.connect(url, connect_timeout=_CONNECT_TIMEOUT_SECONDS)


def _intent_from_row(row: tuple[Any, ...]) -> ProjectionIntent:
    return ProjectionIntent(
        event_seq=int(row[0]),
        object_id=str(row[1]),
        video_id=str(row[2]),
        event_kind=str(row[3]),
        source_identity=copy.deepcopy(row[4] or {}),
        status=row[5],
        error=row[6],
        attempts=int(row[7] or 0),
        created_at=row[8],
        updated_at=row[9],
    )


def _record_from_row(row: tuple[Any, ...]) -> ProjectionRecord:
    return ProjectionRecord(
        object_id=str(row[0]),
        video_id=str(row[1]),
        event_seq=int(row[2]),
        source_identity=copy.deepcopy(row[3] or {}),
        snapshot=copy.deepcopy(row[4] or {}),
        projected_at=row[5],
    )


_INTENT_COLUMNS = """
    event_seq, object_id, video_id, event_kind, source_identity,
    status, last_error, attempts, created_at, updated_at
"""
_RECORD_COLUMNS = """
    object_id, video_id, event_seq, source_identity, snapshot, projected_at
"""


def reserve_intent(
    *,
    object_id: str,
    video_id: str,
    event_kind: str,
    source_identity: Mapping[str, Any],
    database_url: str | None = None,
    connect: ConnectArg = None,
) -> ProjectionIntent | None:
    """Reserve or reuse the event for one exact canonical source identity."""

    object_id = _validate_identifier("object_id", object_id)
    video_id = _validate_identifier("video_id", video_id)
    event_kind = _validate_identifier("event_kind", event_kind)
    identity_json = _canonical_json(source_identity, field="source_identity")
    digest = _source_digest(identity_json)
    connection = _open_connection(database_url, connect)
    if connection is None:
        return None
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO video_pipeline_projection_events
                    (object_id, video_id, event_kind, source_identity, source_digest)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (object_id, video_id, event_kind, source_digest)
                DO UPDATE SET updated_at = video_pipeline_projection_events.updated_at
                RETURNING {_INTENT_COLUMNS}
                """,
                (object_id, video_id, event_kind, identity_json, digest),
            )
            row = cursor.fetchone()
    return _intent_from_row(row)


def _event_seq(intent: ProjectionIntent | int) -> int:
    value = intent.event_seq if isinstance(intent, ProjectionIntent) else intent
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("event_seq deve ser um inteiro positivo")
    return value


def apply_intent(
    intent: ProjectionIntent | int,
    snapshot: Mapping[str, Any],
    *,
    database_url: str | None = None,
    connect: ConnectArg = None,
) -> ProjectionRecord | None:
    """Conditionally apply an intent; an older event can never win."""

    event_seq = _event_seq(intent)
    snapshot_json = _canonical_json(snapshot, field="snapshot")
    connection = _open_connection(database_url, connect)
    if connection is None:
        return None
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT object_id, video_id, source_identity, source_digest
                  FROM video_pipeline_projection_events
                 WHERE event_seq = %s
                 FOR UPDATE
                """,
                (event_seq,),
            )
            event = cursor.fetchone()
            if event is None:
                raise LookupError(f"projection intent {event_seq} nao existe")
            object_id, video_id, source_identity, source_digest = event
            cursor.execute(
                f"""
                INSERT INTO video_pipeline_projection
                    (object_id, video_id, event_seq, source_identity,
                     source_digest, snapshot)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb)
                ON CONFLICT (object_id, video_id) DO UPDATE SET
                    event_seq = EXCLUDED.event_seq,
                    source_identity = EXCLUDED.source_identity,
                    source_digest = EXCLUDED.source_digest,
                    snapshot = EXCLUDED.snapshot,
                    projected_at = now()
                WHERE EXCLUDED.event_seq > video_pipeline_projection.event_seq
                RETURNING {_RECORD_COLUMNS}
                """,
                (
                    object_id,
                    video_id,
                    event_seq,
                    json.dumps(source_identity, sort_keys=True, separators=(",", ":")),
                    source_digest,
                    snapshot_json,
                ),
            )
            applied = cursor.fetchone()
            if applied is None:
                cursor.execute(
                    f"""
                    SELECT {_RECORD_COLUMNS}
                      FROM video_pipeline_projection
                     WHERE object_id = %s AND video_id = %s
                    """,
                    (object_id, video_id),
                )
                applied = cursor.fetchone()
            current = _record_from_row(applied)
            if current.event_seq == event_seq:
                cursor.execute(
                    """
                    UPDATE video_pipeline_projection_events
                       SET status = 'superseded', updated_at = now()
                     WHERE object_id = %s AND video_id = %s
                       AND event_seq < %s AND status <> 'superseded'
                    """,
                    (object_id, video_id, event_seq),
                )
                cursor.execute(
                    """
                    UPDATE video_pipeline_projection_events
                       SET status = 'applied', last_error = NULL,
                           applied_at = now(), updated_at = now()
                     WHERE event_seq = %s
                    """,
                    (event_seq,),
                )
                return current
            cursor.execute(
                """
                UPDATE video_pipeline_projection_events
                   SET status = 'superseded', updated_at = now()
                 WHERE event_seq = %s
                """,
                (event_seq,),
            )
            return current


def fail_intent(
    intent: ProjectionIntent | int,
    error: object,
    *,
    database_url: str | None = None,
    connect: ConnectArg = None,
) -> ProjectionIntent | None:
    """Record a bounded failure while keeping the intent retryable."""

    event_seq = _event_seq(intent)
    connection = _open_connection(database_url, connect)
    if connection is None:
        return None
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE video_pipeline_projection_events
                   SET attempts = attempts + 1,
                       last_error = %s,
                       updated_at = now()
                 WHERE event_seq = %s AND status = 'pending'
                RETURNING {_INTENT_COLUMNS}
                """,
                (sanitize_error(error), event_seq),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"""
                    SELECT {_INTENT_COLUMNS}
                      FROM video_pipeline_projection_events
                     WHERE event_seq = %s
                    """,
                    (event_seq,),
                )
                row = cursor.fetchone()
            if row is None:
                raise LookupError(f"projection intent {event_seq} nao existe")
    return _intent_from_row(row)


def get_many(
    object_id: str,
    video_ids: Iterable[str],
    *,
    database_url: str | None = None,
    connect: ConnectArg = None,
) -> dict[str, ProjectionRecord]:
    """Fetch projections for an object with exactly one database query."""

    object_id = _validate_identifier("object_id", object_id)
    ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    if not ids:
        return {}
    connection = _open_connection(database_url, connect)
    if connection is None:
        return {}
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {_RECORD_COLUMNS}
                  FROM video_pipeline_projection
                 WHERE object_id = %s AND video_id = ANY(%s)
                """,
                (object_id, ids),
            )
            rows = cursor.fetchall()
    records = (_record_from_row(row) for row in rows)
    return {record.video_id: record for record in records}


def pending_intents(
    *,
    limit: int = 100,
    after_event_seq: int = 0,
    database_url: str | None = None,
    connect: ConnectArg = None,
) -> list[ProjectionIntent]:
    """Return a bounded, resumable batch of unapplied intents."""

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("limit deve estar entre 1 e 1000")
    if not isinstance(after_event_seq, int) or isinstance(after_event_seq, bool) or after_event_seq < 0:
        raise ValueError("after_event_seq deve ser um inteiro nao negativo")
    connection = _open_connection(database_url, connect)
    if connection is None:
        return []
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {_INTENT_COLUMNS}
                  FROM video_pipeline_projection_events
                 WHERE status = 'pending' AND event_seq > %s
                 ORDER BY event_seq
                 LIMIT %s
                """,
                (after_event_seq, limit),
            )
            rows = cursor.fetchall()
    return [_intent_from_row(row) for row in rows]
