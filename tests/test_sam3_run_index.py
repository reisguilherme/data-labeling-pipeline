from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from server.sam3_run_index import _effective_prompt


class Sam3RunIndexTests(unittest.TestCase):
    def test_effective_prompt_digest_includes_override_and_is_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            segment = Path(temporary)
            raw = json.dumps({"objects": [{"obj_id": 1, "label": "boom"}]}).encode()
            override = json.dumps(
                {"objects": [{"obj_id": 1, "label": "boom", "box_normalized": [0, 0, 1, 1]}]}
            ).encode()
            (segment / "prompt.json").write_bytes(raw)
            (segment / "_sam3").mkdir()
            (segment / "_sam3" / "prompt_override.json").write_bytes(override)

            prompt, digest = _effective_prompt(segment)

            self.assertEqual(prompt["objects"][0]["box_normalized"], [0, 0, 1, 1])
            self.assertEqual(digest, hashlib.sha256(raw + override).hexdigest())
            self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
