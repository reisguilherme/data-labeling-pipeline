"""One per-video fence shared by sync and async mutation paths."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from . import durable_jobs


_guard = threading.Lock()
_local: dict[tuple[str, str], threading.Lock] = {}


def _local_lock(object_id: str, video_id: str) -> threading.Lock:
    key = (object_id, video_id)
    with _guard:
        return _local.setdefault(key, threading.Lock())


@contextmanager
def video_fence(object_id: str, video_id: str) -> Iterator[None]:
    """Fence a synchronous mutation against every other video mutation."""
    if durable_jobs.enabled():
        with durable_jobs.video_advisory_lock(object_id, video_id):
            yield
        return
    with _local_lock(object_id, video_id):
        yield


@asynccontextmanager
async def async_video_fence(object_id: str, video_id: str) -> AsyncIterator[None]:
    """Async counterpart using the exact same local lock as sync callers."""
    if durable_jobs.enabled():
        async with durable_jobs.video_advisory_lock_async(object_id, video_id):
            yield
        return

    lock = _local_lock(object_id, video_id)
    acquire = asyncio.create_task(asyncio.to_thread(lock.acquire))
    try:
        await asyncio.shield(acquire)
    except BaseException:
        # Cancelling the coroutine cannot cancel a thread already blocked in
        # Lock.acquire(). Wait for it and release immediately, otherwise the
        # orphan thread can acquire later and fence this video forever.
        acquired = await asyncio.shield(acquire)
        if acquired:
            lock.release()
        raise
    try:
        yield
    finally:
        lock.release()
