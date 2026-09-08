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

    async def run(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            task = self._tasks.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._tasks[key] = task

        try:
            return cast(T, await asyncio.shield(task))
        finally:
            if task.done():
                async with self._lock:
                    if self._tasks.get(key) is task:
                        del self._tasks[key]
