from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server.pipeline_state as pipeline_state
from server.pipeline_projection import ProjectionIntent, ProjectionRecord

try:
    import server.pipeline_reconcile as pipeline_reconcile
except ModuleNotFoundError:
    pipeline_reconcile = None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _generation_digest(value: dict) -> str:
    canonical = copy.deepcopy(value)
    canonical.pop("manifest_sha256", None)
    result = canonical.get("result")
    if isinstance(result, dict):
        result.pop("publication", None)
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class _CanonicalVideo:
    def __init__(self, base: Path) -> None:
        self.output_root = base / "dataset"
        self.export_root = self.output_root / "clip"
        self.segment = self.export_root / "seg_00"
        self.annotation_revision = 7
        self.entry = {
            "video_id": "video-1",
            "relpath": "clip.mp4",
            "status": "done",
            "annotation_revision": self.annotation_revision,
            "updated_at": "2026-09-10T10:00:00Z",
            "exported_at": "2026-09-10T10:00:00Z",
            "export": {
                "root": self.export_root.as_posix(),
                "segments": ["seg_00"],
                "total_frames": 1,
                "annotation_revision": self.annotation_revision,
                "owner": {
                    "schema_version": 1,
                    "object_id": "boom",
                    "video_id": "video-1",
                    "relpath": "clip.mp4",
                    "annotation_revision": self.annotation_revision,
                },
            },
        }
        self.prompt = {
            "schema_version": 1,
            "frame_count": 1,
            "image_width": 16,
            "image_height": 9,
            "prompt_frame_idx": 0,
            "objects": [
                {
                    "obj_id": 1,
                    "label": "boom",
                    "box_normalized": [0.1, 0.1, 0.9, 0.9],
                }
            ],
        }
        self.run = {
            "schema_version": 1,
            "status": "done",
            "frame_count": 1,
            "frames_written": 1,
            "objects": [{"obj_id": 1}],
            "artifacts": {
                "format": "png-1bit-v1",
                "files": 1,
                "checksums": {"masks/1/000000.png": "a" * 64},
            },
        }
        self.review = {
            "schema_version": 1,
            "updated_at": "2026-09-10T10:00:00Z",
            "frames": {
                "0": {
                    "revision": 1,
                    "status": "ok",
                    "by": "guilherme",
                }
            },
        }
        _write_json(self.segment / "prompt.json", self.prompt)
        self.publish_generation("generation-a", published_at="2026-09-10T10:00:00Z")

    def publish_generation(self, generation_id: str, *, published_at: str) -> None:
        output = self.export_root / "_sam3" / "runs" / generation_id / "seg_00"
        _write_json(output / "run.json", self.run)
        _write_json(output / "mask_review.json", self.review)
        generation = {
            "schema_version": 1,
            "generation_id": generation_id,
            "annotation_revision": self.annotation_revision,
            "model": {
                "model_id": "sam3-finetuned-v1",
                "checkpoint_sha256": "b" * 64,
                "sam3_commit": "c" * 40,
            },
            "published_at": published_at,
            "result": {
                "segment_runs": [
                    {
                        "segment": "seg_00",
                        "prompt_digest": None,
                    }
                ]
            },
        }
        generation["manifest_sha256"] = _generation_digest(generation)
        _write_json(
            self.export_root / "_sam3" / "runs" / generation_id / "generation.json",
            generation,
        )
        _write_json(
            self.export_root / "_sam3" / "current.json",
            {
                "schema_version": 1,
                "format": "immutable-generation-v1",
                "generation_id": generation_id,
                "annotation_revision": self.annotation_revision,
                "model": generation["model"],
                "manifest_sha256": generation["manifest_sha256"],
                "published_at": published_at,
                "segments": {
                    "seg_00": {
                        "path": f"runs/{generation_id}/seg_00",
                        "prompt_digest": None,
                    }
                },
            },
        )

    @property
    def active_output(self) -> Path:
        pointer = json.loads(
            (self.export_root / "_sam3" / "current.json").read_text(
                encoding="utf-8"
            )
        )
        return (
            self.export_root
            / "_sam3"
            / "runs"
            / pointer["generation_id"]
            / "seg_00"
        )


def _derive(entry: dict | None, sam3: dict | None, output_root: Path):
    function = getattr(pipeline_state, "derive_pipeline_source", None)
    if not callable(function):
        raise AssertionError("derive_pipeline_source ausente")
    return function(entry, sam3, output_root)


class PipelineSourceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.fixture = _CanonicalVideo(self.base)
        self.sam3 = {
            "state": "done",
            "annotation_revision": 7,
            "run_id": "generation-a",
        }

    def test_identity_changes_with_annotation_revision_not_wall_clock_metadata(self) -> None:
        first = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)
        wall_clock_only = copy.deepcopy(self.fixture.entry)
        wall_clock_only["updated_at"] = "2099-01-01T00:00:00Z"
        wall_clock_only["exported_at"] = "2099-01-01T00:00:00Z"

        for path in (
            self.fixture.segment / "prompt.json",
            self.fixture.active_output / "run.json",
            self.fixture.active_output / "mask_review.json",
        ):
            os.utime(path, (4_000_000_000, 4_000_000_000))

        same = _derive(wall_clock_only, self.sam3, self.fixture.output_root)
        newer_entry = copy.deepcopy(wall_clock_only)
        newer_entry["annotation_revision"] = 8
        changed = _derive(newer_entry, self.sam3, self.fixture.output_root)

        self.assertEqual(first.identity, same.identity)
        self.assertNotEqual(first.identity, changed.identity)

    def test_identity_changes_when_active_sam3_pointer_swaps_generation(self) -> None:
        first = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)

        self.fixture.publish_generation(
            "generation-b", published_at="2026-09-10T11:00:00Z"
        )
        changed = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)

        self.assertNotEqual(first.identity, changed.identity)
        self.assertEqual(
            changed.identity["sam3_generation"]["generation_id"], "generation-b"
        )
        self.assertRegex(
            changed.identity["sam3_generation"]["manifest_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_identity_changes_on_review_revision_but_not_review_timestamp(self) -> None:
        first = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)
        timestamp_only = copy.deepcopy(self.fixture.review)
        timestamp_only["updated_at"] = "2099-01-01T00:00:00Z"
        _write_json(self.fixture.active_output / "mask_review.json", timestamp_only)

        same = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)
        revised = copy.deepcopy(timestamp_only)
        revised["frames"]["0"]["revision"] = 2
        _write_json(self.fixture.active_output / "mask_review.json", revised)
        changed = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)

        self.assertEqual(first.identity, same.identity)
        self.assertNotEqual(first.identity, changed.identity)
        review_identity = changed.identity["segments"][0]["mask_review"]
        self.assertEqual(review_identity["max_revision"], 2)

    def test_projection_snapshot_is_flat_deterministic_and_complete_only_for_manifest(self) -> None:
        source = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)

        self.assertEqual(
            source.snapshot_dict,
            {
                "pipeline_stage": "completed",
                "stage_status": "validated",
                "expected_frames": 1,
                "reviewed_frames": 1,
                "edited_frames": 0,
                "artifacts_valid": True,
                "validation_status": "manifest",
                "inconsistencies": [],
                "complete": True,
            },
        )

    def test_legacy_generation_requires_audit_and_never_projects_completed(self) -> None:
        legacy_root = self.base / "legacy-dataset"
        segment = legacy_root / "legacy-video" / "seg_00"
        _write_json(segment / "prompt.json", self.fixture.prompt)
        _write_json(segment / "_sam3" / "run.json", self.fixture.run)
        _write_json(segment / "_sam3" / "mask_review.json", self.fixture.review)
        entry = copy.deepcopy(self.fixture.entry)
        entry["export"]["root"] = (legacy_root / "legacy-video").as_posix()

        source = _derive(entry, self.sam3, legacy_root)

        self.assertEqual(source.snapshot.validation_status, "audit_required")
        self.assertNotEqual(source.snapshot.stage, "completed")
        self.assertFalse(source.snapshot_dict["complete"])
        self.assertEqual(source.identity["sam3_generation"]["state"], "legacy")

    def test_malformed_review_is_invalid_and_never_projects_completed(self) -> None:
        (self.fixture.active_output / "mask_review.json").write_text(
            '{"schema_version": 1, "frames":', encoding="utf-8"
        )

        try:
            source = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)
        except Exception as exc:  # noqa: BLE001 - the contract is fail-closed
            self.fail(f"metadado hostil escapou da derivacao: {exc}")

        self.assertEqual(source.snapshot.validation_status, "invalid")
        self.assertNotEqual(source.snapshot.stage, "completed")
        self.assertFalse(source.snapshot_dict["complete"])
        self.assertEqual(
            source.identity["segments"][0]["mask_review"]["state"], "invalid"
        )

    def test_non_positive_review_revision_is_invalid_not_legacy(self) -> None:
        malformed = copy.deepcopy(self.fixture.review)
        malformed["frames"]["0"]["revision"] = -1
        _write_json(self.fixture.active_output / "mask_review.json", malformed)

        source = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)

        self.assertEqual(
            source.identity["segments"][0]["mask_review"]["state"], "invalid"
        )
        self.assertEqual(source.snapshot.validation_status, "invalid")
        self.assertFalse(source.snapshot_dict["complete"])

    def test_unsafe_generation_identifier_fails_closed_without_escaping_derivation(self) -> None:
        _write_json(
            self.fixture.export_root / "_sam3" / "current.json",
            {
                "schema_version": 1,
                "format": "immutable-generation-v1",
                "generation_id": "../foreign",
                "annotation_revision": 7,
                "model": {},
                "manifest_sha256": "d" * 64,
                "segments": {},
            },
        )

        try:
            source = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)
        except Exception as exc:  # noqa: BLE001 - the contract is fail-closed
            self.fail(f"metadado hostil escapou da derivacao: {exc}")

        self.assertEqual(source.snapshot.validation_status, "invalid")
        self.assertFalse(source.snapshot_dict["complete"])
        self.assertEqual(source.identity["sam3_generation"]["state"], "invalid")

    def test_source_derivation_never_reads_or_enumerates_mask_pngs(self) -> None:
        masks = self.fixture.active_output / "masks" / "1"
        masks.mkdir(parents=True)
        (masks / "000000.png").write_bytes(b"not-read")

        original_open = Path.open

        def guarded_open(path: Path, *args, **kwargs):
            if path.suffix.lower() == ".png":
                raise AssertionError("a reconciliacao tentou abrir PNG")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open), patch.object(
            Path, "glob", side_effect=AssertionError("enumeracao de mascaras")
        ):
            source = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)

        self.assertEqual(source.snapshot.stage, "completed")


class _Store:
    def __init__(self, relpath: str, entry: dict) -> None:
        self.relpath = relpath
        self.value = copy.deepcopy(entry)
        self.loads = 0

    def load(self) -> dict:
        self.loads += 1
        return {"videos": {self.relpath: copy.deepcopy(self.value)}}

    def entry(self, relpath: str) -> dict | None:
        return copy.deepcopy(self.value) if relpath == self.relpath else None


class _Index:
    def __init__(self, *, present: bool = True) -> None:
        self.present = present

    def get(self, video_id: str):
        if self.present and video_id == "video-1":
            return SimpleNamespace(video_id="video-1", relpath="clip.mp4")
        return None


def _intent(identity: dict, *, event_seq: int = 11) -> ProjectionIntent:
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    return ProjectionIntent(
        event_seq=event_seq,
        object_id="boom",
        video_id="video-1",
        event_kind="reconcile",
        source_identity=copy.deepcopy(identity),
        status="pending",
        error=None,
        created_at=now,
        updated_at=now,
    )


def _context(fixture: _CanonicalVideo, *, present: bool = True, archived: bool = False):
    return SimpleNamespace(
        object_id="boom",
        output_root=fixture.output_root,
        store=_Store("clip.mp4", fixture.entry),
        index=_Index(present=present),
        config=SimpleNamespace(archived=archived),
        ensure_loaded=lambda: None,
    )


class PipelineReconcileContractTests(unittest.TestCase):
    def test_reconcile_module_exists(self) -> None:
        self.assertIsNotNone(
            pipeline_reconcile, "server.pipeline_reconcile ainda nao existe"
        )


@unittest.skipIf(pipeline_reconcile is None, "pipeline_reconcile ainda nao existe")
class PipelineReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        self.fixture = _CanonicalVideo(self.base)
        self.ctx = _context(self.fixture)
        self.sam3 = {
            "state": "done",
            "annotation_revision": 7,
            "run_id": "generation-a",
        }

    def _applied(self, intent: ProjectionIntent, snapshot: dict) -> ProjectionRecord:
        return ProjectionRecord(
            object_id=intent.object_id,
            video_id=intent.video_id,
            event_seq=intent.event_seq,
            source_identity=copy.deepcopy(intent.source_identity),
            snapshot=copy.deepcopy(snapshot),
            projected_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        )

    def test_pending_intent_with_same_identity_is_applied_without_new_reservation(self) -> None:
        source = _derive(self.fixture.entry, self.sam3, self.fixture.output_root)
        pending = _intent(source.identity)

        with patch.object(
            pipeline_reconcile.sam3_queue, "public", return_value=self.sam3
        ), patch.object(
            pipeline_reconcile,
            "reserve_intent",
            side_effect=AssertionError("intent igual nao deve ser reservado novamente"),
        ), patch.object(
            pipeline_reconcile,
            "apply_intent",
            side_effect=lambda intent, snapshot: self._applied(intent, snapshot),
        ):
            record = pipeline_reconcile.reconcile_video(
                self.ctx, "video-1", intent=pending
            )

        self.assertEqual(record.event_seq, 11)
        self.assertEqual(record.snapshot["pipeline_stage"], "completed")
        self.assertEqual(self.ctx.store.loads, 1)

    def test_canonical_read_reservation_and_apply_share_the_video_fence(self) -> None:
        events: list[str] = []

        @contextmanager
        def fence(object_id: str, video_id: str):
            self.assertEqual((object_id, video_id), ("boom", "video-1"))
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def reserve(**kwargs):
            events.append("reserve")
            return _intent(kwargs["source_identity"])

        def apply(intent, snapshot):
            events.append("apply")
            return self._applied(intent, snapshot)

        with patch.object(
            pipeline_reconcile.sam3_queue, "public", return_value=self.sam3
        ), patch.object(
            pipeline_reconcile, "video_fence", fence, create=True
        ), patch.object(
            pipeline_reconcile, "reserve_intent", side_effect=reserve
        ), patch.object(pipeline_reconcile, "apply_intent", side_effect=apply):
            pipeline_reconcile.reconcile_video(self.ctx, "video-1")

        self.assertEqual(events, ["enter", "reserve", "apply", "exit"])

    def test_changed_source_reserves_a_new_event_instead_of_applying_old_pending(self) -> None:
        old = _intent({"schema_version": 1, "annotation": {"revision": 6}})
        newer = _intent({}, event_seq=12)
        applied_events: list[int] = []

        def reserve(**kwargs):
            newer_identity = kwargs["source_identity"]
            return _intent(newer_identity, event_seq=12)

        def apply(intent, snapshot):
            applied_events.append(intent.event_seq)
            return self._applied(intent, snapshot)

        with patch.object(
            pipeline_reconcile.sam3_queue, "public", return_value=self.sam3
        ), patch.object(pipeline_reconcile, "reserve_intent", side_effect=reserve), patch.object(
            pipeline_reconcile, "apply_intent", side_effect=apply
        ):
            record = pipeline_reconcile.reconcile_video(
                self.ctx, "video-1", intent=old
            )

        self.assertEqual(record.event_seq, newer.event_seq)
        self.assertEqual(applied_events, [12])
        self.assertNotEqual(record.source_identity, old.source_identity)

    def test_repeated_reconciliation_is_idempotent(self) -> None:
        reserved: dict[str, ProjectionIntent] = {}

        def reserve(**kwargs):
            digest = json.dumps(kwargs["source_identity"], sort_keys=True)
            reserved.setdefault(digest, _intent(kwargs["source_identity"]))
            return reserved[digest]

        with patch.object(
            pipeline_reconcile.sam3_queue, "public", return_value=self.sam3
        ), patch.object(pipeline_reconcile, "reserve_intent", side_effect=reserve), patch.object(
            pipeline_reconcile,
            "apply_intent",
            side_effect=lambda intent, snapshot: self._applied(intent, snapshot),
        ):
            first = pipeline_reconcile.reconcile_video(self.ctx, "video-1")
            second = pipeline_reconcile.reconcile_video(self.ctx, "video-1")

        self.assertEqual(first.event_seq, second.event_seq)
        self.assertEqual(first.source_identity, second.source_identity)
        self.assertEqual(len(reserved), 1)

    def test_absent_video_closes_intent_with_non_completed_tombstone(self) -> None:
        ctx = _context(self.fixture, present=False)

        def reserve(**kwargs):
            return _intent(kwargs["source_identity"])

        with patch.object(pipeline_reconcile, "reserve_intent", side_effect=reserve), patch.object(
            pipeline_reconcile,
            "apply_intent",
            side_effect=lambda intent, snapshot: self._applied(intent, snapshot),
        ):
            record = pipeline_reconcile.reconcile_video(ctx, "video-1")

        self.assertFalse(record.source_identity["video"]["present"])
        self.assertEqual(record.snapshot["stage_status"], "missing")
        self.assertFalse(record.snapshot["complete"])

    def test_archived_object_closes_intent_without_reading_video_state(self) -> None:
        ctx = _context(self.fixture, archived=True)

        def reserve(**kwargs):
            return _intent(kwargs["source_identity"])

        with patch.object(pipeline_reconcile, "reserve_intent", side_effect=reserve), patch.object(
            pipeline_reconcile,
            "apply_intent",
            side_effect=lambda intent, snapshot: self._applied(intent, snapshot),
        ):
            record = pipeline_reconcile.reconcile_video(ctx, "video-1")

        self.assertTrue(record.source_identity["object"]["archived"])
        self.assertEqual(record.snapshot["stage_status"], "archived")
        self.assertFalse(record.snapshot["complete"])
        self.assertEqual(ctx.store.loads, 0)

    def test_apply_failure_keeps_intent_pending_with_sanitized_failure_path(self) -> None:
        pending = _intent({})
        failed: list[tuple[int, str]] = []

        def fail(intent, error):
            failed.append((intent.event_seq, str(error)))
            return intent

        with patch.object(
            pipeline_reconcile.sam3_queue, "public", return_value=self.sam3
        ), patch.object(
            pipeline_reconcile,
            "reserve_intent",
            side_effect=lambda **kwargs: _intent(kwargs["source_identity"]),
        ), patch.object(
            pipeline_reconcile, "apply_intent", side_effect=RuntimeError("database down")
        ), patch.object(pipeline_reconcile, "fail_intent", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "database down"):
                pipeline_reconcile.reconcile_video(
                    self.ctx, "video-1", intent=pending
                )

        self.assertEqual(failed, [(11, "database down")])

    def test_reservation_failure_does_not_mark_stale_intent_as_failed(self) -> None:
        stale = _intent({"schema_version": 1, "annotation": {"revision": 6}})

        with patch.object(
            pipeline_reconcile.sam3_queue, "public", return_value=self.sam3
        ), patch.object(
            pipeline_reconcile,
            "reserve_intent",
            side_effect=RuntimeError("database down before reserve"),
        ), patch.object(pipeline_reconcile, "fail_intent") as fail:
            with self.assertRaisesRegex(RuntimeError, "before reserve"):
                pipeline_reconcile.reconcile_video(
                    self.ctx, "video-1", intent=stale
                )

        fail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
