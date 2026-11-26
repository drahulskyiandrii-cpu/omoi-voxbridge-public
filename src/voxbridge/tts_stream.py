"""Cartesia Sonic raw PCM16 SSE transport, without Qt or persistent audio files.

This module does nothing at import. The host must explicitly authorize a
cloud TTS request, provide a previously chosen owned voice ID, and execute
stream_to_callback in a non-GUI worker thread.
"""
from __future__ import annotations

import base64
import binascii
import threading
from typing import Any, Callable

from .analysis_providers import ProviderFailure
from .cloud_providers import CartesiaTTSProvider, _cloud_error
from .cloud_budget import cloud_budget

SAMPLE_RATE = 24000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
MAX_TEXT_CHARS = 2000
MAX_PCM_BYTES = 10_000_000
MAX_CHUNK_BYTES = 131_072
MAX_EVENTS = 8192


def validate_tts_text(text: str, language: str) -> None:
    if language not in {"de", "ru", "en"}:
        raise ProviderFailure("CARTESIA_INVALID_LANGUAGE")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise ProviderFailure("CARTESIA_TEXT_LIMIT")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise ProviderFailure("CARTESIA_INVALID_TEXT") from None
    if "\x00" in text:
        raise ProviderFailure("CARTESIA_INVALID_TEXT")
    if len(encoded) > 6000:
        raise ProviderFailure("CARTESIA_TEXT_BYTES_LIMIT")


def stream_to_callback(
    provider: CartesiaTTSProvider, text: str, language: str,
    on_pcm: Callable[[bytes], None],
    cancel_event: threading.Event | None = None,
) -> int:
    """Stream PCM16LE 24k mono to a caller-supplied bounded backpressure callback.

    Returns byte count, never records generated audio. The SDK stream is closed
    in finally, but a cancelled remote request may still be processed/billed.
    """
    validate_tts_text(text, language)
    if not callable(on_pcm):
        raise ProviderFailure("CARTESIA_INVALID_CALLBACK")
    if cancel_event is not None and cancel_event.is_set():
        raise ProviderFailure("CARTESIA_CANCELLED")

    stream: Any = None
    total = 0
    chunks = 0
    events_seen = 0
    finished = False
    try:
        with cloud_budget(240, cancel_event):
            stream = provider.client.tts.generate_sse(
                model_id=provider.model,
                transcript=text,
                voice=provider.voice_id,
                language=language,
                output_format={
                    "container": "raw", "encoding": "pcm_s16le",
                    "sample_rate": SAMPLE_RATE,
                },
                timeout=45,
            )
            for event in stream:
                events_seen += 1
                if events_seen > MAX_EVENTS:
                    raise ProviderFailure("CARTESIA_STREAM_TOO_MANY_EVENTS")
                if cancel_event is not None and cancel_event.is_set():
                    raise ProviderFailure("CARTESIA_CANCELLED")
                event_type = getattr(event, "type", None)
                if event_type == "done":
                    if getattr(event, "status_code", 200) >= 400:
                        raise ProviderFailure("CARTESIA_STREAM_REMOTE_ERROR")
                    finished = True
                    break
                if event_type == "error":
                    status = getattr(event, "status_code", None)
                    codes = {400:"BAD_REQUEST",401:"AUTH",402:"PAYMENT_REQUIRED",403:"PERMISSION",404:"NOT_FOUND",422:"INVALID_REQUEST",429:"RATE_LIMIT",500:"SERVER_ERROR",503:"SERVER_ERROR"}
                    suffix = codes.get(status, "REMOTE_ERROR") if type(status) is int else "REMOTE_ERROR"
                    raise ProviderFailure("CARTESIA_STREAM_" + suffix)
                if event_type != "chunk":
                    continue  # timestamps and other metadata do not contain audio
                if getattr(event, "status_code", 200) not in (200, 206):
                    raise ProviderFailure("CARTESIA_STREAM_REMOTE_ERROR")
                value = getattr(event, "data", None)
                if not isinstance(value, str) or len(value) > MAX_CHUNK_BYTES * 2:
                    raise ProviderFailure("CARTESIA_STREAM_INVALID_CHUNK")
                try:
                    pcm = base64.b64decode(value, validate=True)
                except (binascii.Error, ValueError):
                    raise ProviderFailure("CARTESIA_STREAM_INVALID_CHUNK") from None
                if not pcm or len(pcm) % BYTES_PER_SAMPLE or len(pcm) > MAX_CHUNK_BYTES:
                    raise ProviderFailure("CARTESIA_STREAM_INVALID_CHUNK")
                total += len(pcm)
                chunks += 1
                if total > MAX_PCM_BYTES or chunks > MAX_EVENTS:
                    raise ProviderFailure("CARTESIA_STREAM_TOO_LARGE")
                if cancel_event is not None and cancel_event.is_set():
                    raise ProviderFailure("CARTESIA_CANCELLED")
                on_pcm(pcm)
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderFailure("CARTESIA_CANCELLED")
            if not finished or total == 0:
                raise ProviderFailure("CARTESIA_STREAM_INCOMPLETE")
            return total
    except ProviderFailure:
        raise
    except Exception as exc:
        raise ProviderFailure(_cloud_error(exc, "CARTESIA_STREAM")) from None
    finally:
        if stream is not None:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass




def stream_bytes_to_callback(provider, text, language, on_pcm, cancel_event=None):
    """Single raw /tts/bytes request; no silent fallback/retry or saved audio."""
    validate_tts_text(text, language)
    if not callable(on_pcm):
        raise ProviderFailure("CARTESIA_INVALID_CALLBACK")
    if cancel_event is not None and cancel_event.is_set():
        raise ProviderFailure("CARTESIA_CANCELLED")
    total = 0
    tail = b""
    try:
        with cloud_budget(240, cancel_event):
            with provider.client.tts.with_streaming_response.generate(
                    model_id=provider.model, transcript=text, voice=provider.voice_id,
                    language=language, output_format={"container":"raw","encoding":"pcm_s16le","sample_rate":SAMPLE_RATE},
                    save=False, timeout=45) as response:
                for chunk in response.iter_bytes(chunk_size=8192):
                    if cancel_event is not None and cancel_event.is_set():
                        raise ProviderFailure("CARTESIA_CANCELLED")
                    if not isinstance(chunk, bytes) or len(chunk)>MAX_CHUNK_BYTES:
                        raise ProviderFailure("CARTESIA_STREAM_INVALID_CHUNK")
                    total += len(chunk)
                    if total>MAX_PCM_BYTES:
                        raise ProviderFailure("CARTESIA_STREAM_TOO_LARGE")
                    pcm=tail+chunk
                    even=len(pcm)-len(pcm)%2
                    tail=pcm[even:]
                    if even:
                        on_pcm(pcm[:even])
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderFailure("CARTESIA_CANCELLED")
            if tail or not total:
                raise ProviderFailure("CARTESIA_STREAM_INCOMPLETE")
            return total
    except ProviderFailure:
        raise
    except Exception as exc:
        raise ProviderFailure(_cloud_error(exc, "CARTESIA_BYTES")) from None


class BoundedPCMBuffer:
    """Owner-thread queue of PCM bytes, with deterministic overflow rejection."""

    def __init__(self, capacity: int = 192_000):
        if capacity <= 0 or capacity % BYTES_PER_SAMPLE:
            raise ValueError("INVALID_PCM_CAPACITY")
        self.capacity = capacity
        self._data = bytearray()

    def __len__(self) -> int:
        return len(self._data)

    def push(self, pcm: bytes) -> None:
        if (not isinstance(pcm, bytes) or len(pcm) % BYTES_PER_SAMPLE or
                len(self._data) + len(pcm) > self.capacity):
            raise ProviderFailure("PCM_BACKPRESSURE_OVERFLOW")
        self._data.extend(pcm)

    def peek(self, byte_count: int) -> bytes:
        byte_count -= byte_count % BYTES_PER_SAMPLE
        return bytes(self._data[:max(0, byte_count)])

    def discard(self, byte_count: int) -> None:
        if byte_count < 0 or byte_count > len(self._data):
            raise ValueError("INVALID_PCM_DISCARD")
        del self._data[:byte_count]

    def clear(self) -> None:
        self._data.clear()


MAX_READ_CHARS = 12000

def split_read_text(text, language, *, limit=1000):
    """Preserve all nonwhitespace content; partition before any request."""
    if not isinstance(text,str) or not text.strip() or len(text)>MAX_READ_CHARS:
        raise ProviderFailure("TTS_SELECTION_LIMIT_12000")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ProviderFailure("CARTESIA_INVALID_TEXT") from None
    if "\x00" in text:
        raise ProviderFailure("CARTESIA_INVALID_TEXT")
    if type(limit) is not int or not 100 <= limit <= 1000:
        raise ValueError("INVALID_TTS_CHUNK_LIMIT")
    chunks=[]
    minimum = limit // 2
    while text:
        if len(text)<=limit:
            chunk,text=text,""
        else:
            boundary=max(text.rfind(mark,minimum,limit) for mark in ("\n",". ","! ","? "))
            end=boundary+1 if boundary>=minimum else text.rfind(" ",minimum,limit)+1
            if end<minimum:
                end=limit
            chunk,text=text[:end],text[end:]
        if chunk.strip():
            validate_tts_text(chunk,language)
            chunks.append(chunk)
    return tuple(chunks)
