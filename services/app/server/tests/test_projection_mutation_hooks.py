from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from server.pipeline_projection import ProjectionIntent
from server.routers.annotations import NoBoomPayload, mark_no_object, put_one
from server.tests.test_triage_mutation_latency import _context, _user
from server.tests.test_triage_optimistic_concurrency import payload
from server.tests.test_triage_export_completion import _prepared
from server.video_export_completion import finalize_video_export


def intent(**kwargs):
    return ProjectionIntent(event_seq=41, status="pending", error=None,
                            created_at=None, updated_at=None, **kwargs)


class ProjectionMutationHooksTests(unittest.IsolatedAsyncioTestCase):
    def test_response_keeps_reserved_event_when_reconcile_selects_newer_source(self):
        from types import SimpleNamespace
        from server.pipeline_mutation import reserve_mutation
        with patch("server.pipeline_projection.reserve_intent", side_effect=intent), patch(
            "server.pipeline_reconcile.reconcile_video", return_value=SimpleNamespace(event_seq=99, projection_status="current")
        ):
            mutation = reserve_mutation(SimpleNamespace(object_id="boom"), "video-1", "saved", {})
            self.assertEqual(mutation.complete(), {"projection_pending": False, "projection_event_seq": 41})

    async def test_annotation_reservation_failure_prevents_file_commit(self):
        for no_object in (False, True):
            with self.subTest(no_object=no_object), tempfile.TemporaryDirectory() as tmp:
                ctx = _context(Path(tmp))
                before = ctx.store.doc.copy()
                with (patch("server.routers.annotations.proxy.is_complete", return_value=100),
                      patch("server.routers.annotations.durable_jobs.enabled", return_value=False),
                      patch("server.pipeline_projection.reserve_intent", side_effect=RuntimeError("database unavailable"))):
                    with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                        if no_object:
                            await mark_no_object("video-1", NoBoomPayload(expected_revision=0, lock_token="token1234"), True, ctx, _user(), "tab")
                        else:
                            await put_one("video-1", payload(0, "token1234"), True, ctx, _user(), "tab")
                self.assertEqual(ctx.store.doc, before)
                self.assertIsNone(ctx.store.entry("clip.mp4"))

    async def test_no_object_apply_failure_preserves_success_and_retry_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            identities = []
            def reserve(**kwargs):
                identities.append(kwargs["source_identity"])
                return intent(**kwargs)
            with (patch("server.routers.annotations.proxy.is_complete", return_value=100),
                  patch("server.routers.annotations.durable_jobs.enabled", return_value=False),
                  patch("server.pipeline_projection.reserve_intent", side_effect=reserve),
                  patch("server.pipeline_reconcile.reconcile_video", side_effect=RuntimeError("apply failed")),
                  patch("server.pipeline_projection.fail_intent")):
                first = await mark_no_object("video-1", NoBoomPayload(expected_revision=0, lock_token="token1234"), True, ctx, _user(), "tab")
                second = await mark_no_object("video-1", NoBoomPayload(expected_revision=1, lock_token="token1234"), True, ctx, _user(), "tab")
            self.assertTrue(first["projection_pending"])
            self.assertEqual(first["projection_event_seq"], 41)
            self.assertEqual(second["annotation_revision"], 1)
            self.assertEqual(identities[0], identities[1])
            self.assertNotIn("projection_pending", ctx.store.entry("clip.mp4"))

    async def test_conflict_does_not_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            with (patch("server.routers.annotations.proxy.is_complete", return_value=100),
                  patch("server.routers.annotations.durable_jobs.enabled", return_value=False),
                  patch("server.pipeline_projection.reserve_intent", side_effect=AssertionError("reserved before validation"))):
                with self.assertRaises(HTTPException) as raised:
                    await put_one("video-1", payload(9, "token1234"), True, ctx, _user(), "tab")
            self.assertEqual(raised.exception.status_code, 409)

    async def test_export_reservation_failure_prevents_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, root, result = await _prepared(Path(tmp))
            with (patch("server.video_export_completion.durable_jobs.enabled", return_value=False),
                  patch("server.pipeline_projection.reserve_intent", side_effect=RuntimeError("reserve failed"))):
                with self.assertRaisesRegex(RuntimeError, "reserve failed"):
                    await finalize_video_export(ctx, "video-1", result, "ana", "job-1")
            self.assertEqual(ctx.store.entry("clip.mp4")["status"], "in_progress")
            self.assertNotIn("export_completion", ctx.store.entry("clip.mp4"))

    async def test_export_with_outer_lock_defers_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx, root, result = await _prepared(Path(tmp))
            ctx.config.auto_sam3 = False
            with (patch("server.pipeline_projection.reserve_intent", side_effect=intent),
                  patch("server.pipeline_reconcile.reconcile_video", side_effect=RuntimeError("apply failed")) as reconcile,
                  patch("server.pipeline_projection.fail_intent")):
                completed = await finalize_video_export(ctx, "video-1", result, "ana", "job-1", _video_lock_held=True)
                reconcile.assert_not_called()
                self.assertTrue(completed.projection_pending)
                flags = completed.projection.complete()
            self.assertTrue(flags["projection_pending"])
            self.assertEqual(flags["projection_event_seq"], 41)
            self.assertEqual(ctx.store.entry("clip.mp4")["status"], "done")
