from __future__ import annotations

import json
import hashlib
import copy
import shutil
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline_core.sam3_runs import Sam3GenerationError
from pipeline_core.masks import encode_binary_png
from pipeline_core.review_store import FileMaskReviewStore
from PIL import Image
from server.review import SegmentPaths, prompt_contract
from server.routers import sam3 as sam3_router
from server.routers import sam3_session as sam3_session_router
from server.sam3 import Sam3Queue


class Sam3GenerationResolutionTests(unittest.TestCase):
    def test_segment_paths_follow_one_video_level_generation_pointer(self) -> None:
        export_root = Path(tempfile.mkdtemp()) / "video"
        segment = export_root / "seg_00"
        published = export_root / "_sam3" / "runs" / "run-123" / "seg_00"
        segment.mkdir(parents=True)
        published.mkdir(parents=True)
        (export_root / "_sam3" / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "run-123",
                    "annotation_revision": 7,
                    "segments": {"seg_00": {"path": "runs/run-123/seg_00"}},
                }
            ),
            encoding="utf-8",
        )

        paths = SegmentPaths(segment)

        self.assertEqual(paths.out_dir, published)
        self.assertEqual(getattr(paths, "control_dir", None), segment / "_sam3")

    def test_legacy_segment_without_pointer_keeps_existing_layout(self) -> None:
        segment = Path(tempfile.mkdtemp()) / "video" / "seg_00"
        segment.mkdir(parents=True)

        paths = SegmentPaths(segment)

        self.assertEqual(paths.out_dir, segment / "_sam3")
        self.assertEqual(getattr(paths, "control_dir", None), segment / "_sam3")

    def test_new_segment_absent_from_previous_generation_has_no_legacy_override(self) -> None:
        from pipeline_core.sam3_runs import effective_prompt_override_path

        export_root = Path(tempfile.mkdtemp()) / "video"
        old_segment = export_root / "seg_00"
        new_segment = export_root / "seg_01"
        old_segment.mkdir(parents=True)
        new_segment.mkdir()
        (export_root / "_sam3" / "current.json").parent.mkdir()
        (export_root / "_sam3" / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "run-old",
                    "annotation_revision": 6,
                    "segments": {
                        "seg_00": {"path": "runs/run-old/seg_00"}
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertIsNone(
            effective_prompt_override_path(new_segment, migrate_legacy=True)
        )

    def test_generation_reader_rejects_a_symlinked_published_segment(self) -> None:
        export_root = Path(tempfile.mkdtemp()) / "video"
        segment = export_root / "seg_00"
        outside = export_root.parent / "outside"
        published = export_root / "_sam3" / "runs" / "run-123" / "seg_00"
        segment.mkdir(parents=True)
        outside.mkdir()
        published.parent.mkdir(parents=True)
        try:
            published.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")
        (export_root / "_sam3" / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "run-123",
                    "annotation_revision": 7,
                    "segments": {"seg_00": {"path": "runs/run-123/seg_00"}},
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(Sam3GenerationError):
            _ = SegmentPaths(segment).out_dir

    def test_prompt_override_remains_in_control_dir_after_generation_publish(self) -> None:
        export_root = Path(tempfile.mkdtemp()) / "video"
        segment = export_root / "seg_00"
        published = export_root / "_sam3" / "runs" / "run-123" / "seg_00"
        control = segment / "_sam3"
        published.mkdir(parents=True)
        control.mkdir(parents=True)
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "image_width": 10,
                    "image_height": 10,
                    "prompt_frame_idx": 0,
                    "objects": [
                        {"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 0.5, 0.5]}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (control / "prompt_override.json").write_text(
            json.dumps(
                {
                    "objects": [
                        {"obj_id": 1, "label": "boom", "box_normalized": [0.5, 0.5, 1, 1]}
                    ]
                }
            ),
            encoding="utf-8",
        )
        (export_root / "_sam3" / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "run-123",
                    "annotation_revision": 7,
                    "segments": {"seg_00": {"path": "runs/run-123/seg_00"}},
                }
            ),
            encoding="utf-8",
        )

        contract = prompt_contract(SegmentPaths(segment))

        self.assertEqual(contract["objects"][0]["normalized"], [0.5, 0.5, 1.0, 1.0])

    def test_active_generation_is_rejected_after_its_prompt_override_changes(self) -> None:
        export_root = Path(tempfile.mkdtemp()) / "video"
        segment = export_root / "seg_00"
        published = export_root / "_sam3" / "runs" / "run-123" / "seg_00"
        control = segment / "_sam3"
        published.mkdir(parents=True)
        control.mkdir(parents=True)
        prompt = json.dumps(
            {
                "image_width": 10,
                "image_height": 10,
                "objects": [
                    {
                        "obj_id": 1,
                        "label": "boom",
                        "box_normalized": [0, 0, 0.5, 0.5],
                    }
                ],
            }
        ).encode()
        (segment / "prompt.json").write_bytes(prompt)
        (export_root / "_sam3" / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "run-123",
                    "annotation_revision": 7,
                    "segments": {
                        "seg_00": {
                            "path": "runs/run-123/seg_00",
                            "prompt_digest": hashlib.sha256(prompt).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (control / "prompt_override.json").write_text(
            json.dumps(
                {
                    "objects": [
                        {
                            "obj_id": 1,
                            "label": "boom",
                            "box_normalized": [0.5, 0.5, 1, 1],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(Sam3GenerationError, "prompt"):
            _ = SegmentPaths(segment).out_dir

    def test_published_legacy_override_is_lazily_migrated_without_mutating_run(self) -> None:
        export_root = Path(tempfile.mkdtemp()) / "video"
        segment = export_root / "seg_00"
        published = export_root / "_sam3" / "runs" / "run-123" / "seg_00"
        published.mkdir(parents=True)
        segment.mkdir(parents=True)
        (segment / "prompt.json").write_text(
            json.dumps(
                {
                    "image_width": 10,
                    "image_height": 10,
                    "prompt_frame_idx": 0,
                    "objects": [
                        {"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 0.5, 0.5]}
                    ],
                }
            ),
            encoding="utf-8",
        )
        override = json.dumps(
            {
                "objects": [
                    {"obj_id": 1, "label": "boom", "box_normalized": [0.5, 0.5, 1, 1]}
                ]
            }
        ).encode()
        legacy_override = published / "prompt_override.json"
        legacy_override.write_bytes(override)
        expected_digest = hashlib.sha256(
            (segment / "prompt.json").read_bytes() + override
        ).hexdigest()
        (export_root / "_sam3" / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "run-123",
                    "annotation_revision": 7,
                    "segments": {
                        "seg_00": {
                            "path": "runs/run-123/seg_00",
                            "prompt_digest": expected_digest,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        contract = prompt_contract(SegmentPaths(segment))

        migrated = segment / "_sam3" / "prompt_override.json"
        self.assertEqual(contract["objects"][0]["normalized"], [0.5, 0.5, 1.0, 1.0])
        self.assertEqual(migrated.read_bytes(), override)
        self.assertEqual(legacy_override.read_bytes(), override)

    def test_stale_generation_override_is_not_migrated_after_prompt_changes(self) -> None:
        from pipeline_core.sam3_runs import effective_prompt_override_path

        export_root = Path(tempfile.mkdtemp()) / "video"
        segment = export_root / "seg_00"
        published = export_root / "_sam3" / "runs" / "run-123" / "seg_00"
        published.mkdir(parents=True)
        segment.mkdir(parents=True)
        old_prompt = json.dumps(
            {
                "schema_version": 1,
                "objects": [
                    {"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 0.5, 0.5]}
                ],
            }
        ).encode()
        new_prompt = json.dumps(
            {
                "schema_version": 1,
                "objects": [
                    {"obj_id": 2, "label": "carro", "box_normalized": [0.2, 0.2, 0.8, 0.8]}
                ],
            }
        ).encode()
        override = json.dumps(
            {
                "objects": [
                    {"obj_id": 1, "label": "boom", "box_normalized": [0.5, 0.5, 1, 1]}
                ]
            }
        ).encode()
        (segment / "prompt.json").write_bytes(new_prompt)
        (published / "prompt_override.json").write_bytes(override)
        (export_root / "_sam3" / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "run-123",
                    "annotation_revision": 7,
                    "segments": {
                        "seg_00": {
                            "path": "runs/run-123/seg_00",
                            "prompt_digest": hashlib.sha256(
                                old_prompt + override
                            ).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        resolved = effective_prompt_override_path(
            segment, migrate_legacy=True
        )

        self.assertIsNone(resolved)
        self.assertFalse((segment / "_sam3" / "prompt_override.json").exists())


class Sam3StagingContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_pins_the_selected_registry_model_on_enqueue(self) -> None:
        registry = Path(tempfile.mkdtemp()) / "models.yaml"
        registry.write_text(
            """schema_version: 1
default_model: base
models:
  - model_id: base
    source: {type: local, path: /models/base.pt}
    sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    sam3_commit: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
assignments:
  objects: {boom: base}
""",
            encoding="utf-8",
        )
        resolver = getattr(sam3_router, "_selected_model_identity", None)
        self.assertTrue(callable(resolver), "resolver de identidade do modelo ausente")
        with patch.dict("os.environ", {"SAM3_MODELS_CONFIG": str(registry)}):
            identity = resolver("boom")

        self.assertEqual(
            identity,
            {
                "model_id": "base",
                "model_sha256": "a" * 64,
                "sam3_commit": "b" * 40,
            },
        )

    async def test_next_job_assigns_a_lease_private_staging_directory(self) -> None:
        export_root = Path(tempfile.mkdtemp()) / "video"
        (export_root / "seg_00").mkdir(parents=True)
        queue = Sam3Queue()
        queue.bind("boom", export_root.parent)
        queued = queue.enqueue(
            "boom",
            video_id="video-1",
            relpath="clip.mp4",
            name="clip",
            export_root=str(export_root),
            segments=["seg_00"],
            user="ana",
            annotation_revision=7,
            model_id="model-1",
            model_sha256="a" * 64,
            sam3_commit="b" * 40,
        )

        with patch.object(sam3_router, "queue", queue), patch.object(
            sam3_router.workspace, "list", return_value=[]
        ), patch.object(
            sam3_router.workspace,
            "get",
            return_value=SimpleNamespace(label="Boom", output_root=export_root.parent),
        ):
            response = await sam3_router.next_job(
                worker="gpu-1", lease=180, wait=0, _="worker-token"
            )

        body = json.loads(response.body)
        lease_id = body["lease_id"]
        expected = (
            export_root
            / "_sam3"
            / "staging"
            / queued.run_id
            / lease_id
            / "seg_00"
        )
        self.assertEqual(body.get("run_id"), queued.run_id)
        self.assertEqual(body["segments"][0].get("staging_dir"), str(expected))
        self.assertEqual(
            body.get("model"),
            {
                "model_id": "model-1",
                "checkpoint_sha256": "a" * 64,
                "sam3_commit": "b" * 40,
            },
        )


class Sam3PublicationTests(unittest.TestCase):
    def test_reservation_wraps_only_validated_pointer_and_covers_replay(self):
        from pipeline_core.sam3_runs import publish_generation, current_manifest_path
        result = self._worker_result()
        calls = []
        def before_pointer(manifest):
            calls.append(manifest["generation_id"])
            self.assertTrue((self.export_root / "_sam3/runs/run-1/generation.json").is_file())
            raise RuntimeError("reserve failed")
        kwargs = dict(export_root=self.export_root, segments=["seg_00"],
                      generation_id="run-1", attempt_id="lease-1", annotation_revision=7,
                      expected_model=self.model, worker_result=result)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "reserve failed"):
                publish_generation(**kwargs, before_publish=before_pointer)
            self.assertFalse(current_manifest_path(self.export_root).exists())
        self.assertEqual(calls, ["run-1", "run-1"])
        publish_generation(**kwargs)
        self.assertTrue(current_manifest_path(self.export_root).exists())

    def setUp(self) -> None:
        self.export_root = Path(tempfile.mkdtemp()) / "video"
        self.segment = self.export_root / "seg_00"
        self.segment.mkdir(parents=True)
        self.prompt = {
            "schema_version": 1,
            "video": "clip.mp4",
            "video_relpath": "clip.mp4",
            "segment": "seg_00",
            "interval_index": 0,
            "source_start_frame": 10,
            "source_end_frame": 10,
            "frame_count": 1,
            "image_width": 8,
            "image_height": 6,
            "prompt_frame_idx": 0,
            "objects": [
                {
                    "obj_id": 1,
                    "label": "boom",
                    "box_normalized": [0.1, 0.1, 0.5, 0.5],
                }
            ],
        }
        raw_prompt = json.dumps(self.prompt).encode()
        (self.segment / "prompt.json").write_bytes(raw_prompt)
        self.prompt_digest = hashlib.sha256(raw_prompt).hexdigest()
        self.model = {
            "model_id": "model-1",
            "checkpoint_sha256": "a" * 64,
            "sam3_commit": "b" * 40,
        }

    def _worker_result(
        self,
        *,
        checksum: str | None = None,
        generation_id: str = "run-1",
        attempt_id: str = "lease-1",
        fill: int = 1,
        annotation_revision: int = 7,
    ) -> dict:
        from pipeline_core.sam3_runs import staging_segment_output

        staging = staging_segment_output(
            self.export_root, generation_id, attempt_id, "seg_00"
        )
        mask = staging / "masks" / "1" / "000000.png"
        mask.parent.mkdir(parents=True, exist_ok=True)
        mask.write_bytes(encode_binary_png(Image.new("1", (8, 6), fill)))
        actual = hashlib.sha256(mask.read_bytes()).hexdigest()
        report = {
            "schema_version": 1,
            "status": "done",
            "runner_version": "test",
            "prompt_digest": self.prompt_digest,
            "frame_count": 1,
            "frames_written": 1,
            "objects": [
                {
                    "obj_id": 1,
                    "label": "boom",
                    "class_index": 0,
                }
            ],
            "params": {"runner_version": "test", "mask_format": "png-1bit-v1"},
            "model": self.model,
            "artifacts": {
                "format": "png-1bit-v1",
                "files": 1,
                "empty": 0 if fill else 1,
                "checksums": {"masks/1/000000.png": checksum or actual},
            },
        }
        (staging / "labels").mkdir()
        (staging / "labels" / "000000.txt").write_text("", encoding="utf-8")
        (staging / "run.json").write_text(json.dumps(report), encoding="utf-8")
        return {
            "run_id": generation_id,
            "annotation_revision": annotation_revision,
            "model": self.model,
            "runner_version": "test",
            "segments_total": 1,
            "segments_done": 1,
            "segments_failed": 0,
            "segment_runs": [
                {
                    "segment": "seg_00",
                    "dir": str(self.segment),
                    "staging_dir": str(staging),
                    **report,
                }
            ],
        }

    def test_server_validates_and_atomically_publishes_an_immutable_generation(self) -> None:
        from pipeline_core import sam3_runs

        publish = getattr(sam3_runs, "publish_generation", None)
        self.assertTrue(callable(publish), "publicador de geracao SAM3 ausente")
        worker_result = self._worker_result()
        worker_result.update(
            {
                "export_root": "C:/old/workspace",
                "dir": "C:/old/workspace/seg_00",
                "staging_dir": "C:/old/workspace/staging",
            }
        )
        marker_path = (
            self.export_root
            / "_sam3"
            / "staging"
            / "run-1"
            / "lease-1"
            / "seg_00"
            / "run.json"
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker.update(
            {
                "dir": "C:/old/workspace/seg_00",
                "output_dir": "C:/old/workspace/_sam3",
                "staging_dir": "C:/old/workspace/staging",
            }
        )
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        result = publish(
            export_root=self.export_root,
            segments=["seg_00"],
            generation_id="run-1",
            attempt_id="lease-1",
            annotation_revision=7,
            expected_model=self.model,
            worker_result=worker_result,
        )

        published = self.export_root / "_sam3" / "runs" / "run-1" / "seg_00"
        self.assertTrue((published / "masks" / "1" / "000000.png").is_file())
        self.assertFalse((self.segment / "_sam3" / "masks").exists())
        current = json.loads(
            (self.export_root / "_sam3" / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(current["generation_id"], "run-1")
        self.assertEqual(result["publication"]["generation_id"], "run-1")
        self.assertNotIn("dir", result["segment_runs"][0])
        self.assertNotIn("output_dir", result["segment_runs"][0])
        self.assertNotIn("staging_dir", result["segment_runs"][0])
        self.assertNotIn("export_root", result)
        self.assertNotIn("dir", result)
        self.assertNotIn("staging_dir", result)

    def test_generation_manifest_remains_valid_after_workspace_is_moved(self) -> None:
        from pipeline_core.sam3_runs import publish_generation

        worker_result = self._worker_result()
        publish_generation(
            export_root=self.export_root,
            segments=["seg_00"],
            generation_id="run-1",
            attempt_id="lease-1",
            annotation_revision=7,
            expected_model=self.model,
            worker_result=worker_result,
        )
        original_parent = self.export_root.parent
        moved_parent = Path(tempfile.mkdtemp()) / "rehome"
        moved_parent.mkdir()
        moved_root = moved_parent / "video"
        shutil.copytree(self.export_root, moved_root)
        generation_bytes = (
            moved_root / "_sam3" / "runs" / "run-1" / "generation.json"
        ).read_bytes()
        self.assertNotIn(str(original_parent).encode(), generation_bytes)

        result = publish_generation(
            export_root=moved_root,
            segments=["seg_00"],
            generation_id="run-1",
            attempt_id="retry-after-rehome",
            annotation_revision=7,
            expected_model=self.model,
            worker_result=worker_result,
        )

        self.assertEqual(result["publication"]["generation_id"], "run-1")
        self.assertEqual(SegmentPaths(moved_root / "seg_00").out_dir.parent.name, "run-1")

    def test_reader_rejects_pointer_identity_that_differs_from_generation_manifest(self) -> None:
        from pipeline_core.sam3_runs import load_current_manifest, publish_generation

        publish_generation(
            export_root=self.export_root,
            segments=["seg_00"],
            generation_id="run-1",
            attempt_id="lease-1",
            annotation_revision=7,
            expected_model=self.model,
            worker_result=self._worker_result(),
        )
        pointer_path = self.export_root / "_sam3" / "current.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["manifest_sha256"] = "f" * 64
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

        with self.assertRaisesRegex(Sam3GenerationError, "manifesto"):
            load_current_manifest(self.export_root)

    def test_publication_rejects_a_symlinked_staging_segment(self) -> None:
        from pipeline_core.sam3_runs import publish_generation, staging_segment_output

        result = self._worker_result()
        staging = staging_segment_output(
            self.export_root, "run-1", "lease-1", "seg_00"
        )
        outside = self.export_root.parent / "outside-stage"
        staging.rename(outside)
        try:
            staging.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")

        with self.assertRaises(Sam3GenerationError):
            publish_generation(
                export_root=self.export_root,
                segments=["seg_00"],
                generation_id="run-1",
                attempt_id="lease-1",
                annotation_revision=7,
                expected_model=self.model,
                worker_result=result,
            )

        self.assertFalse(
            (self.export_root / "_sam3" / "current.json").exists()
        )

    def test_publication_rejects_a_symlinked_runs_ancestor(self) -> None:
        from pipeline_core.sam3_runs import publish_generation

        result = self._worker_result()
        outside = self.export_root.parent / "outside-runs"
        outside.mkdir()
        runs = self.export_root / "_sam3" / "runs"
        try:
            runs.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")

        with self.assertRaises(Sam3GenerationError):
            publish_generation(
                export_root=self.export_root,
                segments=["seg_00"],
                generation_id="run-1",
                attempt_id="lease-1",
                annotation_revision=7,
                expected_model=self.model,
                worker_result=result,
            )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.export_root / "_sam3" / "current.json").exists())

    def test_publication_rejects_a_checksum_claim_that_does_not_match_bytes(self) -> None:
        from pipeline_core.sam3_runs import publish_generation

        with self.assertRaisesRegex(Sam3GenerationError, "checksum divergente"):
            publish_generation(
                export_root=self.export_root,
                segments=["seg_00"],
                generation_id="run-1",
                attempt_id="lease-1",
                annotation_revision=7,
                expected_model=self.model,
                worker_result=self._worker_result(checksum="f" * 64),
            )

        self.assertFalse((self.export_root / "_sam3" / "current.json").exists())

    def test_publication_rejects_review_state_injected_into_worker_staging(self) -> None:
        from pipeline_core.sam3_runs import publish_generation, staging_segment_output

        result = self._worker_result()
        staging = staging_segment_output(
            self.export_root, "run-1", "lease-1", "seg_00"
        )
        (staging / "mask_review.json").write_text(
            '{"schema_version": 1, "frames": {"0": {"status": "ok"}}}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(Sam3GenerationError, "estrutura"):
            publish_generation(
                export_root=self.export_root,
                segments=["seg_00"],
                generation_id="run-1",
                attempt_id="lease-1",
                annotation_revision=7,
                expected_model=self.model,
                worker_result=result,
            )

        self.assertFalse((self.export_root / "_sam3" / "current.json").exists())

    def test_publication_recomputes_empty_mask_count_instead_of_trusting_worker(self) -> None:
        from pipeline_core.sam3_runs import publish_generation, staging_segment_output

        result = self._worker_result()
        result["segment_runs"][0]["artifacts"]["empty"] = 1
        marker_path = (
            staging_segment_output(self.export_root, "run-1", "lease-1", "seg_00")
            / "run.json"
        )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["artifacts"]["empty"] = 1
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        with self.assertRaisesRegex(Sam3GenerationError, "vazias"):
            publish_generation(
                export_root=self.export_root,
                segments=["seg_00"],
                generation_id="run-1",
                attempt_id="lease-1",
                annotation_revision=7,
                expected_model=self.model,
                worker_result=result,
            )

        self.assertFalse((self.export_root / "_sam3" / "current.json").exists())

    def test_publication_rejects_two_labels_mapped_to_the_same_class_index(self) -> None:
        from pipeline_core.sam3_runs import publish_generation, staging_segment_output

        prompt = json.loads((self.segment / "prompt.json").read_text(encoding="utf-8"))
        prompt["objects"].append({"obj_id": 2, "label": "carro", "box_normalized": [0, 0, 1, 1]})
        (self.segment / "prompt.json").write_text(json.dumps(prompt), encoding="utf-8")
        digest = hashlib.sha256((self.segment / "prompt.json").read_bytes()).hexdigest()
        result = self._worker_result()
        staging = staging_segment_output(self.export_root, "run-1", "lease-1", "seg_00")
        second_mask = staging / "masks" / "2" / "000000.png"
        second_mask.parent.mkdir(parents=True)
        second_mask.write_bytes(encode_binary_png(Image.new("1", (8, 6), 1)))
        checksum = hashlib.sha256(second_mask.read_bytes()).hexdigest()
        marker_path = staging / "run.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["prompt_digest"] = digest
        marker["objects"].append(
            {"obj_id": 2, "label": "carro", "class_index": 0}
        )
        marker["artifacts"]["files"] = 2
        marker["artifacts"]["checksums"]["masks/2/000000.png"] = checksum
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        result["segment_runs"][0].update(marker)

        with self.assertRaisesRegex(Sam3GenerationError, "objetos"):
            publish_generation(
                export_root=self.export_root,
                segments=["seg_00"],
                generation_id="run-1",
                attempt_id="lease-1",
                annotation_revision=7,
                expected_model=self.model,
                worker_result=result,
            )

    def test_revision_zero_publication_is_idempotent_and_never_rewrites_bytes(self) -> None:
        from pipeline_core.sam3_runs import publish_generation

        worker_result = self._worker_result(annotation_revision=0)
        first = publish_generation(
            export_root=self.export_root,
            segments=["seg_00"],
            generation_id="run-1",
            attempt_id="lease-1",
            annotation_revision=0,
            expected_model=self.model,
            worker_result=worker_result,
        )
        published_mask = (
            self.export_root
            / "_sam3"
            / "runs"
            / "run-1"
            / "seg_00"
            / "masks"
            / "1"
            / "000000.png"
        )
        before = published_mask.read_bytes()

        second = publish_generation(
            export_root=self.export_root,
            segments=["seg_00"],
            generation_id="run-1",
            attempt_id="another-lease",
            annotation_revision=0,
            expected_model=self.model,
            worker_result=worker_result,
        )

        self.assertEqual(second["publication"], first["publication"])
        self.assertEqual(published_mask.read_bytes(), before)

    def test_rerun_keeps_old_raw_masks_and_approvals_immutable(self) -> None:
        from pipeline_core.sam3_runs import publish_generation

        publish_generation(
            export_root=self.export_root,
            segments=["seg_00"],
            generation_id="run-1",
            attempt_id="lease-1",
            annotation_revision=7,
            expected_model=self.model,
            worker_result=self._worker_result(),
        )
        first_out = SegmentPaths(self.segment).out_dir
        first_mask = first_out / "masks" / "1" / "000000.png"
        before = first_mask.read_bytes()
        FileMaskReviewStore(
            first_out, image_size=(8, 6), labels={1: "boom"}
        ).save_frame(
            0,
            expected_revision=0,
            status="ok",
            instances=[],
            user="ana",
        )

        publish_generation(
            export_root=self.export_root,
            segments=["seg_00"],
            generation_id="run-2",
            attempt_id="lease-2",
            annotation_revision=7,
            expected_model=self.model,
            worker_result=self._worker_result(
                generation_id="run-2", attempt_id="lease-2", fill=0
            ),
        )

        second_out = SegmentPaths(self.segment).out_dir
        self.assertEqual(
            second_out,
            self.export_root / "_sam3" / "runs" / "run-2" / "seg_00",
        )
        self.assertNotEqual(second_out, first_out)
        self.assertEqual(first_mask.read_bytes(), before)
        self.assertTrue((first_out / "mask_review.json").is_file())
        self.assertFalse((second_out / "mask_review.json").exists())


class Sam3PublicationRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_clearing_a_migrated_legacy_override_does_not_resurrect_it(self) -> None:
        from pipeline_core.sam3_runs import (
            active_segment_output,
            effective_prompt_override_path,
        )

        export_root = Path(tempfile.mkdtemp()) / "video"
        segment = export_root / "seg_00"
        published = export_root / "_sam3" / "runs" / "run-old" / "seg_00"
        segment.mkdir(parents=True)
        published.mkdir(parents=True)
        prompt = b'{"schema_version": 1, "objects": [{"obj_id": 1}]}'
        override = b'{"objects": [{"obj_id": 1, "label": "boom"}]}'
        (segment / "prompt.json").write_bytes(prompt)
        (published / "prompt_override.json").write_bytes(override)
        (export_root / "_sam3" / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "run-old",
                    "annotation_revision": 7,
                    "segments": {
                        "seg_00": {
                            "path": "runs/run-old/seg_00",
                            "prompt_digest": hashlib.sha256(prompt + override).hexdigest(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        migrated = effective_prompt_override_path(segment, migrate_legacy=True)
        self.assertEqual(migrated, segment / "_sam3" / "prompt_override.json")

        with patch.object(
            sam3_session_router,
            "_export_root",
            return_value=(export_root, ["seg_00"], SimpleNamespace(relpath="clip.mp4")),
        ), patch("server.sam3.queue.bind"), patch("server.sam3.queue.cancel"):
            response = await sam3_session_router.clear_override(
                "video-1",
                "seg_00",
                ctx=SimpleNamespace(
                    object_id="boom", output_root=export_root.parent
                ),
                _=SimpleNamespace(user_id="ana"),
            )

        self.assertTrue(response["cleared"])
        self.assertIsNone(effective_prompt_override_path(segment))
        with self.assertRaisesRegex(Sam3GenerationError, "prompt atual diverge"):
            active_segment_output(segment)

    async def test_prompt_override_mutation_uses_the_publish_video_fence(self) -> None:
        export_root = Path(tempfile.mkdtemp()) / "video"
        (export_root / "seg_00").mkdir(parents=True)
        events: list[str] = []

        @asynccontextmanager
        async def shared_fence(object_id: str, video_id: str):
            self.assertEqual((object_id, video_id), ("boom", "video-1"))
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        def resolve(_ctx, _video_id):
            self.assertEqual(events, ["enter"])
            return export_root, ["seg_00"], SimpleNamespace(relpath="clip.mp4")

        with patch("server.durable_jobs.enabled", return_value=True), patch(
            "server.durable_jobs.video_advisory_lock_async",
            side_effect=shared_fence,
        ), patch.object(
            sam3_session_router, "_export_root", side_effect=resolve
        ), patch("server.sam3.queue.bind") as bind, patch(
            "server.sam3.queue.cancel"
        ) as cancel:
            await sam3_session_router.save_override(
                "video-1",
                sam3_session_router.OverrideIn(
                    segment="seg_00",
                    boxes=[
                        {
                            "obj_id": 1,
                            "label": "boom",
                            "normalized": [0.1, 0.1, 0.5, 0.5],
                        }
                    ],
                ),
                ctx=SimpleNamespace(
                    object_id="boom", label="Boom", output_root=export_root.parent
                ),
                user=SimpleNamespace(user_id="ana"),
            )

        self.assertEqual(events, ["enter", "exit"])
        self.assertTrue(
            (export_root / "seg_00" / "_sam3" / "prompt_override.json").is_file()
        )
        bind.assert_called_once_with("boom", export_root.parent)
        cancel.assert_called_once_with("boom", "clip.mp4")

    async def test_done_callback_persists_only_the_server_published_result(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.doc = {
                    "videos": {
                        "clip.mp4": {
                            "annotation_revision": 7,
                            "status": "done",
                        }
                    }
                }

            def entry(self, relpath):
                return self.doc["videos"].get(relpath)

            async def mutate(self, fn):
                return fn(self.doc)

            def load(self):
                return None

        export_root = Path(tempfile.mkdtemp()) / "video"
        export_root.mkdir()
        queue = Sam3Queue()
        queue.bind("boom", export_root.parent)
        queued = queue.enqueue(
            "boom",
            video_id="video-1",
            relpath="clip.mp4",
            name="clip",
            export_root=str(export_root),
            segments=["seg_00"],
            user="ana",
            annotation_revision=7,
            model_id="model-1",
            model_sha256="a" * 64,
            sam3_commit="b" * 40,
        )
        _, leased = queue.take("gpu-1", 180)
        raw_result = {
            "run_id": queued.run_id,
            "annotation_revision": 7,
            "model": {
                "model_id": "model-1",
                "checkpoint_sha256": "a" * 64,
                "sam3_commit": "b" * 40,
            },
            "segment_runs": [],
        }
        published_result = {
            **copy.deepcopy(raw_result),
            "publication": {"generation_id": queued.run_id},
        }
        store = Store()
        ctx = SimpleNamespace(object_id="boom", store=store, ensure_loaded=lambda: None)
        from server.tests.test_projection_mutation_hooks import intent
        events = []
        observations = []
        @asynccontextmanager
        async def fence(*args):
            events.append("video-enter")
            try:
                yield
            finally:
                events.append("video-exit")
        def publish_result(**kwargs):
            kwargs["before_publish"]({"generation_id": queued.run_id, "manifest_sha256": "a" * 64})
            events.append("pointer")
            return published_result
        def reconcile(*args, **kwargs):
            observations.append((list(events), queue.get("boom", "clip.mp4").state,
                                 "sam3" in store.entry("clip.mp4")))
            raise RuntimeError("apply failed")

        with patch.object(sam3_router, "queue", queue), patch.object(
            sam3_router.workspace, "context", return_value=ctx
        ), patch("server.durable_jobs.enabled", return_value=False), patch(
            "server.routers.sam3._resolve_export_root", return_value=str(export_root)
        ), patch(
            "pipeline_core.sam3_runs.publish_generation",
            side_effect=publish_result,
        ) as publish, patch.object(sam3_router, "sam3_video_fence", side_effect=fence), patch(
            "server.pipeline_projection.reserve_intent", side_effect=intent
        ), patch("server.pipeline_projection.fail_intent"), patch(
            "server.pipeline_reconcile.reconcile_video", side_effect=reconcile
        ):
            response = await sam3_router.result(
                leased.lease_id,
                sam3_router.ResultIn(state="done", result=raw_result),
                "worker-token",
            )

        publish.assert_called_once()
        self.assertEqual(response["state"], "done")
        self.assertTrue(response["projection_pending"])
        self.assertEqual(observations, [(["video-enter", "pointer", "video-exit"], "done", True)])
        self.assertEqual(response["projection_event_seq"], 41)
        self.assertEqual(
            store.entry("clip.mp4")["sam3"]["publication"],
            {"generation_id": queued.run_id},
        )


if __name__ == "__main__":
    unittest.main()
