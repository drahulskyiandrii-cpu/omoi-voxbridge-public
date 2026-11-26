"""Local STT adapters: shared Speech Core service and owned model workers."""
from __future__ import annotations
import base64
import http.client
import json
import os
import shutil
import threading
import time
from pathlib import Path

from .audio import RATE
from .loopback_http import loopback_response, CONNECT_SECONDS
from .gpu import require_free_memory, GpuBusy
from .runtime_paths import standalone_root

class SpeechFailure(RuntimeError):
    pass


def _validate_wav(wav: bytes) -> float:
    """Validate the bounded PCM contract and return actual audio duration."""
    import io
    import wave
    if not isinstance(wav, bytes) or len(wav) > 1024 * 1024:
        raise SpeechFailure("INVALID_WAV_LENGTH")
    try:
        with wave.open(io.BytesIO(wav), "rb") as reader:
            if (reader.getframerate() != RATE or reader.getsampwidth() != 2
                    or reader.getnchannels() != 1
                    or not 3200 <= reader.getnframes() * 2 <= 960000):
                raise SpeechFailure("INVALID_AUDIO_FORMAT")
            if len(reader.readframes(reader.getnframes())) != reader.getnframes() * 2:
                raise SpeechFailure("INVALID_WAV_LENGTH")
            return reader.getnframes() / RATE
    except (wave.Error, EOFError):
        raise SpeechFailure("INVALID_WAV_FORMAT") from None


class SpeechCoreHTTP:
    """Authenticated loopback client; cancellation never stops the shared service."""

    HEALTH_SECONDS = 5.0
    DECODE_SECONDS = 45.0
    MAX_REPLY = 8192

    def __init__(self, port: int = 18194, token: str | None = None):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise SpeechFailure("INVALID_PORT")
        self.port = port
        self.token = token if token is not None else os.environ.get("OMOI_SPEECH_CORE_STT_TOKEN", "")
        if (not isinstance(self.token, str) or not 32 <= len(self.token) <= 512
                or any(not 33 <= ord(ch) <= 126 for ch in self.token)):
            raise SpeechFailure("SPEECH_CORE_TOKEN_NOT_CONFIGURED")
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def cancel(self):
        """GUI-safe: signal only; the request thread polls cancellation."""
        self._cancel.set()

    def _check(self, deadline=None):
        if self._cancel.is_set():
            raise SpeechFailure("STT_CANCELLED")
        if deadline is not None and time.monotonic() >= deadline:
            raise SpeechFailure("STT_REQUEST_TIMEOUT")

    def _request(self, method: str, path: str, data: bytes | None = None, *, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        self._check(deadline)
        headers = {"Authorization": "Bearer " + self.token,
                   "Connection": "close", "Cache-Control": "no-store"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with loopback_response(self.port, method, path, headers=headers, body=data,
                                   check=lambda: self._check(deadline),
                                   connect_timeout=min(CONNECT_SECONDS, timeout)) as response:
                if response.status == 429:
                    raise SpeechFailure("STT_BUSY")
                if response.status != 200:
                    raise SpeechFailure("STT_UNAVAILABLE")
                if response.getheader("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    raise SpeechFailure("STT_BAD_CONTENT_TYPE")
                lengths = [value for key, value in response.getheaders() if key.lower() == "content-length"]
                declared = None
                if lengths:
                    value = lengths[0].strip()
                    if len(lengths) != 1 or not value.isascii() or not value.isdigit() or response.chunked:
                        raise SpeechFailure("STT_BAD_RESPONSE")
                    if len(value) > 10 or int(value) > self.MAX_REPLY:
                        raise SpeechFailure("STT_RESPONSE_TOO_LARGE")
                    declared = int(value)
                raw = response.read(self.MAX_REPLY + 1)
                self._check(deadline)
                if len(raw) > self.MAX_REPLY:
                    raise SpeechFailure("STT_RESPONSE_TOO_LARGE")
                if declared is not None and len(raw) != declared:
                    raise SpeechFailure("STT_INCOMPLETE_RESPONSE")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise SpeechFailure("STT_BAD_RESPONSE")
                self._check(deadline)
                return result
        except SpeechFailure:
            self._check(deadline)
            raise
        except (OSError, http.client.HTTPException, TimeoutError):
            self._check(deadline)
            raise SpeechFailure("STT_CONNECTION_FAILED") from None
        except (UnicodeError, ValueError):
            self._check(deadline)
            raise SpeechFailure("STT_BAD_JSON") from None

    def transcribe(self, wav: bytes, language: str) -> str:
        self._check()
        if language not in {"de", "ru", "en"}:
            raise SpeechFailure("UNSUPPORTED_LANGUAGE")
        _validate_wav(wav)
        if not self._lock.acquire(blocking=False):
            raise SpeechFailure("STT_BUSY")
        try:
            health = self._request("GET", "/health", timeout=self.HEALTH_SECONDS)
            if (health.get("service") != "omoi-speech-core-local-stt"
                    or health.get("model") != "faster-whisper-small-ct2"
                    or health.get("ready") is not True):
                raise SpeechFailure("WRONG_STT_SERVICE")
            self._check()
            payload = json.dumps({"audio_b64": base64.b64encode(wav).decode("ascii"),
                                  "language": language}).encode("utf-8")
            answer = self._request("POST", "/v1/audio/transcriptions", payload,
                                   timeout=self.DECODE_SECONDS)
            text = answer.get("text")
            if (answer.get("model") != "faster-whisper-small-ct2"
                    or answer.get("language") != language or not isinstance(text, str)
                    or "\x00" in text):
                raise SpeechFailure("STT_BAD_RESPONSE")
            try:
                if len(text.encode("utf-8")) > 4096:
                    raise SpeechFailure("STT_BAD_RESPONSE")
            except UnicodeError:
                raise SpeechFailure("STT_BAD_RESPONSE") from None
            self._check()
            return text.strip()
        finally:
            self._lock.release()


def _asset_path(env, key, legacy, default):
    """Explicit portable paths win; development fallbacks remain compatible."""
    value = env.get(key)
    try:
        if value is not None:
            path = Path(value)
            if not value.strip() or not path.is_absolute():
                raise ValueError("absolute path required")
            return path.resolve()
        value = env.get(legacy) if legacy else None
        return Path(value or (default() if callable(default) else default)).resolve()
    except (OSError, RuntimeError, ValueError):
        raise SpeechFailure("STT_ASSET_PATH_INVALID") from None


class LocalSTTBackend:
    """Shared configuration and cancellation for local STT adapters."""
    def __init__(self, min_free_mib: int = 8000, *, engine: str = "cuda", hotwords: str = ""):
        if engine not in ("cuda", "openvino"):
            raise SpeechFailure("INVALID_STT_ENGINE")
        self.engine = engine
        from .stt_enhancements import validate_hotwords
        self.hotwords = validate_hotwords(hotwords) if engine == "cuda" else ""
        self.min_free_mib = min_free_mib
        self._cancel = threading.Event()

    def cancel(self) -> None:
        # Called from GUI: no process wait, shell command or I/O.
        self._cancel.set()

    def _launch_spec(self):
        if self._cancel.is_set():
            raise SpeechFailure("STT_CANCELLED")
        # Check VRAM directly before loading; other speech research has priority.
        if self.engine == "cuda":
            try:
                require_free_memory(self.min_free_mib)
            except GpuBusy as exc:
                raise SpeechFailure(str(exc)) from None
        if self._cancel.is_set():
            raise SpeechFailure("STT_CANCELLED")
        try:
            standalone = standalone_root()
        except ValueError as exc:
            raise SpeechFailure(str(exc)) from None
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "PATH",
                   "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME", "HOMEDRIVE",
                   "HOMEPATH", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
                   "TRANSFORMERS_CACHE", "CUDA_PATH", "CUDA_VISIBLE_DEVICES",
                   "OMOI_MINI_CUDA_STT_PYTHON", "OMOI_MINI_CUDA_STT_MODEL",
                   "OMOI_MINI_CUDA_STT_OVERLAY", "OMOI_MINI_STT_MODEL",
                   "VOXBRIDGE_OPENVINO_SDK_DIR", "VOXBRIDGE_OPENVINO_MODEL_DIR",
                   "VOXBRIDGE_CUDA_PYTHON", "VOXBRIDGE_CUDA_MODEL_DIR", "VOXBRIDGE_CUDA_OVERLAY"}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        if standalone is not None:
            # Never inherit sibling-project defaults or environment overrides.
            env = {k: v for k, v in env.items() if not k.upper().startswith("OMOI_MINI_")}
        if self.engine == "cuda":
            python = _asset_path(env, "VOXBRIDGE_CUDA_PYTHON", "OMOI_MINI_CUDA_STT_PYTHON",
                                 (standalone / "cuda-venv" / "Scripts" / "python.exe" if standalone is not None else
                                  local / "OMOI-Mini" / "tts-eval" / "qwen-venv" / "Scripts" / "python.exe"))
            worker = Path(__file__).with_name("cuda_session_worker.py")
            model = _asset_path(env, "VOXBRIDGE_CUDA_MODEL_DIR", "OMOI_MINI_CUDA_STT_MODEL",
                                (standalone / "faster-whisper-small-ct2" if standalone is not None else
                                 local / "OMOI-Mini" / "stt-eval" / "faster-whisper-small-ct2"))
            overlay = _asset_path(env, "VOXBRIDGE_CUDA_OVERLAY", "OMOI_MINI_CUDA_STT_OVERLAY",
                                  (standalone / "faster-whisper-overlay" if standalone is not None else
                                   local / "OMOI-Mini" / "stt-eval" / "faster-whisper-overlay"))
            env["OMOI_MINI_CUDA_STT_MODEL"] = str(model)
            env["OMOI_MINI_CUDA_STT_OVERLAY"] = str(overlay)
            if (not python.is_file() or not (model / "model.bin").is_file()
                    or not overlay.is_dir()):
                raise SpeechFailure("MINI_WORKER_NOT_INSTALLED")
            command = [str(python), "-I", "-B", "-u", str(worker)]
        else:
            worker = Path(__file__).with_name("openvino_worker.mjs")
            sdk_dir = _asset_path(env, "VOXBRIDGE_OPENVINO_SDK_DIR", "VOXBRIDGE_OPENVINO_SDK_DIR",
                                  lambda: standalone / "openvino-sdk" if standalone is not None else
                                  Path.home() / "Desktop" / "OMOI-Mini" / "local-stt")
            model = _asset_path(env, "VOXBRIDGE_OPENVINO_MODEL_DIR", "OMOI_MINI_STT_MODEL",
                                (standalone / "openvino-whisper-medium-int8-ov" if standalone is not None else
                                 Path(env.get("APPDATA", "")) / "aicompanion" / "local-models" / "openvino-whisper-medium-int8-ov"))
            node = shutil.which("node")
            if not node or not model.is_dir() or not (sdk_dir / "node_modules" / "openvino-genai-node").is_dir():
                raise SpeechFailure("OPENVINO_WORKER_NOT_INSTALLED")
            env["OMOI_MINI_STT_MODEL"] = str(model)
            env["OMOI_MINI_STT_DEVICE"] = "CPU"
            env["VOXBRIDGE_OPENVINO_SDK_DIR"] = str(sdk_dir)
            env["VB_OPENVINO_PROFILE"] = "baseline"
            command = [node, str(worker)]
        if not worker.is_file():
            raise SpeechFailure("MINI_WORKER_NOT_INSTALLED")
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   HF_HUB_DISABLE_IMPLICIT_TOKEN="1", PYTHONDONTWRITEBYTECODE="1")
        return command, worker, env


class MiniOneShotSTT(LocalSTTBackend):
    """One final decode using the same bounded worker protocol as streaming STT."""

    def transcribe(self, wav: bytes, language: str) -> str:
        if language not in ("de", "ru"):
            raise SpeechFailure("MINI_WORKER_SUPPORTS_DE_RU_ONLY")
        seconds = _validate_wav(wav)
        if self._cancel.is_set():
            raise SpeechFailure("STT_CANCELLED")
        # Import at call time to avoid a dependency cycle with the shared base.
        from .local_partial import ResidentMiniSTT
        backend = ResidentMiniSTT(
            self.min_free_mib, engine=self.engine, hotwords=self.hotwords,
            cancel_event=self._cancel, launch_spec=self._launch_spec,
        )
        result = ""
        try:
            result = backend.transcribe_final(wav, language)
            return result
        finally:
            cancelled = self._cancel.is_set()
            try:
                backend.close()
            except SpeechFailure as exc:
                from .dictation_quality import inspect_transcript
                if not cancelled and inspect_transcript(result, seconds) == "STT_TEXT_OK":
                    from .long_dictation import IncompleteDictation
                    raise IncompleteDictation(str(exc), result) from None
                raise
