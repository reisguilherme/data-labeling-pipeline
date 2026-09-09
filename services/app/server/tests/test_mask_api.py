from __future__ import annotations

import base64
import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

CORE_SRC = Path(__file__).resolve().parents[3] / "packages" / "pipeline-core" / "src"
sys.path.insert(0, str(CORE_SRC))

from pipeline_core.masks import encode_binary_png
from pipeline_core.review_store import FileMaskReviewStore
from server.mask_api import decode_mask_edits, serialize_frame_state


class MaskApiContractTests(unittest.TestCase):
    def test_serialized_instance_contains_mask_url_revision_and_derived_bbox(self) -> None:
        out_dir = Path(tempfile.mkdtemp()) / "_sam3"
        path = out_dir / "masks" / "3" / "000002.png"
        path.parent.mkdir(parents=True)
        image = Image.new("1", (10, 10), 0)
        image.putpixel((4, 5), 1)
        path.write_bytes(encode_binary_png(image))
        state = FileMaskReviewStore(
            out_dir, image_size=(10, 10), labels={3: "boom"}
        ).get_frame(2)

        payload = serialize_frame_state(state, mask_url=lambda obj_id: f"/masks/{obj_id}.png")

        self.assertEqual(payload["revision"], 0)
        self.assertEqual(payload["instances"][0]["bbox"], [0.4, 0.5, 0.5, 0.6])
        self.assertEqual(
            payload["instances"][0]["mask_url"],
            f"/masks/3.png?revision=0&sha256={payload['instances'][0]['sha256']}",
        )

    def test_decodes_binary_png_from_base64_payload(self) -> None:
        png = encode_binary_png(Image.new("1", (4, 4), 0))

        edits = decode_mask_edits(
            [{"obj_id": 1, "label": "boom", "png_base64": base64.b64encode(png).decode()}]
        )

        self.assertEqual(edits[0].png, png)

    def test_rejects_invalid_base64(self) -> None:
        with self.assertRaisesRegex(ValueError, "base64"):
            decode_mask_edits([{"obj_id": 1, "label": "boom", "png_base64": "%%%"}])

    def test_normalizes_browser_rgba_canvas_to_binary_png(self) -> None:
        canvas = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
        canvas.putpixel((1, 0), (255, 255, 255, 255))
        stream = io.BytesIO()
        canvas.save(stream, format="PNG")

        edit = decode_mask_edits(
            [
                {
                    "obj_id": 1,
                    "label": "boom",
                    "png_base64": base64.b64encode(stream.getvalue()).decode(),
                }
            ]
        )[0]

        normalized = Image.open(io.BytesIO(edit.png))
        self.assertIn(normalized.mode, ("1", "L"))
        self.assertEqual(normalized.convert("1").getbbox(), (1, 0, 2, 1))


if __name__ == "__main__":
    unittest.main()
