"""The worker loop keeps CPU-bound library work off the caller's loop."""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest

from custom_components.findmy.worker import Worker


async def test_runs_on_its_own_thread_without_blocking_the_caller() -> None:
    worker = Worker("test")

    async def busy() -> str:
        # CPU work between awaits, as the key derivation does.
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            hashlib.sha256(b"x").digest()
        await asyncio.sleep(0)
        return "done"

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        assert await worker.run(busy) == "done"
    finally:
        task.cancel()
        worker.stop()
    # The caller's loop kept running while the worker was busy (run on the caller's loop, it would not tick at all).
    assert ticks >= 3


async def test_timeout_cancels_on_the_worker() -> None:
    worker = Worker("test")
    cancelled = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def slow() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            loop.call_soon_threadsafe(cancelled.set)
            raise

    with pytest.raises(TimeoutError):
        await worker.run(slow, timeout=0.05)
    await asyncio.wait_for(cancelled.wait(), 2)
    worker.stop()


async def test_exceptions_reach_the_caller_and_restart_after_stop() -> None:
    worker = Worker("test")

    async def fail() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await worker.run(fail)
    worker.stop()

    async def ok() -> int:
        return 1

    assert await worker.run(ok) == 1
    worker.stop()
