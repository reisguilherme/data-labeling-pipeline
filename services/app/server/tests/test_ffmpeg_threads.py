from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import ffmpeg


class FFmpegThreadBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolve = patch(
            "server.ffmpeg.resolve",
            return_value=SimpleNamespace(ffmpeg="ffmpeg"),
        )
        self.resolve.start()
        self.addCleanup(self.resolve.stop)

    def assert_decoder_budget(self, argv: list[str]) -> None:
        input_index = argv.index("-i")
        self.assertEqual(argv[input_index - 2 : input_index], ["-threads", "4"])

    def assert_output_budget(self, argv: list[str], output: Path) -> None:
        output_index = argv.index(str(output))
        self.assertEqual(argv[output_index - 2 : output_index], ["-threads", "4"])

    def test_proxy_full_limits_decoder_and_each_jpeg_encoder(self) -> None:
        small = Path("small")
        full = Path("full")
        argv = ffmpeg.proxy_full_argv(Path("clip.mp4"), small, full)

        self.assert_decoder_budget(argv)
        self.assert_output_budget(argv, small / "%06d.jpg")
        self.assert_output_budget(argv, full / "%06d.jpg")

    def test_proxy_window_limits_decoder_and_each_jpeg_encoder(self) -> None:
        small = Path("small")
        full = Path("full")
        argv = ffmpeg.proxy_window_argv(Path("clip.mp4"), small, full, 10, 20)

        self.assert_decoder_budget(argv)
        self.assert_output_budget(argv, small / "%06d.jpg")
        self.assert_output_budget(argv, full / "%06d.jpg")

    def test_segment_export_limits_decoder_and_encoder(self) -> None:
        output = Path("frames") / "%06d.jpg"
        argv = ffmpeg.export_segment_argv(Path("clip.mp4"), output.parent, 10, 20)

        self.assert_decoder_budget(argv)
        self.assert_output_budget(argv, output)

    def test_thumbnail_limits_decoder_and_encoder(self) -> None:
        output = Path("thumb.jpg")
        argv = ffmpeg.thumb_argv(Path("clip.mp4"), output, 1.25)

        self.assert_decoder_budget(argv)
        self.assert_output_budget(argv, output)


if __name__ == "__main__":
    unittest.main()
