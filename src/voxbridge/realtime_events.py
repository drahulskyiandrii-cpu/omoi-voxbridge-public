"""OpenAI Realtime transcription protocol builders and a bounded event mapper.

Pure Python; NO network connection, microphone access, billing or model startup.
Based on official gpt-live-transcribe 24k PCM transcription-session events.
A future transport adapter must own WebSocket consent and explicit commit/VAD.
"""
from __future__ import annotations

import base64
from collections import OrderedDict, deque
import re
from typing import Any

from .captions_core import CaptionCoordinator, MAX_ORIGINAL_CHARS
from .session_limits import MAX_REALTIME_COMMITS

REALTIME_TRANSCRIPTION_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
REALTIME_MODEL = "gpt-live-transcribe"
RATE_HZ = 24000
PCM_MAX_CHUNK_BYTES = 9600
_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_OPEN_ITEMS = 12
MAX_EARLY_ITEMS = 8


class RealtimeEventError(ValueError):
    """Safe error codes; never include the audio, transcript, key or server body."""


def build_session_update(source_language: str = "de", delay: str = "low") -> dict:
    """Manual turn detection for gpt-live-transcribe (no server_vad)."""
    if source_language not in ("de", "ru", "en"):
        raise RealtimeEventError("REALTIME_INVALID_LANGUAGE")
    if delay not in ("minimal", "low", "medium", "high", "xhigh"):
        raise RealtimeEventError("REALTIME_INVALID_DELAY")
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": RATE_HZ},
                "transcription": {"model": REALTIME_MODEL, "languages": [source_language], "delay": delay},
                "turn_detection": None,
            }},
        },
    }


def build_audio_append(pcm16_24k_mono: bytes) -> dict:
    """One bounded, even-length PCM16 LE chunk. Caller owns consent/recording."""
    data = pcm16_24k_mono
    if (not isinstance(data, bytes) or not 2 <= len(data) <= PCM_MAX_CHUNK_BYTES
            or len(data) % 2):
        raise RealtimeEventError("REALTIME_INVALID_PCM")
    return {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(data).decode("ascii"),
    }


def build_audio_commit() -> dict:
    """Caller sends only after its own VAD or explicit turn-end action."""
    return {"type": "input_audio_buffer.commit"}


class RealtimeTranscriptionMapper:
    """Correlate item_id across commit/delta/completed/failed events.

    Completion events may be out of order. Hold them until prior committed
    items have finished; never pair a translation with the wrong source.
    """

    def __init__(self, controller: CaptionCoordinator, generation: int):
        if not isinstance(controller, CaptionCoordinator):
            raise RealtimeEventError("REALTIME_COORDINATOR_REQUIRED")
        if type(generation) is not int or generation < 0:
            raise RealtimeEventError("REALTIME_INVALID_GENERATION")
        self.controller = controller
        self.generation = generation
        self._closed = False
        self._next_sequence = 0
        self._order: deque[str] = deque()
        self._known: dict[str, int] = {}
        self._early: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._early_deltas: dict[str, str] = {}
        self._completed: dict[str, tuple[str, str]] = {}
        self._partials: dict[str, str] = {}
        self._revisions: dict[str, int] = {}
        self._seen_items: set[str] = set()
        self.dropped_failed_turns = 0
        self.settled_turns = 0
        self.acknowledged_turns = 0

    @staticmethod
    def _item(event: dict) -> str:
        value = event.get("item_id")
        if not isinstance(value, str) or not _ITEM_ID.fullmatch(value):
            raise RealtimeEventError("REALTIME_INVALID_ITEM_ID")
        if event.get("content_index", 0) != 0:
            raise RealtimeEventError("REALTIME_UNSUPPORTED_CONTENT_INDEX")
        return value

    @staticmethod
    def _text(value: Any) -> str:
        if not isinstance(value, str) or len(value) > MAX_ORIGINAL_CHARS or "\x00" in value:
            raise RealtimeEventError("REALTIME_INVALID_TRANSCRIPT")
        return value

    def _current(self) -> bool:
        snap = self.controller.snapshot()
        return snap.active and snap.generation == self.generation and not self._closed

    def close(self) -> None:
        was_active = self._current()
        self._closed = True
        self._order.clear()
        self._known.clear()
        self._early.clear()
        self._early_deltas.clear()
        self._completed.clear()
        self._partials.clear()
        self._revisions.clear()
        self._seen_items.clear()
        if was_active:
            self.controller.stop()

    def _set_early(self, item_id: str, kind: str, text: str = "") -> None:
        if item_id not in self._early and len(self._early) >= MAX_EARLY_ITEMS:
            raise RealtimeEventError("REALTIME_EARLY_EVENTS_LIMIT")
        self._early[item_id] = (kind, text)

    def _flush(self) -> None:
        while self._order and self._order[0] in self._completed:
            item_id = self._order.popleft()
            kind, text = self._completed.pop(item_id)
            sequence = self._known.pop(item_id)
            self._partials.pop(item_id, None)
            self._revisions.pop(item_id, None)
            if kind == "completed" and text.strip():
                self.controller.final(self.generation, sequence, text)
            else:
                self.controller.discard_turn(self.generation, sequence)
                if kind == "failed":
                    self.dropped_failed_turns += 1
            self.settled_turns += 1

    def accept(self, event: dict, *, max_acknowledgements: int = MAX_REALTIME_COMMITS,
               allow_live_preview: bool = False) -> bool:
        """Accept a relevant event, never more unique commits than locally sent."""
        if not self._current():
            return False
        if not isinstance(event, dict):
            raise RealtimeEventError("REALTIME_INVALID_EVENT")
        kind = event.get("type")
        if kind == "error":
            raise RealtimeEventError("REALTIME_REMOTE_ERROR")
        if kind == "input_audio_buffer.committed":
            item_id = self._item(event)
            if item_id in self._known or item_id in self._completed or item_id in self._seen_items:
                return False
            if self.acknowledged_turns >= min(max_acknowledgements, MAX_REALTIME_COMMITS):
                raise RealtimeEventError("REALTIME_UNEXPECTED_COMMIT")
            if len(self._order) >= MAX_OPEN_ITEMS:
                raise RealtimeEventError("REALTIME_COMMIT_BACKLOG_LIMIT")
            sequence = self._next_sequence
            self._next_sequence += 1
            self.acknowledged_turns += 1
            self._known[item_id] = sequence
            self._seen_items.add(item_id)
            self._order.append(item_id)
            # A preview has no trusted sequence until the actual ack arrives.
            self.controller.live_preview(self.generation, "")
            early_text = self._early_deltas.pop(item_id, "")
            if item_id in self._early:
                self._completed[item_id] = self._early.pop(item_id)
            elif early_text:
                self._partials[item_id] = early_text
                self._revisions[item_id] = 0
                self.controller.partial(self.generation, sequence, 0, early_text)
            self._flush()
            return True

        if kind not in {
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.input_audio_transcription.failed",
        }:
            return False
        item_id = self._item(event)
        if item_id in self._seen_items and item_id not in self._known:
            return False  # already committed and drained; ignore late repeats
        if kind.endswith(".delta"):
            delta = self._text(event.get("delta"))
            if item_id in self._completed or item_id in self._early:
                return False
            if item_id not in self._known:
                # Only a transport that has actually submitted audio may show
                # tentative pre-commit deltas. Never assign a final sequence here.
                if not allow_live_preview:
                    return False
                if item_id not in self._early_deltas and len(self._early_deltas) >= MAX_EARLY_ITEMS:
                    raise RealtimeEventError("REALTIME_EARLY_EVENTS_LIMIT")
                accumulated = self._early_deltas.get(item_id, "") + delta
                if len(accumulated) > MAX_ORIGINAL_CHARS:
                    raise RealtimeEventError("REALTIME_PARTIAL_TOO_LARGE")
                self._early_deltas[item_id] = accumulated
                preview = accumulated if len(self._early_deltas) == 1 else ""
                self.controller.live_preview(self.generation, preview)
                return True
            accumulated = self._partials.get(item_id, "") + delta
            if len(accumulated) > MAX_ORIGINAL_CHARS:
                raise RealtimeEventError("REALTIME_PARTIAL_TOO_LARGE")
            self._partials[item_id] = accumulated
            rev = self._revisions.get(item_id, -1) + 1
            self._revisions[item_id] = rev
            self.controller.partial(
                self.generation, self._known[item_id], rev, accumulated,
            )
            return True

        if kind.endswith(".completed"):
            text = self._text(event.get("transcript"))
            result = ("completed", text)
        else:
            result = ("failed", "")
        if item_id in self._known:
            if item_id in self._completed:
                return False
            self._completed[item_id] = result
            self._flush()
        else:
            if item_id in self._early:
                return False  # first terminal event wins even before ack
            self._set_early(item_id, result[0], result[1])
            self._early_deltas.pop(item_id, None)
            self.controller.live_preview(self.generation, "")
        return True
