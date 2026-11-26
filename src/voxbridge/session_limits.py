"""Deterministic per-session cloud-request quotas; NOT a USD billing guarantee.

Counts all attempted translation calls (including failed requests) BEFORE the
provider call. It never stores text, voice, credentials or user prompt content.
OpenAI / external provider pricing is not assumed to be fixed.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading

from .captions_core import TranslationJob

MAX_SESSION_TRANSLATION_CALLS = 12
MAX_TOTAL_INPUT_CHARS = 12000
MAX_SINGLE_INPUT_CHARS = 4000
MAX_TRANSLATION_OUTPUT_TOKENS = 256


class SessionQuotaExceeded(RuntimeError):
    """Safe local quota error code; no transcript or prompt included."""


class TranslationQuota:
    def __init__(
        self, *,
        max_calls: int = MAX_SESSION_TRANSLATION_CALLS,
        max_input_chars: int = MAX_TOTAL_INPUT_CHARS,
        max_one_input_chars: int = MAX_SINGLE_INPUT_CHARS,
    ):
        if type(max_calls) is not int or not 1 <= max_calls <= 12:
            raise ValueError("INVALID_TRANSLATION_QUOTA")
        if type(max_input_chars) is not int or not 512 <= max_input_chars <= 12000:
            raise ValueError("INVALID_TRANSLATION_QUOTA")
        if type(max_one_input_chars) is not int or not 100 <= max_one_input_chars <= 4000:
            raise ValueError("INVALID_TRANSLATION_QUOTA")
        self.max_calls = max_calls
        self.max_input_chars = max_input_chars
        self.max_one_input_chars = max_one_input_chars
        self._lock = threading.Lock()
        self._calls = 0
        self._input_chars = 0
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True

    def reserve(self, job: TranslationJob, *, stop: threading.Event) -> bool:
        """Reserve immediately before a remote request; no refunds on error."""
        if not isinstance(job, TranslationJob):
            raise ValueError("INVALID_TRANSLATION_JOB")
        if not isinstance(stop, threading.Event):
            raise ValueError("INVALID_STOP_EVENT")
        # This counts raw user text + retained history. System prompt overhead
        # and provider tokenization are *not* included.
        char_count = len(job.text) + sum(map(len, job.context))
        with self._lock:
            if self._closed or stop.is_set() or job.cancel_event.is_set():
                raise SessionQuotaExceeded("TRANSLATION_SESSION_CANCELLED")
            if (char_count > self.max_one_input_chars or
                    char_count < 1 or self._calls >= self.max_calls or
                    self._input_chars + char_count > self.max_input_chars):
                return False
            self._calls += 1
            self._input_chars += char_count
            return True

    @property
    def calls_reserved(self) -> int:
        with self._lock:
            return self._calls

    @property
    def input_chars_reserved(self) -> int:
        with self._lock:
            return self._input_chars


# Local attempt counters, not provider-reported usage or billing.
MAX_REALTIME_COMMITS = 120
MAX_UNSETTLED_COMMITS = 12
REALTIME_SAMPLE_RATE = 24000


@dataclass(frozen=True)
class RealtimeUsageSnapshot:
    attempted_connections: int
    attempted_audio_samples: int
    attempted_commits: int

    @property
    def attempted_audio_seconds(self) -> float:
        return self.attempted_audio_samples / REALTIME_SAMPLE_RATE


class RealtimeUsage:
    """Single-use, thread-safe local budget; never refunds failed sends."""
    def __init__(self, seconds: int, *, max_commits: int = MAX_REALTIME_COMMITS):
        if type(seconds) is not int or not 1 <= seconds <= 120:
            raise ValueError("INVALID_REALTIME_QUOTA")
        if type(max_commits) is not int or not 1 <= max_commits <= MAX_REALTIME_COMMITS:
            raise ValueError("INVALID_REALTIME_QUOTA")
        self.seconds = seconds
        self.max_commits = max_commits
        self._lock = threading.Lock()
        self._connections = self._samples = self._commits = 0

    def claim(self, seconds: int) -> None:
        with self._lock:
            if seconds != self.seconds or self._connections:
                raise SessionQuotaExceeded("REALTIME_USAGE_ALREADY_CLAIMED")
            self._connections = 1

    def reserve_audio(self, samples: int) -> None:
        if type(samples) is not int or not 1 <= samples <= 4800:
            raise ValueError("INVALID_REALTIME_AUDIO_RESERVATION")
        with self._lock:
            if not self._connections or self._samples + samples > self.seconds * REALTIME_SAMPLE_RATE:
                raise SessionQuotaExceeded("REALTIME_AUDIO_QUOTA")
            self._samples += samples

    def reserve_commit(self) -> None:
        with self._lock:
            if not self._connections or self._commits >= self.max_commits:
                raise SessionQuotaExceeded("REALTIME_COMMIT_QUOTA")
            self._commits += 1

    def snapshot(self) -> RealtimeUsageSnapshot:
        with self._lock:
            return RealtimeUsageSnapshot(self._connections, self._samples, self._commits)
