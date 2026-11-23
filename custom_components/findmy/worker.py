"""A thread with its own event loop, where the Find My network library runs.

Fetching reports of an accessory derives its rolling keys (a KDF step per 15 minutes since pairing), multiplies
points on P-224 and decrypts every report: seconds to minutes of CPU, done between the library's awaits. Run on
Home Assistant's loop, that work freezes Home Assistant for as long. Here it runs on a loop of its own, in its own
thread, and Home Assistant only awaits the result.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

_LOGGER = logging.getLogger(__name__)


class Worker:
    """An event loop in a daemon thread; coroutines are submitted to it from any other loop."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        """Start the thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def run[T](self, factory: Callable[[], Coroutine[Any, Any, T]], timeout: float | None = None) -> T:
        """Run the coroutine made by `factory` on the worker loop and await its result from the caller's loop.

        The coroutine is created on the worker loop, so that objects it creates (HTTP sessions) belong there.
        A timeout cancels it on the worker loop too.
        """
        self.start()
        assert self._loop is not None

        async def _wrapped() -> T:
            if timeout is None:
                return await factory()
            return await asyncio.wait_for(factory(), timeout)

        future = asyncio.run_coroutine_threadsafe(_wrapped(), self._loop)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

    def stop(self) -> None:
        """Stop the loop; pending coroutines are cancelled."""
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return

        def _cancel_all() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.call_soon(loop.stop)

        if loop.is_running():
            loop.call_soon_threadsafe(_cancel_all)
        thread.join(timeout=10)
        if thread.is_alive():
            _LOGGER.warning("Worker thread %s did not stop in time", self._name)
        self._loop = None
        self._thread = None
