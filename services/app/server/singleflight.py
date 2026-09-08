"""Coalesce identical asynchronous work while it is in flight."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import TypeVar, cast

T = TypeVar("T")


class AsyncSingleFlight:
    """Share one in-flight task between callers using the same key."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[Hashable, asyncio.Task[object]] = {}

    @staticmethod
    def _consume_exception(task: asyncio.Task[object]) -> None:
        if not task.cancelled():
            task.exception()

    async def _run_and_cleanup(
        self, key: Hashable, factory: Callable[[], Awaitable[T]]
    ) -> T:
        try:
            return await factory()
        finally:
            current = asyncio.current_task()
            async with self._lock:
                if self._tasks.get(key) is current:
                    del self._tasks[key]

    async def run(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            task = self._tasks.get(key)
            if task is None:
                task = asyncio.create_task(self._run_and_cleanup(key, factory))
                task.add_done_callback(self._consume_exception)
                self._tasks[key] = task

        return cast(T, await asyncio.shield(task))
