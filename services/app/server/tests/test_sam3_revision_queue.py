from __future__ import annotations

import copy
import unittest
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from server.routers import sam3 as sam3_router
from server.sam3 import Sam3Queue
from server.sam3_postgres import PostgresSam3Queue


class _Cursor:
    def __init__(self, rows: list[object]) -> None:
        self.rows = iter(rows)
        self.calls: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement: str, params=None) -> None:
        self.calls.append((statement, params))

    def fetchone(self):
        return next(self.rows)


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def _row(payload: dict, state: str = "queued") -> tuple:
    return (
        "job-id",
        payload,
        state,
        0,
        None,
        None,
        None,
        False,
        {},
        None,
        None,
        None,
        None,
        None,
    )


class Sam3RevisionQueueTests(unittest.TestCase):
    @staticmethod
    def _enqueue_file(queue: Sam3Queue, revision: int):
        return queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=revision,
        )

    def test_postgres_enqueue_keys_jobs_by_annotation_revision_and_cancels_older_work(self) -> None:
        payload = {
            "object_id": "boom",
            "video_id": "video-1",
            "relpath": "clip.mp4",
            "name": "clip",
            "export_root": "/dataset/clip",
            "segments": ["seg_00"],
            "annotation_revision": 4,
        }
        cursor = _Cursor([None, _row(payload)])
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=_Connection(cursor))

        item = queue.enqueue(
            "boom",
            video_id="video-1",
            relpath="clip.mp4",
            name="clip",
            export_root="/dataset/clip",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
        )

        self.assertEqual(item.annotation_revision, 4)
        flattened = "\n".join(statement for statement, _ in cursor.calls)
        self.assertIn("pg_advisory_xact_lock", cursor.calls[0][0])
        self.assertIn("cancel_requested", flattened)
        self.assertIn("payload->>'object_id'", flattened)
        params = [params for _, params in cursor.calls]
        self.assertTrue(
            any(
                "sam3:boom:video-1:r4" in str(item)
                for value in params
                for item in (value or ())
            )
        )

    def test_postgres_reader_accepts_legacy_payload_without_revision(self) -> None:
        item = PostgresSam3Queue._item(
            _row(
                {
                    "object_id": "boom",
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                },
                state="done",
            )
        )
        self.assertEqual(item.annotation_revision, 0)

    def test_revision_zero_reuses_an_active_legacy_idempotency_key(self) -> None:
        legacy = _row(
            {
                "object_id": "boom",
                "video_id": "video-1",
                "relpath": "clip.mp4",
                "segments": ["seg_00"],
            }
        )
        cursor = _Cursor([legacy])
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=_Connection(cursor))

        item = queue.enqueue(
            "boom",
            video_id="video-1",
            relpath="clip.mp4",
            name="clip",
            export_root="/dataset/clip",
            segments=["seg_00"],
            user="ana",
            annotation_revision=0,
        )

        self.assertEqual(item.annotation_revision, 0)
        self.assertEqual(len(cursor.calls), 2)
        self.assertIn("pg_advisory_xact_lock", cursor.calls[0][0])
        self.assertEqual(cursor.calls[1][1], ("boom", "clip.mp4"))

    def test_postgres_enqueue_never_downgrades_a_newer_revision(self) -> None:
        newer = _row(
            {
                "object_id": "boom",
                "video_id": "video-1",
                "relpath": "clip.mp4",
                "annotation_revision": 5,
            }
        )
        cursor = _Cursor([newer])
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=_Connection(cursor))

        item = queue.enqueue(
            "boom",
            video_id="video-1",
            relpath="clip.mp4",
            name="clip",
            export_root="/dataset/clip",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
        )

        self.assertEqual(item.annotation_revision, 5)
        self.assertEqual(len(cursor.calls), 2)
        self.assertIn("pg_advisory_xact_lock", cursor.calls[0][0])
        self.assertIn("payload->>'object_id'", cursor.calls[1][0])
        self.assertIn("ORDER BY", cursor.calls[1][0])

    def test_get_and_cancel_find_both_legacy_and_revision_specific_jobs(self) -> None:
        payload = {"object_id": "boom", "video_id": "v", "relpath": "clip.mp4"}
        for method in ("get", "cancel"):
            with self.subTest(method=method):
                cursor = _Cursor([_row(payload)])
                queue = PostgresSam3Queue("postgresql://unused")
                queue._connect = MagicMock(return_value=_Connection(cursor))
                getattr(queue, method)("boom", "clip.mp4")
                statement, params = cursor.calls[0]
                self.assertIn("payload->>'object_id'", statement)
                self.assertIn("payload->>'relpath'", statement)
                self.assertEqual(params[:2], ("boom", "clip.mp4"))

    def test_file_queue_deduplicates_same_revision_but_replaces_old_revision(self) -> None:
        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        first = queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=1,
        )
        same = queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=1,
        )
        newer = queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=2,
        )
        self.assertIs(first, same)
        self.assertEqual(newer.annotation_revision, 2)
        self.assertIs(queue.get("boom", "clip.mp4"), newer)

    def test_force_rerun_gets_a_new_immutable_generation_identity(self) -> None:
        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        first = self._enqueue_file(queue, 4)
        _, leased = queue.take("worker", 180)
        queue.finish(leased.lease_id, state="done", result={"ok": True}, error=None)

        rerun = queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
            force=True,
        )

        self.assertRegex(getattr(first, "run_id", ""), r"^[a-f0-9]{32}$")
        self.assertRegex(getattr(rerun, "run_id", ""), r"^[a-f0-9]{32}$")
        self.assertNotEqual(first.run_id, rerun.run_id)

    def test_file_queue_model_change_never_reuses_active_generation(self) -> None:
        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        first = queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
            model_id="model-old",
            model_sha256="a" * 64,
            sam3_commit="b" * 40,
        )
        replacement = queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
            model_id="model-new",
            model_sha256="c" * 64,
            sam3_commit="d" * 40,
        )

        self.assertEqual(replacement.model_id, "model-new")
        self.assertNotEqual(replacement.run_id, first.run_id)

    def test_postgres_model_change_cancels_active_job_and_uses_new_key(self) -> None:
        old_payload = {
            "object_id": "boom",
            "video_id": "video-1",
            "relpath": "clip.mp4",
            "annotation_revision": 4,
            "model_id": "model-old",
            "model_sha256": "a" * 64,
            "sam3_commit": "b" * 40,
        }
        new_payload = {
            **old_payload,
            "model_id": "model-new",
            "model_sha256": "c" * 64,
            "sam3_commit": "d" * 40,
        }
        cursor = _Cursor([_row(old_payload, "running"), _row(new_payload)])
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=_Connection(cursor))

        item = queue.enqueue(
            "boom",
            video_id="video-1",
            relpath="clip.mp4",
            name="clip",
            export_root="/dataset/clip",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
            model_id="model-new",
            model_sha256="c" * 64,
            sam3_commit="d" * 40,
        )

        self.assertEqual(item.model_id, "model-new")
        statements = "\n".join(statement for statement, _ in cursor.calls)
        self.assertIn("cancel_requested", statements)
        key = next(
            str(value)
            for _, params in cursor.calls
            for value in (params or ())
            if str(value).startswith("sam3:boom:video-1:r4:")
        )
        self.assertRegex(key, r":m[0-9a-f]{24}:g[0-9a-f]{32}$")

    def test_postgres_switching_back_to_an_old_model_creates_a_fresh_execution(self) -> None:
        latest_payload = {
            "object_id": "boom",
            "video_id": "video-1",
            "relpath": "clip.mp4",
            "annotation_revision": 4,
            "run_id": "b" * 32,
            "model_id": "model-b",
            "model_sha256": "b" * 64,
            "sam3_commit": "c" * 40,
        }
        returned_payload = {
            **latest_payload,
            "run_id": "a" * 32,
            "model_id": "model-a",
            "model_sha256": "a" * 64,
        }
        cursor = _Cursor([_row(latest_payload, "done"), _row(returned_payload)])
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=_Connection(cursor))

        item = queue.enqueue(
            "boom",
            video_id="video-1",
            relpath="clip.mp4",
            name="clip",
            export_root="/dataset/clip",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
            model_id="model-a",
            model_sha256="a" * 64,
            sam3_commit="c" * 40,
        )

        self.assertEqual(item.model_id, "model-a")
        statements = "\n".join(statement for statement, _ in cursor.calls)
        self.assertNotIn("WHERE idempotency_key = %s", statements)
        inserted_key = next(
            str(value)
            for statement, params in cursor.calls
            if "INSERT INTO jobs" in statement
            for value in (params or ())
            if str(value).startswith("sam3:boom:video-1:r4:")
        )
        self.assertRegex(inserted_key, r":m[0-9a-f]{24}:g[0-9a-f]{32}$")

    def test_postgres_operator_list_returns_only_the_latest_run_per_video(self) -> None:
        connection = MagicMock()
        cursor = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchall.return_value = []
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=connection)

        self.assertEqual(queue.list("boom"), [])

        statement = cursor.execute.call_args.args[0]
        self.assertIn("DISTINCT ON", statement)
        self.assertIn("created_at DESC", statement)

    def test_file_queue_never_replaces_a_newer_revision_with_an_older_one(self) -> None:
        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        newer = self._enqueue_file(queue, 5)
        delayed = self._enqueue_file(queue, 4)
        self.assertIs(delayed, newer)
        self.assertEqual(queue.get("boom", "clip.mp4").annotation_revision, 5)

    def test_file_queue_rejects_done_result_after_cancel_or_lease_expiry(self) -> None:
        for condition in ("cancel", "expiry"):
            with self.subTest(condition=condition):
                queue = Sam3Queue()
                queue.bind("boom", Path(tempfile.mkdtemp()))
                self._enqueue_file(queue, 4)
                _, item = queue.take("worker", 180)
                if condition == "cancel":
                    item.cancel_requested = True
                else:
                    item.lease_expires_at_epoch = time.time() - 1
                self.assertIsNone(
                    queue.finish(
                        item.lease_id,
                        state="done",
                        result={"ok": True},
                        error=None,
                    )
                )

    def test_file_queue_cancel_wins_worker_error_without_retrying(self) -> None:
        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        self._enqueue_file(queue, 4)
        _, item = queue.take("worker", 180)
        lease_id = item.lease_id
        queue.cancel("boom", "clip.mp4")

        found = queue.finish(
            lease_id,
            state="error",
            result={"partial": True},
            error="worker failed",
        )

        self.assertIsNotNone(found)
        self.assertEqual(found[1].state, "cancelled")
        self.assertIsNone(found[1].result)
        self.assertEqual(found[1].error, "worker failed")
        self.assertTrue(found[1].cancel_requested)
        self.assertIsNone(queue.take("another-worker", 180))

    def test_file_queue_cancel_survives_worker_death_and_server_restart(self) -> None:
        output_root = Path(tempfile.mkdtemp())
        queue = Sam3Queue()
        queue.bind("boom", output_root)
        self._enqueue_file(queue, 4)
        _, item = queue.take("worker", 180)
        queue.cancel("boom", "clip.mp4")

        restarted = Sam3Queue()
        restarted.bind("boom", output_root)

        restored = restarted.get("boom", "clip.mp4")
        self.assertEqual(restored.state, "cancelled")
        self.assertTrue(restored.cancel_requested)
        self.assertIsNone(restarted.take("another-worker", 180))

    def test_file_queue_cancelled_expired_lease_is_never_reclaimed(self) -> None:
        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        self._enqueue_file(queue, 4)
        _, item = queue.take("worker", 180)
        queue.cancel("boom", "clip.mp4")
        item.lease_expires_at_epoch = time.time() - 1

        self.assertIsNone(queue.take("another-worker", 180))
        cancelled = queue.get("boom", "clip.mp4")
        self.assertEqual(cancelled.state, "cancelled")
        self.assertTrue(cancelled.cancel_requested)

    def test_reading_live_file_lease_renews_completion_window(self) -> None:
        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        self._enqueue_file(queue, 4)
        _, item = queue.take("worker", 1)
        before = item.lease_expires_at_epoch

        found = queue.get_lease(item.lease_id)

        self.assertIsNotNone(found)
        self.assertGreater(found[1].lease_expires_at_epoch, before)

    def test_postgres_done_result_requires_live_uncancelled_lease(self) -> None:
        cursor = _Cursor([None])
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=_Connection(cursor))
        self.assertIsNone(
            queue.finish("lease", state="done", result={"ok": True}, error=None)
        )
        statement = cursor.calls[0][0]
        self.assertIn("lease_expires_at > now()", statement)
        self.assertIn("cancel_requested", statement)
        self.assertIn("kind = 'sam3_propagation'", statement)

    def test_postgres_live_lease_read_renews_completion_window(self) -> None:
        cursor = _Cursor([None])
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=_Connection(cursor))

        self.assertIsNone(queue.get_lease("lease"))

        statement = cursor.calls[0][0]
        self.assertIn("UPDATE jobs", statement)
        self.assertIn("make_interval", statement)
        self.assertIn("lease_expires_at > now()", statement)
        self.assertIn("kind = 'sam3_propagation'", statement)

    def test_postgres_owned_lease_holds_row_until_terminal_ack_commits(self) -> None:
        payload = {
            "object_id": "boom",
            "video_id": "v",
            "relpath": "clip.mp4",
            "annotation_revision": 4,
        }
        leased = list(_row(payload, state="running"))
        leased[5] = "lease"
        leased[6] = datetime.now(timezone.utc)
        done = list(_row(payload, state="done"))
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value = cursor
        cursor.fetchone.side_effect = [tuple(leased), tuple(done)]
        queue = PostgresSam3Queue("postgresql://unused")
        queue._connect = MagicMock(return_value=connection)

        with patch("server.sam3_run_index.index_completed_runs") as index:
            with queue.owned_lease("lease") as owned:
                self.assertIsNotNone(owned)
                connection.commit.assert_not_called()
                found = owned.finish(
                    state="done",
                    result={"runner_version": "test"},
                    error=None,
                    export_root="/workspace/new/export",
                )
                self.assertEqual(found[1].state, "done")
                connection.commit.assert_not_called()

        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        index.assert_called_once()
        self.assertEqual(
            index.call_args.kwargs["export_root"], "/workspace/new/export"
        )
        self.assertIn("lease_expires_at > now()", cursor.execute.call_args_list[0].args[0])
        self.assertIn(
            "kind = 'sam3_propagation'", cursor.execute.call_args_list[0].args[0]
        )
        self.assertNotIn(
            "lease_expires_at > now()", cursor.execute.call_args_list[1].args[0]
        )
        self.assertIn(
            "kind = 'sam3_propagation'", cursor.execute.call_args_list[1].args[0]
        )


class Sam3RevisionRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_newer_enqueue_waits_for_owned_completion_fence(self) -> None:
        import asyncio

        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
        )
        _, leased = queue.take("worker", 180)

        async with queue.owned_lease_async(leased.lease_id) as owned:
            delayed = asyncio.create_task(
                asyncio.to_thread(
                    queue.enqueue,
                    "boom",
                    video_id="v",
                    relpath="clip.mp4",
                    name="clip",
                    export_root="/x",
                    segments=["seg_00"],
                    user="ana",
                    annotation_revision=5,
                )
            )
            await asyncio.sleep(0.02)
            self.assertFalse(delayed.done())
            self.assertIsNotNone(
                await owned.finish(state="done", result={"ok": True}, error=None)
            )

        newer = await asyncio.wait_for(delayed, timeout=1)
        self.assertEqual(newer.annotation_revision, 5)
        self.assertEqual(queue.get("boom", "clip.mp4").annotation_revision, 5)

    async def test_manual_enqueue_passes_the_current_annotation_revision(self) -> None:
        video = SimpleNamespace(video_id="v", relpath="clip.mp4", name="clip")
        entry = {
            "annotation_revision": 9,
            "export": {"root": "/dataset/clip", "segments": ["seg_00"]},
        }
        ctx = SimpleNamespace(
            object_id="boom",
            output_root=SimpleNamespace(),
            index=SimpleNamespace(get=lambda _video_id: video),
            store=SimpleNamespace(entry=lambda _relpath: entry),
        )
        item = SimpleNamespace(public=lambda: {"state": "queued"})
        with patch.object(
            sam3_router,
            "_selected_model_identity",
            return_value={
                "model_id": "model-1",
                "model_sha256": "a" * 64,
                "sam3_commit": "b" * 40,
            },
        ), patch.object(sam3_router.queue, "bind"), patch.object(
            sam3_router.queue, "enqueue", return_value=item
        ) as enqueue:
            await sam3_router.enqueue(
                "v",
                sam3_router.EnqueueIn(force=False),
                ctx,
                SimpleNamespace(user_id="ana"),
            )

        self.assertEqual(enqueue.call_args.kwargs["annotation_revision"], 9)

    def test_enqueue_fails_closed_when_model_registry_is_unavailable(self) -> None:
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(HTTPException) as raised:
                sam3_router._selected_model_identity("boom")

        self.assertEqual(raised.exception.status_code, 503)

    def test_worker_rejects_an_existing_export_outside_registered_output_root(self) -> None:
        output_root = Path(tempfile.mkdtemp()) / "dataset"
        output_root.mkdir()
        outside = Path(tempfile.mkdtemp()) / "foreign-export"
        outside.mkdir()

        with patch.object(
            sam3_router.workspace,
            "get",
            return_value=SimpleNamespace(output_root=output_root),
        ):
            with self.assertRaises(HTTPException) as raised:
                sam3_router._resolve_export_root("boom", str(outside))

        self.assertEqual(raised.exception.status_code, 409)

    async def test_stale_worker_result_is_cancelled_without_touching_new_revision(self) -> None:
        class Store:
            def __init__(self):
                self.doc = {
                    "videos": {
                        "clip.mp4": {
                            "annotation_revision": 5,
                            "status": "in_progress",
                        }
                    }
                }

            def entry(self, relpath):
                return self.doc["videos"].get(relpath)

            async def mutate(self, fn):
                return fn(self.doc)

        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
        )
        _, leased = queue.take("worker", 180)
        lease_id = leased.lease_id
        store = Store()
        ctx = SimpleNamespace(store=store, ensure_loaded=lambda: None)

        with patch.object(sam3_router, "queue", queue), patch.object(
            sam3_router.workspace, "context", return_value=ctx
        ), patch("server.durable_jobs.enabled", return_value=False), patch.object(
            sam3_router, "_resolve_export_root", return_value="/x"
        ), patch(
            "pipeline_core.sam3_runs.publish_generation",
            side_effect=lambda **kwargs: kwargs["worker_result"],
        ) as publish:
            response = await sam3_router.result(
                lease_id,
                sam3_router.ResultIn(state="done", result={"runner_version": "x"}),
                "worker-token",
            )

        self.assertTrue(response["stale"])
        self.assertEqual(response["state"], "cancelled")
        self.assertNotIn("sam3", store.entry("clip.mp4"))
        self.assertEqual(queue.get("boom", "clip.mp4").state, "cancelled")
        publish.assert_not_called()

    async def test_annotation_persistence_failure_keeps_lease_retriable(self) -> None:
        class Store:
            def __init__(self):
                self.doc = {
                    "videos": {
                        "clip.mp4": {
                            "annotation_revision": 4,
                            "status": "done",
                        }
                    }
                }
                self.fail_once = True

            def entry(self, relpath):
                return self.doc["videos"].get(relpath)

            async def mutate(self, fn):
                before = copy.deepcopy(self.doc)
                result = fn(self.doc)
                if self.fail_once:
                    self.fail_once = False
                    self.doc = before
                    raise OSError("annotations flush failed")
                return result

        queue = Sam3Queue()
        queue.bind("boom", Path(tempfile.mkdtemp()))
        queue.enqueue(
            "boom",
            video_id="v",
            relpath="clip.mp4",
            name="clip",
            export_root="/x",
            segments=["seg_00"],
            user="ana",
            annotation_revision=4,
        )
        _, leased = queue.take("worker", 180)
        lease_id = leased.lease_id
        store = Store()
        ctx = SimpleNamespace(store=store, ensure_loaded=lambda: None)

        with patch.object(sam3_router, "queue", queue), patch.object(
            sam3_router.workspace, "context", return_value=ctx
        ), patch("server.durable_jobs.enabled", return_value=False), patch.object(
            sam3_router, "_resolve_export_root", return_value="/x"
        ), patch(
            "pipeline_core.sam3_runs.publish_generation",
            side_effect=lambda **kwargs: kwargs["worker_result"],
        ):
            with self.assertRaisesRegex(OSError, "flush failed"):
                await sam3_router.result(
                    lease_id,
                    sam3_router.ResultIn(
                        state="done", result={"runner_version": "first"}
                    ),
                    "worker-token",
                )

            pending = queue.get("boom", "clip.mp4")
            self.assertEqual(pending.state, "leased")
            self.assertEqual(pending.lease_id, lease_id)
            self.assertNotIn("sam3", store.entry("clip.mp4"))

            response = await sam3_router.result(
                lease_id,
                sam3_router.ResultIn(
                    state="done", result={"runner_version": "first"}
                ),
                "worker-token",
            )

        self.assertEqual(response["state"], "done")
        self.assertEqual(store.entry("clip.mp4")["sam3"]["runner_version"], "first")

    async def test_file_completion_reservation_prevents_mid_commit_cancel_or_expiry(
        self,
    ) -> None:
        for interference in ("cancel", "expiry"):
            with self.subTest(interference=interference):
                queue = Sam3Queue()
                queue.bind("boom", Path(tempfile.mkdtemp()))
                queue.enqueue(
                    "boom",
                    video_id="v",
                    relpath="clip.mp4",
                    name="clip",
                    export_root="/x",
                    segments=["seg_00"],
                    user="ana",
                    annotation_revision=4,
                )
                _, leased = queue.take("worker", 180)

                class Store:
                    def __init__(self):
                        self.doc = {
                            "videos": {
                                "clip.mp4": {
                                    "annotation_revision": 4,
                                    "status": "done",
                                }
                            }
                        }

                    def entry(self, relpath):
                        return self.doc["videos"].get(relpath)

                    async def mutate(self, fn):
                        result = fn(self.doc)
                        if interference == "cancel":
                            queue.cancel("boom", "clip.mp4")
                        else:
                            leased.lease_expires_at_epoch = time.time() - 1
                        return result

                store = Store()
                ctx = SimpleNamespace(store=store, ensure_loaded=lambda: None)
                with patch.object(sam3_router, "queue", queue), patch.object(
                    sam3_router.workspace, "context", return_value=ctx
                ), patch("server.durable_jobs.enabled", return_value=False), patch.object(
                    sam3_router, "_resolve_export_root", return_value="/x"
                ), patch(
                    "pipeline_core.sam3_runs.publish_generation",
                    side_effect=lambda **kwargs: kwargs["worker_result"],
                ):
                    response = await sam3_router.result(
                        leased.lease_id,
                        sam3_router.ResultIn(
                            state="done", result={"runner_version": "test"}
                        ),
                        "worker-token",
                    )

                self.assertEqual(response["state"], "done")
                self.assertEqual(queue.get("boom", "clip.mp4").state, "done")
                self.assertIn("sam3", store.entry("clip.mp4"))


if __name__ == "__main__":
    unittest.main()
