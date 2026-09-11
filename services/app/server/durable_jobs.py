"""Read/write helpers for CPU jobs stored in PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def _connect(*, connect_timeout: int | None = None):
    import psycopg

    if connect_timeout is not None:
        return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=connect_timeout)
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


def enqueue_projection_reconciles(object_id: str, requests: list[dict]) -> int:
    """Enqueue at most 20 metadata repairs with one bounded bulk statement."""
    if not enabled() or not requests:
        return 0
    rows = {}
    for request in requests[:20]:
        body = {"object_id": object_id, "video_id": request["video_id"],
                "source_identity": request["source_identity"]}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = "pipeline-projection:" + hashlib.sha256(canonical.encode()).hexdigest()
        rows[key] = {"key": key, "payload": body}
    with _connect(connect_timeout=1) as connection, connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '500ms'")
        cursor.execute("SET LOCAL statement_timeout = '1000ms'")
        cursor.execute(
            """
            INSERT INTO jobs(kind, worker_kind, state, priority, payload, progress, idempotency_key)
            SELECT 'pipeline_projection_reconcile', 'cpu', 'queued', 90, item.payload, '{}'::jsonb, item.key
              FROM jsonb_to_recordset(%s::jsonb) AS item(key text, payload jsonb)
            ON CONFLICT (idempotency_key) DO UPDATE SET
                state='queued', payload=EXCLUDED.payload, result=NULL, error=NULL,
                attempts=0, worker_id=NULL, lease_token=NULL, lease_expires_at=NULL,
                cancel_requested=FALSE, progress='{}'::jsonb, started_at=NULL,
                finished_at=NULL, updated_at=now()
            WHERE jobs.state IN ('done','error','cancelled')
            """,
            (json.dumps(list(rows.values())),),
        )
        return cursor.rowcount


def enqueue_mask_review_sync(
    *,
    object_id: str,
    video_id: str,
    relpath: str,
    segment: str,
    frames: list[dict],
    review_sha256: str,
    user: str | None,
) -> str | None:
    """Agenda o índice/espelho da revisão sem prender a resposta interativa."""

    if not enabled() or not frames:
        return None
    body = {
        "object_id": object_id,
        "video_id": video_id,
        "relpath": relpath,
        "segment": segment,
        "frames": frames,
        "review_sha256": review_sha256,
        "user": user,
    }
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    key = "mask-review-sync:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with _connect(connect_timeout=2) as connection, connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '750ms'")
        cursor.execute("SET LOCAL statement_timeout = '1500ms'")
        cursor.execute(
            """
            INSERT INTO jobs(kind, worker_kind, state, priority, payload, progress,
                             idempotency_key)
            VALUES ('mask_review_sync', 'cpu', 'queued', 60, %s::jsonb, '{}'::jsonb, %s)
            ON CONFLICT (idempotency_key) DO UPDATE SET
                state='queued', payload=EXCLUDED.payload, result=NULL, error=NULL,
                attempts=0, worker_id=NULL, lease_token=NULL, lease_expires_at=NULL,
                cancel_requested=FALSE, progress='{}'::jsonb, started_at=NULL,
                finished_at=NULL, updated_at=now()
            WHERE jobs.state IN ('done','error','cancelled')
            RETURNING id::text
            """,
            (json.dumps(body, ensure_ascii=False), key),
        )
        row = cursor.fetchone()
        if row is not None:
            return row[0]
        cursor.execute(
            "SELECT id::text FROM jobs WHERE idempotency_key = %s",
            (key,),
        )
        existing = cursor.fetchone()
        return existing[0] if existing is not None else None


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
        "root_basename": payload.get("root_basename"),
        "relpath": payload.get("relpath"),
        "owner": payload.get("owner"),
    }


def renew_owned_lease(
    job_id: str, lease_token: str, *, lease_seconds: int = 180
) -> dict[str, Any] | None:
    """Fence an internal callback and return only server-owned job fields."""
    try:
        UUID(job_id)
        UUID(lease_token)
    except (TypeError, ValueError, AttributeError):
        return None
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("lease_seconds invalido")

    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE jobs
               SET lease_expires_at = now() + %(lease_duration)s,
                   updated_at = now()
             WHERE id = %(job_id)s::uuid
               AND lease_token = %(lease_token)s::uuid
               AND state IN ('leased', 'running')
               AND lease_expires_at > now()
               AND cancel_requested = FALSE
            RETURNING id::text, kind, worker_kind, payload
            """,
            {
                "job_id": job_id,
                "lease_token": lease_token,
                "lease_duration": timedelta(seconds=lease_seconds),
            },
        )
        row = cursor.fetchone()
    if row is None:
        return None
    payload = row[3] or {}
    return {
        "job_id": row[0],
        "kind": row[1],
        "worker_kind": row[2],
        "object_id": payload.get("object_id"),
        "video_id": payload.get("video_id"),
        "relpath": payload.get("relpath"),
        "annotation_revision": payload.get("annotation_revision"),
        "root_basename": payload.get("root_basename"),
        "owner": payload.get("owner"),
        "total": payload.get("total"),
        "user": payload.get("user"),
    }


@contextmanager
def owned_job_lease(
    job_id: str, lease_token: str, *, lease_seconds: int = 180
):
    """Keep the active job row locked for an external authoritative commit."""
    try:
        UUID(job_id)
        UUID(lease_token)
    except (TypeError, ValueError, AttributeError):
        yield None
        return
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("lease_seconds invalido")

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                   SET lease_expires_at = now() + %(lease_duration)s,
                       updated_at = now()
                 WHERE id = %(job_id)s::uuid
                   AND lease_token = %(lease_token)s::uuid
                   AND state IN ('leased', 'running')
                   AND lease_expires_at > now()
                   AND cancel_requested = FALSE
                RETURNING id::text, kind, worker_kind, payload
                """,
                {
                    "job_id": job_id,
                    "lease_token": lease_token,
                    "lease_duration": timedelta(seconds=lease_seconds),
                },
            )
            row = cursor.fetchone()
        if row is None:
            yield None
        else:
            payload = row[3] or {}
            yield {
                "job_id": row[0],
                "kind": row[1],
                "worker_kind": row[2],
                "object_id": payload.get("object_id"),
                "video_id": payload.get("video_id"),
                "relpath": payload.get("relpath"),
                "annotation_revision": payload.get("annotation_revision"),
                "root_basename": payload.get("root_basename"),
                "owner": payload.get("owner"),
                "total": payload.get("total"),
                "user": payload.get("user"),
            }
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


@asynccontextmanager
async def owned_job_lease_async(
    job_id: str, lease_token: str, *, lease_seconds: int = 180
):
    """Async adapter that retains the row lock until the caller exits."""
    manager = owned_job_lease(job_id, lease_token, lease_seconds=lease_seconds)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job-lease")
    enter_future = loop.run_in_executor(executor, manager.__enter__)
    entered = False
    try:
        try:
            job = await asyncio.shield(enter_future)
            entered = True
        except asyncio.CancelledError:
            try:
                await asyncio.shield(enter_future)
            except Exception:
                pass
            else:
                await asyncio.shield(
                    loop.run_in_executor(executor, manager.__exit__, None, None, None)
                )
            raise

        try:
            yield job
        except BaseException as exc:
            exit_future = loop.run_in_executor(
                executor,
                manager.__exit__,
                type(exc),
                exc,
                exc.__traceback__,
            )
            entered = False
            try:
                await asyncio.shield(exit_future)
            except asyncio.CancelledError:
                await asyncio.shield(exit_future)
            raise
        else:
            exit_future = loop.run_in_executor(
                executor, manager.__exit__, None, None, None
            )
            entered = False
            try:
                await asyncio.shield(exit_future)
            except asyncio.CancelledError:
                await asyncio.shield(exit_future)
                raise
    finally:
        if entered:
            # Defensive cleanup for unusual BaseExceptions raised by the loop.
            try:
                await asyncio.shield(
                    loop.run_in_executor(executor, manager.__exit__, None, None, None)
                )
            except BaseException:
                pass
        executor.shutdown(wait=False)


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
