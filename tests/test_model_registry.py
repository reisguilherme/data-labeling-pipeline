from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
import sys

CORE_SRC = Path(__file__).resolve().parents[1] / "packages" / "pipeline-core" / "src"
sys.path.insert(0, str(CORE_SRC))

from pipeline_core.model_registry import CheckpointIntegrityError, ModelRegistry


class ModelRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.checkpoint = self.root / "fine-tuned.pt"
        self.checkpoint.write_bytes(b"checkpoint-v1")
        self.sha = hashlib.sha256(b"checkpoint-v1").hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _registry(self, sha: str | None = None) -> ModelRegistry:
        config = self.root / "models.yaml"
        config.write_text(
            f"""schema_version: 1
default_model: base
models:
  - model_id: base
    source:
      type: local
      path: {self.checkpoint.as_posix()}
    sha256: "{sha or self.sha}"
    sam3_commit: 8f0b7f4d4e7eda2ed606ebde6702c93359ad01da
  - model_id: boom-v2
    source:
      type: huggingface
      repo_id: org/boom
      filename: model.pt
      revision: aabbccdd
    sha256: "{'1' * 64}"
    sam3_commit: 8f0b7f4d4e7eda2ed606ebde6702c93359ad01da
assignments:
  objects:
    boom: boom-v2
""",
            encoding="utf-8",
        )
        return ModelRegistry.load(config)

    def test_object_assignment_overrides_default_model(self) -> None:
        registry = self._registry()

        self.assertEqual(registry.select("unknown").model_id, "base")
        self.assertEqual(registry.select("boom").model_id, "boom-v2")

    def test_local_checkpoint_is_resolved_only_after_sha256_validation(self) -> None:
        registry = self._registry()

        self.assertEqual(registry.resolve_local("base"), self.checkpoint.resolve())

    def test_checksum_mismatch_is_fatal(self) -> None:
        registry = self._registry("0" * 64)

        with self.assertRaisesRegex(CheckpointIntegrityError, "sha256"):
            registry.resolve_local("base")

    def test_huggingface_revision_must_be_pinned(self) -> None:
        config = self.root / "models.yaml"
        config.write_text(
            f"""schema_version: 1
default_model: bad
models:
  - model_id: bad
    source: {{type: huggingface, repo_id: org/model, filename: model.pt}}
    sha256: "{'1' * 64}"
    sam3_commit: 8f0b7f4d4e7eda2ed606ebde6702c93359ad01da
""",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "revision"):
            ModelRegistry.load(config)


if __name__ == "__main__":
    unittest.main()
