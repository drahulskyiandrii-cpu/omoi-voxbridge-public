"""Long dictation: monotonic non-overlapping speech chunks, RAM only.

A committed prefix is never decoded again. Cut in a quiet 120ms valley
within a 16..20 second window, leaving at least one second of lookahead.
Every request stays within the existing 30-second provider contract.
"""
from __future__ import annotations
import array
import io
import sys
import threading
import time
import wave

from .audio import RATE, MAX_RECORD_SECONDS, MAX_SEGMENT_SECONDS, measure_pcm, pcm_to_wav
from .dictation_quality import MAX_RECORD_TEXT, inspect_transcript
from .stt import SpeechFailure
from .stt_enhancements import condition_pcm

FRAME_BYTES = RATE * 2
TARGET_BYTES = 20 * FRAME_BYTES
MIN_CUT_BYTES = 16 * FRAME_BYTES
LOOKAHEAD_BYTES = FRAME_BYTES
FRAME = RATE // 50 * 2
EARLY_MIN_BYTES = 6 * FRAME_BYTES
EARLY_MAX_BYTES = 10 * FRAME_BYTES


def recording_pcm(wav):
    if not isinstance(wav, bytes) or not 44 <= len(wav) <= MAX_RECORD_SECONDS * FRAME_BYTES + 4096:
        raise SpeechFailure("INVALID_WAV_LENGTH")
    try:
        with wave.open(io.BytesIO(wav), "rb") as reader:
            count = reader.getnframes()
            if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth(),
                    reader.getcomptype()) != (RATE, 1, 2, "NONE") or not 1600 <= count <= MAX_RECORD_SECONDS * RATE:
                raise SpeechFailure("INVALID_AUDIO_FORMAT")
            data = reader.readframes(count)
            if len(data) != count * 2:
                raise SpeechFailure("INVALID_WAV_LENGTH")
            return data
    except (wave.Error, EOFError):
        raise SpeechFailure("INVALID_WAV_FORMAT") from None


def quiet_cut(pcm, offset):
    """Prefer quiet context on both sides rather than a single quiet frame."""
    half = 3  # three 20ms frames on each side
    start = offset + MIN_CUT_BYTES - half * FRAME
    end = offset + TARGET_BYTES + half * FRAME
    samples = array.array("h", pcm[start:end])
    if sys.byteorder != "little":
        samples.byteswap()
    width = FRAME // 2
    energy = []
    for i in range(0, len(samples), width):
        frame = samples[i:i + width]
        if len(frame) != width:
            raise ValueError("INCOMPLETE_BOUNDARY_WINDOW")
        total = sum(frame)
        energy.append(sum(x*x for x in frame)*width - total*total)
    def score(i):
        context = energy[i-half:i+half]
        return max(context), sum(context), -i
    i = min(range(half, len(energy)-half+1), key=score)
    return start + i*FRAME

class IncompleteDictation(SpeechFailure):
    """Only confirmed earlier chunks; caller may offer manual copy, never insert."""
    def __init__(self, code, text):
        super().__init__(code)
        self.text = text


class ChunkedRecording:
    def __init__(self, decode, language, cancelled, *, conditioning=False):
        self.decode = decode
        self.language = language
        self.cancelled = cancelled
        self.conditioning = conditioning
        self.offset = 0
        self.parts = []
        self.decoded = 0

    def check(self):
        if self.cancelled():
            raise SpeechFailure("STT_CANCELLED")

    @property
    def text(self):
        return " ".join(self.parts)

    def consume(self, wav, *, final=False, on_update=None):
        try:
            return self._consume(wav, final=final, on_update=on_update)
        except (SpeechFailure, RuntimeError) as exc:
            self.check()
            if self.parts:
                code = str(exc) if isinstance(exc, SpeechFailure) else "STT_SEGMENT_FAILED"
                raise IncompleteDictation(code, self.text) from None
            raise

    def _consume(self, wav, *, final=False, on_update=None):
        self.check()
        pcm = recording_pcm(wav)
        if len(pcm) < self.offset:
            raise SpeechFailure("STT_AUDIO_SHRANK")
        while True:
            self.check()
            remaining = len(pcm) - self.offset
            # Finish short recordings/tails intact. Split only when needed.
            if final and remaining <= MAX_SEGMENT_SECONDS * FRAME_BYTES:
                end = len(pcm)
            elif remaining >= TARGET_BYTES + LOOKAHEAD_BYTES:
                end = quiet_cut(pcm, self.offset)
            else:
                break
            if end == self.offset:
                break
            chunk = pcm[self.offset:end]
            quality = measure_pcm(chunk)
            changed = False
            if quality.code == "MIC_SIGNAL_OK":
                data = condition_pcm(chunk) if self.conditioning else chunk
                text = self.decode(pcm_to_wav(data), self.language)
                self.check()
                if inspect_transcript(text, quality.seconds) != "STT_TEXT_OK":
                    raise SpeechFailure("STT_SEGMENT_INVALID")
                if len(self.text) + len(text) + 1 > MAX_RECORD_TEXT:
                    raise SpeechFailure("STT_RECORD_TEXT_LIMIT")
                self.parts.append(text.strip())
                self.decoded += 1
                changed = True
            elif quality.code != "MICROPHONE_SIGNAL_TOO_QUIET":
                # No silent truncation of clipped or malformed speech.
                raise SpeechFailure(quality.code)
            self.offset = end
            if on_update and changed:
                on_update(self.text)
        self.check()
        if final and not self.parts:
            raise SpeechFailure("STT_TEXT_EMPTY")
        return self.text


class LongDictationTurn:
    """Decode committed segments during capture and the tail after Stop."""
    def __init__(self, backend, snapshot, language, on_partial, *, keep_resident=False,
                 conditioning=False, interval=.5, native_preview=False):
        self.backend = backend
        self.snapshot = snapshot
        self.on_partial = on_partial
        self.keep_resident = keep_resident
        self.interval = interval
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._cancel = threading.Event()
        self._final = None
        self._confirmed = ""
        self._native_preview = native_preview
        self._early_attempted = False
        self._early_decoded = False
        self._early_wav = None
        self._early_text = ""
        self._conditioning = conditioning
        self._last_preview = ""
        self._first_preview_ms = None
        self._started = time.monotonic()
        self._chunks = ChunkedRecording(self._decode_segment, language, self._cancel.is_set,
                                        conditioning=conditioning)

    def _publish(self, text, *, confirmed=False):
        with self._lock:
            if self._cancel.is_set():
                return
            if confirmed:
                self._confirmed = text
            if self._final is not None or text == self._last_preview:
                return
            self._last_preview = text
            if self._first_preview_ms is None:
                self._first_preview_ms = round((time.monotonic()-self._started)*1000)
        self.on_partial(text)

    def _draft(self, text):
        # Streamed text is only a provisional suffix. Never commit it or
        # include it in the manual recovery prefix on a device failure.
        if inspect_transcript(text) != "STT_TEXT_OK":
            return
        combined = " ".join(part for part in (self._chunks.text, text.strip()) if part)
        if inspect_transcript(combined, max_chars=MAX_RECORD_TEXT) == "STT_TEXT_OK":
            self._publish(combined)

    def _decode_segment(self, wav, language):
        # A speculative result can be reused only for identical input,
        # conditioning and final-quality decode on the same backend.
        early_wav, early_text = self._early_wav, self._early_text
        self._early_wav, self._early_text = None, ""
        if early_wav == wav and early_text:
            self._chunks.check()
            return early_text
        return self.backend.transcribe_final(wav, language)

    def _try_early_preview(self, wav):
        if not self._native_preview or self._early_attempted:
            return
        pcm = recording_pcm(wav)
        if len(pcm) < EARLY_MIN_BYTES:
            return
        if len(pcm) > EARLY_MAX_BYTES or self._chunks.offset:
            self._early_attempted = True
            return
        quality = measure_pcm(pcm)
        if quality.code != "MIC_SIGNAL_OK":
            return
        with self._lock:
            if self._final is not None or self._cancel.is_set():
                return
        self._early_attempted = True
        self._early_decoded = True
        audio = pcm_to_wav(condition_pcm(pcm) if self._conditioning else pcm)
        text = self.backend.transcribe_final(audio, self._chunks.language)
        self._chunks.check()
        if inspect_transcript(text, quality.seconds) == "STT_TEXT_OK":
            self._early_wav, self._early_text = audio, text
            self._draft(text)

    def finalize(self, wav):
        # Validate complete capture before publishing it to the worker.
        recording_pcm(wav)
        with self._lock:
            if self._cancel.is_set() or self._final is not None:
                return False
            self._final = wav
        self._wake.set()
        return True

    def cancel(self):
        self._cancel.set()
        with self._lock:
            self._final = None
            self._confirmed = ""
        self.backend.cancel()
        self._wake.set()

    def confirmed_text(self):
        with self._lock:
            return self._confirmed

    def summary(self):
        return (f"Long dictation: {self._chunks.decoded} decoded segments; "
                f"early decode={int(self._early_decoded)}; "
                f"first={self._first_preview_ms if self._first_preview_ms is not None else '-'}ms")

    def _failure(self, exc, fallback):
        # Never expose an unconfirmed draft or private exception text.
        code = str(exc) if isinstance(exc, SpeechFailure) else fallback
        if self._cancel.is_set():
            return SpeechFailure("STT_CANCELLED" if fallback == "STT_ERROR" else code)
        if self._chunks.text:
            return IncompleteDictation(code, self._chunks.text)
        return SpeechFailure(code)

    def run(self):
        success = False
        previous_delta = None
        if self._native_preview:
            previous_delta = self.backend.on_delta
            self.backend.on_delta = self._draft
        try:
            self.backend.ensure()
            while True:
                self._chunks.check()
                with self._lock:
                    final = self._final
                if final is not None:
                    result = self._chunks.consume(final, final=True)
                    success = True
                    return result
                wav = self.snapshot()
                if wav:
                    self._try_early_preview(wav)
                    # Stop may have arrived while the speculative decode ran.
                    # Do not start a new preview decode in that case.
                    with self._lock:
                        pending = self._final is not None or self._cancel.is_set()
                    if not pending:
                        self._chunks.consume(wav, on_update=lambda text: self._publish(text, confirmed=True))
                self._wake.wait(self.interval)
                self._wake.clear()
        except Exception as exc:
            raise self._failure(exc, "STT_ERROR") from None
        finally:
            with self._lock:
                self._final = None
                self._last_preview = ""
            self._early_wav, self._early_text = None, ""
            if self._native_preview:
                self.backend.on_delta = previous_delta
            try:
                if not (success and self.keep_resident and not self._cancel.is_set()):
                    self.backend.close()
            except Exception as exc:
                # A cleanup failure is still a failure, even if all audio was
                # decoded. Preserve only the verified text for manual copying.
                raise self._failure(exc, "STT_BACKEND_CLEANUP_FAILED") from None
            finally:
                if self._cancel.is_set():
                    self._chunks.parts.clear()
