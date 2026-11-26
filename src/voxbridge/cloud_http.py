"""Cancellable sync SDK transport over HTTPX's public async transport.

The SDK still serializes requests and parses responses. Each client is
owned by one worker thread. No second network worker, retries or fallback.
"""
from __future__ import annotations

import asyncio
import threading

import httpx
from .cloud_budget import CloudAbort, RequestBudget, current_budget, poll_awaitable

CLEANUP_SECONDS = 2.0


class _RequestBody(httpx.AsyncByteStream):
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget

    async def __aiter__(self):
        # SDK inputs are validated in-memory JSON/WAV/image bytes.
        for chunk in self.stream:
            self.budget.check()
            yield chunk


class _ResponseBody(httpx.SyncByteStream):
    def __init__(self, owner, response, budget):
        self.owner, self.response, self.budget = owner, response, budget
        self.closed = False
        self.iterator = response.stream.__aiter__()

    def __iter__(self):
        iterator = self.iterator
        total = 0
        try:
            while True:
                self.budget.check()
                try:
                    chunk = self.owner.run(iterator.__anext__(), self.budget)
                except StopAsyncIteration:
                    break
                self.budget.check()
                total += len(chunk)
                if total > self.budget.max_bytes:
                    raise CloudAbort("RESPONSE_TOO_LARGE")
                yield chunk
            self.budget.check()
        except BaseException:
            self.close()
            raise

    async def _close_async(self):
        try:
            close = getattr(self.iterator, "aclose", None)
            if callable(close):
                await close()
        finally:
            await self.response.aclose()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.owner.run(self._close_async(), RequestBudget(CLEANUP_SECONDS))
            except Exception:
                self.owner.failed = True
                raise CloudAbort("CLEANUP_FAILED") from None


class DeadlineTransport(httpx.BaseTransport):
    def __init__(self, seconds, *, max_bytes=24_000_000, transport=None):
        self.seconds = seconds
        self.max_bytes = max_bytes
        self.transport = transport if transport is not None else httpx.AsyncHTTPTransport(
            trust_env=False, retries=0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
        self.loop = None
        self.thread = None
        self.failed = False
        self.closed = False

    def _owner(self):
        if self.closed:
            raise CloudAbort("TRANSPORT_CLOSED")
        # Never nest the client's synchronous loop inside an active event loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise CloudAbort("SYNC_WORKER_REQUIRED")
        if self.thread is None:
            self.thread = threading.get_ident()
            self.loop = asyncio.new_event_loop()
        elif self.thread != threading.get_ident():
            raise CloudAbort("WRONG_WORKER")

    def run(self, awaitable, budget):
        # run() is only called after _owner() created the loop.
        try:
            self._owner()
        except BaseException:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise
        return self.loop.run_until_complete(poll_awaitable(awaitable, budget))

    def handle_request(self, request):
        self._owner()
        if self.failed:
            raise CloudAbort("CLEANUP_FAILED")
        budget = current_budget() or RequestBudget(self.seconds, max_bytes=self.max_bytes)
        budget.check()
        # Keep the SDK's original URL, headers and timeout settings; the
        # monotonic deadline additionally bounds every network await.
        outgoing = httpx.Request(request.method, request.url, headers=request.headers,
                                 stream=_RequestBody(request.stream, budget),
                                 extensions=request.extensions)
        response = self.run(self.transport.handle_async_request(outgoing), budget)
        body = _ResponseBody(self, response, budget)
        try:
            budget.check()
            length = response.headers.get("content-length")
            if length is not None and length.isascii() and length.isdigit():
                if len(length) > 10 or int(length) > budget.max_bytes:
                    raise CloudAbort("RESPONSE_TOO_LARGE")
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=body, extensions=response.extensions)
        except BaseException:
            body.close()
            raise

    async def _close_async(self):
        try:
            await self.transport.aclose()
        finally:
            await self.loop.shutdown_asyncgens()

    def close(self):
        if self.closed:
            if self.failed:
                raise CloudAbort("CLEANUP_FAILED")
            return
        if self.loop is None:
            # No network operation ever started; no live connection exists.
            self.closed = True
            return
        self._owner()
        try:
            self.run(self._close_async(), RequestBudget(CLEANUP_SECONDS))
            if self.failed:
                raise CloudAbort("CLEANUP_FAILED")
        except Exception:
            self.failed = True
            raise CloudAbort("CLEANUP_FAILED") from None
        finally:
            self.closed = True
            self.loop.close()
            self.loop = None
