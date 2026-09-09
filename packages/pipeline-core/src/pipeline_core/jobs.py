"""Durable PostgreSQL queue primitives shared by CPU and GPU workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable


PRIORITIES = {
    "sam3_preview": 10,
    "proxy_window": 30,
    "proxy_full": 40,
    "video_export": 50,
    "sam3_propagation": 50,
    "gcs_import": 60,
    "dataset_export": 70,
    "dataset_export_global": 70,
    "object_purge": 80,
    "migration": 90,
}


def priority_for(kind: str) -> int:
    return PRIORITIES.get(kind, 100)


CLAIM_SQL = """
WITH expired_terminal_candidates AS (
    SELECT id
      FROM jobs
     WHERE state IN ('leased', 'running')
       AND lease_expires_at <= now()
       AND (cancel_requested OR attempts >= max_attempts)
     ORDER BY updated_at ASC
     FOR UPDATE SKIP LOCKED
     LIMIT 100
),
expired_terminal AS (
    UPDATE jobs AS expired
       SET state = CASE
               WHEN expired.cancel_requested THEN 'cancelled'::job_state
               ELSE 'error'::job_state
           END,
           error = CASE
               WHEN expired.cancel_requested THEN COALESCE(expired.error, 'cancelamento solicitado')
               ELSE COALESCE(expired.error, 'lease expirada após esgotar tentativas')
           END,
           worker_id = NULL,
           lease_token = NULL,
           lease_expires_at = NULL,
           finished_at = now(),
           updated_at = now()
      FROM expired_terminal_candidates AS candidate
     WHERE expired.id = candidate.id
    RETURNING expired.id
),
candidate AS (
    SELECT id
      FROM jobs
     WHERE cancel_requested = FALSE
       AND attempts < max_attempts
       AND (
            state = 'queued'
            OR (state IN ('leased', 'running') AND lease_expires_at <= now())
       )
       AND (%(worker_kind)s::text IS NULL OR worker_kind = %(worker_kind)s::text)
     ORDER BY priority ASC, created_at ASC
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE jobs AS job
   SET state = 'leased',
       worker_id = %(worker_id)s,
       lease_token = gen_random_uuid(),
       lease_expires_at = now() + %(lease_duration)s,
       attempts = attempts + 1,
       error = NULL,
       started_at = COALESCE(started_at, now()),
       updated_at = now()
  FROM candidate
 WHERE job.id = candidate.id
RETURNING job.*
"""


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    kind: str
    payload: dict[str, Any]
    lease_token: str
    attempts: int


class PostgresJobQueue:
    """Small DB-API adapter; every state change checks the lease token."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self.connect = connect

    def claim(
        self,
        *,
        worker_id: str,
        worker_kind: str | None,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    CLAIM_SQL,
                    {
                        "worker_id": worker_id,
                        "worker_kind": worker_kind,
                        "lease_duration": timedelta(seconds=lease_seconds),
                    },
                )
                row = cursor.fetchone()
                if row is None:
                    connection.commit()
                    return None
                columns = [description.name for description in cursor.description]
                connection.commit()
                return dict(zip(columns, row, strict=True))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def heartbeat(self, job_id: str, lease_token: str, *, lease_seconds: int) -> bool:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE jobs
                       SET state = 'running',
                           lease_expires_at = now() + %(lease_duration)s,
                           updated_at = now()
                     WHERE id = %(job_id)s
                       AND lease_token = %(lease_token)s::uuid
                       AND state IN ('leased', 'running')
                       AND lease_expires_at > now()
                    RETURNING cancel_requested
                    """,
                    {
                        "job_id": job_id,
                        "lease_token": lease_token,
                        "lease_duration": timedelta(seconds=lease_seconds),
                    },
                )
                row = cursor.fetchone()
                connection.commit()
                if row is None:
                    raise RuntimeError("lease perdido")
                return not bool(row[0])
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_progress(
        self,
        job_id: str,
        lease_token: str,
        progress: dict[str, Any],
        *,
        lease_seconds: int = 180,
    ) -> bool:
        import json

        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE jobs SET state='running', progress=%(progress)s::jsonb,
                        lease_expires_at=now() + %(lease_duration)s, updated_at=now()
                     WHERE id=%(job_id)s AND lease_token=%(lease_token)s::uuid
                       AND state IN ('leased','running')
                       AND lease_expires_at > now()
                    RETURNING cancel_requested
                    """,
                    {
                        "job_id": job_id,
                        "lease_token": lease_token,
                        "progress": json.dumps(progress),
                        "lease_duration": timedelta(seconds=lease_seconds),
                    },
                )
                row = cursor.fetchone()
                connection.commit()
                if row is None:
                    raise RuntimeError("lease perdido")
                return not bool(row[0])
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def retry_or_fail(self, job_id: str, lease_token: str, error: str) -> str:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE jobs SET
                        state = CASE
                            WHEN cancel_requested THEN 'cancelled'::job_state
                            WHEN attempts < max_attempts THEN 'queued'::job_state
                            ELSE 'error'::job_state
                        END,
                        error=%(error)s, worker_id=NULL, lease_token=NULL,
                        lease_expires_at=NULL,
                        finished_at=CASE
                            WHEN cancel_requested OR attempts >= max_attempts THEN now()
                            ELSE NULL
                        END,
                        updated_at=now()
                     WHERE id=%(job_id)s AND lease_token=%(lease_token)s::uuid
                       AND state IN ('leased','running')
                       AND lease_expires_at > now()
                    RETURNING state::text
                    """,
                    {"job_id": job_id, "lease_token": lease_token, "error": error},
                )
                row = cursor.fetchone()
                connection.commit()
                if row is None:
                    raise RuntimeError("lease perdido")
                return row[0]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish(
        self,
        job_id: str,
        lease_token: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if state not in {"done", "error", "cancelled"}:
            raise ValueError(f"estado terminal invalido: {state}")
        import json

        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE jobs
                       SET state = CASE
                               WHEN cancel_requested THEN 'cancelled'::job_state
                               ELSE %(state)s::job_state
                           END,
                           result = CASE
                               WHEN cancel_requested THEN NULL
                               ELSE %(result)s::jsonb
                           END,
                           error = %(error)s,
                           finished_at = now(),
                           lease_expires_at = NULL,
                           updated_at = now()
                     WHERE id = %(job_id)s
                       AND lease_token = %(lease_token)s::uuid
                       AND state IN ('leased', 'running')
                       AND lease_expires_at > now()
                    """,
                    {
                        "job_id": job_id,
                        "lease_token": lease_token,
                        "state": state,
                        "result": json.dumps(result) if result is not None else None,
                        "error": error,
                    },
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("lease perdido")
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
