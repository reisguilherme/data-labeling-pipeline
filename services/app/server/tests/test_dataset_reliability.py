from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pipeline_core.masks import encode_binary_png
from server import dataset
from server.routers import dataset as dataset_router


def _mask(size: tuple[int, int], x: int, y: int) -> bytes:
    image = Image.new("1", size, 0)
    image.putpixel((x, y), 1)
    return encode_binary_png(image)


class DatasetSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "sam3_classes.json").write_text(
            json.dumps({"names": ["boom"]}), encoding="utf-8"
        )
        self.output = self.root / "output"
        self.segment = self.output / "video-a" / "seg_000"
        self.segment.mkdir(parents=True)
        Image.new("RGB", (4, 4), "white").save(self.segment / "000000.jpg")
        prompt_text = json.dumps(
            {
                "schema_version": 1,
                "frame_count": 1,
                "image_width": 4,
                "image_height": 4,
                "prompt_frame_idx": 0,
                "objects": [
                    {"obj_id": 1, "label": "boom", "normalized": [0, 0, 1, 1]}
                ],
            }
        )
        (self.segment / "prompt.json").write_text(prompt_text, encoding="utf-8")
        self.prompt_digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        raw = _mask((4, 4), 1, 1)
        raw_path = self.segment / "_sam3" / "masks" / "1" / "000000.png"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_bytes(raw)
        (self.segment / "_sam3" / "labels").mkdir()
        (self.segment / "_sam3" / "labels" / "000000.txt").write_text(
            "0 0.375 0.375 0.25 0.25\n", encoding="utf-8"
        )
        edited = _mask((4, 4), 2, 2)
        self.edited_path = (
            self.segment / "_sam3" / "reviews" / "000000" / "rev_000003" / "1.png"
        )
        self.edited_path.parent.mkdir(parents=True)
        self.edited_path.write_bytes(edited)
        self.run = {
            "schema_version": 1,
            "status": "done",
            "prompt_digest": self.prompt_digest,
            "frame_count": 1,
            "frames_written": 1,
            "frames_with_objects": 1,
            "objects": [{"obj_id": 1, "label": "boom"}],
            "params": {"mask_threshold": 0.5},
            "model": {
                "model_id": "sam3-finetuned-v1",
                "checkpoint_sha256": "b" * 64,
                "sam3_commit": "c" * 40,
            },
            "artifacts": {
                "format": "png-1bit-v1",
                "files": 1,
                "checksums": {
                    "masks/1/000000.png": hashlib.sha256(raw).hexdigest()
                },
            },
        }
        (self.segment / "_sam3" / "run.json").write_text(
            json.dumps(self.run), encoding="utf-8"
        )
        self.review = {
            "schema_version": 1,
            "frames": {
                "0": {
                    "revision": 3,
                    "status": "edited",
                    "instances": [
                        {
                            "obj_id": 1,
                            "label": "boom",
                            "path": "reviews/000000/rev_000003/1.png",
                            "sha256": hashlib.sha256(edited).hexdigest(),
                            "area_pixels": 1,
                            "bbox_normalized": [0.5, 0.5, 0.75, 0.75],
                        }
                    ],
                }
            },
        }
        self.review_path = self.segment / "_sam3" / "mask_review.json"
        self.review_path.write_text(json.dumps(self.review), encoding="utf-8")
        entry = {
            "status": "done",
            "video_id": "video-id-1",
            "annotation_revision": 7,
            "export": {
                "root": str(self.output / "video-a"),
                "segments": ["seg_000"],
            },
            "intervals": [{"segment": "seg_000", "frame_count": 1, "flags": {}}],
            "media": {"width": 4, "height": 4},
        }
        self.ctx = SimpleNamespace(
            object_id="boom",
            label="boom",
            output_root=self.output,
            store=SimpleNamespace(doc={"videos": {"bucket/video.mp4": entry}}),
        )

    def _snapshot(self) -> dict:
        builder = getattr(dataset, "build_snapshot", None)
        self.assertTrue(callable(builder), "build_snapshot ausente")
        return builder(
            self.ctx,
            dataset.Filters(reviewed_only=True),
            self.workspace,
            task="detection",
        )

    def test_collect_rejects_an_export_root_outside_the_object_output(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        entry = self.ctx.store.doc["videos"]["bucket/video.mp4"]
        entry["export"]["root"] = str(outside)

        with self.assertRaisesRegex(ValueError, "fora da raiz"):
            dataset.collect(self.ctx, dataset.Filters(), self.workspace)

    def test_snapshot_freezes_review_and_run_identity_without_decoding_pngs(self) -> None:
        with patch.object(dataset, "_mask_state", side_effect=AssertionError("PNG lido no POST")):
            snapshot = self._snapshot()

        segment = snapshot["segments"][0]
        frame = segment["frames"][0]
        self.assertEqual(segment["annotation_revision"], 7)
        self.assertEqual(segment["run"]["model_id"], "sam3-finetuned-v1")
        self.assertEqual(segment["run"]["checkpoint_sha256"], "b" * 64)
        self.assertEqual(frame["review_revision"], 3)
        self.assertEqual(
            frame["instances"][0]["path"],
            "reviews/000000/rev_000003/1.png",
        )
        self.assertRegex(snapshot["snapshot_id"], r"^[0-9a-f]{64}$")

    def test_snapshot_keeps_the_exact_sam3_generation_after_current_pointer_moves(self) -> None:
        video_root = self.segment.parent
        control_dir = video_root / "_sam3"
        first_generation = control_dir / "runs" / "generation-a" / self.segment.name
        shutil.copytree(self.segment / "_sam3", first_generation)
        current = control_dir / "current.json"
        current.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "generation-a",
                    "segments": {
                        self.segment.name: {
                            "path": f"runs/generation-a/{self.segment.name}"
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        snapshot = self._snapshot()
        self.assertEqual(
            snapshot["segments"][0]["annotation_path"],
            "video-a/_sam3/runs/generation-a/seg_000",
        )

        second_generation = control_dir / "runs" / "generation-b" / self.segment.name
        shutil.copytree(first_generation, second_generation)
        replacement = _mask((4, 4), 3, 3)
        replacement_path = (
            second_generation / "reviews" / "000000" / "rev_000003" / "1.png"
        )
        replacement_path.write_bytes(replacement)
        second_review_path = second_generation / "mask_review.json"
        second_review = json.loads(second_review_path.read_text(encoding="utf-8"))
        second_review["frames"]["0"]["instances"][0].update(
            {
                "sha256": hashlib.sha256(replacement).hexdigest(),
                "bbox_normalized": [0.75, 0.75, 1, 1],
            }
        )
        second_review_path.write_text(json.dumps(second_review), encoding="utf-8")
        current.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "generation-b",
                    "segments": {
                        self.segment.name: {
                            "path": f"runs/generation-b/{self.segment.name}"
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        out = self.root / "generation-frozen-dataset"
        dataset.export(
            self.ctx,
            dataset.Filters(),
            out_dir=out,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            workspace_root=self.workspace,
            snapshot=snapshot,
        )

        label = next((out / "labels" / "train").glob("*.txt")).read_text().strip()
        self.assertEqual(label, "0 0.625000 0.625000 0.250000 0.250000")

    def test_same_source_state_has_stable_snapshot_identity(self) -> None:
        with patch.object(dataset, "iso", side_effect=["first", "second"]):
            first = self._snapshot()
            second = self._snapshot()

        self.assertNotEqual(first["created_at"], second["created_at"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

    def test_preview_does_not_decode_mask_payloads(self) -> None:
        with patch.object(
            dataset, "inspect_binary_png", side_effect=AssertionError("PNG lido")
        ), patch.object(dataset, "_mask_state", side_effect=AssertionError("PNG lido")):
            result = dataset.preview(
                self.ctx,
                dataset.Filters(reviewed_only=True),
                self.workspace,
                task="detection",
            )

        self.assertEqual(result["frames"], 1)
        self.assertTrue(result["export_allowed"])

    def test_preview_blocks_a_missing_declared_mask_using_only_metadata(self) -> None:
        self.edited_path.unlink()

        with patch.object(dataset, "_mask_state", side_effect=AssertionError("PNG lido")):
            result = dataset.preview(
                self.ctx,
                dataset.Filters(reviewed_only=True),
                self.workspace,
                task="segmentation",
            )

        self.assertFalse(result["export_allowed"])
        self.assertIn("mascara revisada ausente", result["blocking_reasons"][0])

    def test_snapshot_blocks_masks_generated_from_an_obsolete_prompt(self) -> None:
        prompt = json.loads((self.segment / "prompt.json").read_text(encoding="utf-8"))
        prompt["objects"][0]["normalized"] = [0.25, 0.25, 1, 1]
        (self.segment / "prompt.json").write_text(json.dumps(prompt), encoding="utf-8")

        snapshot = self._snapshot()

        self.assertFalse(snapshot["export_allowed"])
        self.assertTrue(
            any("digest do prompt divergente" in item for item in snapshot["blocking_reasons"])
        )

    def test_snapshot_blocks_a_structurally_inconsistent_run_manifest(self) -> None:
        run_path = self.segment / "_sam3" / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["artifacts"]["files"] = 2
        run["artifacts"]["checksums"]["masks/99/000000.png"] = "f" * 64
        run_path.write_text(json.dumps(run), encoding="utf-8")

        snapshot = self._snapshot()

        self.assertFalse(snapshot["export_allowed"])
        self.assertTrue(
            any("manifesto de artefatos divergente" in item for item in snapshot["blocking_reasons"])
        )

    def test_export_uses_frozen_revision_when_live_review_changes(self) -> None:
        snapshot = self._snapshot()
        replacement = _mask((4, 4), 3, 3)
        replacement_path = (
            self.segment / "_sam3" / "reviews" / "000000" / "rev_000004" / "1.png"
        )
        replacement_path.parent.mkdir(parents=True)
        replacement_path.write_bytes(replacement)
        self.review["frames"]["0"] = {
            "revision": 4,
            "status": "edited",
            "instances": [
                {
                    "obj_id": 1,
                    "label": "boom",
                    "path": "reviews/000000/rev_000004/1.png",
                    "sha256": hashlib.sha256(replacement).hexdigest(),
                    "area_pixels": 1,
                    "bbox_normalized": [0.75, 0.75, 1, 1],
                }
            ],
        }
        self.review_path.write_text(json.dumps(self.review), encoding="utf-8")

        out = self.root / "dataset"
        dataset.export(
            self.ctx,
            dataset.Filters(),
            out_dir=out,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            workspace_root=self.workspace,
            snapshot=snapshot,
        )

        label = next((out / "labels" / "train").glob("*.txt")).read_text().strip()
        self.assertEqual(label, "0 0.625000 0.625000 0.250000 0.250000")
        manifest = json.loads((out / "dataset_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(
            manifest["source_snapshot"]["segments"][0]["frames"][0]["review_revision"],
            3,
        )
        artifacts = {item["path"]: item for item in manifest["output_artifacts"]}
        label_relative = next(path for path in artifacts if path.startswith("labels/"))
        label_path = out / label_relative
        self.assertEqual(
            artifacts[label_relative]["sha256"],
            hashlib.sha256(label_path.read_bytes()).hexdigest(),
        )

    def test_export_rejects_selected_mask_changed_after_snapshot(self) -> None:
        snapshot = self._snapshot()
        self.edited_path.write_bytes(_mask((4, 4), 0, 0))

        with self.assertRaisesRegex(ValueError, "checksum.*divergente"):
            dataset.export(
                self.ctx,
                dataset.Filters(),
                out_dir=self.root / "bad-dataset",
                fmt="yolo",
                task="detection",
                val_fraction=0,
                workspace_root=self.workspace,
                snapshot=snapshot,
            )

    def test_segmentation_revalidates_the_exact_bytes_it_polygonizes(self) -> None:
        snapshot = dataset.build_snapshot(
            self.ctx,
            dataset.Filters(reviewed_only=True),
            self.workspace,
            task="segmentation",
        )
        original_boxes = dataset._mask_boxes

        def mutate_after_first_validation(state):
            boxes = original_boxes(state)
            self.edited_path.write_bytes(_mask((4, 4), 0, 0))
            return boxes

        with patch.object(dataset, "_mask_boxes", side_effect=mutate_after_first_validation):
            with self.assertRaisesRegex(ValueError, "checksum de mascara divergente"):
                dataset.export(
                    self.ctx,
                    dataset.Filters(),
                    out_dir=self.root / "segmentation-race",
                    fmt="yolo",
                    task="segmentation",
                    val_fraction=0,
                    workspace_root=self.workspace,
                    snapshot=snapshot,
                )

    def test_export_rechecks_source_frame_after_generation(self) -> None:
        snapshot = self._snapshot()
        original_materialize = dataset._materialize_image

        def mutate_after_materialize(
            source: Path, destination: Path, *, immutable: bool
        ) -> None:
            original_materialize(source, destination, immutable=immutable)
            stat = source.stat()
            source.touch()
            if source.stat().st_mtime_ns == stat.st_mtime_ns:
                source.write_bytes(source.read_bytes() + b"changed")

        with patch.object(
            dataset, "_materialize_image", side_effect=mutate_after_materialize
        ):
            with self.assertRaisesRegex(ValueError, "frame selecionado mudou"):
                dataset.export(
                    self.ctx,
                    dataset.Filters(),
                    out_dir=self.root / "changed-frame",
                    fmt="yolo",
                    task="detection",
                    val_fraction=0,
                    workspace_root=self.workspace,
                    snapshot=snapshot,
                )

    def test_atomic_export_retry_never_exposes_or_reuses_partial_staging(self) -> None:
        snapshot = self._snapshot()
        exporter = getattr(dataset, "export_snapshot_atomic", None)
        self.assertTrue(callable(exporter), "export_snapshot_atomic ausente")
        target = self.output / "_datasets" / "stable"

        def fail_once(*args, out_dir: Path, **kwargs):
            (out_dir / "partial").mkdir(parents=True)
            (out_dir / "partial" / "junk.txt").write_text("junk", encoding="utf-8")
            raise RuntimeError("simulated crash")

        with patch.object(dataset, "export", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                exporter(
                    self.ctx,
                    snapshot,
                    out_dir=target,
                    fmt="yolo",
                    task="detection",
                    val_fraction=0,
                    test_fraction=0,
                    workspace_root=self.workspace,
                    owner="job-1",
                )
        self.assertFalse(target.exists())

        result = exporter(
            self.ctx,
            snapshot,
            out_dir=target,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
            workspace_root=self.workspace,
            owner="job-1",
        )
        self.assertTrue((target / "dataset_manifest.json").is_file())
        self.assertFalse((target / "partial").exists())
        self.assertEqual(result["snapshot_id"], snapshot["snapshot_id"])

        with patch.object(dataset, "export", side_effect=AssertionError("retry regenerou")):
            replay = exporter(
                self.ctx,
                snapshot,
                out_dir=target,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                workspace_root=self.workspace,
                owner="job-1",
            )
        self.assertEqual(replay["snapshot_id"], snapshot["snapshot_id"])

    def test_atomic_retry_rejects_corrupt_published_artifact(self) -> None:
        snapshot = self._snapshot()
        target = self.output / "_datasets" / "corrupt"
        dataset.export_snapshot_atomic(
            self.ctx,
            snapshot,
            out_dir=target,
            fmt="yolo",
            task="detection",
            val_fraction=0,
            test_fraction=0,
            workspace_root=self.workspace,
            owner="job-1",
        )
        label = next((target / "labels" / "train").glob("*.txt"))
        label.write_text("corrupt", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "artefato publicado.*divergente"):
            dataset.export_snapshot_atomic(
                self.ctx,
                snapshot,
                out_dir=target,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                workspace_root=self.workspace,
                owner="job-1",
            )

    def test_publish_guard_can_fence_a_stale_worker_before_atomic_rename(self) -> None:
        snapshot = self._snapshot()
        exporter = getattr(dataset, "export_snapshot_atomic", None)
        self.assertTrue(callable(exporter), "export_snapshot_atomic ausente")
        target = self.output / "_datasets" / "fenced"

        with self.assertRaisesRegex(RuntimeError, "lease perdido"):
            exporter(
                self.ctx,
                snapshot,
                out_dir=target,
                fmt="yolo",
                task="detection",
                val_fraction=0,
                test_fraction=0,
                workspace_root=self.workspace,
                owner="stale-job",
                before_publish=lambda: (_ for _ in ()).throw(RuntimeError("lease perdido")),
            )

        self.assertFalse(target.exists())


class DatasetTargetReservationTests(unittest.TestCase):
    def test_same_snapshot_reuses_reservation_but_other_snapshot_is_rejected(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "_datasets"

        first = dataset.reserve_dataset_target(root, "nightly", "a" * 64)
        replay = dataset.reserve_dataset_target(root, "nightly", "a" * 64)

        self.assertEqual(first, replay)
        with self.assertRaises(dataset.DatasetTargetConflict):
            dataset.reserve_dataset_target(root, "nightly", "b" * 64)

    def test_concurrent_identical_reservations_never_observe_a_partial_record(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "_datasets"
        writer_opened = threading.Event()
        release_writer = threading.Event()
        real_fdopen = dataset.os.fdopen

        class DelayedFile:
            def __init__(self, wrapped) -> None:
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def write(self, payload):
                writer_opened.set()
                release_writer.wait(timeout=2)
                return self.wrapped.write(payload)

            def flush(self):
                return self.wrapped.flush()

            def fileno(self):
                return self.wrapped.fileno()

            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)

        calls = 0

        def delayed_fdopen(*args, **kwargs):
            nonlocal calls
            calls += 1
            opened = real_fdopen(*args, **kwargs)
            return DelayedFile(opened) if calls == 1 else opened

        errors: list[Exception] = []

        def reserve() -> None:
            try:
                dataset.reserve_dataset_target(root, "nightly", "a" * 64)
            except Exception as exc:  # noqa: BLE001 - captured across the test thread
                errors.append(exc)

        with patch.object(dataset.os, "fdopen", side_effect=delayed_fdopen):
            first = threading.Thread(target=reserve)
            first.start()
            self.assertTrue(writer_opened.wait(timeout=2))
            second = threading.Thread(target=reserve)
            second.start()
            second.join(timeout=0.2)
            release_writer.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])

    def test_export_configuration_is_part_of_the_reserved_snapshot_identity(self) -> None:
        base = {
            "schema_version": 1,
            "created_at": "2026-09-09T10:00:00+00:00",
            "object_id": "boom",
            "task": "detection",
            "classes": ["boom"],
            "filters": {},
            "segments": [],
            "export_allowed": True,
            "blocking_reasons": [],
        }
        base["snapshot_id"] = dataset._snapshot_digest(base)
        binder = getattr(dataset, "bind_export_spec", None)
        self.assertTrue(callable(binder), "bind_export_spec ausente")

        detection = binder(base, fmt="yolo", val_fraction=0.2, test_fraction=0)
        another_split = binder(base, fmt="yolo", val_fraction=0.3, test_fraction=0)

        self.assertNotEqual(detection["snapshot_id"], another_split["snapshot_id"])
        self.assertEqual(detection["export_spec"]["val_fraction"], 0.2)

    def test_deleting_a_dataset_releases_its_name_for_a_new_snapshot(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / "_datasets"
        dataset.reserve_dataset_target(root, "nightly", "a" * 64)
        release = getattr(dataset, "release_dataset_target", None)
        self.assertTrue(callable(release), "release_dataset_target ausente")

        release(root, "nightly")
        reserved = dataset.reserve_dataset_target(root, "nightly", "b" * 64)

        self.assertEqual(reserved, root / "nightly")

    def test_reserved_target_cannot_be_a_symlink_outside_the_dataset_root(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root = base / "_datasets"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        (outside / "dataset_manifest.json").write_text(
            json.dumps({"snapshot_id": "a" * 64, "output_artifacts": []}),
            encoding="utf-8",
        )
        try:
            (root / "nightly").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")

        with self.assertRaisesRegex(ValueError, "link simbolico"):
            dataset.reserve_dataset_target(root, "nightly", "a" * 64)

    def test_atomic_export_rejects_a_symlinked_staging_root(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        output = base / "output"
        datasets = output / "_datasets"
        datasets.mkdir(parents=True)
        outside = base / "outside"
        outside.mkdir()
        try:
            (datasets / ".staging").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")
        snapshot = {
            "schema_version": 1,
            "created_at": "now",
            "object_id": "boom",
            "task": "detection",
            "classes": ["boom"],
            "filters": {},
            "segments": [],
            "export_allowed": True,
            "blocking_reasons": [],
        }
        snapshot["snapshot_id"] = dataset._snapshot_digest(snapshot)
        ctx = SimpleNamespace(object_id="boom", output_root=output, label="boom")

        with self.assertRaisesRegex(ValueError, "staging.*link simbolico"):
            dataset.export_snapshot_atomic(
                ctx,
                snapshot,
                out_dir=datasets / "nightly",
                fmt="yolo",
                task="detection",
                val_fraction=0.2,
                test_fraction=0,
                workspace_root=None,
                owner="job-1",
            )
        self.assertEqual(list(outside.iterdir()), [])


class DatasetExportRouteTests(unittest.TestCase):
    def test_retrying_a_published_snapshot_reuses_the_idempotent_job(self) -> None:
        root = Path(tempfile.mkdtemp())
        published = root / "_datasets" / "nightly"
        published.mkdir(parents=True)
        snapshot = {
            "schema_version": 1,
            "snapshot_id": "d" * 64,
            "created_at": "2026-09-09T10:00:00+00:00",
            "object_id": "boom",
            "classes": ["boom"],
            "filters": {},
            "segments": [{"video_id": "video-id-1", "segment": "seg_000"}],
            "export_allowed": True,
            "blocking_reasons": [],
        }
        (published / "dataset_manifest.json").write_text(
            json.dumps({"snapshot_id": snapshot["snapshot_id"]}), encoding="utf-8"
        )
        ctx = SimpleNamespace(object_id="boom", output_root=root, label="boom")
        request = dataset_router.ExportIn(name="nightly", format="yolo", task="detection")

        with patch.object(
            dataset_router.dataset_module, "build_snapshot", return_value=snapshot
        ), patch.object(
            dataset_router.dataset_module, "bind_export_spec", return_value=snapshot
        ), patch.object(
            dataset_router.dataset_module, "reserve_dataset_target"
        ) as reserve, patch.object(
            dataset_router.durable_jobs, "enabled", return_value=True
        ), patch.object(
            dataset_router.durable_jobs, "create", return_value="job-1"
        ):
            result = asyncio.run(
                dataset_router.export(
                    request,
                    ctx=ctx,
                    user=SimpleNamespace(user_id="gui"),
                    client_id="client-1",
                )
            )

        self.assertEqual(result["job_id"], "job-1")
        reserve.assert_called_once()

    def test_post_enqueues_the_frozen_snapshot_with_target_idempotency(self) -> None:
        root = Path(tempfile.mkdtemp())
        ctx = SimpleNamespace(object_id="boom", output_root=root, label="boom")
        snapshot = {
            "schema_version": 1,
            "snapshot_id": "d" * 64,
            "created_at": "2026-09-09T10:00:00+00:00",
            "object_id": "boom",
            "classes": ["boom"],
            "filters": {},
            "segments": [{"video_id": "video-id-1", "segment": "seg_000"}],
            "export_allowed": True,
            "blocking_reasons": [],
        }
        request = dataset_router.ExportIn(name="nightly", format="yolo", task="detection")

        with patch.object(dataset_router.dataset_module, "build_snapshot", return_value=snapshot), patch.object(
            dataset_router.dataset_module, "bind_export_spec", return_value=snapshot
        ), patch.object(
            dataset_router.dataset_module, "reserve_dataset_target"
        ), patch.object(dataset_router.durable_jobs, "enabled", return_value=True), patch.object(
            dataset_router.durable_jobs, "create", return_value="job-1"
        ) as create:
            result = asyncio.run(
                dataset_router.export(
                    request,
                    ctx=ctx,
                    user=SimpleNamespace(user_id="gui"),
                    client_id="client-1",
                )
            )

        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(create.call_args.kwargs["payload"]["snapshot"], snapshot)
        self.assertEqual(
            create.call_args.kwargs["idempotency_key"],
            "dataset-export:boom:nightly",
        )


if __name__ == "__main__":
    unittest.main()
