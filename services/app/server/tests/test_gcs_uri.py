from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from server.gcs import credentials_file_valid, parse_gs_uri


class GcsUriTests(unittest.TestCase):
    def test_parses_bucket_prefix_and_blob(self) -> None:
        self.assertEqual(parse_gs_uri("gs://bucket/folder/video.mp4"), ("bucket", "folder/video.mp4"))

    def test_rejects_missing_bucket(self) -> None:
        with self.assertRaisesRegex(ValueError, "bucket"):
            parse_gs_uri("gs:///video.mp4")

    def test_placeholder_is_not_a_valid_service_account(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "credentials.json"
            path.write_text("{}", encoding="utf-8")
            self.assertFalse(credentials_file_valid(path))

    def test_accepts_minimal_service_account_shape(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "credentials.json"
            path.write_text(
                '{"type":"service_account","project_id":"p",'
                '"client_email":"worker@example.test","private_key":"key"}',
                encoding="utf-8",
            )
            self.assertTrue(credentials_file_valid(path))


if __name__ == "__main__":
    unittest.main()
