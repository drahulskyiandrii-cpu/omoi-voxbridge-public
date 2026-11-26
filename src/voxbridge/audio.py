"""PCM16 16-kHz mono microphone; callback only moves bytes to a bounded RAM buffer."""
from __future__ import annotations
import io
import threading
import wave
from dataclasses import dataclass

from .microphone_devices import MicrophoneError, resolve_input, valid_microphone_id

RATE = 16000
MAX_RECORD_SECONDS = 120
MAX_SEGMENT_SECONDS = 30

def pcm_to_wav(pcm: bytes) -> bytes:
    if not isinstance(pcm, bytes) or len(pcm) % 2:
        raise ValueError("INVALID_PCM")
    with io.BytesIO() as out:
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(pcm)
        return out.getvalue()

@dataclass(frozen=True)
class AudioQuality:
    seconds: float = 0.0
    rms: float = 0.0
    peak: int = 0
    voice_ms: int = 0
    clipped_percent: float = 0.0
    gaps: bool = False

    @property
    def code(self):
        if self.gaps:
            return "MIC_INPUT_OVERFLOW"
        if self.seconds < .2:
            return "AUDIO_TOO_SHORT"
        if self.voice_ms < 100:
            return "MICROPHONE_SIGNAL_TOO_QUIET"
        if self.clipped_percent >= 25:
            return "MICROPHONE_SIGNAL_CLIPPED"
        return "MIC_SIGNAL_OK"

    def summary(self):
        return (f"{self.code}; {self.seconds:.2f}s, RMS={self.rms:.4f}, "
                f"peak={self.peak}, voice={self.voice_ms}ms, clip={self.clipped_percent:.1f}%")


def measure_pcm(pcm, *, gaps=False):
    """Bounded local level metrics, not neural VAD. Remove per-frame DC bias."""
    import array
    import math
    import sys
    if not isinstance(pcm, bytes) or len(pcm) % 2 or len(pcm)>RATE*2*MAX_RECORD_SECONDS:
        raise MicrophoneError("INVALID_PCM")
    signal = array.array("h")
    signal.frombytes(pcm)
    if sys.byteorder != "little":
        signal.byteswap()
    n = len(signal)
    if not n:
        return AudioQuality(gaps=gaps)
    voice = 0
    energy = 0.0
    width = RATE // 50
    for offset in range(0, n, width):
        frame = signal[offset:offset+width]
        count = len(frame)
        mean = sum(frame)/count
        e = sum((x-mean)**2 for x in frame)
        energy += e
        if count == width and e/count > 82**2:
            voice += 20
    return AudioQuality(n/RATE, math.sqrt(energy/n)/32768, max(abs(x) for x in signal),
                        voice, 100*sum(abs(x)>=32760 for x in signal)/n, gaps)


class Microphone:
    def __init__(self, max_seconds: int = 25, *, microphone_id: str = ""):
        if type(max_seconds) is not int or not 3 <= max_seconds <= MAX_RECORD_SECONDS:
            raise ValueError("INVALID_MAX_SECONDS")
        if not valid_microphone_id(microphone_id):
            raise ValueError("INVALID_MICROPHONE_ID")
        self.microphone_id = microphone_id
        self._limit = RATE * 2 * max_seconds
        self.long_recording = max_seconds > MAX_SEGMENT_SECONDS
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream = None
        self._reached_limit = False
        self._gaps = False
        self.last_quality = AudioQuality()
        self.conditioning = False

    @property
    def max_reached(self) -> bool:
        with self._lock:
            return self._reached_limit

    def start(self, *, cancelled=None):
        if self._stream is not None:
            raise RuntimeError("MIC_ALREADY_RECORDING")
        if cancelled is not None and cancelled.is_set():
            raise MicrophoneError("MIC_CANCELLED")
        import sounddevice as sd  # no device used until explicitly invoked
        device = resolve_input(sd, self.microphone_id)
        with self._lock:
            self._buffer.clear()
            self._reached_limit = False
            self._gaps = False
            self.last_quality = AudioQuality()

        def callback(indata, frames, timing, status):
            del timing
            raw = bytes(indata)
            with self._lock:
                if cancelled is not None and cancelled.is_set():
                    return
                if (status and getattr(status, "input_overflow", False)) or len(raw) != frames * 2:
                    self._gaps = True
                if len(raw) % 2 or len(raw) != frames * 2:
                    return
                room = self._limit - len(self._buffer)
                if room > 0:
                    self._buffer.extend(raw[:room])
                if len(self._buffer) >= self._limit:
                    self._reached_limit = True

        try:
            sd.check_input_settings(device=device, samplerate=RATE, channels=1, dtype="int16")
        except Exception:
            raise MicrophoneError("MIC_FORMAT_UNSUPPORTED_OR_UNAVAILABLE") from None
        try:
            if cancelled is not None and cancelled.is_set():
                raise MicrophoneError("MIC_CANCELLED")
            stream = sd.RawInputStream(device=device, samplerate=RATE, channels=1, dtype="int16",
                                       blocksize=1024, callback=callback)
            # Keep a handle if startup/close fails so cleanup can be retried.
            self._stream = stream
            try:
                if cancelled is not None and cancelled.is_set():
                    raise MicrophoneError("MIC_CANCELLED")
                stream.start()
            except Exception:
                stream.close()
                self._stream = None
                raise
        except Exception:
            with self._lock:
                self._buffer.clear()
            raise MicrophoneError("MIC_START_FAILED") from None
        self._stream = stream

    def raw_snapshot(self) -> bytes:
        """Cumulative raw PCM in RAM; quality/conditioning runs in STT worker."""
        with self._lock:
            if self._stream is None or self._gaps or len(self._buffer) < RATE * 2:
                return b""
            data = bytes(self._buffer)
        return pcm_to_wav(data)

    def snapshot(self) -> bytes:
        """In-memory cumulative WAV from at least one second of recording."""
        with self._lock:
            if len(self._buffer) < RATE * 2 or self._stream is None:
                return b""
            data = bytes(self._buffer)
            gaps = self._gaps
        if measure_pcm(data, gaps=gaps).code != "MIC_SIGNAL_OK":
            return b""
        if self.conditioning:
            from .stt_enhancements import condition_pcm
            data = condition_pcm(data)
        return pcm_to_wav(data)

    def stop(self) -> bytes:
        stream = self._stream
        if stream is None:
            return b""
        try:
            try:
                stream.stop()
            finally:
                stream.close()
                self._stream = None
        finally:
            with self._lock:
                data = bytes(self._buffer)
                gaps = self._gaps
                self._buffer.clear()
        self.last_quality = measure_pcm(data, gaps=gaps)
        if self.last_quality.code != "MIC_SIGNAL_OK":
            raise MicrophoneError(self.last_quality.code)
        if self.conditioning and not self.long_recording:
            from .stt_enhancements import condition_pcm
            data = condition_pcm(data)
        return pcm_to_wav(data)

    def cancel(self) -> None:
        stream = self._stream
        try:
            if stream is not None:
                try:
                    stream.stop()
                finally:
                    stream.close()
                    self._stream = None
        finally:
            with self._lock:
                self._buffer.clear()
