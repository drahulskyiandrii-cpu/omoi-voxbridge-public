"""Authenticated loopback PCM streaming adapter used by the owned Qwen runtime.

The adapter does not start a model. Its caller verifies service identity and
authorization before sending text.
"""
from __future__ import annotations

import http.client
import json
import os
import struct
import threading
import time
from typing import Callable

from .loopback_http import loopback_response, CONNECT_SECONDS

MODEL = "qwen3-tts-1.7b-base"
CF50_MODEL = MODEL + "-cf050"
PCM_RATE = 24000
MAX_TEXT_BYTES = 4096
MAX_FRAME_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 10_000_000
MAX_FRAMES = 8192
CALLBACK_CHUNK_BYTES = 32768


class LocalTTSFailure(RuntimeError):
    """Stable safe code; never includes input, token, voice or response body."""


class SpeechCoreTTS:
    """One request to a configured, authorized loopback service; never starts it."""

    STREAM_SECONDS = 90.0

    def __init__(self, *, port: int, token: str | None = None,
                 connection_factory=http.client.HTTPConnection, model=MODEL, peer=None):
        if model not in (MODEL, CF50_MODEL):
            raise LocalTTSFailure("LOCAL_TTS_MODEL_INVALID")
        self._model = model
        if type(port) is not int or not 1024 <= port <= 65535:
            raise LocalTTSFailure("LOCAL_TTS_INVALID_PORT")
        self._port = port
        self._token = token
        self._peer = peer
        if peer is not None and peer.port != port:
            raise LocalTTSFailure("LOCAL_TTS_PEER_PORT_MISMATCH")
        self._factory = connection_factory
        self._lock = threading.Lock()

    def stream(self, text: str, language: str, on_pcm: Callable[[bytes], None],
               *, authorized: bool, stop: threading.Event) -> int:
        """Return streamed PCM byte count; user must authorize local inference.

        Authenticated POST only to 127.0.0.1. The inspected protocol does not
        establish an authenticated preflight identity endpoint; callers must
        separately verify the service/port before enabling this adapter.
        Cancellation fences late PCM, but cannot guarantee remote GPU abort.
        """
        if authorized is not True:
            raise LocalTTSFailure("LOCAL_TTS_AUTHORIZATION_REQUIRED")
        if not isinstance(stop, threading.Event) or stop.is_set():
            raise LocalTTSFailure("LOCAL_TTS_CANCELLED")
        if (language not in ("de", "ru", "en") or not callable(on_pcm)
                or not isinstance(text, str) or not text.strip() or "\x00" in text
                or len(text) > 2000):
            raise LocalTTSFailure("LOCAL_TTS_INVALID_INPUT")
        try:
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise LocalTTSFailure("LOCAL_TTS_INVALID_INPUT")
        except UnicodeError:
            raise LocalTTSFailure("LOCAL_TTS_INVALID_INPUT") from None
        token = self._token if self._token is not None else os.environ.get("OMOI_SPEECH_CORE_TTS_TOKEN", "")
        if self._peer is None and (not isinstance(token, str) or not 32 <= len(token) <= 512
                or any(not 33 <= ord(ch) <= 126 for ch in token)):
            raise LocalTTSFailure("LOCAL_TTS_TOKEN_REQUIRED")
        if not self._lock.acquire(blocking=False):
            raise LocalTTSFailure("LOCAL_TTS_BUSY")
        deadline = time.monotonic() + self.STREAM_SECONDS

        def check():
            if stop.is_set():
                raise LocalTTSFailure("LOCAL_TTS_CANCELLED")
            if time.monotonic() >= deadline:
                raise LocalTTSFailure("LOCAL_TTS_TIMEOUT")

        def exact(size: int) -> bytes:
            data = bytearray()
            while len(data) < size:
                check()
                amount = min(size - len(data), CALLBACK_CHUNK_BYTES)
                part = response.read(amount)
                check()
                if not isinstance(part, bytes) or not part or len(part) > amount:
                    raise LocalTTSFailure("LOCAL_TTS_INCOMPLETE_STREAM")
                data.extend(part)
            return bytes(data)

        try:
            check()
            payload = json.dumps({
                "model": self._model, "voice": "reference", "language": language,
                "input": text, "emotion": "neutral", "speed": 1.0,
                "response_format": "pcm",
            }, ensure_ascii=False).encode("utf-8")
            check()
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/x-omoi-pcm-stream",
                "Cache-Control": "no-store", "Connection": "close",
            }
            if self._peer is None:
                headers["Authorization"] = "Bearer " + token
            with loopback_response(self._port, "POST", "/v1/audio/stream",
                                   body=payload, headers=headers, check=check,
                                   connect_timeout=min(CONNECT_SECONDS, self.STREAM_SECONDS),
                                   connection_factory=self._factory,
                                   verify_connected=self._peer.verify if self._peer is not None else None) as response:
                if response.status == 429:
                    raise LocalTTSFailure("LOCAL_TTS_BUSY")
                if response.status != 200:
                    raise LocalTTSFailure("LOCAL_TTS_UNAVAILABLE")
                if (response.getheader("Content-Type", "").split(";")[0].strip().lower()
                        != "application/x-omoi-pcm-stream"
                        or response.getheader("X-OMOI-PCM-Rate") != str(PCM_RATE)
                        or response.getheader("X-OMOI-PCM-Format") != "s16le"
                        or response.getheader("X-OMOI-TTS-Model") != self._model):
                    raise LocalTTSFailure("LOCAL_TTS_FORMAT_MISMATCH")
                total = frames = 0
                while True:
                    length = struct.unpack(">I", exact(4))[0]
                    if length == 0:
                        if total == 0:
                            raise LocalTTSFailure("LOCAL_TTS_EMPTY_STREAM")
                        return total
                    if length == 0xffffffff:
                        raise LocalTTSFailure("LOCAL_TTS_REMOTE_ERROR")
                    if (length % 2 or length > MAX_FRAME_BYTES
                            or total + length > MAX_STREAM_BYTES or frames >= MAX_FRAMES):
                        raise LocalTTSFailure("LOCAL_TTS_FRAME_LIMIT")
                    pcm = exact(length)
                    frames += 1
                    total += length
                    for offset in range(0, length, CALLBACK_CHUNK_BYTES):
                        check()
                        on_pcm(pcm[offset:offset + CALLBACK_CHUNK_BYTES])
                        check()
        except LocalTTSFailure:
            raise
        except Exception:
            check()
            raise LocalTTSFailure("LOCAL_TTS_STREAM_FAILED") from None
        finally:
            self._lock.release()
