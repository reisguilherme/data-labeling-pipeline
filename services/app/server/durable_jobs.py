"""Read/write helpers for CPU jobs stored in PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
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
        if not idempotency_key:
            cursor.execute(
                """
                INSERT INTO jobs(kind, worker_kind, state, priority, payload, progress,
                                 idempotency_key)
                VALUES (%s, 'cpu', 'queued', %s, %s::jsonb, '{}'::jsonb, NULL)
                RETURNING id::text
                """,
                (kind, priority, json.dumps(body)),
            )
            return cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO jobs(kind, worker_kind, state, priority, payload, progress,
                             idempotency_key)
            VALUES (%s, 'cpu', 'queued', %s, %s::jsonb, '{}'::jsonb, %s)
            ON CONFLICT (idempotency_key) DO UPDATE SET
                kind=EXCLUDED.kind,
                worker_kind='cpu',
                state='queued',
                priority=EXCLUDED.priority,
                payload=EXCLUDED.payload,
                result=NULL,
                error=NULL,
                attempts=0,
                worker_id=NULL,
                lease_token=NULL,
                lease_expires_at=NULL,
                cancel_requested=FALSE,
                progress='{}'::jsonb,
                started_at=NULL,
                finished_at=NULL,
                updated_at=now()
            WHERE jobs.state IN ('done','error','cancelled')
            RETURNING id::text
            """,
            (kind, priority, json.dumps(body), idempotency_key),
        )
        row = cursor.fetchone()
        if row is not None:
            return row[0]
        # A conflicting row was active. This second statement gets a fresh
        # READ COMMITTED snapshot after any concurrent INSERT has committed.
        cursor.execute(
            "SELECT id::text FROM jobs WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        return cursor.fetchone()[0]


def _advisory_key(object_id: str, video_id: str) -> int:
    digest = hashlib.blake2b(
        f"video:{object_id}:{video_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _annotation_advisory_key(object_id: str) -> int:
    digest = hashlib.blake2b(
        f"annotations:{object_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def video_advisory_lock(object_id: str, video_id: str):
    """Serialize the shared annotation document and one video's media commit.

    ``annotations.json`` is shared by every video of an object, so the object
    key prevents two application processes from publishing stale whole-file
    snapshots. The video key is the fence shared with CPU media jobs.
    """
    connection = _connect()
    keys = sorted(
        {_annotation_advisory_key(object_id), _advisory_key(object_id, video_id)}
    )
    acquired: list[int] = []
    try:
        with connection.cursor() as cursor:
            for key in keys:
                cursor.execute("SELECT pg_advisory_lock(%s)", (key,))
                acquired.append(key)
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                for key in reversed(acquired):
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            connection.close()


@asynccontextmanager
async def video_advisory_lock_async(object_id: str, video_id: str):
    manager = video_advisory_lock(object_id, video_id)
    loop = asyncio.get_running_loop()
    # A aquisição pode esperar outro processo. Um executor exclusivo impede
    # que vários waiters ocupem todas as threads que o holder precisa para
    # flush/reload e unlock (deadlock por starvation do executor padrão).
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-lock")
    enter_future = loop.run_in_executor(executor, manager.__enter__)
    try:
        try:
            await asyncio.shield(enter_future)
        except asyncio.CancelledError:
            # run_in_executor não cancela uma chamada já iniciada. Aguarde a
            # aquisição e solte a sessão antes de propagar o cancelamento.
            try:
                await asyncio.shield(enter_future)
            except Exception:  # __enter__ já executa seu próprio finally
                pass
            else:
                await asyncio.shield(
                    loop.run_in_executor(
                        executor, manager.__exit__, None, None, None
                    )
                )
            raise
    finally:
        executor.shutdown(wait=False)

    try:
        yield
    finally:
        exit_task = asyncio.create_task(
            asyncio.to_thread(manager.__exit__, None, None, None)
        )
        try:
            await asyncio.shield(exit_task)
        except asyncio.CancelledError:
            await asyncio.shield(exit_task)
            raise


def cancel_stale_video_exports(
    *, object_id: str, video_id: str, before_revision: int
) -> int:
    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs SET
                state=CASE WHEN state='queued' THEN 'cancelled'::job_state ELSE state END,
                finished_at=CASE WHEN state='queued' THEN now() ELSE finished_at END,
                cancel_requested=CASE WHEN state IN ('leased','running') THEN TRUE ELSE cancel_requested END,
                updated_at=now()
             WHERE worker_kind='cpu' AND kind='video_export'
               AND payload->>'object_id'=%s AND payload->>'video_id'=%s
               AND CASE
                     WHEN COALESCE(payload->>'annotation_revision', '') ~ '^[0-9]+$'
                     THEN (payload->>'annotation_revision')::bigint
                     ELSE 0
                   END < %s
               AND state IN ('queued','leased','running')
            """,
            (object_id, video_id, before_revision),
        )
        return cursor.rowcount


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
        "annotation_revision": payload.get("annotation_revision"),
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
