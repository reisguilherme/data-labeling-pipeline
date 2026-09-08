"""Read/write helpers for CPU jobs stored in PostgreSQL."""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def _connect():
    import psycopg

    return psycopg.connect(os.environ["DATABASE_URL"])


def create(
    *,
    kind: str,
    object_id: str,
    payload: dict,
    priority: int = 70,
    idempotency_key: str | None = None,
) -> str:
    body = {**payload, "object_id": object_id}
    with _connect() as connection, connection.cursor() as cursor:
        if idempotency_key:
            cursor.execute(
                "SELECT id::text, state::text FROM jobs WHERE idempotency_key = %s FOR UPDATE",
                (idempotency_key,),
            )
            current = cursor.fetchone()
            if current and current[1] in ("queued", "leased", "running"):
                return current[0]
            if current:
                cursor.execute(
                    """
                    UPDATE jobs SET kind=%s, worker_kind='cpu', state='queued',
                        priority=%s, payload=%s::jsonb, result=NULL, error=NULL,
                        attempts=0, worker_id=NULL, lease_token=NULL,
                        lease_expires_at=NULL, cancel_requested=FALSE,
                        progress='{}'::jsonb, started_at=NULL, finished_at=NULL,
                        updated_at=now()
                     WHERE id=%s::uuid RETURNING id::text
                    """,
                    (kind, priority, json.dumps(body), current[0]),
                )
                return cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO jobs(kind, worker_kind, state, priority, payload, progress,
                             idempotency_key)
            VALUES (%s, 'cpu', 'queued', %s, %s::jsonb, '{}'::jsonb, %s)
            RETURNING id::text
            """,
            (kind, priority, json.dumps(body), idempotency_key),
        )
        return cursor.fetchone()[0]


def cancel_others(
    *, object_id: str, video_id: str | None, kinds: tuple[str, ...], client_id: str
) -> int:
    """Cancela proxies da mesma aba ao trocar de video."""
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs SET
                state=CASE WHEN state='queued' THEN 'cancelled'::job_state ELSE state END,
                finished_at=CASE WHEN state='queued' THEN now() ELSE finished_at END,
                cancel_requested=CASE WHEN state IN ('leased','running') THEN TRUE ELSE cancel_requested END,
                updated_at=now()
             WHERE worker_kind='cpu' AND kind = ANY(%s)
               AND payload->>'object_id'=%s AND payload->>'client_id'=%s
               AND (%s::text IS NULL OR payload->>'video_id' <> %s::text)
               AND state IN ('queued','leased','running')
            """,
            (list(kinds), object_id, client_id, video_id, video_id),
        )
        return cursor.rowcount


def get(job_id: str) -> dict[str, Any] | None:
    try:
        UUID(job_id)
    except ValueError:
        return None
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id::text, kind, payload, state::text, progress, result, error,
                   attempts, created_at, started_at, finished_at, cancel_requested
              FROM jobs WHERE id = %s::uuid
            """,
            (job_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    payload, progress = row[2] or {}, row[4] or {}
    current = int(progress.get("current") or progress.get("seen") or 0)
    total = int(progress.get("total") or 0)
    return {
        "job_id": row[0],
        "kind": row[1],
        "object_id": payload.get("object_id", ""),
        "video_id": payload.get("video_id", ""),
        "state": row[3],
        "current": current,
        "total": total,
        "progress": min(current / total, 1) if total else (1 if row[3] == "done" else 0),
        "message": progress.get("message") or payload.get("message") or "",
        "error": row[6],
        "result": row[5] or {},
        "started_at": row[9].isoformat() if row[9] else row[8].isoformat(),
        "finished_at": row[10].isoformat() if row[10] else None,
        "client_id": payload.get("client_id"),
        "user": payload.get("user"),
        "queue_pos": 0,
        "attempts": row[7],
        "cancel_requested": row[11],
    }


def cancel(job_id: str) -> bool:
    try:
        UUID(job_id)
    except ValueError:
        return False
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs SET
                state = CASE WHEN state = 'queued' THEN 'cancelled'::job_state ELSE state END,
                finished_at = CASE WHEN state = 'queued' THEN now() ELSE finished_at END,
                cancel_requested = CASE WHEN state IN ('leased','running') THEN TRUE ELSE cancel_requested END,
                updated_at = now()
             WHERE id = %s::uuid AND state IN ('queued','leased','running')
            """,
            (job_id,),
        )
        return cursor.rowcount == 1
