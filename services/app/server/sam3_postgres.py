"""PostgreSQL-backed SAM3 queue with leases and SKIP LOCKED claims."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path


class PostgresSam3Queue:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._arrivals: asyncio.Event | None = None

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url)

    @staticmethod
    def _item(row):
        from .sam3 import QueueItem

        payload = row[1] or {}
        progress = row[8] or {}
        return QueueItem(
            video_id=payload.get("video_id", ""),
            relpath=payload.get("relpath", ""),
            name=payload.get("name", ""),
            export_root=payload.get("export_root", ""),
            segments=list(payload.get("segments") or []),
            state=row[2],
            attempts=int(row[3]),
            force=bool(payload.get("force")),
            cancel_requested=bool(row[7]),
            enqueued_at=row[4].isoformat() if row[4] else "",
            enqueued_by=payload.get("user"),
            lease_id=str(row[5]) if row[5] else None,
            lease_expires_at_epoch=row[6].timestamp() if row[6] else None,
            worker=row[9],
            started_at=row[10].isoformat() if row[10] else None,
            finished_at=row[11].isoformat() if row[11] else None,
            progress=progress,
            result=row[12],
            error=row[13],
        )

    @staticmethod
    def _select() -> str:
        return """
            SELECT id, payload, state::text, attempts, created_at, lease_token,
                   lease_expires_at, cancel_requested, progress, worker_id,
                   started_at, finished_at, result, error
              FROM jobs
        """

    def bind(self, object_id: str, output_root: Path) -> None:
        return None

    def enqueue(
        self,
        object_id: str,
        *,
        video_id: str,
        relpath: str,
        name: str,
        export_root: str,
        segments: list[str],
        user: str | None,
        force: bool = False,
    ):
        key = f"sam3:{object_id}:{relpath}"
        payload = {
            "object_id": object_id,
            "video_id": video_id,
            "relpath": relpath,
            "name": name,
            "export_root": export_root,
            "segments": segments,
            "user": user,
            "force": force,
        }
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(self._select() + " WHERE idempotency_key = %s", (key,))
            existing = cursor.fetchone()
            if existing and (existing[2] in ("queued", "leased", "running") or (existing[2] == "done" and not force)):
                return self._item(existing)
            cursor.execute(
                """
                INSERT INTO jobs(kind, worker_kind, state, priority, payload,
                                 idempotency_key, progress, attempts, max_attempts)
                VALUES ('sam3_propagation', 'gpu', 'queued', 50, %s::jsonb,
                        %s, %s::jsonb, 0, 3)
                ON CONFLICT (idempotency_key) DO UPDATE
                    SET state = 'queued', payload = EXCLUDED.payload,
                        progress = EXCLUDED.progress, attempts = 0,
                        result = NULL, error = NULL, worker_id = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        cancel_requested = FALSE, started_at = NULL,
                        finished_at = NULL, updated_at = now()
                RETURNING id, payload, state::text, attempts, created_at,
                          lease_token, lease_expires_at, cancel_requested,
                          progress, worker_id, started_at, finished_at, result, error
                """,
                (json.dumps(payload), key, json.dumps({"segments_done": 0, "segments_total": len(segments)})),
            )
            item = self._item(cursor.fetchone())
        self._wake()
        return item

    def cancel(self, object_id: str, relpath: str):
        key = f"sam3:{object_id}:{relpath}"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET
                    state = CASE WHEN state = 'queued' THEN 'cancelled'::job_state ELSE state END,
                    finished_at = CASE WHEN state = 'queued' THEN now() ELSE finished_at END,
                    cancel_requested = CASE WHEN state IN ('leased','running') THEN TRUE ELSE cancel_requested END,
                    updated_at = now()
                WHERE idempotency_key = %s
                RETURNING id, payload, state::text, attempts, created_at,
                          lease_token, lease_expires_at, cancel_requested,
                          progress, worker_id, started_at, finished_at, result, error
                """,
                (key,),
            )
            row = cursor.fetchone()
        return self._item(row) if row else None

    def list(self, object_id: str):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                self._select()
                + " WHERE kind = 'sam3_propagation' AND payload->>'object_id' = %s ORDER BY priority, created_at",
                (object_id,),
            )
            return [self._item(row) for row in cursor.fetchall()]

    def get(self, object_id: str, relpath: str):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                self._select() + " WHERE idempotency_key = %s",
                (f"sam3:{object_id}:{relpath}",),
            )
            row = cursor.fetchone()
        return self._item(row) if row else None

    def public(self, object_id: str, relpath: str):
        item = self.get(object_id, relpath)
        return item.public() if item else None

    def map_for(self, object_id: str) -> dict[str, dict]:
        return {item.relpath: item.public() for item in self.list(object_id)}

    def take(self, worker: str, lease_seconds: int = 180):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET state='error', error='lease expirou no limite de tentativas',
                    finished_at=now(), updated_at=now()
                 WHERE kind='sam3_propagation' AND state IN ('leased','running')
                   AND lease_expires_at <= now() AND attempts >= max_attempts
                """
            )
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT id FROM jobs
                     WHERE kind = 'sam3_propagation'
                       AND cancel_requested = FALSE
                       AND attempts < max_attempts
                       AND (state = 'queued' OR
                            (state IN ('leased','running') AND lease_expires_at <= now()))
                     ORDER BY priority, created_at
                     FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE jobs AS job SET state = 'leased', worker_id = %s,
                    lease_token = gen_random_uuid(),
                    lease_expires_at = now() + make_interval(secs => %s),
                    attempts = attempts + 1, started_at = COALESCE(started_at, now()),
                    error = NULL, updated_at = now()
                FROM candidate WHERE job.id = candidate.id
                RETURNING job.id, job.payload, job.state::text, job.attempts,
                          job.created_at, job.lease_token, job.lease_expires_at,
                          job.cancel_requested, job.progress, job.worker_id,
                          job.started_at, job.finished_at, job.result, job.error
                """,
                (worker, lease_seconds),
            )
            row = cursor.fetchone()
        if not row:
            return None
        item = self._item(row)
        return str((row[1] or {}).get("object_id") or ""), item

    def heartbeat(self, lease_id: str, progress: dict | None, lease_seconds: int = 180):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET state = 'running', progress = COALESCE(%s::jsonb, progress),
                    lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
                 WHERE lease_token = %s::uuid AND state IN ('leased','running')
                   AND lease_expires_at > now()
                RETURNING id, payload, state::text, attempts, created_at,
                          lease_token, lease_expires_at, cancel_requested,
                          progress, worker_id, started_at, finished_at, result, error
                """,
                (json.dumps(progress) if progress else None, lease_seconds, lease_id),
            )
            row = cursor.fetchone()
        if not row:
            return None
        item = self._item(row)
        return item, item.cancel_requested

    def finish(self, lease_id: str, *, state: str, result: dict | None, error: str | None):
        terminal = state if state in ("done", "error", "cancelled") else "error"
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET
                    state = CASE WHEN %s = 'error' AND attempts < max_attempts
                                 THEN 'queued'::job_state ELSE %s::job_state END,
                    result = %s::jsonb, error = %s,
                    finished_at = CASE WHEN %s = 'error' AND attempts < max_attempts
                                       THEN NULL ELSE now() END,
                    worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
                    cancel_requested = FALSE, updated_at = now()
                 WHERE lease_token = %s::uuid AND state IN ('leased','running')
                RETURNING id, payload, state::text, attempts, created_at,
                          lease_token, lease_expires_at, cancel_requested,
                          progress, worker_id, started_at, finished_at, result, error
                """,
                (
                    terminal,
                    terminal,
                    json.dumps(result) if result is not None else None,
                    error,
                    terminal,
                    lease_id,
                ),
            )
            row = cursor.fetchone()
            if row and terminal == "done" and result:
                from .sam3_run_index import index_completed_runs

                job_payload = row[1] or {}
                index_completed_runs(
                    connection,
                    object_id=str(job_payload.get("object_id") or ""),
                    relpath=str(job_payload.get("relpath") or ""),
                    result=result,
                )
        if not row:
            return None
        payload = row[1] or {}
        return str(payload.get("object_id") or ""), self._item(row)

    def arrivals(self) -> asyncio.Event:
        if self._arrivals is None:
            self._arrivals = asyncio.Event()
        return self._arrivals

    def _wake(self) -> None:
        if self._arrivals is not None:
            self._arrivals.set()

    def has_pending(self) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='sam3_propagation' AND state='queued')"
            )
            return bool(cursor.fetchone()[0])
