"""Library projection acceptance, bulk access and fail-closed repair contracts."""
from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import pipeline_projection
from server.pipeline_state import derive_pipeline_source
from server.routers import library
from server.tests.test_pipeline_reconcile import _CanonicalVideo


class LibraryProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = _CanonicalVideo(Path(self.temp.name))
        self.video = SimpleNamespace(video_id="video-1", relpath="clip.mp4", name="clip",
                                     size_bytes=1, file_mtime=1, abspath=Path(self.temp.name) / "clip.mp4")
        self.ctx = SimpleNamespace(object_id="boom", label="Boom", videos_root=Path(self.temp.name),
                                   output_root=self.fixture.output_root,
                                   index=SimpleNamespace(all=lambda: [self.video], cached_probe=lambda _: None, scanned_at=None),
                                   store=SimpleNamespace(listing_snapshot=lambda _: ({"clip.mp4": self.fixture.entry}, {"total": 1})))
        self.source = derive_pipeline_source(self.fixture.entry, None, self.fixture.output_root)

    def record(self, **changes):
        values = dict(object_id="boom", video_id="video-1", event_seq=10,
                      source_identity=copy.deepcopy(self.source.identity), snapshot=self.source.snapshot_dict,
                      projected_at=datetime(2026, 9, 10, tzinfo=timezone.utc), projection_status="current")
        values.update(changes)
        return SimpleNamespace(**values)

    def listing(self, records):
        with patch.object(pipeline_projection, "get_many", return_value=records) as lookup, \
             patch.object(library.locks, "map_for", return_value={}), \
             patch.object(library.sam3_queue, "map_for", return_value={}), \
             patch("server.durable_jobs.enqueue_projection_reconciles", create=True) as enqueue, \
             patch("server.pipeline_state.inspect_binary_png", side_effect=AssertionError("PNG read forbidden")):
            result = library._build_video_listing(self.ctx)
        return result, lookup, enqueue

    def test_exact_current_projection_serves_verified_counts_with_one_bulk_lookup(self):
        result, lookup, enqueue = self.listing({"video-1": self.record()})
        item = result["videos"][0]
        self.assertEqual(item.get("projection_status"), "current")
        self.assertEqual(item["pipeline_stage"], "completed")
        self.assertEqual(item["stage_progress"]["reviewed_frames"], 1)
        self.assertEqual(item["projected_at"], "2026-09-10T00:00:00+00:00")
        lookup.assert_called_once_with("boom", ["video-1"])
        enqueue.assert_not_called()

    def test_unusable_projection_falls_back_and_enqueues_repair(self):
        self.fixture.entry["status"] = "pending"
        for record, expected_status in [(None, "missing"), (self.record(projection_status="pending"), "pending"),
                                        (self.record(projection_status="stale"), "stale"), (self.record(), "stale")]:
            with self.subTest(status=expected_status):
                result, _, enqueue = self.listing({"video-1": record} if record else {})
                item = result["videos"][0]
                self.assertEqual(item.get("projection_status"), expected_status)
                self.assertEqual(item["pipeline_stage"], "triage")
                enqueue.assert_called_once()

    def test_matching_but_invalid_completion_is_never_accepted(self):
        for mutation in [{"artifacts_valid": False}, {"validation_status": "audit_required"},
                         {"reviewed_frames": 0}, {"expected_frames": 0}, {"complete": False}]:
            with self.subTest(mutation=mutation):
                snapshot = {**self.source.snapshot_dict, **mutation}
                result, _, enqueue = self.listing({"video-1": self.record(snapshot=snapshot)})
                self.assertEqual(result["videos"][0].get("projection_status"), "stale")
                self.assertEqual(result["videos"][0]["pipeline_stage"], "review")
                enqueue.assert_called_once()

    def test_missing_projection_cannot_complete_even_when_metadata_is_reviewed(self):
        result, _, _ = self.listing({})
        self.assertEqual(result["videos"][0]["pipeline_stage"], "review")
        self.assertEqual(result["pipeline_counts"], {"review": 1})

    def test_malformed_nested_diagnostics_and_incoherent_snapshots_fall_back_safely(self):
        mutations = [
            {"inconsistencies": [{"bad": "not renderable"}]},
            {"inconsistencies": [["nested"]]}, {"inconsistencies": [None]},
            {"inconsistencies": [1]}, {"inconsistencies": [""]},
            {"inconsistencies": ["x" * 1001]}, {"inconsistencies": ["error"] * 257},
            {"inconsistencies": ["bad\x00text"]}, {"inconsistencies": ["\ud800"]},
            {"inconsistencies": ["valid diagnostic still prevents completion"]},
            {"reviewed_frames": 2}, {"edited_frames": 2}, {"edited_frames": True},
            {"pipeline_stage": "triage", "stage_status": "validated", "complete": False},
            {"pipeline_stage": "review", "stage_status": "waiting", "complete": False},
            {"pipeline_stage": "sam3", "stage_status": "not-a-status", "complete": False},
            {"pipeline_stage": "sam3", "stage_status": "running", "complete": "false"},
            {"pipeline_stage": "sam3", "stage_status": "ready", "complete": False},
            {"pipeline_stage": "sam3", "stage_status": "invalid", "complete": False},
            {"pipeline_stage": "review", "stage_status": "audit_required", "validation_status": "audit_required", "complete": False},
        ]
        for mutation in mutations:
            with self.subTest(mutation=repr(mutation)[:100]):
                result, _, enqueue = self.listing({"video-1": self.record(snapshot={**self.source.snapshot_dict, **mutation})})
                item = result["videos"][0]
                self.assertEqual(item["projection_status"], "stale")
                self.assertEqual(item["pipeline_stage"], "review")
                self.assertEqual(item["stage_progress"]["inconsistencies"], [])
                enqueue.assert_called_once()

    def test_noncompleted_diagnostics_are_bounded_strings_and_valid_errors_are_kept(self):
        base = {**self.source.snapshot_dict, "pipeline_stage": "sam3", "stage_status": "invalid",
                "complete": False, "artifacts_valid": False, "validation_status": "invalid"}
        for diagnostic in [{"nested": "object"}, ["nested"], None, 3, "", "x" * 1001, "\ud800", "bad\x00"]:
            with self.subTest(diagnostic=repr(diagnostic)[:40]):
                result, _, enqueue = self.listing({"video-1": self.record(snapshot={**base, "inconsistencies": [diagnostic]})})
                self.assertEqual(result["videos"][0]["projection_status"], "stale")
                self.assertEqual(result["videos"][0]["stage_progress"]["inconsistencies"], [])
                enqueue.assert_called_once()
        result, _, enqueue = self.listing({"video-1": self.record(snapshot={**base, "inconsistencies": ["Manifesto inválido"]})})
        self.assertEqual(result["videos"][0]["projection_status"], "current")
        self.assertEqual(result["videos"][0]["stage_progress"]["inconsistencies"], ["Manifesto inválido"])
        enqueue.assert_not_called()

    def test_repair_batches_rotate_past_permanently_failing_first_twenty(self):
        self.ctx.object_id = "rotation-test"
        videos = [SimpleNamespace(**{**vars(self.video), "video_id": f"v-{n:03}"}) for n in range(45)]
        self.ctx.index.all = lambda: list(reversed(videos))
        batches = []
        for _ in range(3):
            _, lookup, enqueue = self.listing({})
            lookup.assert_called_once()
            enqueue.assert_called_once()
            batches.append([item["video_id"] for item in enqueue.call_args.args[1]])
        self.assertEqual(batches[0], [f"v-{n:03}" for n in range(20)])
        self.assertEqual(batches[1], [f"v-{n:03}" for n in range(20, 40)])
        self.assertEqual(batches[2], [f"v-{n:03}" for n in list(range(40, 45)) + list(range(15))])
        self.assertEqual(set().union(*map(set, batches)), {video.video_id for video in videos})

    def test_large_listing_bounds_repair_batch_without_per_video_queries(self):
        videos = [SimpleNamespace(**{**vars(self.video), "video_id": f"v-{n}"}) for n in range(100)]
        self.ctx.index.all = lambda: videos
        result, lookup, enqueue = self.listing({})
        self.assertEqual(len(result["videos"]), 100)
        lookup.assert_called_once_with("boom", [v.video_id for v in videos])
        enqueue.assert_called_once()
        self.assertGreater(len(enqueue.call_args.args[1]), 0)
        self.assertLessEqual(len(enqueue.call_args.args[1]), 20)


if __name__ == "__main__":
    unittest.main()
