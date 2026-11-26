"""Request-scoped cloud limits; no SDK import or network access."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import time


class CloudAbort(RuntimeError):
    """Only a stable code, never a request, response, or credential."""


class RequestBudget:
    def __init__(self, seconds, cancel=None, max_bytes=24_000_000):
        if not 0 < seconds <= 600 or not 0 < max_bytes <= 32_000_000:
            raise ValueError("INVALID_CLOUD_BUDGET")
        self.deadline = time.monotonic() + seconds
        self.cancel = cancel
        self.max_bytes = max_bytes

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise CloudAbort("CANCELLED")
        if time.monotonic() >= self.deadline:
            raise CloudAbort("TIMEOUT")


POLL_SECONDS = .05


async def poll_awaitable(awaitable, budget):
    """Poll an async network operation from its owning worker loop."""
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            budget.check()
            await asyncio.wait({task}, timeout=min(POLL_SECONDS,
                                max(0, budget.deadline-time.monotonic())))
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


_current = ContextVar("voxbridge_cloud_budget", default=None)


def current_budget():
    return _current.get()


@contextmanager
def cloud_budget(seconds, cancel=None, max_bytes=24_000_000):
    # A caller can supply a tighter budget (e.g. UI vision cancellation).
    previous = current_budget()
    budget = previous or RequestBudget(seconds, cancel, max_bytes)
    token = _current.set(budget)
    try:
        budget.check()
        yield budget
        budget.check()
    finally:
        _current.reset(token)


def abort_code(exc):
    """SDKs may wrap transport errors in APIConnectionError."""
    for _ in range(8):
        if isinstance(exc, CloudAbort):
            return str(exc)
        exc = getattr(exc, "__cause__", None)
        if exc is None:
            break
    return None
