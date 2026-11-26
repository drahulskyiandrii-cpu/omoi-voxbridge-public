"""Explicit Windows WASAPI speaker loopback to 24 kHz mono PCM16.

No device discovery/capture or local ML takes place on import or construction.
Caller must receive opt-in cloud/audio consent before invoking capture().
Only SoundCard loopback devices can be selected; never microphone devices.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import threading
import time
from typing import Iterator, Any

SOURCE_RATE = 48000
DEST_RATE = 24000
SOURCE_FRAMES = 9600                 # requested 200 ms chunks; WASAPI may vary
MAX_FRAMES = SOURCE_RATE * 2         # defend against oversized device response
MAX_SECONDS = 120


class CaptureFailure(RuntimeError):
    """Sanitized local sound-device error (never device names or audio samples)."""


@dataclass(frozen=True)
class LoopbackDevice:
    id: str
    name: str
    is_default: bool = False


def _soundcard():
    try:
        import soundcard
        return soundcard
    except ImportError:
        raise CaptureFailure("SOUNDCARD_NOT_INSTALLED") from None


def list_loopback_devices(*, backend: Any = None) -> list[LoopbackDevice]:
    """Metadata only. Caller must click an explicit device-refresh action."""
    if os.name != "nt" and backend is None:
        raise CaptureFailure("WASAPI_WINDOWS_REQUIRED")
    sc = backend if backend is not None else _soundcard()
    try:
        devices = sc.all_microphones(include_loopback=True)
    except Exception:
        raise CaptureFailure("WASAPI_DEVICE_ENUMERATION_FAILED") from None
    default_id = None
    try:
        default_id = sc.default_speaker().id
    except Exception:
        pass
    result = []
    for dev in devices:
        if (getattr(dev, "isloopback", False) and
                isinstance(getattr(dev, "id", None), str) and dev.id and
                isinstance(getattr(dev, "name", None), str)):
            result.append(LoopbackDevice(id=dev.id, name=dev.name[:160], is_default=dev.id == default_id))
    return result[:64]


class Downsample48To24:
    """Streaming 31-tap FIR anti-alias filter + factor-2 decimation.

    Preserves odd chunk phase and filter state between packets. NumPy is CPU
    only. The first ~15 samples have filter startup delay; no persistent data.
    """

    def __init__(self):
        import numpy as np
        n = np.arange(-15, 16, dtype=np.float64)
        cutoff = 0.21875   # 10.5kHz of 48k source; below output Nyquist 12k
        taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(31)
        taps /= taps.sum()
        self._np = np
        self._taps = taps
        self._history = np.zeros(len(taps) - 1, dtype=np.float64)
        self._offset = 0

    def convert(self, frames: Any) -> bytes:
        np = self._np
        try:
            arr = np.asarray(frames)
        except (ValueError, TypeError):
            raise CaptureFailure("WASAPI_INVALID_FRAMES") from None
        if arr.ndim == 1:
            arr = arr[:, None]
        if (arr.ndim != 2 or not 1 <= arr.shape[1] <= 8 or
                not 1 <= arr.shape[0] <= MAX_FRAMES or
                not np.issubdtype(arr.dtype, np.number)):
            raise CaptureFailure("WASAPI_INVALID_FRAMES")
        values = arr.astype(np.float64, copy=False)
        if not np.isfinite(values).all():
            raise CaptureFailure("WASAPI_NONFINITE_AUDIO")
        mono = values.mean(axis=1)
        signal = np.concatenate((self._history, mono))
        filtered = np.convolve(signal, self._taps, mode="valid")
        self._history = signal[-(len(self._taps) - 1):].copy()
        begin = (-self._offset) % 2
        self._offset = (self._offset + len(mono)) % 2
        decimated = np.clip(filtered[begin::2], -1.0, 1.0)
        pcm = np.rint(decimated * 32767).astype("<i2")
        return pcm.tobytes()


def capture_loopback(
    device_id: str, stop: threading.Event, *,
    seconds: int = 60, backend: Any = None,
) -> Iterator[bytes]:
    """Explicit single-session generator; blocks in WASAPI .record() on caller's worker.

    No auto-start, no file output, no external network. Stream may block on
    certain Windows audio drivers when fully silent; host Stop cannot guarantee
    immediate cancellation in that driver. Session duration is capped.
    """
    if not isinstance(device_id, str) or not device_id or len(device_id) > 512:
        raise CaptureFailure("WASAPI_INVALID_DEVICE")
    if not isinstance(stop, threading.Event):
        raise CaptureFailure("WASAPI_CANCEL_EVENT_REQUIRED")
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise CaptureFailure("WASAPI_INVALID_DURATION")
    if stop.is_set():
        return
    if os.name != "nt" and backend is None:
        raise CaptureFailure("WASAPI_WINDOWS_REQUIRED")
    sc = backend if backend is not None else _soundcard()
    # Validate ID against current loopback devices, not regular microphones.
    permitted = {d.id for d in list_loopback_devices(backend=sc)}
    if stop.is_set():
        return
    if device_id not in permitted:
        raise CaptureFailure("WASAPI_NOT_LOOPBACK_DEVICE")
    try:
        mic = sc.get_microphone(device_id, include_loopback=True)
        if not getattr(mic, "isloopback", False) or mic.id != device_id:
            raise CaptureFailure("WASAPI_NOT_LOOPBACK_DEVICE")
        if stop.is_set():
            return
        converter = Downsample48To24()
        remaining = SOURCE_RATE * seconds
        pending = bytearray()
        chunk_bytes = SOURCE_FRAMES  # 2:1 decimation, 2 bytes per output sample
        # A cooperative deadline, not a way to interrupt a blocked driver.
        deadline = time.monotonic() + seconds + 2
        with mic.recorder(samplerate=SOURCE_RATE) as recorder:
            while not stop.is_set() and remaining > 0:
                if time.monotonic() >= deadline:
                    raise CaptureFailure("WASAPI_CAPTURE_TIMEOUT")
                amount = min(SOURCE_FRAMES, remaining)
                frames = recorder.record(numframes=amount)
                if stop.is_set():
                    return
                if time.monotonic() >= deadline:
                    raise CaptureFailure("WASAPI_CAPTURE_TIMEOUT")
                pcm = converter.convert(frames)
                # Count actual frames, not requested frames. Fail closed on a
                # backend returning more than requested, rather than send extra.
                actual = len(frames)
                if actual > amount:
                    raise CaptureFailure("WASAPI_FRAME_OVERRUN")
                remaining -= actual
                pending.extend(pcm)
                # Short driver reads must not shorten VAD timing.
                while len(pending) >= chunk_bytes and not stop.is_set():
                    packet = bytes(pending[:chunk_bytes])
                    del pending[:chunk_bytes]
                    yield packet
            if pending and not stop.is_set():
                yield bytes(pending)
    except CaptureFailure:
        raise
    except Exception:
        raise CaptureFailure("WASAPI_CAPTURE_FAILED") from None
