"""Explicit Cloud Mode-only OpenAI live STT session with bounded turns.

No network on import or construction; run() is user-triggered only. The
host owns speaker-loopback capture, cloud disclosure, opt-in and Stop.
A real API request can incur charges even if interrupted. No auto reconnect.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Iterable

from .captions_core import CaptionCoordinator
from .cloud_budget import CloudAbort
from .realtime_socket import RealtimeSocket
from .wasapi_capture import CaptureFailure
from .capture_isolation import CAPTURE_IDLE
from .session_limits import (MAX_UNSETTLED_COMMITS, RealtimeUsage, SessionQuotaExceeded)
from .realtime_events import (
    RealtimeEventError, RealtimeTranscriptionMapper,
    REALTIME_TRANSCRIPTION_URL, RATE_HZ,
    build_audio_append, build_audio_commit, build_session_update,
)

MAX_SERVER_MESSAGE = 65536
MAX_SESSION_SECONDS = 120
MAX_PCM_SAMPLES = RATE_HZ * MAX_SESSION_SECONDS
MAX_READ_EVENTS_PER_CHUNK = 12


class RealtimeTransportFailure(RuntimeError):
    """No private server text, token or audio is ever used in failure messages."""


class EnergyTurnDetector:
    """Bounded speech/quiet heuristic; not a validated voice-activity model.

    24kHz mono signed16 PCM. Low-volume speech may be missed, while music
    and gaming sounds may trigger commits. Manual consent and review required.
    """
    def __init__(self, *, threshold: int = 260, silence_chunks: int = 3,
                 max_chunks: int = 12):
        if not 1 <= threshold <= 32767 or not 1 <= silence_chunks <= 10:
            raise ValueError("INVALID_TURN_DETECTOR")
        if not 3 <= max_chunks <= 20:
            raise ValueError("INVALID_TURN_DETECTOR")
        self.threshold, self.silence_chunks, self.max_chunks = (
            threshold, silence_chunks, max_chunks
        )
        self.active = False
        self.chunks = 0
        self.quiet = 0

    def feed(self, pcm: bytes) -> tuple[bool, bool]:
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise RealtimeTransportFailure("REALTIME_BAD_AUDIO")
        # NumPy imported only when the user explicitly starts this worker.
        import numpy as np
        samples = np.frombuffer(pcm, dtype="<i2")
        if not len(samples):
            raise RealtimeTransportFailure("REALTIME_BAD_AUDIO")
        values = samples.astype(np.int32)
        # A 20 ms speech onset must not be averaged away in a 200 ms packet.
        # Keep silence/max-turn accounting on the existing transport packets.
        squares = values.astype(np.float64) ** 2
        windows = len(squares) // 480
        peak = np.max(np.mean(squares[:windows * 480].reshape(-1, 480), axis=1)) if windows else 0.0
        if len(squares) % 480:
            peak = max(peak, float(np.mean(squares[windows * 480:])))
        heard = peak >= self.threshold ** 2
        if not self.active:
            if not heard:
                return (False, False)  # skip leading silence, avoid cloud billing
            self.active = True
            self.chunks, self.quiet = 0, 0
        self.chunks += 1
        self.quiet = 0 if heard else self.quiet + 1
        done = self.quiet >= self.silence_chunks or self.chunks >= self.max_chunks
        if done:
            self.active, self.chunks, self.quiet = False, 0, 0
        return (True, done)

    def finish(self) -> bool:
        pending = self.active
        self.active, self.chunks, self.quiet = False, 0, 0
        return pending


def _recv_json(socket: Any, timeout: float) -> dict | None:
    try:
        raw = socket.recv(timeout=timeout)
    except TimeoutError:
        return None
    if not isinstance(raw, (bytes, str)) or len(raw) > MAX_SERVER_MESSAGE:
        raise RealtimeTransportFailure("REALTIME_BAD_SERVER_MESSAGE")
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RealtimeTransportFailure("REALTIME_BAD_SERVER_JSON") from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise RealtimeTransportFailure("REALTIME_BAD_SERVER_EVENT")
    return message


def run_realtime_transcription(
    controller: CaptionCoordinator,
    generation: int,
    source_pcm: Iterable[bytes | object],
    *,
    approved: bool,
    stop: threading.Event,
    api_key: str | None = None,
    socket_factory: Callable[..., Any] | None = None,
    on_update: Callable[[], None] | None = None,
    max_seconds: int = 60,
    usage: RealtimeUsage | None = None,
    delay: str = "low",
    on_stage: Callable[[str], None] | None = None,
) -> int:
    """Send only an explicitly approved bounded stream; return committed turns.

    CAPTURE_IDLE notifications are local polling ticks, never PCM or silence.
    All calls here belong on a worker thread. The user must opt in to sending
    system playback and accept possible API charges before invocation.
    """
    if approved is not True:
        raise RealtimeTransportFailure("REALTIME_CLOUD_CONSENT_REQUIRED")
    if not isinstance(stop, threading.Event) or stop.is_set():
        raise RealtimeTransportFailure("REALTIME_CANCELLED")
    if type(max_seconds) is not int or not 1 <= max_seconds <= MAX_SESSION_SECONDS:
        raise RealtimeTransportFailure("REALTIME_INVALID_DURATION")
    key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
    if not isinstance(key, str) or not key.strip():
        raise RealtimeTransportFailure("OPENAI_API_KEY_MISSING")
    snapshot = controller.snapshot()
    if not snapshot.active or snapshot.generation != generation:
        raise RealtimeTransportFailure("REALTIME_SESSION_MISMATCH")
    if usage is None:
        usage = RealtimeUsage(max_seconds)
    if not isinstance(usage, RealtimeUsage):
        raise RealtimeTransportFailure("REALTIME_INVALID_USAGE")
    try:
        session_update = build_session_update(snapshot.source_language, delay)
    except RealtimeEventError as exc:
        raise RealtimeTransportFailure(str(exc)) from None
    events = RealtimeTranscriptionMapper(controller, generation)
    detector = EnergyTurnDetector()
    budget = RATE_HZ * max_seconds
    samples = 0
    commits = 0
    sent_audio = False
    started = time.monotonic()
    def stage(code):
        if on_stage is not None:
            on_stage(code)
    socket = None
    audio_iterator = None
    try:
        if stop.is_set():
            return commits
        usage.claim(max_seconds)
        stage("REALTIME_CONNECTING")
        # A fixed wss endpoint, no user-specified network target, no proxy.
        if socket_factory is None:
            socket = RealtimeSocket(stop, started + max_seconds + 12)
            connect = socket.connect
        else:
            connect = socket_factory
        connected = connect(
            REALTIME_TRANSCRIPTION_URL,
            additional_headers={"Authorization": "Bearer " + key},
            open_timeout=8, close_timeout=2, max_size=MAX_SERVER_MESSAGE,
            max_queue=16, compression=None, proxy=None,
        )
        if socket_factory is not None:
            socket = connected
        if stop.is_set():
            return commits
        stage("REALTIME_CONFIGURING")
        socket.send(json.dumps(session_update, separators=(",", ":")))
        ready = False
        deadline = min(time.monotonic() + 8, started + max_seconds + 8)
        while not stop.is_set() and time.monotonic() < deadline:
            event = _recv_json(socket, min(0.2, max(0.01, deadline - time.monotonic())))
            if event is None:
                continue
            if event["type"] == "error":
                raise RealtimeTransportFailure("REALTIME_SESSION_REJECTED")
            if event["type"] == "session.updated":
                session = event.get("session")
                if not isinstance(session, dict) or session.get("type") != "transcription":
                    raise RealtimeTransportFailure("REALTIME_WRONG_SESSION_TYPE")
                ready = True
                break
        if stop.is_set():
            return commits
        if not ready:
            raise RealtimeTransportFailure("REALTIME_SESSION_NOT_READY")

        def accept_event(event):
            updated = events.accept(event, max_acknowledgements=commits,
                                    allow_live_preview=sent_audio)
            if updated and on_update is not None:
                on_update()

        def receive_some():
            for _ in range(MAX_READ_EVENTS_PER_CHUNK):
                if stop.is_set():
                    break
                event = _recv_json(socket, 0.001)
                if event is None or stop.is_set():
                    break
                accept_event(event)

        def commit_turn():
            nonlocal commits
            usage.reserve_commit()
            socket.send(json.dumps(build_audio_commit()))
            commits += 1

        stage("WASAPI_WAITING_FOR_AUDIO")
        audio_iterator = iter(source_pcm)
        first_pcm = True
        first_voice = True
        while not stop.is_set():
            if time.monotonic() - started > max_seconds + 8:
                raise RealtimeTransportFailure("REALTIME_SESSION_TIMEOUT")
            try:
                pcm = next(audio_iterator)
            except StopIteration:
                break
            if stop.is_set():
                break
            if time.monotonic() - started > max_seconds + 8:
                raise RealtimeTransportFailure("REALTIME_SESSION_TIMEOUT")
            if pcm is CAPTURE_IDLE:
                receive_some()
                continue
            if (not isinstance(pcm, bytes) or len(pcm) % 2 or
                    not 2 <= len(pcm) <= 9600 or
                    samples + len(pcm) // 2 > budget):
                raise RealtimeTransportFailure("REALTIME_AUDIO_LIMIT")
            samples += len(pcm) // 2
            if first_pcm:
                first_pcm = False
                stage("WASAPI_AUDIO_RECEIVED")
            append, commit = detector.feed(pcm)
            if append and first_voice:
                first_voice = False
                stage("REALTIME_SPEECH_DETECTED")
            if append and not stop.is_set():
                if commits - events.settled_turns >= MAX_UNSETTLED_COMMITS:
                    receive_some()
                    if commits - events.settled_turns >= MAX_UNSETTLED_COMMITS:
                        raise RealtimeTransportFailure("REALTIME_UNSETTLED_LIMIT")
                if stop.is_set():
                    break
                usage.reserve_audio(len(pcm) // 2)
                socket.send(json.dumps(build_audio_append(pcm), separators=(",", ":")))
                sent_audio = True
            if commit and not stop.is_set():
                commit_turn()
            receive_some()
        # The capture child must be closed before waiting for cloud finals.
        # A normal iterator has already exhausted; close() is idempotent.
        close_audio = getattr(audio_iterator, "close", None)
        if callable(close_audio):
            close_audio()
        if not stop.is_set() and detector.finish():
            commit_turn()
        if not stop.is_set() and commits:
            stage("REALTIME_WAITING_FOR_FINAL")
            # A bounded drain allows the last turn to complete without sleep loops
            # that can hang after audio ends; never open a second connection.
            deadline = min(time.monotonic() + 4, started + max_seconds + 12)
            # Visible lines are evicted and empty/failed finals create no row.
            # Count ordered terminal protocol events, not the overlay contents.
            while (events.settled_turns < commits and time.monotonic() < deadline
                   and not stop.is_set()):
                event = _recv_json(socket, min(.15, max(.001, deadline-time.monotonic())))
                if event is not None and not stop.is_set():
                    accept_event(event)
            if not stop.is_set() and events.settled_turns < commits:
                raise RealtimeTransportFailure("REALTIME_FINAL_TIMEOUT")
        controller.live_preview(generation, "")
        return commits
    except CloudAbort as exc:
        if str(exc) == "CANCELLED" and stop.is_set():
            return commits
        raise RealtimeTransportFailure("REALTIME_" + str(exc)) from None
    except SessionQuotaExceeded as exc:
        raise RealtimeTransportFailure(str(exc)) from None
    except CaptureFailure:
        raise  # keep the sanitized WASAPI error code intact
    except (RealtimeTransportFailure, RealtimeEventError) as exc:
        # Only stable error codes, never server/body/secret.
        if isinstance(exc, RealtimeEventError):
            raise RealtimeTransportFailure(str(exc)) from None
        raise
    except Exception:
        raise RealtimeTransportFailure("REALTIME_WEBSOCKET_FAILED") from None
    finally:
        # A WASAPI generator is suspended inside 'with mic.recorder(...)'
        # after each yield. Close it explicitly on ALL exits, especially if
        # WebSocket rejects a request or the user presses Stop. Do not rely on
        # CPython garbage collection to eventually release the audio device.
        cleanup_error = None
        for source in (audio_iterator, source_pcm) if audio_iterator is not source_pcm else (source_pcm,):
            close_source = getattr(source, "close", None)
            if callable(close_source):
                try:
                    close_source()
                except CaptureFailure as exc:
                    if str(exc) == "WASAPI_CHILD_CLEANUP_FAILED":
                        cleanup_error = exc
                except Exception:
                    pass
        if socket is not None:
            try:
                socket.close()
            except Exception:
                if cleanup_error is None:
                    cleanup_error = RealtimeTransportFailure("REALTIME_CLEANUP_FAILED")
        if stop.is_set():
            events.close()  # clears private session on user cancellation
        if cleanup_error is not None:
            raise cleanup_error from None
