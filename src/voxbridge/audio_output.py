"""Playback state machine; one owner thread/process for all native audio calls."""
from __future__ import annotations

from dataclasses import dataclass
import time

from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices
from .analysis_providers import ProviderFailure
from .tts_stream import BoundedPCMBuffer, BYTES_PER_SAMPLE, SAMPLE_RATE

BLOCK_BYTES = 4096
BUFFER_BYTES = 192_000
SHADOW_BYTES = 192_000
STALL_SECONDS = 8.0


@dataclass(frozen=True)
class OutputState:
    capacity: int
    recovery_count: int
    queued: bool
    done: bool = False
    error: str = ""


class PlaybackEngine:
    """Bounded playback and same-endpoint recovery, with no UI or provider."""

    def __init__(self, *, clock=time.monotonic, media=QMediaDevices, sink_factory=QAudioSink):
        self.clock = clock
        self.media = media
        self.sink_factory = sink_factory
        self.pending = BoundedPCMBuffer(BUFFER_BYTES)
        self.outstanding = bytearray()
        self.replay = bytearray()
        self.sink = self.io = None
        self.device_id = None
        self.recovery_count = 0
        self.reopen_at = 0.0
        self.progress_at = None
        self.last_write = None
        self.submitted = self.acknowledged = self.processed = 0
        self.ever_written = False
        self.remote_done = False
        self.done = False
        self.error = ""
        self.cleanup_failed = False

    def feed(self, data):
        if self.done or self.remote_done or not isinstance(data, bytes) or not data or len(data) > BLOCK_BYTES:
            raise ProviderFailure("AUDIO_OUTPUT_PROTOCOL")
        if not self.state().queued:
            self.progress_at = self.clock()
        self.pending.push(data)

    def finish(self):
        self.remote_done = True

    def state(self):
        return OutputState(self.pending.capacity - len(self.pending),
                           self.recovery_count,
                           bool(len(self.pending) or self.replay or self.outstanding),
                           self.done, self.error)

    def _release(self, immediate):
        if self.cleanup_failed:
            return False
        self.io = None
        if self.sink is not None:
            try:
                if immediate:
                    self.sink.reset()
                else:
                    self.sink.stop()
                self.sink.deleteLater()
            except (RuntimeError, OSError):
                self.cleanup_failed = True
                return False
        self.sink = None
        return True

    def _end(self, error="", *, immediate=False):
        if self.done:
            return
        self.pending.clear()
        self.replay.clear()
        self.outstanding.clear()
        if not self._release(immediate):
            error = "AUDIO_OUTPUT_CLEANUP_FAILED"
        self.error = error
        self.done = True

    def cancel(self):
        self._end("AUDIO_OUTPUT_CANCELLED", immediate=True)
        return self.state()

    def _open(self):
        if self.sink is not None or self.cleanup_failed:
            raise ProviderFailure("AUDIO_OUTPUT_CLEANUP_FAILED")
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if self.device_id is None:
            device = self.media.defaultAudioOutput()
            if device.isNull():
                raise ProviderFailure("AUDIO_DEVICE_UNAVAILABLE")
            self.device_id = bytes(device.id())
        else:
            device = next((d for d in self.media.audioOutputs()
                           if bytes(d.id()) == self.device_id), None)
            if device is None:
                raise ProviderFailure("AUDIO_DEVICE_UNAVAILABLE")
        if not device.isFormatSupported(fmt):
            raise ProviderFailure("AUDIO_FORMAT_NOT_SUPPORTED")
        self.sink = self.sink_factory(device, fmt)
        self.sink.setBufferSize(96_000)
        self.io = self.sink.start()
        if self.io is None or self.sink.state() == QAudio.State.StoppedState:
            raise ProviderFailure("AUDIO_DEVICE_UNAVAILABLE")
        self.progress_at = self.clock()

    def _ack(self, state, error):
        processed = self.sink.processedUSecs()
        if type(processed) is not int or processed < self.processed:
            return
        if processed > self.processed:
            self.progress_at = self.clock()
        self.processed = processed
        acknowledged = min(self.submitted,
                           processed * SAMPLE_RATE // 1_000_000 * BYTES_PER_SAMPLE)
        if state == QAudio.State.IdleState and error in (QAudio.Error.NoError, QAudio.Error.UnderrunError):
            acknowledged = self.submitted
        if acknowledged > self.acknowledged:
            del self.outstanding[:acknowledged - self.acknowledged]
            self.acknowledged = acknowledged

    def _recover(self, reason):
        if self.remote_done and self.ever_written and not self.state().queued:
            self._end()
            return
        if self.recovery_count >= 2:
            self._end("AUDIO_DEVICE_FAILED: " + reason, immediate=True)
            return
        self.recovery_count += 1
        self.replay[:0] = self.outstanding
        self.outstanding.clear()
        self.submitted = self.acknowledged = self.processed = 0
        self.last_write = None
        if not self._release(True):
            self._end("AUDIO_OUTPUT_CLEANUP_FAILED", immediate=True)
            return
        self.reopen_at = self.clock() + (.35 if self.recovery_count == 1 else 1.0)
        self.progress_at = self.clock()

    def tick(self):
        if not self.done:
            try:
                self._tick()
            except (ProviderFailure, RuntimeError, OSError, ValueError) as exc:
                code = str(exc) if isinstance(exc, ProviderFailure) else "AUDIO_OUTPUT_FAILED"
                self._end(code, immediate=True)
        return self.state()

    def _tick(self):
        if self.remote_done and self.ever_written and not self.state().queued and self.submitted == 0:
            self._end()
            return
        if self.sink is None and (len(self.pending) or self.replay or self.reopen_at):
            if self.clock() < self.reopen_at:
                return
            try:
                self._open()
            except (ProviderFailure, RuntimeError, OSError) as exc:
                reason = str(exc) if isinstance(exc, ProviderFailure) else "AUDIO_OPEN_ERROR"
                if reason == "AUDIO_FORMAT_NOT_SUPPORTED":
                    self._end(reason, immediate=True)
                else:
                    self._recover(reason)
                return
            self.reopen_at = 0.0
        if self.sink is None:
            if self.remote_done:
                self._end("TTS_STREAM_NO_AUDIO")
            return
        state, error = self.sink.state(), self.sink.error()
        if state == QAudio.State.StoppedState or error in (QAudio.Error.OpenError, QAudio.Error.IOError, QAudio.Error.FatalError):
            reason = error.name if isinstance(error, QAudio.Error) and error != QAudio.Error.NoError else "STOPPED"
            self._recover(reason)
            return
        self._ack(state, error)
        replaying = bool(self.replay)
        available = len(self.replay) if replaying else len(self.pending)
        writable = min(max(0, int(self.sink.bytesFree())), available,
                       8192, SHADOW_BYTES - len(self.outstanding))
        writable -= writable % BYTES_PER_SAMPLE
        sent = 0
        if writable:
            data = bytes(self.replay[:writable]) if replaying else self.pending.peek(writable)
            sent = self.io.write(data)
            if type(sent) is int and sent < 0:
                self._recover("AUDIO_WRITE_FAILED")
                return
            if type(sent) is not int or sent > len(data) or sent % BYTES_PER_SAMPLE:
                self._end("AUDIO_WRITE_FAILED", immediate=True)
                return
            if sent:
                if replaying:
                    del self.replay[:sent]
                else:
                    self.pending.discard(sent)
                self.outstanding.extend(data[:sent])
                self.submitted += sent
                self.ever_written = True
                self.last_write = self.progress_at = self.clock()
        if (self.remote_done and not self.state().queued and not sent
                and self.last_write is not None and self.clock() - self.last_write > .15):
            self._end()
            return
        if self.state().queued or self.remote_done:
            if self.progress_at is None:
                self.progress_at = self.clock()
            if self.clock() - self.progress_at > STALL_SECONDS:
                self._recover("AUDIO_PLAYBACK_STALLED")
