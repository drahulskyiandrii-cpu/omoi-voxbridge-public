"""Worker-owned bounded WebSocket I/O; no network on import.

The caption pipeline stays synchronous on its existing worker. Only network
operations run on this private asyncio loop. No reconnect, proxy or redirect.
"""
from __future__ import annotations

import asyncio
import logging

from .cloud_budget import CloudAbort, RequestBudget, poll_awaitable

SEND_SECONDS = 3.0
CLOSE_SECONDS = 2.5


class RealtimeSocket:
    def __init__(self, stop, deadline):
        self.stop = stop
        self.deadline = deadline
        self.loop = asyncio.new_event_loop()
        self.connection = None
        self.failed = False
        self.closed = False

    def _run(self, operation, seconds, *, cleanup=False):
        budget = RequestBudget(seconds, None if cleanup else self.stop)
        if not cleanup:
            budget.deadline = min(budget.deadline, self.deadline)
        result = self.loop.run_until_complete(poll_awaitable(operation, budget))
        budget.check()
        return result

    def connect(self, url, **kwargs):
        from websockets.asyncio.client import connect

        class NoRedirect(connect):
            def process_redirect(self, exc):
                return exc

        async def opening():
            # Keep a successfully opened connection even if cancellation races
            # with completion; the owner will close it in its finally block.
            self.connection = await NoRedirect(
                url, logger=logging.Logger("voxbridge.realtime.socket", logging.CRITICAL+1),
                **kwargs)
        try:
            self._run(opening(), kwargs.get("open_timeout", 8))
            if self.stop.is_set():
                raise CloudAbort("CANCELLED")
        except BaseException:
            self.failed = True
            raise

    def send(self, raw):
        try:
            self._run(self.connection.send(raw), SEND_SECONDS)
        except BaseException:
            self.failed = True
            raise

    def recv(self, timeout):
        async def reading():
            # websockets documents that cancelling recv is safe: the next
            # receive still gets the next complete message.
            return await asyncio.wait_for(self.connection.recv(), timeout)
        return self._run(reading(), timeout + .1)

    async def _close_connection(self):
        if self.connection is None:
            return
        if self.failed or self.stop.is_set():
            # Do not flush queued audio after cancellation or a failed send.
            self.connection.transport.abort()
        else:
            await self.connection.close()
        await self.connection.wait_closed()

    def close(self):
        if self.closed:
            return
        try:
            try:
                self._run(self._close_connection(), CLOSE_SECONDS, cleanup=True)
            except Exception:
                if self.connection is None:
                    raise
                self.connection.transport.abort()
                self._run(self.connection.wait_closed(), .5, cleanup=True)
            # Give the library's keepalive and connection callbacks a turn
            # before closing the worker-owned loop.
            self.loop.run_until_complete(asyncio.sleep(0))
            pending = asyncio.all_tasks(self.loop)
            if pending:
                for task in pending:
                    task.cancel()
                async def settle():
                    await asyncio.gather(*pending, return_exceptions=True)
                self._run(settle(), .5, cleanup=True)
        except Exception:
            raise CloudAbort("CLEANUP_FAILED") from None
        finally:
            self.closed = True
            self.loop.close()
