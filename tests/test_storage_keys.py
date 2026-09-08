from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

CORE_SRC = Path(__file__).resolve().parents[1] / "packages" / "pipeline-core" / "src"
sys.path.insert(0, str(CORE_SRC))

from pipeline_core.storage import MinioBlobStore, object_key_for


class StorageKeyTests(unittest.TestCase):
    def test_key_is_relative_portable_and_confined_to_root(self) -> None:
        root = Path(tempfile.mkdtemp()).resolve()
        artifact = root / "boom" / "seg" / "mask.png"

        self.assertEqual(object_key_for(root, artifact), "boom/seg/mask.png")

    def test_key_rejects_artifact_outside_root(self) -> None:
        root = Path(tempfile.mkdtemp()).resolve()
        outside = root.parent / "outside.png"

        with self.assertRaisesRegex(ValueError, "fora"):
            object_key_for(root, outside)

    def test_versioned_model_is_not_uploaded_again_when_size_matches(self) -> None:
        checkpoint = Path(tempfile.mkdtemp()) / "model.pt"
        checkpoint.write_bytes(b"checkpoint")

        class Existing:
            size = len(b"checkpoint")
            etag = "existing-etag"
            version_id = "v1"

        class Client:
            uploads = 0

            def stat_object(self, bucket, key):
                return Existing()

            def fput_object(self, bucket, key, path):
                self.uploads += 1
                raise AssertionError("checkpoint existente nao deve ser reenviado")

        client = Client()
        stored = MinioBlobStore(client).put_file_if_absent(
            "models", "sam3/sha/model.pt", checkpoint
        )

        self.assertEqual(client.uploads, 0)
        self.assertEqual(stored.etag, "existing-etag")


if __name__ == "__main__":
    unittest.main()
