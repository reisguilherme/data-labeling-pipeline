from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pipeline_core.masks import encode_binary_png
from pipeline_core import sam3_runs
from server import dataset
from server import multiclass_dataset as multiclass
from server.routers import dataset as dataset_router


def _mask(size: tuple[int, int], x: int, y: int) -> bytes:
    image = Image.new("1", size, 0)
    image.putpixel((x, y), 1)
    return encode_binary_png(image)


class GlobalDatasetSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sam3_classes.json").write_text(
            json.dumps({"names": ["boom", "microphone"]}), encoding="utf-8"
        )
        self.contexts = [
            self._context("boom", "boom", x=1),
            self._context("microfone", "microphone", x=2),
        ]

    def _context(self, object_id: str, label: str, *, x: int):
        output = self.root / "data" / "objects" / object_id / "output"
        segment = output / "video" / "seg_00"
        annotation = segment / "_sam3"
        masks = annotation / "masks" / "1"
        labels = annotation / "labels"
        masks.mkdir(parents=True)
        labels.mkdir()
        Image.new("RGB", (4, 4), "white").save(segment / "000000.jpg")
        prompt_raw = json.dumps(
            {
                "schema_version": 1,
                "frame_count": 1,
                "image_width": 4,
                "image_height": 4,
                "prompt_frame_idx": 0,
                "objects": [
                    {
                        "obj_id": 1,
                        "label": label,
                        "normalized": [0, 0, 1, 1],
                    }
                ],
            }
        ).encode("utf-8")
        (segment / "prompt.json").write_bytes(prompt_raw)
        mask = _mask((4, 4), x, 1)
        (masks / "000000.png").write_bytes(mask)
        (labels / "000000.txt").write_text(
            f"0 {(x + 0.5) / 4} 0.375 0.25 0.25\n", encoding="utf-8"
        )
        (annotation / "run.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "done",
                    "runner_version": "0.2.0",
                    "prompt_digest": hashlib.sha256(prompt_raw).hexdigest(),
                    "frame_count": 1,
                    "frames_written": 1,
                    "objects": [{"obj_id": 1, "label": label, "class_index": 0}],
                    "params": {"mask_threshold": 0.5},
                    "model": {
                        "model_id": "sam3-finetuned-v1",
                        "checkpoint_sha256": object_id[0] * 64,
                        "sam3_commit": "c" * 40,
                    },
                    "artifacts": {
                        "format": "png-1bit-v1",
                        "files": 1,
                        "checksums": {
                            "masks/1/000000.png": hashlib.sha256(mask).hexdigest()
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        (annotation / "mask_review.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "frames": {"0": {"revision": 2, "status": "ok"}},
                }
            ),
            encoding="utf-8",
        )
        entry = {
            "status": "done",
            "video_id": "shared-video-id",
            "annotation_revision": 9,
            "export": {"root": str(output / "video"), "segments": ["seg_00"]},
            "intervals": [{"segment": "seg_00", "frame_count": 1, "flags": {}}],
            "media": {"width": 4, "height": 4},
        }
        return SimpleNamespace(
            object_id=object_id,
            label=label,
            output_root=output,
            store=SimpleNamespace(doc={"videos": {f"{object_id}.mp4": entry}}),
        )

    def _bound_snapshot(self) -> dict:
        with patch.object(
            multiclass,
            "_eligible_ids",
            side_effect=lambda ctx, _: ["shared-video-id"],
        ):
            source = multiclass.build_multiclass_snapshot(
                self.contexts,
                [],
                flags={},
                include_empty=False,
                task="detection",
                workspace_root=self.root,
            )
        return multiclass.bind_multiclass_export_spec(
            source,
            fmt="yolo",
            val_fraction=0,
            test_fraction=0,
        )

    def test_global_snapshot_freezes_all_object_runs_revisions_masks_and_split(self) -> None:
        builder = getattr(multiclass, "build_multiclass_snapshot", None)
        self.assertTrue(callable(builder), "build_multiclass_snapshot ausente")

        with patch.object(
            multiclass,
            "_eligible_ids",
            side_effect=lambda ctx, _: ["shared-video-id"],
        ), patch.object(
            dataset, "inspect_binary_png", side_effect=AssertionError("PNG decodificado")
        ):
            snapshot = builder(
                self.contexts,
                [],
                flags={},
                include_empty=False,
                task="detection",
                workspace_root=self.root,
            )
        bound = multiclass.bind_multiclass_export_spec(
            snapshot,
            fmt="yolo",
            val_fraction=0.2,
            test_fraction=0.1,
        )

        self.assertEqual(
            [(item["object_id"], item["name"]) for item in bound["classes"]],
            [("boom", "boom"), ("microfone", "microphone")],
        )
        self.assertEqual(len(bound["objects"]), 2)
        for item in bound["objects"]:
            source = item["dataset_snapshot"]
            segment = source["segments"][0]
            self.assertEqual(segment["annotation_revision"], 9)
            self.assertEqual(segment["frames"][0]["review_revision"], 2)
            self.assertRegex(segment["frames"][0]["instances"][0]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(segment["run"]["model_id"], "sam3-finetuned-v1")
        self.assertEqual(
            sorted(bound["export_spec"]["split"]["videos"]),
            ["boom:shared-video-id", "microfone:shared-video-id"],
        )
        self.assertRegex(bound["snapshot_id"], r"^[0-9a-f]{64}$")

    def test_preview_is_a_projection_of_one_snapshot_without_a_second_live_read(self) -> None:
        frozen = {
            "totals": {
                "segments": 2,
                "videos": 2,
                "frames": 20,
                "frames_with_objects": 17,
                "frames_reviewed": 20,
            },
            "classes": [
                {"id": 0, "object_id": "boom", "name": "boom"},
                {"id": 1, "object_id": "microfone", "name": "microphone"},
            ],
            "by_class": [],
            "by_flag": {"difficulty": {"hard": 20}},
            "task": "detection",
            "completed_only": True,
            "export_allowed": True,
            "blocking_reasons": [],
            "snapshot_id": "a" * 64,
        }
        with patch.object(
            multiclass, "build_multiclass_snapshot", return_value=frozen
        ), patch.object(
            multiclass, "_eligible_ids", side_effect=AssertionError("segunda leitura")
        ):
            result = multiclass.preview_multiclass(
                self.contexts,
                [],
                flags={},
                include_empty=False,
                task="detection",
                workspace_root=self.root,
            )

        self.assertEqual(result["frames"], 20)
        self.assertEqual(result["snapshot_id"], "a" * 64)

    def test_unselected_class_does_not_read_or_block_on_its_live_annotations(self) -> None:
        original = dataset.build_snapshot

        def guarded(ctx, *args, **kwargs):
            if ctx.object_id == "microfone":
                raise AssertionError("objeto sem video elegivel foi relido")
            return original(ctx, *args, **kwargs)

        with patch.object(
            multiclass,
            "_eligible_ids",
            side_effect=lambda ctx, _: ["shared-video-id"]
            if ctx.object_id == "boom"
            else [],
        ), patch.object(dataset, "build_snapshot", side_effect=guarded):
            snapshot = multiclass.build_multiclass_snapshot(
                self.contexts,
                [{"object_id": "boom", "video_id": "shared-video-id"}],
                flags={},
                include_empty=False,
                task="detection",
                workspace_root=self.root,
            )

        selected = {
            item["object_id"]: item["dataset_snapshot"]["segments"]
            for item in snapshot["objects"]
        }
        self.assertEqual(len(selected["boom"]), 1)
        self.assertEqual(selected["microfone"], [])
        self.assertTrue(snapshot["export_allowed"])

    def test_global_snapshot_identity_ignores_all_capture_timestamps(self) -> None:
        with patch.object(
            multiclass,
            "_eligible_ids",
            side_effect=lambda ctx, _: ["shared-video-id"],
        ), patch.object(
            dataset, "iso", side_effect=["child-a1", "child-a2", "child-b1", "child-b2"]
        ), patch(
            "server.videos.iso", side_effect=["global-a", "global-b"]
        ):
            first = multiclass.build_multiclass_snapshot(
                self.contexts,
                [],
                flags={},
                include_empty=False,
                task="detection",
                workspace_root=self.root,
            )
            second = multiclass.build_multiclass_snapshot(
                self.contexts,
                [],
                flags={},
                include_empty=False,
                task="detection",
                workspace_root=self.root,
            )

        self.assertNotEqual(first["created_at"], second["created_at"])
        self.assertNotEqual(
            first["objects"][0]["dataset_snapshot"]["created_at"],
            second["objects"][0]["dataset_snapshot"]["created_at"],
        )
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

    def test_global_export_uses_only_the_frozen_object_snapshots(self) -> None:
        snapshot = self._bound_snapshot()

        out = self.root / "_datasets" / "frozen-global"
        progress: list[tuple[int, int]] = []
        with patch.object(
            multiclass, "_eligible_ids", side_effect=AssertionError("estado vivo relido")
        ):
            result = multiclass.export_multiclass(
                [],
                [],
                flags={"ignored": ["live"]},
                include_empty=True,
                out_dir=out,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                workspace_root=self.root,
                on_progress=lambda current, total: progress.append((current, total)),
                snapshot=snapshot,
            )

        labels = sorted((out / "labels" / "train").glob("*.txt"))
        self.assertEqual(result["images"], 2)
        self.assertEqual({path.read_text().split()[0] for path in labels}, {"0", "1"})
        manifest = json.loads((out / "dataset_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(manifest["source_snapshot"]["objects"][0]["object_id"], "boom")
        artifacts = manifest["output_artifacts"]
        self.assertTrue(artifacts)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in artifacts))
        self.assertEqual(progress[-1], (2, 2))

    def test_published_global_images_do_not_change_with_the_source_frame(self) -> None:
        snapshot = self._bound_snapshot()
        target = self.root / "_datasets" / "immutable-global"
        multiclass.export_multiclass_snapshot_atomic(
            self.root,
            snapshot,
            out_dir=target,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
            owner="job-immutable",
        )
        published = next((target / "images" / "train").glob("boom__*.jpg"))
        expected = published.read_bytes()
        source = self.contexts[0].output_root / "video" / "seg_00" / "000000.jpg"

        source.write_bytes(b"source frame replaced after publication")

        self.assertEqual(published.read_bytes(), expected)

    def test_frozen_global_coco_segmentation_keeps_multiclass_run_identity(self) -> None:
        with patch.object(
            multiclass,
            "_eligible_ids",
            side_effect=lambda ctx, _: ["shared-video-id"],
        ):
            source = multiclass.build_multiclass_snapshot(
                self.contexts,
                [],
                flags={},
                include_empty=False,
                task="segmentation",
                workspace_root=self.root,
            )
        snapshot = multiclass.bind_multiclass_export_spec(
            source,
            fmt="coco",
            val_fraction=0,
            test_fraction=0,
        )
        target = self.root / "_datasets" / "global-coco-seg"

        result = multiclass.export_multiclass_snapshot_atomic(
            self.root,
            snapshot,
            out_dir=target,
            fmt="coco",
            task="segmentation",
            val_fraction=0,
            test_fraction=0,
            owner="job-coco",
        )

        document = json.loads(
            (target / "instances_train.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (target / "dataset_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result["images"], 2)
        self.assertEqual(
            [(item["object_id"], item["name"]) for item in document["categories"]],
            [("boom", "boom"), ("microfone", "microphone")],
        )
        self.assertTrue(
            all(isinstance(item.get("segmentation"), dict) for item in document["annotations"])
        )
        self.assertEqual(len(manifest["split"]["videos"]), 2)
        self.assertEqual(
            {item["run"]["model_id"] for item in manifest["segments"]},
            {"sam3-finetuned-v1"},
        )

    def test_atomic_global_export_retry_never_reuses_partial_staging(self) -> None:
        snapshot = self._bound_snapshot()
        exporter = getattr(multiclass, "export_multiclass_snapshot_atomic", None)
        self.assertTrue(
            callable(exporter), "export_multiclass_snapshot_atomic ausente"
        )
        target = self.root / "_datasets" / "stable-global"

        def fail_once(*args, out_dir: Path, **kwargs):
            (out_dir / "partial").mkdir(parents=True)
            (out_dir / "partial" / "junk.txt").write_text("junk", encoding="utf-8")
            raise RuntimeError("simulated global crash")

        with patch.object(multiclass, "export_multiclass", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "simulated global crash"):
                exporter(
                    self.root,
                    snapshot,
                    out_dir=target,
                    fmt="yolo",
                    task="detection",
                    val_fraction=0,
                    test_fraction=0,
                    owner="job-1",
                )
        self.assertFalse(target.exists())

        result = exporter(
            self.root,
            snapshot,
            out_dir=target,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
            owner="job-1",
        )
        self.assertEqual(result["snapshot_id"], snapshot["snapshot_id"])
        self.assertFalse((target / "partial").exists())

        with patch.object(
            multiclass,
            "export_multiclass",
            side_effect=AssertionError("retry regenerou"),
        ):
            replay = exporter(
                self.root,
                snapshot,
                out_dir=target,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                owner="job-1",
            )
        self.assertTrue(replay["replayed"])

    def test_new_lease_removes_only_staging_from_the_same_job(self) -> None:
        snapshot = self._bound_snapshot()
        target = self.root / "_datasets" / "lease-global"
        staging_root = self.root / "_datasets" / ".staging"
        staging_root.mkdir(parents=True)
        name_key = hashlib.sha256(target.name.encode("utf-8")).hexdigest()[:16]
        snapshot_key = snapshot["snapshot_id"][:16]

        def stage_for(owner: str) -> Path:
            job_id = owner.split(":", 1)[0]
            job_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
            owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
            return staging_root / f"{name_key}-{snapshot_key}-{job_key}-{owner_key}"

        abandoned = stage_for("job-1:expired-lease")
        unrelated = stage_for("job-2:valid-lease")
        abandoned.mkdir()
        unrelated.mkdir()
        (abandoned / "partial.txt").write_text("partial", encoding="utf-8")
        (unrelated / "partial.txt").write_text("active", encoding="utf-8")

        def inspect_cleanup(*args, **kwargs):
            self.assertFalse(abandoned.exists())
            self.assertTrue(unrelated.exists())
            raise RuntimeError("stop after observing cleanup")

        with patch.object(multiclass, "export_multiclass", side_effect=inspect_cleanup):
            with self.assertRaisesRegex(RuntimeError, "stop after observing cleanup"):
                multiclass.export_multiclass_snapshot_atomic(
                    self.root,
                    snapshot,
                    out_dir=target,
                    fmt="yolo",
                    task="detection",
                    val_fraction=0,
                    test_fraction=0,
                    owner="job-1:new-lease",
                )

        self.assertTrue(unrelated.exists())

    def test_atomic_global_replay_accepts_new_observational_timestamps(self) -> None:
        snapshot = self._bound_snapshot()
        target = self.root / "_datasets" / "timestamp-global"
        multiclass.export_multiclass_snapshot_atomic(
            self.root,
            snapshot,
            out_dir=target,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
            owner="job-1",
        )
        replay_snapshot = deepcopy(snapshot)
        replay_snapshot["created_at"] = "later-global-capture"
        for item in replay_snapshot["objects"]:
            item["dataset_snapshot"]["created_at"] = (
                f"later-{item['object_id']}-capture"
            )
        self.assertEqual(replay_snapshot["snapshot_id"], snapshot["snapshot_id"])

        with patch.object(
            multiclass,
            "export_multiclass",
            side_effect=AssertionError("replay regenerou"),
        ):
            replay = multiclass.export_multiclass_snapshot_atomic(
                self.root,
                replay_snapshot,
                out_dir=target,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                owner="job-2",
            )

        self.assertTrue(replay["replayed"])

    def test_atomic_global_retry_rejects_corrupt_published_artifact(self) -> None:
        snapshot = self._bound_snapshot()
        target = self.root / "_datasets" / "corrupt-global"
        multiclass.export_multiclass_snapshot_atomic(
            self.root,
            snapshot,
            out_dir=target,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
            owner="job-1",
        )
        label = next((target / "labels" / "train").glob("*.txt"))
        label.write_text("corrupt", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "artefato publicado.*divergente"):
            multiclass.export_multiclass_snapshot_atomic(
                self.root,
                snapshot,
                out_dir=target,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                owner="job-1",
            )

    def test_atomic_global_retry_rejects_a_tampered_source_manifest(self) -> None:
        snapshot = self._bound_snapshot()
        target = self.root / "_datasets" / "tampered-global"
        multiclass.export_multiclass_snapshot_atomic(
            self.root,
            snapshot,
            out_dir=target,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
            owner="job-1",
        )
        manifest_path = target / "dataset_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_snapshot"]["classes"][0]["name"] = "tampered"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "snapshot fonte.*divergente"):
            multiclass.export_multiclass_snapshot_atomic(
                self.root,
                snapshot,
                out_dir=target,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                owner="job-1",
            )

    def test_global_publish_guard_fences_stale_worker_before_rename(self) -> None:
        snapshot = self._bound_snapshot()
        target = self.root / "_datasets" / "fenced-global"

        with self.assertRaisesRegex(RuntimeError, "lease perdido"):
            multiclass.export_multiclass_snapshot_atomic(
                self.root,
                snapshot,
                out_dir=target,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                owner="stale-job",
                before_publish=lambda: (_ for _ in ()).throw(
                    RuntimeError("lease perdido")
                ),
            )

        self.assertFalse(target.exists())

    def test_global_post_enqueues_bound_snapshot_with_target_idempotency(self) -> None:
        snapshot = self._bound_snapshot()
        request = dataset_router.GlobalExportIn(
            object_ids=["microfone", "boom"],
            name="nightly-global",
            format="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
        )

        with patch.object(dataset_router.workspace, "root", self.root), patch.object(
            dataset_router, "_global_contexts", return_value=self.contexts
        ) as contexts, patch.object(
            multiclass, "build_multiclass_snapshot", return_value=snapshot
        ) as build, patch.object(
            multiclass, "bind_multiclass_export_spec", return_value=snapshot
        ), patch.object(
            dataset_router.durable_jobs, "enabled", return_value=True
        ), patch.object(
            dataset_router.durable_jobs, "create", return_value="job-global-1"
        ) as create:
            result = asyncio.run(
                dataset_router.global_export(
                    request,
                    user=SimpleNamespace(user_id="gui"),
                    client_id="client-1",
                )
            )

        self.assertEqual(result["job_id"], "job-global-1")
        contexts.assert_called_once_with(["microfone", "boom"])
        build.assert_called_once()
        with self.assertRaises(dataset.DatasetTargetConflict):
            dataset.reserve_dataset_target(
                self.root / "_datasets",
                "nightly-global",
                "b" * 64,
            )
        self.assertEqual(
            result["out_dir"],
            (self.root / "_datasets" / "nightly-global").as_posix(),
        )
        self.assertEqual(create.call_args.kwargs["payload"]["snapshot"], snapshot)
        self.assertEqual(
            create.call_args.kwargs["idempotency_key"],
            "dataset-export-global:nightly-global",
        )

    def test_global_post_releases_a_new_reservation_when_job_creation_fails(self) -> None:
        snapshot = self._bound_snapshot()
        request = dataset_router.GlobalExportIn(
            object_ids=["boom"],
            name="failed-global",
            format="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
        )

        with patch.object(dataset_router.workspace, "root", self.root), patch.object(
            dataset_router, "_global_contexts", return_value=self.contexts[:1]
        ), patch.object(
            multiclass, "build_multiclass_snapshot", return_value=snapshot
        ), patch.object(
            multiclass, "bind_multiclass_export_spec", return_value=snapshot
        ), patch.object(
            dataset_router.durable_jobs, "enabled", return_value=True
        ), patch.object(
            dataset_router.durable_jobs,
            "create",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                asyncio.run(
                    dataset_router.global_export(
                        request,
                        user=SimpleNamespace(user_id="gui"),
                        client_id="client-1",
                    )
                )

        reserved = dataset.reserve_dataset_target(
            self.root / "_datasets",
            "failed-global",
            "b" * 64,
        )
        self.assertEqual(reserved, self.root / "_datasets" / "failed-global")

    def test_snapshot_reads_prompt_override_without_migrating_legacy_state(self) -> None:
        segment = self.contexts[0].output_root / "video" / "seg_00"
        legacy_override = segment / "generation-prompt-override.json"
        override_payload = json.dumps(
            {"objects": [{"obj_id": 1, "label": "boom", "normalized": [0, 0, 1, 1]}]}
        ).encode("utf-8")
        legacy_override.write_bytes(override_payload)
        paths = SimpleNamespace(
            segment_dir=segment,
            control_dir=segment / "_sam3-control-without-override",
        )

        with patch.object(
            sam3_runs,
            "effective_prompt_override_path",
            return_value=legacy_override,
        ) as resolve:
            _, override_sha, effective_sha = dataset._effective_prompt_identity(paths)

        prompt_payload = (segment / "prompt.json").read_bytes()
        resolve.assert_called_once_with(segment, migrate_legacy=False)
        self.assertEqual(override_sha, hashlib.sha256(override_payload).hexdigest())
        self.assertEqual(
            effective_sha,
            hashlib.sha256(prompt_payload + override_payload).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
