"""Explicit, session-only CUDA warmup with one owned worker and serial STT executor."""
import threading

from .local_partial import ResidentMiniSTT
from .stt import SpeechFailure
from .stt_process import stt_block_reason


class WarmCudaSTT(ResidentMiniSTT):
    """Session-resident CUDA specialization of the shared owned worker."""

    def __init__(self, min_free_mib=8000, *, hotwords=""):
        super().__init__(min_free_mib, engine="cuda", hotwords=hotwords)

    def ready(self):
        proc = self._process
        return (self._ready and proc is not None and proc.poll() is None
                and not self._cancel.is_set() and not self._failed.is_set())


class WarmCudaController:
    def __init__(self, executor, *, factory=WarmCudaSTT):
        self.executor = executor
        self.factory = factory
        self._lock = threading.RLock()
        self._backend = None
        self._state = "off"
        self._error = ""

    def status(self):
        with self._lock:
            if self._state == "ready" and not self._backend.ready():
                return "failed", "CUDA_STT_WORKER_EXITED"
            return self._state, self._error

    def current(self):
        with self._lock:
            return self._backend if self.status()[0] == "ready" else None

    def activate(self, min_free_mib, *, hotwords="", approved=False):
        if approved is not True:
            raise SpeechFailure("CUDA_EXPLICIT_ACTIVATION_REQUIRED")
        if reason := stt_block_reason():
            raise SpeechFailure(reason)
        with self._lock:
            if self._state != "off":
                return False
            backend = self.factory(min_free_mib, hotwords=hotwords)
            self._backend, self._state, self._error = backend, "loading", ""
            try:
                self.executor.submit(self._load, backend)
            except RuntimeError:
                self._backend, self._state = None, "off"
                raise SpeechFailure("CUDA_STT_START_FAILED") from None
        return True

    def _load(self, backend):
        error = ""
        try:
            backend.ensure()
        except Exception as exc:
            error = str(exc) if isinstance(exc, SpeechFailure) else "CUDA_STT_START_FAILED"
            try:
                backend.close()
            except Exception:
                error = "CUDA_STT_CLEANUP_FAILED"
        with self._lock:
            if self._backend is backend and self._state == "loading":
                self._state, self._error = ("failed" if error else "ready"), error

    def deactivate(self):
        with self._lock:
            if self._backend is None or self._state == "closing":
                return False
            backend = self._backend
            backend.cancel()
            self._state, self._error = "closing", ""
            try:
                self.executor.submit(self._close, backend)
            except RuntimeError:
                # Keep ownership and expose the failure; do not advertise off
                # while a worker may still exist. A later Stop can retry.
                self._state, self._error = "failed", "CUDA_STT_CLEANUP_SCHEDULE_FAILED"
                return False
        return True

    def _close(self, backend):
        try:
            backend.close()
        except Exception:
            with self._lock:
                if self._backend is backend:
                    self._state, self._error = "failed", "CUDA_STT_CLEANUP_FAILED"
        else:
            with self._lock:
                if self._backend is backend:
                    self._backend, self._state, self._error = None, "off", ""
