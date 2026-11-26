"""Serialize native microphone operations away from the Qt event loop."""
from __future__ import annotations

import threading
from PySide6.QtCore import QObject, Signal
from .microphone_devices import MicrophoneError


class MicrophoneLifecycle(QObject):
    ready = Signal(int, str)
    stopped = Signal(int, bytes, str)

    def __init__(self, recorder, parent=None):
        super().__init__(parent)
        self.recorder = recorder
        self._lock = threading.Lock()
        self._active = None
        self._cleanup_failed = False

    @property
    def busy(self):
        with self._lock:
            return self._active is not None or self._cleanup_failed

    @property
    def pending(self):
        """Actual worker lifetime, excluding a sticky cleanup failure."""
        with self._lock:
            return self._active is not None

    @property
    def cleanup_failed(self):
        with self._lock:
            return self._cleanup_failed

    def start(self, generation):
        with self._lock:
            if self._active is not None or self._cleanup_failed:
                raise MicrophoneError("MIC_CLEANUP_PENDING")
            cancelled = threading.Event()
            stop = threading.Event()
            self._active = (cancelled, stop)
        try:
            threading.Thread(target=self._run, args=(generation, cancelled, stop),
                             name="voxbridge-microphone", daemon=True).start()
        except RuntimeError:
            with self._lock:
                self._active = None
            raise MicrophoneError("MIC_WORKER_START_FAILED") from None

    def finish(self):
        with self._lock:
            if self._active is None:
                raise MicrophoneError("MIC_NOT_RECORDING")
            self._active[1].set()

    def cancel(self):
        # Signal only: no PortAudio calls, thread joins or driver waits.
        with self._lock:
            if self._active is not None:
                cancelled, stop = self._active
                cancelled.set()
                stop.set()

    def _run(self, generation, cancelled, stop):
        stage = "start"
        wav, error = b"", ""
        cleanup_failed = False
        try:
            if not cancelled.is_set():
                self.recorder.start(cancelled=cancelled)
                if not cancelled.is_set():
                    stage = "stop"
                    self.ready.emit(generation, "")
                    stop.wait()
                    if not cancelled.is_set():
                        wav = self.recorder.stop()
        except Exception as exc:
            error = str(exc) if isinstance(exc, MicrophoneError) else "MIC_" + stage.upper() + "_FAILED"
        finally:
            # Even a failed start/stop may have allocated a native stream.
            if cancelled.is_set() or error:
                try:
                    self.recorder.cancel()
                except Exception:
                    cleanup_failed = True
                    error = "MIC_CLEANUP_FAILED"
            with self._lock:
                self._cleanup_failed = cleanup_failed
                self._active = None
            if not cancelled.is_set():
                try:
                    if stage == "start":
                        self.ready.emit(generation, error or "MIC_START_FAILED")
                    else:
                        self.stopped.emit(generation, wav, error)
                except RuntimeError:
                    pass  # Qt already destroyed during application shutdown.
