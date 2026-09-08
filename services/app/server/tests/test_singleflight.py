"""Behavior tests for asynchronous single-flight work."""

from __future__ import annotations

import asyncio
import gc
import unittest

from server.singleflight import AsyncSingleFlight


class AsyncSingleFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_shares_one_in_flight_factory(self) -> None:
        singleflight = AsyncSingleFlight()
        factory_started = asyncio.Event()
        release_factory = asyncio.Event()
        factory_calls = 0

        async def factory() -> str:
            nonlocal factory_calls
            factory_calls += 1
            factory_started.set()
            await release_factory.wait()
            return "shared-result"

        first = asyncio.create_task(singleflight.run("video-list", factory))
        second = asyncio.create_task(singleflight.run("video-list", factory))
        await factory_started.wait()
        await asyncio.sleep(0)

        self.assertEqual(factory_calls, 1)
        release_factory.set()
        self.assertEqual(
            await asyncio.gather(first, second),
            ["shared-result", "shared-result"],
        )

    async def test_failed_task_is_not_reused_by_a_later_call(self) -> None:
        singleflight = AsyncSingleFlight()
        factory_started = asyncio.Event()
        release_factory = asyncio.Event()
        factory_calls = 0

        async def failing_factory() -> str:
            nonlocal factory_calls
            factory_calls += 1
            factory_started.set()
            await release_factory.wait()
            raise RuntimeError("factory failed")

        first = asyncio.create_task(singleflight.run("video-list", failing_factory))
        second = asyncio.create_task(singleflight.run("video-list", failing_factory))
        await factory_started.wait()
        await asyncio.sleep(0)
        self.assertEqual(factory_calls, 1)

        release_factory.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(
            [type(result) for result in results],
            [RuntimeError, RuntimeError],
        )

        async def succeeding_factory() -> str:
            nonlocal factory_calls
            factory_calls += 1
            return "fresh-result"

        self.assertEqual(
            await singleflight.run("video-list", succeeding_factory),
            "fresh-result",
        )
        self.assertEqual(factory_calls, 2)

    async def test_cancelled_waiters_do_not_retain_successful_task(self) -> None:
        singleflight = AsyncSingleFlight()
        factory_started = asyncio.Event()
        release_factory = asyncio.Event()
        factory_finished = asyncio.Event()
        factory_calls = 0

        async def shared_factory() -> str:
            nonlocal factory_calls
            factory_calls += 1
            factory_started.set()
            try:
                await release_factory.wait()
                return "abandoned-result"
            finally:
                factory_finished.set()

        waiters = [
            asyncio.create_task(singleflight.run("cancel-success", shared_factory))
            for _ in range(2)
        ]
        await factory_started.wait()
        await asyncio.sleep(0)
        for waiter in waiters:
            waiter.cancel()
        cancelled = await asyncio.gather(*waiters, return_exceptions=True)
        self.assertTrue(
            all(isinstance(result, asyncio.CancelledError) for result in cancelled)
        )

        release_factory.set()
        await factory_finished.wait()
        await asyncio.sleep(0)

        async def fresh_factory() -> str:
            nonlocal factory_calls
            factory_calls += 1
            return "fresh-result"

        self.assertEqual(
            await singleflight.run("cancel-success", fresh_factory),
            "fresh-result",
        )
        self.assertEqual(factory_calls, 2)

    async def test_cancelled_waiters_do_not_retain_orphaned_error(self) -> None:
        singleflight = AsyncSingleFlight()
        factory_started = asyncio.Event()
        release_factory = asyncio.Event()
        factory_finished = asyncio.Event()
        factory_calls = 0
        orphaned_errors: list[dict] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: orphaned_errors.append(context)
        )

        async def shared_factory() -> str:
            nonlocal factory_calls
            factory_calls += 1
            factory_started.set()
            try:
                await release_factory.wait()
                raise RuntimeError("abandoned failure")
            finally:
                factory_finished.set()

        waiters = [
            asyncio.create_task(singleflight.run("cancel-error", shared_factory))
            for _ in range(2)
        ]
        try:
            await factory_started.wait()
            await asyncio.sleep(0)
            for waiter in waiters:
                waiter.cancel()
            cancelled = await asyncio.gather(*waiters, return_exceptions=True)
            self.assertTrue(
                all(isinstance(result, asyncio.CancelledError) for result in cancelled)
            )

            release_factory.set()
            await factory_finished.wait()
            await asyncio.sleep(0)

            async def fresh_factory() -> str:
                nonlocal factory_calls
                factory_calls += 1
                return "fresh-result"

            self.assertEqual(
                await singleflight.run("cancel-error", fresh_factory),
                "fresh-result",
            )
            self.assertEqual(factory_calls, 2)
            del waiters
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(orphaned_errors, [])
        finally:
            release_factory.set()
            loop.set_exception_handler(previous_handler)


if __name__ == "__main__":
    unittest.main()
