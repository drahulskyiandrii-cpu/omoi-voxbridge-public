"""Bounded, thread-safe caption and translation coordinator (no audio or network).

Protocol-independent boundary between future live STT, translation and Qt overlay.
Only committed STT segments may be translated. Partial text remains tentative.
All private text lives in RAM and is never logged or persisted by this module.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import threading
import time

MAX_ORIGINAL_CHARS = 2000
MAX_TRANSLATION_CHARS = 3000
MAX_SEQUENCE = 2**31 - 1


class CaptionError(ValueError):
    """Stable safe error codes; don't embed user content."""


@dataclass(frozen=True)
class CaptionLine:
    sequence: int
    original: str = field(repr=False)
    translation: str | None = field(default=None, repr=False)
    status: str = "pending"  # pending, running, complete, skipped, failed, original_only


@dataclass(frozen=True)
class CaptionSnapshot:
    generation: int
    active: bool
    source_language: str
    target_language: str | None
    lines: tuple[CaptionLine, ...] = field(repr=False)
    partial: str = field(repr=False)
    pending_count: int = 0
    translation_running: bool = False


@dataclass(frozen=True)
class TranslationJob:
    generation: int
    sequence: int
    source_language: str
    target_language: str
    text: str = field(repr=False)
    context: tuple[str, ...] = field(repr=False)
    cancel_event: threading.Event = field(repr=False, compare=False)
    sequences: tuple[int, ...] = ()


class CaptionCoordinator:
    """One active session, <=2 waiting translations, <=1 in-flight job.

    Caller owns consent, audio devices and async worker scheduling. Nothing in
    this coordinator invokes an LLM or reads a microphone/screen/clipboard.
    """

    def __init__(self, *, max_visible: int = 6, max_pending: int = 2,
                 context_segments: int = 3, clock=time.monotonic):
        if not 2 <= max_visible <= 20:
            raise CaptionError("CAPTIONS_INVALID_VISIBLE_LIMIT")
        if not 1 <= max_pending <= 2:
            raise CaptionError("CAPTIONS_INVALID_QUEUE_LIMIT")
        if not 0 <= context_segments <= 3:
            raise CaptionError("CAPTIONS_INVALID_CONTEXT_LIMIT")
        self.max_visible = max_visible
        self.max_pending = max_pending
        self.context_segments = context_segments
        self._lock = threading.RLock()
        self.translation_ready = threading.Event()
        self._generation = 0
        self._active = False
        self._source = "de"
        self._target: str | None = None
        self._committed: OrderedDict[int, CaptionLine] = OrderedDict()
        self._last_final = -1
        self._partial_seq = -1
        self._partial_revision = -1
        self._partial = ""
        self._live_preview = ""
        self._waiting: deque[tuple[int, ...]] = deque()
        self._batcher = None
        self._clock = clock
        self._source_ended = False
        self._translation_disabled = False
        self._running: TranslationJob | None = None

    @staticmethod
    def _check_text(value: str, max_chars: int) -> str:
        if not isinstance(value, str) or len(value) > max_chars:
            raise CaptionError("CAPTIONS_INVALID_TEXT")
        if "\x00" in value:
            raise CaptionError("CAPTIONS_INVALID_TEXT")
        return value.strip()

    @staticmethod
    def _check_sequence(sequence: int) -> None:
        if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
            raise CaptionError("CAPTIONS_INVALID_SEQUENCE")

    def begin(self, *, source_language: str = "de", target_language: str | None = "ru", group_sentences: bool = False) -> int:
        if not isinstance(source_language, str) or source_language not in {"de", "ru", "en"}:
            raise CaptionError("CAPTIONS_INVALID_SOURCE_LANGUAGE")
        if target_language is not None and (
            not isinstance(target_language, str) or target_language not in {"de", "ru", "en"}
        ):
            raise CaptionError("CAPTIONS_INVALID_TARGET_LANGUAGE")
        if target_language == source_language:
            raise CaptionError("CAPTIONS_SAME_TRANSLATION_LANGUAGE")
        if type(group_sentences) is not bool:
            raise CaptionError("CAPTIONS_INVALID_GROUPING")
        with self._lock:
            self._stop_locked()
            self._generation += 1
            self._source, self._target = source_language, target_language
            self._active = True
            if group_sentences and target_language is not None:
                from .caption_batching import SentenceBatcher
                self._batcher = SentenceBatcher(wait_ms=600, max_fragments=min(3, self.max_visible))
            self.translation_ready.clear()
            return self._generation

    def _stop_locked(self) -> None:
        if self._running is not None:
            self._running.cancel_event.set()
        self._running = None
        self._waiting.clear()
        self._batcher = None
        self._source_ended = False
        self._translation_disabled = False
        self._committed.clear()
        self._partial, self._partial_seq, self._partial_revision = "", -1, -1
        self._last_final = -1
        self._live_preview = ""
        self._active = False

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
            self._generation += 1  # fence stale events from the prior stream
            self.translation_ready.set()

    def abort(self, generation: int) -> bool:
        """Fence failed input and translations, retaining only finalized rows."""
        with self._lock:
            if not self._accepts(generation):
                return False
            if self._running is not None:
                self._running.cancel_event.set()
                self._mark_group(self._job_sequences(self._running), "skipped")
                self._running = None
            self._waiting.clear()
            self._batcher = None
            for sequence, line in tuple(self._committed.items()):
                if line.status in ("pending", "running"):
                    self._change_status(sequence, "skipped")
            self._partial, self._partial_seq, self._partial_revision = "", -1, -1
            self._live_preview = ""
            self._active = False
            self.translation_ready.set()
            return True

    def _accepts(self, generation: int) -> bool:
        return self._active and type(generation) is int and generation == self._generation

    def partial(self, generation: int, sequence: int, revision: int, text: str) -> bool:
        """Only latest revision of future uncommitted segment is displayable."""
        self._check_sequence(sequence)
        if type(revision) is not int or revision < 0:
            raise CaptionError("CAPTIONS_INVALID_REVISION")
        cleaned = self._check_text(text, MAX_ORIGINAL_CHARS)
        with self._lock:
            if not self._accepts(generation) or sequence <= self._last_final:
                return False
            if (sequence < self._partial_seq or
                    (sequence == self._partial_seq and revision <= self._partial_revision)):
                return False
            self._partial_seq, self._partial_revision = sequence, revision
            self._partial = cleaned
            return True

    def live_preview(self, generation: int, text: str) -> bool:
        """Tentative unacknowledged stream; never a translation or final row."""
        cleaned = self._check_text(text, MAX_ORIGINAL_CHARS)
        with self._lock:
            if not self._accepts(generation):
                return False
            self._live_preview = cleaned
            return True

    def final(self, generation: int, sequence: int, text: str) -> bool:
        """Commit only strictly increasing originals; keep the latest ones visible."""
        self._check_sequence(sequence)
        cleaned = self._check_text(text, MAX_ORIGINAL_CHARS)
        if not cleaned:
            return False
        with self._lock:
            if not self._accepts(generation) or self._source_ended or sequence <= self._last_final:
                return False
            self._last_final = sequence
            if self._partial_seq <= sequence:
                self._partial, self._partial_seq, self._partial_revision = "", -1, -1
            status = ("original_only" if self._target is None else
                      "failed" if self._translation_disabled else "pending")
            self._committed[sequence] = CaptionLine(sequence, cleaned, status=status)
            if status == "pending":
                if self._batcher is not None:
                    from .caption_batching import FinalFragment
                    self._enqueue_batches(self._batcher.feed(
                        FinalFragment(sequence, self._now_ms(), cleaned)))
                else:
                    self._enqueue((sequence,))
                self.translation_ready.set()
            while len(self._committed) > self.max_visible:
                old_seq, _ = self._committed.popitem(last=False)
                for group in tuple(self._waiting):
                    if old_seq in group:
                        self._waiting.remove(group)
                        self._mark_group(group, "skipped")
                if self._running is not None and old_seq in self._job_sequences(self._running):
                    self._running.cancel_event.set()
                    self._mark_group(self._job_sequences(self._running), "skipped")
            return True

    def discard_turn(self, generation: int, sequence: int) -> bool:
        """Retire a failed/empty final without creating a row or translation."""
        self._check_sequence(sequence)
        with self._lock:
            if not self._accepts(generation) or sequence <= self._last_final:
                return False
            self._last_final = sequence
            if self._partial_seq <= sequence:
                self._partial, self._partial_seq, self._partial_revision = "", -1, -1
            return True

    def _change_status(self, sequence: int, status: str, translation: str | None = None) -> None:
        old = self._committed.get(sequence)
        if old is not None:
            self._committed[sequence] = CaptionLine(
                old.sequence, old.original, translation=translation, status=status,
            )

    def _now_ms(self):
        return int(self._clock() * 1000)

    @staticmethod
    def _job_sequences(job):
        return job.sequences or (job.sequence,)

    def _mark_group(self, sequences, status):
        for sequence in sequences:
            self._change_status(sequence, status)

    def _enqueue(self, group):
        if not all(seq in self._committed for seq in group):
            self._mark_group(group, "skipped")
            return
        self._waiting.append(group)
        while len(self._waiting) > self.max_pending:
            self._mark_group(self._waiting.popleft(), "skipped")

    def _enqueue_batches(self, batches):
        for batch in batches:
            self._enqueue(batch.sequences)

    def translation_wait_seconds(self):
        """Called by worker after checking queue. Event also wakes on new final."""
        with self._lock:
            deadline = self._batcher.deadline_ms if self._batcher is not None else None
            return None if deadline is None else max(0, (deadline - self._now_ms()) / 1000)

    def finish_source(self, generation):
        """Flush trailing unfinished sentence before signalling source completion."""
        with self._lock:
            if not self._accepts(generation) or self._source_ended:
                return
            self._source_ended = True
            if self._batcher is not None:
                self._enqueue_batches(self._batcher.finish(self._now_ms()))
                self._batcher = None
            self.translation_ready.set()

    def disable_translation(self, generation):
        """Keep originals live after permanent local engine failure; never retry."""
        with self._lock:
            if not self._accepts(generation):
                return
            self._translation_disabled = True
            self._batcher = None
            self._waiting.clear()
            if self._running is not None:
                self._running.cancel_event.set()
                self._running = None
            for seq, line in tuple(self._committed.items()):
                if line.status in ("pending", "running"):
                    self._change_status(seq, "failed")
            self.translation_ready.set()

    def take_translation(self) -> TranslationJob | None:
        """Pop one bounded group only when no other translation is running."""
        with self._lock:
            if not self._active or self._target is None or self._translation_disabled:
                return None
            if self._batcher is not None:
                self._enqueue_batches(self._batcher.tick(self._now_ms()))
            if self._running is not None:
                return None
            while self._waiting:
                group = self._waiting.popleft()
                lines = [self._committed.get(seq) for seq in group]
                if any(line is None or line.status != "pending" for line in lines):
                    self._mark_group(group, "skipped")
                    continue
                previous = [x.original for k, x in self._committed.items() if k < group[0]]
                context = tuple(previous[-self.context_segments:]) if self.context_segments else ()
                job = TranslationJob(
                    generation=self._generation, sequence=group[-1],
                    source_language=self._source, target_language=self._target,
                    text=" ".join(line.original for line in lines), context=context,
                    cancel_event=threading.Event(), sequences=group if len(group)>1 else (),
                )
                self._running = job
                self._mark_group(group, "running")
                return job
            return None

    def finish_translation(self, job: TranslationJob, text: str = "",
                           *, failed: bool = False) -> bool:
        """Apply current matching job only; drop stale, cancelled or missing rows."""
        if not isinstance(job, TranslationJob):
            raise CaptionError("CAPTIONS_INVALID_JOB")
        if type(failed) is not bool:
            raise CaptionError("CAPTIONS_INVALID_RESULT")
        cleaned = self._check_text(text, MAX_TRANSLATION_CHARS)
        with self._lock:
            if job is not self._running:
                return False
            self._running = None
            group = self._job_sequences(job)
            if (not self._accepts(job.generation) or job.cancel_event.is_set() or
                    any(seq not in self._committed for seq in group)):
                self._mark_group(group, "skipped")
                return False
            if failed or not cleaned:
                self._mark_group(group, "failed")
            else:
                self._mark_group(group[:-1], "grouped")
                self._change_status(job.sequence, "complete", cleaned)
            return True

    def snapshot(self) -> CaptionSnapshot:
        with self._lock:
            return CaptionSnapshot(
                generation=self._generation, active=self._active,
                source_language=self._source, target_language=self._target,
                lines=tuple(self._committed.values()), partial=self._live_preview or self._partial,
                pending_count=len(self._waiting),
                translation_running=self._running is not None,
            )
