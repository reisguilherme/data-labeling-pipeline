from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sam3_runner.prompt import Prompt
from sam3_runner.segment import run_segment


class _Predictor:
    def __init__(self) -> None:
        self.frames_yielded = 0

    def init_state(self, **_kwargs):
        return object()

    def clear_all_points_in_video(self, _state) -> None:
        return None

    def propagate_in_video(self, **_kwargs):
        for frame_idx in range(3):
            self.frames_yielded += 1
            yield frame_idx, [], None, [], None


class _Classes:
    def ensure(self, labels: list[str]) -> dict[str, int]:
        return {label: index for index, label in enumerate(labels)}


class _Config:
    mask_threshold = 0.0
    min_box_px = 2
    max_box_frac = 0.95
    model_id = "test-model"
    model_sha256 = "abc"
    sam3_commit = "test"

    def params(self) -> dict:
        return {"runner_version": "test", "mask_format": "png-1bit-v1"}


class SegmentCancellationTests(unittest.TestCase):
    def test_false_frame_callback_stops_propagation_without_completion_marker(self) -> None:
        segment_dir = Path(tempfile.mkdtemp()) / "seg_00"
        segment_dir.mkdir()
        prompt = Prompt(
            segment_dir=segment_dir,
            video="clip.mp4",
            video_relpath="clip.mp4",
            segment="seg_00",
            interval_index=0,
            source_start_frame=0,
            frame_count=3,
            image_width=32,
            image_height=24,
            prompt_frame_idx=0,
            objects=(),
            flags={},
            digest="prompt-digest",
        )
        predictor = _Predictor()

        with patch.dict(sys.modules, {"torch": SimpleNamespace()}), patch(
            "sam3_runner.segment.model.init_state_kwargs", return_value={}
        ), patch("sam3_runner.segment.model.device_info", return_value={}), patch(
            "sam3_runner.segment.model.free_vram"
        ):
            with self.assertRaisesRegex(RuntimeError, "cancel"):
                run_segment(
                    predictor,
                    prompt,
                    _Config(),
                    _Classes(),
                    on_frame=lambda _frame_idx: False,
                )

        self.assertEqual(predictor.frames_yielded, 1)
        self.assertFalse(prompt.marker_path.exists())


if __name__ == "__main__":
    unittest.main()
