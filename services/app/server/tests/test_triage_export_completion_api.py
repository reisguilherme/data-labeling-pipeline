from __future__ import annotations

import copy
import os
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from server import durable_jobs, export as export_module
from server.routers import annotations
from server.routers.sam3 import require_worker
from server.sam3 import Sam3Queue
from server.store import AnnotationStore
from server.users import User
from server.videos import VideoFile
from server.video_export_completion import finalize_video_export


class _Index:
    def __init__(self, root: Path) -> None:
        source = root / "clip.mp4"
        source.write_bytes(b"video")
        self.video = VideoFile(
            video_id="video-1",
            relpath="nested/clip.mp4",
            abspath=source,
            name="clip",
            size_bytes=5,
            file_mtime="2026-01-01T00:00:00Z",
        )

    def get(self, video_id: str):
        return self.video if video_id == "video-1" else None


async def _prepared(base: Path):
    output = base / "dataset"
    store = AnnotationStore(
        annotations_path=output / "annotations.json",
        cache_dir=output / "_cache",
        videos_root=base,
        output_root=output,
        object_id="boom",
        label="boom",
        total_provider=lambda: 1,
    )
    store.load()
    ctx = SimpleNamespace(
        object_id="boom",
        label="boom",
        output_root=output,
        cache_dir=output / "_cache",
        store=store,
        index=_Index(base),
        config=SimpleNamespace(auto_sam3=True),
    )
    await store.put_entry(
        "nested/clip.mp4",
        {
            "status": "in_progress",
            "annotation_revision": 7,
            "intervals": [{"segment": "seg_00", "frame_count": 2}],
        },
    )
    root = export_module.export_root_for(ctx, ctx.index.video)
    segment = root / "seg_00"
    segment.mkdir(parents=True)
    (segment / "prompt.json").write_text("{}", encoding="utf-8")
    owner = export_module.export_owner(ctx, ctx.index.video, 7)
    export_module.write_export_owner(root, owner)
    result = {
        "root": root.as_posix(),
        "segments": ["seg_00"],
        "total_frames": 2,
        "jpeg_qscale": 2,
        "frame_naming": "restart_per_segment",
        "ffmpeg_version": "ffmpeg-test",
        "annotation_revision": 7,
        "owner": owner,
    }
    return ctx, result


@asynccontextmanager
async def _owned(job):
    yield job


class ExportCompletionApiContractTests(unittest.TestCase):
    def test_worker_completion_contract_exists(self) -> None:
        self.assertTrue(
            callable(getattr(annotations, "complete_export_internal", None)),
            "endpoint interno de conclusao ausente",
        )
        self.assertTrue(
            callable(getattr(durable_jobs, "renew_owned_lease", None)),
            "fence de lease do callback ausente",
        )
        self.assertTrue(
            callable(getattr(durable_jobs, "owned_job_lease_async", None)),
            "fence transacional da lease ausente",
        )


@unittest.skipUnless(
    callable(getattr(annotations, "complete_export_internal", None)),
    "endpoint ainda nao implementado",
)
class ExportCompletionApiTests(unittest.IsolatedAsyncioTestCase):
    def _payload(self, result: dict):
        payload_type = annotations.WorkerExportCompletion
        return payload_type(
            job_id="11111111-1111-1111-1111-111111111111",
            lease_token="22222222-2222-2222-2222-222222222222",
            result=result,
        )

    @staticmethod
    def _job(result: dict) -> dict:
        return {
            "job_id": "11111111-1111-1111-1111-111111111111",
            "kind": "video_export",
            "worker_kind": "cpu",
            "object_id": "boom",
            "video_id": "video-1",
            "relpath": "nested/clip.mp4",
            "annotation_revision": 7,
            "root_basename": "clip__video-1",
            "owner": result["owner"],
            "total": 2,
            "user": "guilherme",
        }

    async def test_active_worker_completion_uses_authoritative_job_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, result = await _prepared(Path(temporary))
            queue = Sam3Queue()
            job = self._job(result)
            with (
                patch(
                    "server.routers.annotations.durable_jobs.owned_job_lease_async",
                    side_effect=lambda *_args, **_kwargs: _owned(job),
                ),
                patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    side_effect=lambda *_args, **_kwargs: _owned(None),
                ),
                patch("server.video_export_completion.sam3_queue", queue),
            ):
                response = await annotations.complete_export_internal(
                    "video-1", self._payload(result), ctx, "worker-token"
                )

            self.assertTrue(response["ok"])
            self.assertEqual(ctx.store.entry("nested/clip.mp4")["exported_by"], "guilherme")
            self.assertIsNotNone(queue.get("boom", "nested/clip.mp4"))

    async def test_completion_holds_video_then_job_fences_through_finalizer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, result = await _prepared(Path(temporary))
            job = self._job(result)
            events: list[str] = []

            @asynccontextmanager
            async def video_lock(*_args):
                events.append("video-enter")
                try:
                    yield
                finally:
                    events.append("video-exit")

            @asynccontextmanager
            async def job_lock(*_args):
                self.assertEqual(events, ["video-enter"])
                events.append("job-enter")
                try:
                    yield job
                finally:
                    events.append("job-exit")

            async def finalize(*_args, **_kwargs):
                self.assertEqual(events, ["video-enter", "job-enter"])
                events.append("finalize")
                def complete():
                    self.assertEqual(events[-2:], ["job-exit", "video-exit"])
                    events.append("reconcile")
                    return {"projection_pending": False, "projection_event_seq": 42}
                return SimpleNamespace(digest="digest", replayed=False,
                                       projection=SimpleNamespace(complete=complete))

            with patch(
                "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                side_effect=video_lock,
            ), patch.object(
                durable_jobs,
                "owned_job_lease_async",
                create=True,
                side_effect=job_lock,
            ), patch(
                "server.routers.annotations.finalize_video_export",
                side_effect=finalize,
            ):
                await annotations.complete_export_internal(
                    "video-1", self._payload(result), ctx, "worker-token"
                )

            self.assertEqual(
                events,
                ["video-enter", "job-enter", "finalize", "job-exit", "video-exit", "reconcile"],
            )

    async def test_missing_expired_or_cancelled_lease_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, result = await _prepared(Path(temporary))
            with patch(
                "server.routers.annotations.durable_jobs.owned_job_lease_async",
                side_effect=lambda *_args, **_kwargs: _owned(None),
            ), patch(
                "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                side_effect=lambda *_args, **_kwargs: _owned(None),
            ), self.assertRaises(HTTPException) as raised:
                await annotations.complete_export_internal(
                    "video-1", self._payload(result), ctx, "worker-token"
                )
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(ctx.store.entry("nested/clip.mp4")["status"], "in_progress")

    async def test_wrong_job_identity_is_rejected_before_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, result = await _prepared(Path(temporary))
            mutations = {
                "kind": {"kind": "dataset_export"},
                "worker": {"worker_kind": "gpu"},
                "object": {"object_id": "other"},
                "video": {"video_id": "video-2"},
                "relpath": {"relpath": "other.mp4"},
                "revision": {"annotation_revision": 6},
                "root": {"root_basename": "other"},
                "owner": {"owner": {**result["owner"], "video_id": "video-2"}},
                "total": {"total": 9},
            }
            for label, change in mutations.items():
                job = {**self._job(result), **change}
                with self.subTest(label=label), patch(
                    "server.routers.annotations.durable_jobs.owned_job_lease_async",
                    side_effect=lambda *_args, _job=job, **_kwargs: _owned(_job),
                ), patch(
                    "server.routers.annotations.durable_jobs.video_advisory_lock_async",
                    side_effect=lambda *_args, **_kwargs: _owned(None),
                ), self.assertRaises(HTTPException) as raised:
                    await annotations.complete_export_internal(
                        "video-1", self._payload(result), ctx, "worker-token"
                    )
                self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(ctx.store.entry("nested/clip.mp4")["status"], "in_progress")

    def test_body_is_strict_and_bounded(self) -> None:
        payload_type = annotations.WorkerExportCompletion
        with self.assertRaises(ValidationError):
            payload_type(
                job_id=str(uuid.uuid4()),
                lease_token=str(uuid.uuid4()),
                result={
                    "root": "/workspace/out",
                    "segments": ["seg_00"],
                    "total_frames": 1,
                    "jpeg_qscale": 2,
                    "frame_naming": "restart_per_segment",
                    "ffmpeg_version": "x",
                    "annotation_revision": 1,
                    "owner": {
                        "schema_version": 1,
                        "object_id": "boom",
                        "video_id": "video-1",
                        "relpath": "clip.mp4",
                        "annotation_revision": 1,
                    },
                    "unexpected": "not persisted",
                },
            )

    def test_wrong_shared_worker_token_is_rejected(self) -> None:
        with patch.dict(os.environ, {"MST_WORKER_TOKEN": "correct"}), self.assertRaises(
            HTTPException
        ) as raised:
            require_worker("wrong")
        self.assertEqual(raised.exception.status_code, 401)

    async def test_legacy_finish_replays_the_same_finalizer_without_requeueing_old_revision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx, result = await _prepared(Path(temporary))
            queue = Sam3Queue()
            user = User("guilherme", "Guilherme", "#fff", "2026-01-01T00:00:00Z")
            local_job = SimpleNamespace(
                job_id="jlegacy",
                kind="export",
                object_id="boom",
                video_id="video-1",
                state="done",
                result=result,
            )
            with (
                patch("server.video_export_completion.sam3_queue", queue),
            ):
                await finalize_video_export(
                    ctx, "video-1", result, "guilherme", "jlegacy"
                )
                before = copy.deepcopy(ctx.store.entry("nested/clip.mp4"))
                with patch("server.jobs.jobs.get", return_value=local_job):
                    response = await annotations.finish_export(
                        "video-1",
                        annotations.FinishPayload(job_id="jlegacy"),
                        ctx,
                        user,
                    )

            after = ctx.store.entry("nested/clip.mp4")
            self.assertEqual(response["export_completion"], before["export_completion"])
            self.assertEqual(after["exported_at"], before["exported_at"])
            self.assertEqual(queue.get("boom", "nested/clip.mp4").annotation_revision, 7)


@unittest.skipUnless(
    callable(getattr(durable_jobs, "renew_owned_lease", None)),
    "fence ainda nao implementado",
)
class OwnedLeaseTests(unittest.TestCase):
    def test_invalid_ids_are_rejected_without_opening_database(self) -> None:
        with patch("server.durable_jobs._connect") as connect:
            self.assertIsNone(durable_jobs.renew_owned_lease("bad", "also-bad"))
        connect.assert_not_called()

    def test_active_lease_returns_only_authoritative_job_payload(self) -> None:
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = (
            "11111111-1111-1111-1111-111111111111",
            "video_export",
            "cpu",
            {
                "object_id": "boom",
                "video_id": "video-1",
                "relpath": "clip.mp4",
                "annotation_revision": 3,
                "root_basename": "clip__video-1",
                "owner": {"schema_version": 1},
                "total": 4,
                "user": "ana",
            },
        )
        with patch("server.durable_jobs._connect", return_value=connection):
            found = durable_jobs.renew_owned_lease(
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            )
        self.assertEqual(found["kind"], "video_export")
        self.assertEqual(found["object_id"], "boom")
        self.assertEqual(found["annotation_revision"], 3)
        sql = cursor.execute.call_args.args[0]
        self.assertIn("lease_expires_at > now()", sql)
        self.assertIn("cancel_requested = FALSE", sql)

    def test_owned_lease_keeps_row_transaction_open_until_commit_finishes(self) -> None:
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = (
            "11111111-1111-1111-1111-111111111111",
            "video_export",
            "cpu",
            {"object_id": "boom", "video_id": "video-1"},
        )
        with patch("server.durable_jobs._connect", return_value=connection):
            with durable_jobs.owned_job_lease(
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ) as job:
                self.assertEqual(job["kind"], "video_export")
                connection.commit.assert_not_called()
                connection.close.assert_not_called()

        connection.commit.assert_called_once_with()
        connection.rollback.assert_not_called()
        connection.close.assert_called_once_with()
        sql = cursor.execute.call_args.args[0]
        self.assertIn("lease_token", sql)
        self.assertIn("lease_expires_at > now()", sql)
        self.assertIn("cancel_requested = FALSE", sql)


if __name__ == "__main__":
    unittest.main()
