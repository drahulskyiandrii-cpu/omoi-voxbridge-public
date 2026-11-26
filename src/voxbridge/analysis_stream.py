"""Bounded worker-to-GUI preview mailbox. Never emits per-token Qt signals."""
from __future__ import annotations

import threading
from .analysis_providers import ProviderFailure


class AnalysisPreview:
    def __init__(self, cancel: threading.Event, *, max_chars: int = 24000):
        self._cancel = cancel
        self._max_chars = max_chars
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._total = 0
        self._closed = False

    def push(self, text: str):
        with self._lock:
            if self._closed or self._cancel.is_set():
                raise ProviderFailure("ANALYSIS_CANCELLED")
            if not isinstance(text, str) or "\x00" in text:
                raise ProviderFailure("LLM_INVALID_RESULT")
            if self._total + len(text) > self._max_chars:
                raise ProviderFailure("LLM_RESPONSE_TOO_LARGE")
            if text:
                self._total += len(text)
                self._pending.append(text)

    def drain(self) -> str:
        with self._lock:
            if self._closed or self._cancel.is_set():
                self._pending.clear()
                return ""
            value = "".join(self._pending)
            self._pending.clear()
            return value

    def close(self):
        with self._lock:
            self._closed = True
            self._pending.clear()
