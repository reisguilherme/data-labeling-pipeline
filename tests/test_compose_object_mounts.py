from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_compose_object_mounts.py"
SPEC = importlib.util.spec_from_file_location("compose_object_mounts", SCRIPT)
assert SPEC and SPEC.loader
compose_object_mounts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compose_object_mounts)


class ComposeObjectMountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "data" / "workspace"
        self.workspace.mkdir(parents=True)
        for object_id in ("boom", "microfone"):
            (self.workspace / object_id / "raw").mkdir(parents=True)
            (self.workspace / object_id / "dataset").mkdir()
        self.sentinel = self.workspace / "boom" / "raw" / "manual.mp4"
        self.sentinel.write_bytes(b"manual-work")
        (self.workspace / "objects.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "objects": [
                        {
                            "object_id": object_id,
                            "videos_root": f"{object_id}/raw",
                            "output_root": f"{object_id}/dataset",
                        }
                        for object_id in ("boom", "microfone")
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.output = Path(self.temporary.name) / "compose.override.yml"

    def test_generates_identical_explicit_mounts_for_every_runtime(self) -> None:
        result = compose_object_mounts.generate(self.workspace, self.output)

        document = yaml.safe_load(self.output.read_text(encoding="utf-8"))
        self.assertEqual(result.object_count, 2)
        self.assertEqual(result.mount_count, 4)
        expected = {
            (str((self.workspace / object_id / kind).resolve()), f"/workspace/{object_id}/{kind}")
            for object_id in ("boom", "microfone")
            for kind in ("raw", "dataset")
        }
        for service in ("app", "worker", "sam3-worker"):
            mounts = document["services"][service]["volumes"]
            self.assertEqual(
                {(mount["source"], mount["target"]) for mount in mounts}, expected
            )
            self.assertTrue(
                all(mount["bind"]["create_host_path"] is False for mount in mounts)
            )
        self.assertEqual(self.sentinel.read_bytes(), b"manual-work")

    def test_missing_root_never_gets_created_or_replaces_previous_override(self) -> None:
        missing = self.workspace / "microfone" / "raw"
        missing.rmdir()
        self.output.write_text("previous-safe-config", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "nao existe"):
            compose_object_mounts.generate(self.workspace, self.output)

        self.assertFalse(missing.exists())
        self.assertEqual(
            self.output.read_text(encoding="utf-8"), "previous-safe-config"
        )

    def test_registry_path_cannot_escape_workspace(self) -> None:
        registry = json.loads(
            (self.workspace / "objects.json").read_text(encoding="utf-8")
        )
        registry["objects"][0]["videos_root"] = "../foreign/raw"
        (self.workspace / "objects.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "fora do workspace"):
            compose_object_mounts.generate(self.workspace, self.output)

        self.assertFalse(self.output.exists())

    def test_duplicate_or_overlapping_roots_are_rejected(self) -> None:
        registry = json.loads(
            (self.workspace / "objects.json").read_text(encoding="utf-8")
        )
        registry["objects"][1]["videos_root"] = "boom/raw"
        (self.workspace / "objects.json").write_text(
            json.dumps(registry), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "sobrepostas"):
            compose_object_mounts.generate(self.workspace, self.output)

        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
