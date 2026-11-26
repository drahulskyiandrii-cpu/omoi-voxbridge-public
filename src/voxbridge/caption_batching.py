"""Experimental, deterministic translation batching; not wired to live captions.

Only finalized source segments enter. Each input appears once, in order.
A caller must publish original text independently and tick the deadline.
"""
from dataclasses import dataclass, field
import re

from .captions_core import CaptionError

_ABBREVIATIONS = frozenset(("dr", "prof", "nr", "ca", "usw", "bzw", "ggf",
                            "z.b", "d.h", "u.a", "vgl", "etc", "т.д", "т.п", "им"))
_CLOSERS = "\"'»”’)]}"


def ends_sentence(text: str) -> bool:
    value = text.rstrip().rstrip(_CLOSERS + "“„")
    if not value:
        return False
    if value.endswith(("?", "!")):
        return True
    if not value.endswith(".") or value.endswith("..."):
        return False
    token = value[:-1].split()[-1] if value[:-1].split() else ""
    token = token.lstrip("([\"'").lower()
    if not token or token in _ABBREVIATIONS or len(token) == 1 or token.isdecimal():
        return False
    if re.fullmatch(r"(?:\d+\.)+\d+", token) or "://" in token or "@" in token:
        return False
    return True


@dataclass(frozen=True)
class FinalFragment:
    sequence: int
    at_ms: int
    text: str = field(repr=False)


@dataclass(frozen=True)
class TranslationBatch:
    fragments: tuple[FinalFragment, ...] = field(repr=False)
    released_ms: int
    reason: str

    @property
    def text(self):
        return " ".join(f.text for f in self.fragments)

    @property
    def sequences(self):
        return tuple(f.sequence for f in self.fragments)


class SentenceBatcher:
    """Bounded wait for a sentence boundary; never splits or rewrites a final."""
    def __init__(self, *, wait_ms=900, max_chars=600, max_fragments=3):
        if (type(wait_ms) is not int or not 0 <= wait_ms <= 2000 or
                type(max_chars) is not int or not 64 <= max_chars <= 2000 or
                type(max_fragments) is not int or not 1 <= max_fragments <= 4):
            raise CaptionError("BATCH_LIMITS")
        self.wait_ms, self.max_chars, self.max_fragments = wait_ms, max_chars, max_fragments
        self._pending = []
        self._last_sequence = -1
        self._clock = -1
        self._closed = False

    def _advance(self, now_ms):
        if self._closed or type(now_ms) is not int or now_ms < self._clock or now_ms < 0:
            raise CaptionError("BATCH_CLOCK_OR_CLOSED")
        self._clock = now_ms

    def _flush(self, reason):
        if not self._pending:
            return []
        batch = TranslationBatch(tuple(self._pending), self._clock, reason)
        self._pending.clear()
        return [batch]

    @property
    def deadline_ms(self):
        return self._pending[0].at_ms + self.wait_ms if self._pending else None

    def tick(self, now_ms):
        self._advance(now_ms)
        if self.deadline_ms is not None and now_ms >= self.deadline_ms:
            return self._flush("deadline")
        return []

    def feed(self, fragment: FinalFragment):
        if (not isinstance(fragment, FinalFragment) or type(fragment.sequence) is not int
                or fragment.sequence <= self._last_sequence or
                not isinstance(fragment.text, str) or not fragment.text.strip()
                or len(fragment.text) > 2000 or "\x00" in fragment.text):
            raise CaptionError("BATCH_INVALID_FRAGMENT")
        out = self.tick(fragment.at_ms)
        if self._pending and (
            fragment.sequence != self._last_sequence + 1 or
            len(" ".join(f.text for f in self._pending)) + 1 + len(fragment.text) > self.max_chars
        ):
            out += self._flush("gap_or_size")
        self._last_sequence = fragment.sequence
        self._pending.append(fragment)
        if ends_sentence(fragment.text):
            out += self._flush("sentence")
        elif len(self._pending) >= self.max_fragments or len(fragment.text) >= self.max_chars:
            out += self._flush("limit")
        elif self.wait_ms == 0:
            out += self._flush("deadline")
        return out

    def finish(self, now_ms):
        out = self.tick(now_ms)
        out += self._flush("end")
        self._closed = True
        return out

    def cancel(self):
        self._pending.clear()
        self._closed = True
