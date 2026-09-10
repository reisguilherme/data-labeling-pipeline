"""PostgreSQL-backed SAM3 queue with leases and SKIP LOCKED claims."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from functools import partial
from pathlib import Path


def _model_identity(
    model_id: str | None,
    model_sha256: str | None,
    sam3_commit: str | None,
) -> tuple[str | None, str | None, str | None]:
    return model_id, model_sha256, sam3_commit


def _model_key(identity: tuple[str | None, str | None, str | None]) -> str:
    canonical = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:24]


class _OwnedPostgresLease:
    def __init__(self, queue, connection, cursor, row) -> None:
        self._queue = queue
        self._connection = connection
        self._cursor = cursor
        self.object_id = str((row[1] or {}).get("object_id") or "")
        self.item = queue._item(row)

    def finish(
        self,
        *,
        state: str,
        result: dict | None,
        error: str | None,
        export_root: str | Path | None = None,
    ):
        found = self._queue._finish_cursor(
            self._connection,
            self._cursor,
            self.item.lease_id or "",
            state=state,
            result=result,
            error=error,
            fenced=True,
            export_root=export_root,
        )
        if found is not None:
            self.object_id, self.item = found
        return found


class _AsyncOwnedPostgresLease:
    def __init__(self, lease, executor: ThreadPoolExecutor) -> None:
        self._lease = lease
        self._executor = executor
        self.object_id = lease.object_id
        self.item = lease.item

    async def finish(
        self,
        *,
        state: str,
        result: dict | None,
        error: str | None,
        export_root: str | Path | None = None,
    ):
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(
            self._executor,
            partial(
                self._lease.finish,
                state=state,
                result=result,
                error=error,
                export_root=export_root,
            ),
        )
        self.object_id = self._lease.object_id
        self.item = self._lease.item
        return found


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
            annotation_revision=int(payload.get("annotation_revision") or 0),
            run_id=str(
                payload.get("run_id")
                or uuid.uuid5(uuid.NAMESPACE_URL, f"sam3-postgres:{row[0]}").hex
            ),
            model_id=payload.get("model_id"),
            model_sha256=payload.get("model_sha256"),
            sam3_commit=payload.get("sam3_commit"),
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
    def _select(*, latest_by_video: bool = False) -> str:
        distinct = "DISTINCT ON (payload->>'relpath') " if latest_by_video else ""
        return f"""
            SELECT {distinct}id, payload, state::text, attempts, created_at, lease_token,
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
        annotation_revision: int = 0,
        force: bool = False,
        model_id: str | None = None,
        model_sha256: str | None = None,
        sam3_commit: str | None = None,
    ):
        if type(annotation_revision) is not int or annotation_revision < 0:
            raise ValueError("annotation_revision invalida")
        requested_model = _model_identity(model_id, model_sha256, sam3_commit)
        run_id = uuid.uuid4().hex
        key = (
            f"sam3:{object_id}:{video_id}:r{annotation_revision}:"
            f"m{_model_key(requested_model)}:g{run_id}"
        )
        payload = {
            "object_id": object_id,
            "video_id": video_id,
            "relpath": relpath,
            "name": name,
            "export_root": export_root,
            "segments": segments,
            "annotation_revision": annotation_revision,
            "run_id": run_id,
            "model_id": model_id,
            "model_sha256": model_sha256,
            "sam3_commit": sam3_commit,
            "user": user,
            "force": force,
        }
        with self._connect() as connection, connection.cursor() as cursor:
            # Serialize every revision decision for this logical video inside
            # PostgreSQL. Without this transaction-scoped fence, concurrent
            # r4/r5 inserts can both observe an empty queue and both commit.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"sam3:{object_id}:{relpath}",),
            )
            # Select by the logical video first, not only by the requested
            # idempotency key. This makes enqueue monotonic when an older
            # export callback is delayed until after a newer revision.
            cursor.execute(
                self._select()
                + """ WHERE kind = 'sam3_propagation'
                         AND payload->>'object_id' = %s
                         AND payload->>'relpath' = %s
                       ORDER BY
                         CASE
                           WHEN COALESCE(payload->>'annotation_revision', '') ~ '^[0-9]+$'
                           THEN (payload->>'annotation_revision')::bigint
                           ELSE 0
                         END DESC,
                         created_at DESC
                       LIMIT 1""",
                (object_id, relpath),
            )
            latest_row = cursor.fetchone()
            latest = self._item(latest_row) if latest_row else None
            if latest is not None and latest.annotation_revision > annotation_revision:
                return latest
            if (
                latest is not None
                and latest.annotation_revision == annotation_revision
                and latest.model_identity == requested_model
                and (latest.active or (latest.state == "done" and not force))
            ):
                return latest

            # A newer annotation must never silently share an active job with
            # an older export. Queued work can be cancelled immediately;
            # leased work receives the normal cooperative cancellation flag.
            cursor.execute(
                """
                UPDATE jobs SET
                    state = CASE WHEN state = 'queued' THEN 'cancelled'::job_state ELSE state END,
                    finished_at = CASE WHEN state = 'queued' THEN now() ELSE finished_at END,
                    cancel_requested = CASE WHEN state IN ('leased','running') THEN TRUE ELSE cancel_requested END,
                    updated_at = now()
                 WHERE kind = 'sam3_propagation'
                   AND payload->>'object_id' = %s
                   AND payload->>'relpath' = %s
                   AND CASE
                         WHEN COALESCE(payload->>'annotation_revision', '') ~ '^[0-9]+$'
                         THEN (payload->>'annotation_revision')::bigint
                         ELSE 0
                       END <= %s
                   AND state IN ('queued','leased','running')
                """,
                (object_id, relpath, annotation_revision),
            )
            cursor.execute(
                """
                INSERT INTO jobs(kind, worker_kind, state, priority, payload,
                                 idempotency_key, progress, attempts, max_attempts)
                VALUES ('sam3_propagation', 'gpu', 'queued', 50, %s::jsonb,
                        %s, %s::jsonb, 0, 3)
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
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs SET
                    state = CASE WHEN state = 'queued' THEN 'cancelled'::job_state ELSE state END,
                    finished_at = CASE WHEN state = 'queued' THEN now() ELSE finished_at END,
                    cancel_requested = CASE WHEN state IN ('leased','running') THEN TRUE ELSE cancel_requested END,
                    updated_at = now()
                WHERE kind = 'sam3_propagation'
                  AND payload->>'object_id' = %s
                  AND payload->>'relpath' = %s
                  AND state IN ('queued','leased','running')
                """,
                (object_id, relpath),
            )
            cursor.execute(
                self._select()
                + """ WHERE kind = 'sam3_propagation'
                         AND payload->>'object_id' = %s
                         AND payload->>'relpath' = %s
                       ORDER BY created_at DESC LIMIT 1""",
                (object_id, relpath),
            )
            row = cursor.fetchone()
        return self._item(row) if row else None

    def list(self, object_id: str):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                self._select(latest_by_video=True)
                + """ WHERE kind = 'sam3_propagation'
                           AND payload->>'object_id' = %s
                       ORDER BY payload->>'relpath', created_at DESC""",
                (object_id,),
            )
            return [self._item(row) for row in cursor.fetchall()]

    def get(self, object_id: str, relpath: str):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                self._select()
                + """ WHERE kind = 'sam3_propagation'
                         AND payload->>'object_id' = %s
                         AND payload->>'relpath' = %s
                       ORDER BY created_at DESC LIMIT 1""",
                (object_id, relpath),
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
                UPDATE jobs SET
                    state = CASE WHEN cancel_requested
                                 THEN 'cancelled'::job_state
                                 ELSE 'error'::job_state END,
                    result = CASE WHEN cancel_requested THEN NULL ELSE result END,
                    error = CASE WHEN cancel_requested THEN error
                                 ELSE 'lease expirou no limite de tentativas' END,
                    worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
                    finished_at=now(), updated_at=now()
                 WHERE kind='sam3_propagation' AND state IN ('leased','running')
                   AND lease_expires_at <= now()
                   AND (cancel_requested OR attempts >= max_attempts)
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
                 WHERE lease_token = %s::uuid
                   AND kind = 'sam3_propagation'
                   AND state IN ('leased','running')
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

    def get_lease(self, lease_id: str):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                   SET lease_expires_at = now() + make_interval(secs => %s),
                       updated_at = now()
                 WHERE lease_token = %s::uuid
                   AND kind = 'sam3_propagation'
                   AND state IN ('leased','running')
                   AND lease_expires_at > now()
                RETURNING id, payload, state::text, attempts, created_at,
                          lease_token, lease_expires_at, cancel_requested,
                          progress, worker_id, started_at, finished_at, result, error
                """,
                (180, lease_id),
            )
            row = cursor.fetchone()
        if not row:
            return None
        payload = row[1] or {}
        return str(payload.get("object_id") or ""), self._item(row)

    @contextmanager
    def owned_lease(self, lease_id: str, *, lease_seconds: int = 180):
        """Hold the live SAM3 job row until annotation metadata and ACK agree."""
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds invalido")
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE jobs
                   SET lease_expires_at = now() + make_interval(secs => %s),
                       updated_at = now()
                 WHERE lease_token = %s::uuid
                   AND kind = 'sam3_propagation'
                   AND state IN ('leased','running')
                   AND lease_expires_at > now()
                RETURNING id, payload, state::text, attempts, created_at,
                          lease_token, lease_expires_at, cancel_requested,
                          progress, worker_id, started_at, finished_at, result, error
                """,
                (lease_seconds, lease_id),
            )
            row = cursor.fetchone()
            yield (
                _OwnedPostgresLease(self, connection, cursor, row)
                if row is not None
                else None
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

    @asynccontextmanager
    async def owned_lease_async(self, lease_id: str, *, lease_seconds: int = 180):
        manager = self.owned_lease(lease_id, lease_seconds=lease_seconds)
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sam3-lease")
        enter_future = loop.run_in_executor(executor, manager.__enter__)
        entered = False
        try:
            try:
                lease = await asyncio.shield(enter_future)
                entered = True
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(enter_future)
                except Exception:
                    pass
                else:
                    await asyncio.shield(
                        loop.run_in_executor(
                            executor, manager.__exit__, None, None, None
                        )
                    )
                raise

            proxy = (
                _AsyncOwnedPostgresLease(lease, executor)
                if lease is not None
                else None
            )
            try:
                yield proxy
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
                try:
                    await asyncio.shield(
                        loop.run_in_executor(
                            executor, manager.__exit__, None, None, None
                        )
                    )
                except BaseException:
                    pass
            executor.shutdown(wait=False)

    def _finish_cursor(
        self,
        connection,
        cursor,
        lease_id: str,
        *,
        state: str,
        result: dict | None,
        error: str | None,
        fenced: bool,
        export_root: str | Path | None = None,
    ):
        terminal = state if state in ("done", "error", "cancelled") else "error"
        live_guard = "" if fenced else """
                   AND lease_expires_at > now()
                   AND (completion.terminal <> 'done' OR jobs.cancel_requested = FALSE)"""
        cursor.execute(
            """
            WITH completion AS (
                SELECT %s::text AS terminal, %s::jsonb AS result, %s::text AS error
            )
            UPDATE jobs SET
                state = CASE
                    WHEN jobs.cancel_requested OR completion.terminal = 'cancelled'
                    THEN 'cancelled'::job_state
                    WHEN completion.terminal = 'error' AND attempts < max_attempts
                    THEN 'queued'::job_state
                    ELSE completion.terminal::job_state
                END,
                result = CASE
                    WHEN jobs.cancel_requested OR completion.terminal = 'cancelled'
                    THEN NULL ELSE completion.result
                END,
                error = completion.error,
                finished_at = CASE
                    WHEN jobs.cancel_requested OR completion.terminal = 'cancelled'
                    THEN now()
                    WHEN completion.terminal = 'error' AND attempts < max_attempts
                    THEN NULL ELSE now()
                END,
                worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
                cancel_requested = CASE
                    WHEN jobs.cancel_requested OR completion.terminal = 'cancelled'
                    THEN TRUE ELSE FALSE
                END,
                updated_at = now()
              FROM completion
             WHERE lease_token = %s::uuid
               AND kind = 'sam3_propagation'
               AND state IN ('leased','running')
            """
            + live_guard
            + """
            RETURNING jobs.id, jobs.payload, jobs.state::text, jobs.attempts,
                      jobs.created_at, jobs.lease_token, jobs.lease_expires_at,
                      jobs.cancel_requested, jobs.progress, jobs.worker_id,
                      jobs.started_at, jobs.finished_at, jobs.result, jobs.error
            """,
            (
                terminal,
                json.dumps(result) if result is not None else None,
                error,
                lease_id,
            ),
        )
        row = cursor.fetchone()
        if row and row[2] == "done" and result:
            from .sam3_run_index import index_completed_runs

            job_payload = row[1] or {}
            index_completed_runs(
                connection,
                object_id=str(job_payload.get("object_id") or ""),
                relpath=str(job_payload.get("relpath") or ""),
                result=result,
                export_root=export_root or job_payload.get("export_root"),
            )
        if not row:
            return None
        payload = row[1] or {}
        return str(payload.get("object_id") or ""), self._item(row)

    def finish(self, lease_id: str, *, state: str, result: dict | None, error: str | None):
        with self._connect() as connection, connection.cursor() as cursor:
            return self._finish_cursor(
                connection,
                cursor,
                lease_id,
                state=state,
                result=result,
                error=error,
                fenced=False,
            )

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
