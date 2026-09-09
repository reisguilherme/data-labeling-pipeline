from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.params import Depends

from server import proxy
from server.deps import current_user
from server.routers.jobs import clear_cache as clear_cache_endpoint


VIDEO_ID = "0123456789ab"


class _Index:
    def get(self, video_id: str):
        if video_id == VIDEO_ID:
            return SimpleNamespace(video_id=VIDEO_ID)
        return None


class CacheClearSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.cache = self.root / "object" / "_cache"
        self.cache.mkdir(parents=True)
        self.ctx = SimpleNamespace(cache_dir=self.cache, index=_Index())

    def test_destructive_cache_endpoint_requires_current_user(self) -> None:
        user = inspect.signature(clear_cache_endpoint).parameters.get("user")

        self.assertIsNotNone(user)
        self.assertIsInstance(user.default, Depends)
        self.assertIs(user.default.dependency, current_user)

    def test_unknown_traversal_id_cannot_delete_outside_cache(self) -> None:
        victim = self.cache.parent / "victim"
        victim.mkdir()
        sentinel = victim / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with self.assertRaises(KeyError):
            proxy.clear_cache(self.ctx, "../../victim")

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_unknown_absolute_id_cannot_delete_outside_cache(self) -> None:
        victim = self.root / "absolute-victim"
        victim.mkdir()
        sentinel = victim / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with self.assertRaises(KeyError):
            proxy.clear_cache(self.ctx, str(victim.resolve()))

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_symlink_cache_target_is_rejected_without_touching_destination(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        (self.cache / "proxy").mkdir()
        try:
            (self.cache / "proxy" / VIDEO_ID).symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")

        with self.assertRaises(ValueError):
            proxy.clear_cache(self.ctx, VIDEO_ID)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_known_video_deletes_only_its_two_cache_roots(self) -> None:
        proxy_target = self.cache / "proxy" / VIDEO_ID
        window_target = self.cache / "windows" / VIDEO_ID
        sibling = self.cache / "proxy" / "sibling"
        for path in (proxy_target, window_target, sibling):
            path.mkdir(parents=True)
            (path / "frame.jpg").write_bytes(b"jpeg")

        proxy.clear_cache(self.ctx, VIDEO_ID)

        self.assertFalse(proxy_target.exists())
        self.assertFalse(window_target.exists())
        self.assertTrue((sibling / "frame.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
