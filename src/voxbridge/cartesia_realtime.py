"""Explicit Cartesia Ink Preview streaming adapter. No I/O on import."""
from collections import deque
import os
import threading
import time
from urllib.parse import urlencode

from .capture_isolation import CAPTURE_IDLE
from .cloud_budget import CloudAbort
from .realtime_socket import RealtimeSocket
from .realtime_transport import (
    EnergyTurnDetector, RealtimeTransportFailure, _recv_json, MAX_SESSION_SECONDS,
)
from .session_limits import RealtimeUsage, SessionQuotaExceeded
from .wasapi_capture import CaptureFailure
from .captions_core import MAX_ORIGINAL_CHARS


class InkEventError(ValueError):
    pass


class InkTurnMapper:
    """Final token chunks are not complete turns; commit only at flush_done.

    The caller must serialize finalize commands: no next-turn audio before
    flush acknowledgement. The transport bounds queued audio while awaiting acknowledgement.
    """

    def __init__(self, coordinator, generation):
        self.coordinator = coordinator
        self.generation = generation
        self.sequence = 0
        self.revision = 0
        self.confirmed = ""
        self.tentative = ""
        self.flushing = False
        self.closed = False
        self.request_id = None
        self.committed = 0

    def _current(self):
        snapshot = self.coordinator.snapshot()
        if not snapshot.active or snapshot.generation != self.generation:
            raise InkEventError("INK_STALE_SESSION")

    def begin_flush(self):
        self._current()
        if self.closed or self.flushing:
            raise InkEventError("INK_INVALID_FLUSH")
        self.flushing = True

    def _preview(self):
        text = self.confirmed + self.tentative
        if len(text) > MAX_ORIGINAL_CHARS:
            raise InkEventError("INK_TEXT_LIMIT")
        self.revision += 1
        if self.revision > 512:
            raise InkEventError("INK_EVENT_LIMIT")
        self.coordinator.partial(self.generation, self.sequence, self.revision, text)

    def accept(self, event):
        self._current()
        if self.closed or not isinstance(event, dict):
            raise InkEventError("INK_INVALID_EVENT")
        kind = event.get("type")
        if kind == "error":
            raise InkEventError("INK_SERVER_ERROR")
        if kind not in ("transcript", "flush_done", "done"):
            raise InkEventError("INK_UNKNOWN_EVENT")
        request_id = event.get("request_id")
        if (not isinstance(request_id,str) or not 1<=len(request_id)<=128
                or not request_id.isascii() or any(ord(c)<33 or ord(c)>126 for c in request_id)):
            raise InkEventError("INK_INVALID_REQUEST_ID")
        if self.request_id is None:
            self.request_id = request_id
        elif request_id != self.request_id:
            raise InkEventError("INK_REQUEST_MISMATCH")
        if kind == "transcript":
            text = event.get("text")
            final = event.get("is_final")
            if (not isinstance(text,str) or len(text)>MAX_ORIGINAL_CHARS
                    or "\x00" in text or type(final) is not bool):
                raise InkEventError("INK_INVALID_TRANSCRIPT")
            if final:
                self.confirmed += text
                self.tentative = ""
            else:
                self.tentative = text
            self._preview()
        elif kind == "flush_done":
            if not self.flushing or self.tentative.strip():
                raise InkEventError("INK_UNEXPECTED_FLUSH")
            if self.confirmed.strip():
                if not self.coordinator.final(self.generation,self.sequence,self.confirmed):
                    raise InkEventError("INK_COMMIT_REJECTED")
                self.committed += 1
            else:
                self.revision += 1
                self.coordinator.partial(self.generation,self.sequence,self.revision,"")
            self.sequence += 1
            self.revision = 0
            self.confirmed = self.tentative = ""
            self.flushing = False
        else:
            if self.flushing or self.confirmed.strip() or self.tentative.strip():
                raise InkEventError("INK_UNFLUSHED_CLOSE")
            self.closed = True


INK_MODEL = "ink-preview"
MAX_PENDING_BYTES = 96000  # two seconds of 24 kHz mono PCM
FLUSH_SECONDS = 4.0


def run_ink_transcription(controller, generation, source_pcm, *, approved, stop,
                          api_key=None, socket_factory=None, on_update=None,
                          max_seconds=60, usage=None, delay="low", on_stage=None):
    """Same worker contract as OpenAI; no reconnect or provider fallback.

    Capture heartbeats service the socket. Audio arriving during finalize
    is buffered, never assigned to the previous turn. Fail visibly on overflow.
    """
    if approved is not True:
        raise RealtimeTransportFailure("INK_CLOUD_CONSENT_REQUIRED")
    if not isinstance(stop, threading.Event) or stop.is_set():
        raise RealtimeTransportFailure("REALTIME_CANCELLED")
    if type(max_seconds) is not int or not 1 <= max_seconds <= MAX_SESSION_SECONDS:
        raise RealtimeTransportFailure("REALTIME_INVALID_DURATION")
    key = api_key if api_key is not None else os.environ.get("CARTESIA_API_KEY", "")
    if not isinstance(key,str) or not key or any(not 33<=ord(c)<=126 for c in key):
        raise RealtimeTransportFailure("CARTESIA_API_KEY_MISSING_OR_INVALID")
    snap=controller.snapshot()
    if not snap.active or snap.generation!=generation:
        raise RealtimeTransportFailure("REALTIME_SESSION_MISMATCH")
    if usage is None:
        usage=RealtimeUsage(max_seconds)
    if not isinstance(usage,RealtimeUsage):
        raise RealtimeTransportFailure("REALTIME_INVALID_USAGE")
    mapper=InkTurnMapper(controller,generation)
    detector=EnergyTurnDetector(max_chunks=20)
    started=time.monotonic()
    pending=deque()
    pending_bytes=0
    received=0
    flush_deadline=0.0
    closing=False
    turn_audio=False
    first_voice=True
    socket=None
    iterator=None

    def stage(code):
        if on_stage is not None:on_stage(code)

    def receive(timeout):
        nonlocal turn_audio
        if stop.is_set():return False
        event=_recv_json(socket,timeout)
        if event is None or stop.is_set():return False
        if event["type"]=="done" and not closing:
            raise InkEventError("INK_EARLY_CLOSE")
        if event["type"]=="transcript" and not turn_audio and event.get("text"):
            raise InkEventError("INK_TRANSCRIPT_WITHOUT_AUDIO")
        mapper.accept(event)
        if event["type"]=="flush_done":turn_audio=False
        if on_update is not None:on_update()
        return True

    def service():
        for _ in range(12):
            if not receive(.001):break
        if mapper.flushing and time.monotonic()>=flush_deadline:
            raise InkEventError("INK_FLUSH_TIMEOUT")

    def finalize():
        nonlocal flush_deadline
        if stop.is_set():return
        usage.reserve_commit()
        mapper.begin_flush()
        socket.send("finalize")
        flush_deadline=time.monotonic()+FLUSH_SECONDS

    def drain():
        nonlocal pending_bytes,turn_audio,first_voice
        while pending and not mapper.flushing and not stop.is_set():
            pcm=pending.popleft()
            pending_bytes-=len(pcm)
            append,commit=detector.feed(pcm)
            if append and not stop.is_set():
                if first_voice:
                    first_voice=False
                    stage("REALTIME_SPEECH_DETECTED")
                usage.reserve_audio(len(pcm)//2)
                turn_audio=True
                socket.send(pcm)
            if commit:finalize()
            service()

    try:
        usage.claim(max_seconds)
        stage("REALTIME_CONNECTING")
        url="wss://api.cartesia.ai/stt/websocket?"+urlencode({
            "model":INK_MODEL,"encoding":"pcm_s16le","sample_rate":24000,
            "cartesia_version":"2026-08-14","language_hint":snap.source_language})
        kwargs=dict(additional_headers={"X-API-Key":key},proxy=None,open_timeout=8,
                    close_timeout=2,max_size=65536,max_queue=16,compression=None)
        if socket_factory is None:
            socket=RealtimeSocket(stop,started+max_seconds+16)
            socket.connect(url,**kwargs)
        else:
            socket=socket_factory(url,**kwargs)
        stage("WASAPI_WAITING_FOR_AUDIO")
        iterator=iter(source_pcm)
        for pcm in iterator:
            if stop.is_set():break
            if time.monotonic()>started+max_seconds+8:
                raise InkEventError("INK_SESSION_TIMEOUT")
            service()
            drain()
            if pcm is CAPTURE_IDLE:continue
            if (not isinstance(pcm,bytes) or not 2<=len(pcm)<=9600 or len(pcm)%2
                    or received+len(pcm)>max_seconds*48000):
                raise InkEventError("REALTIME_AUDIO_LIMIT")
            received+=len(pcm)
            if received==len(pcm):stage("WASAPI_AUDIO_RECEIVED")
            pending.append(pcm)
            pending_bytes+=len(pcm)
            if pending_bytes>MAX_PENDING_BYTES or len(pending)>32:
                raise InkEventError("INK_PENDING_AUDIO_LIMIT")
            drain()
        close=getattr(iterator,"close",None)
        if callable(close):close()
        if not stop.is_set():
            stage("REALTIME_WAITING_FOR_FINAL")
            deadline=min(started+max_seconds+14,time.monotonic()+8)
            while not stop.is_set():
                if time.monotonic()>=deadline:raise InkEventError("INK_FINAL_TIMEOUT")
                if mapper.flushing:
                    receive(.1)
                    if mapper.flushing and time.monotonic()>=flush_deadline:
                        raise InkEventError("INK_FLUSH_TIMEOUT")
                else:
                    drain()
                    if mapper.flushing:continue
                    if detector.finish():finalize()
                    else:break
            if not stop.is_set():
                closing=True
                socket.send("close")
                deadline=time.monotonic()+3
                while not mapper.closed and not stop.is_set() and time.monotonic()<deadline:
                    receive(.1)
                if not mapper.closed and not stop.is_set():raise InkEventError("INK_DONE_TIMEOUT")
        return mapper.committed
    except CloudAbort as exc:
        if stop.is_set() and str(exc)=="CANCELLED":return mapper.committed
        raise RealtimeTransportFailure("INK_"+str(exc)) from None
    except (InkEventError,SessionQuotaExceeded) as exc:
        raise RealtimeTransportFailure(str(exc)) from None
    except (CaptureFailure,RealtimeTransportFailure):raise
    except Exception:
        raise RealtimeTransportFailure("INK_WEBSOCKET_FAILED") from None
    finally:
        pending.clear()
        mapper.confirmed=mapper.tentative=""
        cleanup=None
        for source in (iterator,source_pcm) if iterator is not source_pcm else (source_pcm,):
            close=getattr(source,"close",None)
            if callable(close):
                try:close()
                except Exception:cleanup=CaptureFailure("WASAPI_CHILD_CLEANUP_FAILED")
        if socket is not None:
            try:
                if isinstance(socket,RealtimeSocket) and not mapper.closed:socket.failed=True
                socket.close()
            except Exception:
                if cleanup is None:cleanup=RealtimeTransportFailure("REALTIME_CLEANUP_FAILED")
        if stop.is_set():controller.stop()
        if cleanup is not None:raise cleanup from None
