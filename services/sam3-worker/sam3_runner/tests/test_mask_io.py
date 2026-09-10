from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from sam3_runner.mask_io import save_mask_png


class _Store:
    def __init__(self) -> None:
        self.immutable: list[tuple[str, str, Path]] = []
        self.mutable: list[tuple[str, str, Path]] = []

    def put_file_if_absent(self, bucket: str, key: str, path: Path) -> None:
        self.immutable.append((bucket, key, path))

    def put_file(self, bucket: str, key: str, path: Path) -> None:
        self.mutable.append((bucket, key, path))


class MaskStorageTests(unittest.TestCase):
    def test_mask_blob_uses_its_content_hash_instead_of_staging_path(self) -> None:
        root = Path(tempfile.mkdtemp())
        store = _Store()
        with patch(
            "sam3_runner.mask_io.MinioBlobStore.from_env", return_value=store
        ), patch.dict("os.environ", {"MST_WORKSPACE": str(root)}):
            path = save_mask_png(
                root / "video" / "_sam3" / "staging" / "run" / "lease" / "seg_00",
                obj_id=1,
                frame_idx=0,
                mask=Image.new("1", (8, 6), 1),
            )

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(store.mutable, [])
        self.assertEqual(
            store.immutable,
            [("masks", f"sha256/{digest[:2]}/{digest}.png", path)],
        )


if __name__ == "__main__":
    unittest.main()
