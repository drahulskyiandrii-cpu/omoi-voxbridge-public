"""Mini-compatible per-recording resident STT and cumulative hypotheses.

One owned process, one in-flight request; no model loaded until explicit local
recording. Snapshots and results live only in RAM. Final decode replaces every
partial, and a cancelled turn never produces an insertion.
"""
from __future__ import annotations

import base64
import json
import queue
import threading
import time

from .stt import LocalSTTBackend, SpeechFailure, _validate_wav
from .stt_process import spawn_owned_stt, quarantine_stt, stt_block_reason, STT_CLEANUP_FAILED
from .dictation_quality import inspect_transcript


class ResidentMiniSTT(LocalSTTBackend):
    """Reuse Mini's stdin protocol without repeatedly loading Whisper."""

    def __init__(self, min_free_mib=8000, *, engine="cuda", enhanced=False, hotwords="", on_delta=None,
                 cancel_event=None, launch_spec=None):
        super().__init__(min_free_mib, engine=engine)
        from .stt_enhancements import validate_hotwords
        self.enhanced = enhanced and engine == "openvino"
        self.hotwords = validate_hotwords(hotwords) if self.enhanced or engine == "cuda" else ""
        self.distinct_final = engine == "cuda"
        self.on_delta = on_delta
        self.native_chunks = 0
        self.last_score = None
        self._messages = queue.Queue(maxsize=128)
        self._reader = None
        self._writer = None
        self._serial = 0
        self._failed = threading.Event()
        self._process = self._job = None
        self._ready = False
        self._cleanup_failed = False
        if cancel_event is not None:
            self._cancel = cancel_event
        self._custom_launch_spec = launch_spec

    def _launch_spec(self):
        command, worker, env = (self._custom_launch_spec or super()._launch_spec)()
        if self.enhanced:
            env = {**env, "VB_OPENVINO_PROFILE": "enhanced"}
        return command, worker, env

    def _read(self, proc):
        try:
            while not self._cancel.is_set():
                line = proc.stdout.readline(32769)
                if not line:
                    break
                if len(line) > 32768 or not line.endswith("\n"):
                    self._failed.set()
                    return
                try:
                    msg = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    self._messages.put_nowait(msg)
                except queue.Full:
                    self._failed.set()
                    return
        except (OSError, ValueError, UnicodeError):
            self._failed.set()
        finally:
            self._failed.set()

    def _check(self):
        if self._cancel.is_set():
            raise SpeechFailure("STT_CANCELLED")

    def _receive(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self._check()
            if time.monotonic() >= deadline:
                raise SpeechFailure("LOCAL_PARTIAL_TIMEOUT")
            try:
                message = self._messages.get(timeout=min(.05, max(0, deadline-time.monotonic())))
                self._check()
                if time.monotonic() >= deadline:
                    raise SpeechFailure("LOCAL_PARTIAL_TIMEOUT")
                return message
            except queue.Empty:
                if self._failed.is_set():
                    raise SpeechFailure("LOCAL_PARTIAL_WORKER_EXITED")

    def _spawn(self, command, worker, env):
        try:
            proc, self._job = spawn_owned_stt(command, worker, env, self._cancel)
            return proc
        except SpeechFailure:
            raise
        except Exception:
            self._check()
            raise SpeechFailure("STT_OWNED_JOB_REQUIRED") from None

    def ensure(self):
        if stt_block_reason():
            raise SpeechFailure(STT_CLEANUP_FAILED)
        self._check()
        if self._process is not None:
            if not self._ready or self._process.poll() is not None or self._failed.is_set():
                raise SpeechFailure("LOCAL_PARTIAL_WORKER_EXITED")
            return
        command, worker, env = self._launch_spec()
        self._check()
        try:
            self._process = self._spawn(command, worker, env)
            proc = self._process
            self._reader = threading.Thread(target=self._read, args=(proc,), daemon=True,
                                            name="voxbridge-stt-reader")
            self._reader.start()
            msg = self._receive(95)
            if msg.get("event") != "ready":
                code = msg.get("error")
                if msg.get("event") == "fatal" and code in (
                        "CUDA_STT_RUNTIME_NOT_READY", "CUDA_STT_MODEL_MISSING",
                        "CUDA_UNAVAILABLE", "MULTILINGUAL_WHISPER_REQUIRED",
                        "OPENVINO_START_FAILED", "STT_MODEL_OR_RUNTIME_UNAVAILABLE",
                        "STT_MODEL_PATH_NOT_CONFIGURED"):
                    raise SpeechFailure(code)
                raise SpeechFailure("LOCAL_PARTIAL_NOT_READY")
            self._check()
            self._ready = True
        except Exception as exc:
            self.cancel()
            self.close()
            if isinstance(exc, SpeechFailure):
                raise
            raise SpeechFailure("LOCAL_PARTIAL_START_FAILED") from None

    def transcribe_final(self, wav, language):
        return self.transcribe(wav, language, final=True)

    def transcribe(self, wav, language, *, final=False):
        if language not in ("de", "ru"):
            raise SpeechFailure("MINI_WORKER_SUPPORTS_DE_RU_ONLY")
        seconds = _validate_wav(wav)
        self.ensure()
        self._serial += 1
        rid = self._serial
        request = {"id":rid, "audio":base64.b64encode(wav).decode("ascii"), "language":language}
        if self.enhanced:
            request.update(hotwords=self.hotwords, stream=self.on_delta is not None)
        elif self.engine == "cuda":
            request["options"] = {"beam_size": 5 if final else 1, "hotwords": self.hotwords}
        data = json.dumps(request) + "\n"
        proc = self._process
        error = threading.Event()
        def write():
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                error.set()
                self._failed.set()
        self._writer = threading.Thread(target=write, daemon=True, name="voxbridge-stt-writer")
        self._writer.start()
        deadline = time.monotonic()+100
        while True:
            msg = self._receive(max(.01,deadline-time.monotonic()))
            if msg.get('event') != 'partial':
                break
            draft = msg.get('text')
            if (not self.enhanced or type(msg.get('id')) is not int or msg['id'] != rid
                    or not isinstance(draft,str) or len(draft)>3000 or '\x00' in draft
                    or time.monotonic()>deadline):
                raise SpeechFailure('LOCAL_PARTIAL_BAD_REPLY')
            self._check()
            self.native_chunks += 1
            if self.on_delta and inspect_transcript(draft, seconds) == 'STT_TEXT_OK':
                self.on_delta(draft)
        self._writer.join(.5)
        if self._writer.is_alive() or error.is_set():
            raise SpeechFailure("LOCAL_PARTIAL_WRITE_FAILED")
        self._check()
        if type(msg.get("id")) is not int or msg["id"] != rid or msg.get("ok") is not True:
            raise SpeechFailure("LOCAL_PARTIAL_BAD_REPLY")
        if self.enhanced:
            import math
            score=msg.get('score')
            self.last_score=score if type(score) in (int,float) and math.isfinite(score) else None
        text = msg.get("text")
        if not isinstance(text,str) or len(text)>3000 or "\x00" in text:
            raise SpeechFailure("LOCAL_PARTIAL_BAD_REPLY")
        return text.strip()

    def close(self):
        """Verify owned tree exit before joining I/O threads or closing pipes."""
        self._ready = False
        if self._cleanup_failed:
            raise SpeechFailure(STT_CLEANUP_FAILED)
        proc = self._process
        if proc is None:
            return
        self._cancel.set()
        try:
            if self._job is None:
                raise SpeechFailure(STT_CLEANUP_FAILED)
            # No graceful stdin flush before termination: a blocked worker or
            # a pending buffered write can otherwise hold a pipe lock forever.
            self._job.terminate_and_close()
            self._job = None
            proc.wait(timeout=2)
            for thread in (self._writer, self._reader):
                if thread is not None and thread.ident is not None:
                    thread.join(1)
                    if thread.is_alive():
                        raise SpeechFailure(STT_CLEANUP_FAILED)
            for pipe in (proc.stdin, proc.stdout):
                if pipe is not None:
                    pipe.close()
        except Exception:
            self._cleanup_failed = True
            quarantine_stt(self)
            raise SpeechFailure(STT_CLEANUP_FAILED) from None
        self._process = None
        self._reader = self._writer = None
        # Discard any unread private hypotheses from this ended session.
        while True:
            try:
                self._messages.get_nowait()
            except queue.Empty:
                break


class LocalPartialTurn:
    """Run until final audio supplied or cancelled; no queued stale snapshots."""
    def __init__(self, backend, snapshot, language, on_partial, *, interval=1.2, keep_resident=False):
        if not .05 <= interval <= 10:
            raise ValueError("INVALID_PARTIAL_INTERVAL")
        self.backend = backend
        self.keep_resident = keep_resident
        self.snapshot = snapshot
        self.language = language
        self.on_partial = on_partial
        self.interval = interval
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._cancelled = False
        self._final = None
        self._started = time.monotonic()
        self._ready_at = None
        self._stop_at = None
        self._first_at = None
        self._finished_at = None
        self._final_ms = None
        self._decoded = 0
        self._valid = 0
        self._published = 0
        self._reused = False
        self._agreement_words = 0
        self._last_words = 0

    def summary(self):
        """Timing and counts only; never include speech or audio."""
        with self._lock:
            def elapsed(a, b):
                return "-" if a is None or b is None else str(round((b-a)*1000))
            return (f"Partial: {self._published} emitted/{self._valid} valid/{self._decoded} decoded; "
                    f"warmup={elapsed(self._started,self._ready_at)}ms; "
                    f"first={elapsed(self._started,self._first_at)}ms; "
                    f"stop-final={elapsed(self._stop_at,self._finished_at)}ms; "
                    f"final decode={self._final_ms if self._final_ms is not None else '-'}ms; "
                    f"same audio reuse={self._reused}; prefix={self._agreement_words}/{self._last_words}; "
                    f"native chunks={getattr(self.backend,'native_chunks',0) if isinstance(self.backend,ResidentMiniSTT) else 0}")

    def finalize(self, wav):
        _validate_wav(wav)
        with self._lock:
            if self._cancelled or self._final is not None:
                return False
            self._final = wav
            self._stop_at = time.monotonic()
        self._wake.set()
        return True

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._final = None
        self.backend.cancel()
        self._wake.set()

    def run(self):
        previous_size = 0
        previous_text = ""
        last_wav, last_text = None, ""
        last_valid = False
        success = False
        result = ""
        try:
            self.backend.ensure()
            with self._lock:
                self._ready_at = time.monotonic()
            while True:
                with self._lock:
                    if self._cancelled:
                        raise SpeechFailure("STT_CANCELLED")
                    final = self._final
                if final is not None:
                    start = time.monotonic()
                    # Only an exact byte-for-byte full recording can reuse a decode.
                    distinct = isinstance(self.backend, ResidentMiniSTT) and self.backend.distinct_final
                    reuse = not distinct and last_wav == final and last_valid
                    if distinct:
                        result = self.backend.transcribe_final(final, self.language)
                    else:
                        result = last_text if reuse else self.backend.transcribe(final, self.language)
                    left, right = previous_text.split(), result.split()
                    agreed = 0
                    for a, b in zip(left, right):
                        if a.casefold().strip(".,!?;:") != b.casefold().strip(".,!?;:"):
                            break
                        agreed += 1
                    with self._lock:
                        self._reused = reuse
                        self._final_ms = round((time.monotonic()-start)*1000)
                        self._finished_at = time.monotonic()
                        self._agreement_words = agreed
                        self._last_words = len(left)
                        if self._cancelled:
                            raise SpeechFailure("STT_CANCELLED")
                    success = True
                    return result
                wav = self.snapshot()
                # Only one decode at a time. Do not repeatedly decode unchanged
                # audio, or queue a backlog if CPU/GPU is slower than capture.
                # Cumulative CUDA snapshots re-decode all audio. Require a
                # meaningful amount of new speech to avoid excessive work.
                min_growth = 64000 if isinstance(self.backend, ResidentMiniSTT) and self.backend.engine == "cuda" and previous_size else 1
                if wav and len(wav) - previous_size >= min_growth:
                    previous_size = len(wav)
                    text = self.backend.transcribe(wav, self.language)
                    last_wav, last_text = wav, text
                    valid = inspect_transcript(text, _validate_wav(wav)) == "STT_TEXT_OK"
                    last_valid = valid
                    with self._lock:
                        self._decoded += 1
                        self._valid += int(valid)
                        publish = not self._cancelled and self._final is None and valid and text != previous_text
                        if publish:
                            self._published += 1
                            if self._first_at is None:
                                self._first_at = time.monotonic()
                    if publish:
                        previous_text = text
                        self.on_partial(text)
                # A final submitted during decoding is already signalled.
                with self._lock:
                    pending = self._final is not None or self._cancelled
                if not pending:
                    self._wake.wait(self.interval)
                self._wake.clear()
        finally:
            with self._lock:
                self._final = None
            if not (success and self.keep_resident and not self._cancelled):
                try:
                    self.backend.close()
                except SpeechFailure as exc:
                    if (success and not self._cancelled
                            and inspect_transcript(result, _validate_wav(final)) == "STT_TEXT_OK"):
                        from .long_dictation import IncompleteDictation
                        raise IncompleteDictation(str(exc), result) from None
                    raise
