from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from server.locks import LockRegistry
from server.models import BBoxIn, IntervalIn, VideoEntryIn
from server.routers.annotations import NoBoomPayload, put_one
from server.tests.test_triage_mutation_latency import _context, _user


def payload(expected_revision: int, lock_token: str) -> VideoEntryIn:
    return VideoEntryIn(
        status="in_progress",
        expected_revision=expected_revision,
        lock_token=lock_token,
        intervals=[
            IntervalIn(
                start_frame=2,
                end_frame=4,
                prompt_frame=2,
                bboxes=[BBoxIn(normalized=[0.1, 0.2, 0.3, 0.4])],
                flags={"dificuldade": "facil"},
            )
        ],
    )


class LockEpochTests(unittest.TestCase):
    def test_release_and_reacquire_same_client_gets_a_new_token(self) -> None:
        registry = LockRegistry()
        with tempfile.TemporaryDirectory() as temporary:
            registry.bind(Path(temporary))
            first = registry.acquire("boom", "video-1", "clip.mp4", "Ana", "tab-1")
            self.assertTrue(first.token)
            self.assertTrue(
                registry.release("boom", "video-1", "tab-1", first.token)
            )
            second = registry.acquire("boom", "video-1", "clip.mp4", "Ana", "tab-1")

            self.assertNotEqual(first.token, second.token)
            self.assertFalse(
                registry.release("boom", "video-1", "tab-1", first.token)
            )
            self.assertIsNone(
                registry.heartbeat("boom", "video-1", "tab-1", first.token)
            )
            self.assertIsNotNone(
                registry.heartbeat("boom", "video-1", "tab-1", second.token)
            )


class MutationFenceContractTests(unittest.IsolatedAsyncioTestCase):
    def test_mutation_payloads_require_revision_and_lock_token(self) -> None:
        with self.assertRaises(ValidationError):
            VideoEntryIn(status="in_progress", intervals=[])
        with self.assertRaises(ValidationError):
            NoBoomPayload()

    async def test_stale_annotation_revision_never_overwrites_newer_save(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            owner = SimpleNamespace(
                client_id="tab-1",
                token="lock-token-1",
                public=lambda: {"user": "Ana", "since": "now"},
            )
            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=100),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=False),
                patch("server.routers.annotations.locks.get", return_value=owner),
            ):
                first = await put_one(
                    "video-1", payload(0, owner.token), False, ctx, _user(), "tab-1"
                )
                self.assertEqual(first["annotation_revision"], 1)
                with self.assertRaises(HTTPException) as stale:
                    await put_one(
                        "video-1",
                        payload(0, owner.token),
                        False,
                        ctx,
                        _user(),
                        "tab-1",
                    )

            self.assertEqual(stale.exception.status_code, 409)
            self.assertEqual(ctx.store.entry("clip.mp4")["annotation_revision"], 1)

    async def test_lock_is_revalidated_inside_commit_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ctx = _context(Path(temporary))
            owner = SimpleNamespace(
                client_id="tab-1",
                token="lock-token-1",
                public=lambda: {"user": "Ana", "since": "now"},
            )
            replacement = SimpleNamespace(
                client_id="tab-2",
                token="lock-token-2",
                public=lambda: {"user": "Bia", "since": "now"},
            )
            with (
                patch("server.routers.annotations.proxy.is_complete", return_value=100),
                patch("server.routers.annotations.durable_jobs.enabled", return_value=False),
                patch(
                    "server.routers.annotations.locks.get",
                    side_effect=[owner, replacement],
                ),
            ):
                with self.assertRaises(HTTPException) as conflict:
                    await put_one(
                        "video-1",
                        payload(0, owner.token),
                        False,
                        ctx,
                        _user(),
                        "tab-1",
                    )

            self.assertEqual(conflict.exception.status_code, 409)
            self.assertIsNone(ctx.store.entry("clip.mp4"))


if __name__ == "__main__":
    unittest.main()
