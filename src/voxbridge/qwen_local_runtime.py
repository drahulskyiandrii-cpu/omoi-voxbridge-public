"""Session-only explicit activation of owned, offline Qwen local TTS."""
from __future__ import annotations
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import tempfile
import time

from .windows_network import listener_owners
from .gpu import require_free_memory, GpuBusy
from .qwen_local_worker import WORKER_SHAS, DISCONNECT_SAFE_SHA
from .speech_core_tts import SpeechCoreTTS, LocalTTSFailure, MODEL, CF50_MODEL
from .windows_owned_job import KillOnCloseJob
from .loopback_http import loopback_response, CONNECT_SECONDS
from .local_services import mini_qwen_peers, LocalServiceError
from .runtime_paths import standalone_root

PORT = 18190
MODELS = {"qwen_base": MODEL, "qwen_cf050": CF50_MODEL}
PINNED_MINI_COMMIT = "19d191589fd31e50467663e0f0b53e677126c7f7"
PINNED_MINI_BLOB_SHA = "210d6daea3f7d3847a1c40a3bb79be384c2c91339f44eee627858b9b7e75fb15"
# Reviewed Mini D148 adds disconnect-safe stream locking.
BORROWED_WORKER_SHAS = (DISCONNECT_SAFE_SHA,)
CF_SHA = "88a94c0fbe85800d309f94bb9c28d698bf9c86d9494617b6cf4dd996cc13648b"

def _asset_path(key, default):
    """An explicit path must be absolute and must never silently fall back."""
    value = os.environ.get(key)
    if value is None:
        return default() if callable(default) else default
    try:
        path = Path(value)
        if not value.strip() or not path.is_absolute():
            raise ValueError("absolute path required")
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        raise LocalTTSFailure("QWEN_ASSET_PATH_INVALID") from None


def _standalone():
    try:
        return standalone_root()
    except ValueError as exc:
        raise LocalTTSFailure(str(exc)) from None


def runtime_files():
    standalone = _standalone()
    base = standalone if standalone is not None else Path(os.environ["LOCALAPPDATA"]) / "OMOI-Mini" / "tts-eval"
    worker = _asset_path("VOXBRIDGE_QWEN_WORKER",
                         lambda: base / "qwen_server.py" if standalone is not None else
                         Path.home() / "Desktop" / "OMOI-Mini" / "local-tts" / "qwen_server.py")
    python = _asset_path("VOXBRIDGE_QWEN_PYTHON",
                         base / "qwen-venv" / "Scripts" / "python.exe")
    return python, worker, base


def _voice_profile_root(base):
    # The reviewed external worker restricts references to these internal
    # directory names. Isolate that compatibility layout from actual Mini data.
    return base / "qwen-profile" / "OMOI-Mini"


def voice_assets(base):
    standalone = _standalone()
    default = (_voice_profile_root(base) / "tts-eval" / "private-voice-reference" / "voice.wav"
                if standalone is not None else
                base / "private-voice-reference" / "own-voice-reference-part-a.wav")
    reference = _asset_path("VOXBRIDGE_QWEN_REFERENCE", default)
    adapter = _asset_path("VOXBRIDGE_QWEN_CF50_ADAPTER",
                          base / "cf-lora" / "adapter.safetensors")
    return reference, adapter

def immutable_worker(worker, cache, *, allow_git_fallback=True):
    """Use audited bytes; concurrent Mini edits cannot change our launch source."""
    data = worker.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest not in WORKER_SHAS:
        if not allow_git_fallback:
            raise LocalTTSFailure("QWEN_WORKER_CHANGED")
        try:
            data = subprocess.run(
                ["git", "-C", str(worker.parent.parent), "show",
                 PINNED_MINI_COMMIT + ":local-tts/qwen_server.py"],
                check=True, capture_output=True, timeout=4).stdout
        except (OSError, subprocess.SubprocessError):
            raise LocalTTSFailure("QWEN_WORKER_CHANGED") from None
        digest = hashlib.sha256(data).hexdigest()
        if digest != PINNED_MINI_BLOB_SHA:
            raise LocalTTSFailure("QWEN_WORKER_CHANGED")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / ("qwen_server_" + digest + ".py")
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
        return path
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(data)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return path


def verify_files(mode):
    python, worker, base = runtime_files()
    if not python.is_file() or not worker.is_file():
        raise LocalTTSFailure("QWEN_RUNTIME_MISSING")
    reference, adapter = voice_assets(base)
    if not reference.is_file() or reference.is_symlink():
        raise LocalTTSFailure("QWEN_REFERENCE_MISSING")
    if _standalone() is not None:
        profile = _voice_profile_root(base)
        allowed = (profile / "tts-eval" / "private-voice-reference", profile / "voices")
        if not any(reference.resolve().is_relative_to(directory.resolve()) for directory in allowed):
            raise LocalTTSFailure("QWEN_REFERENCE_OUTSIDE_STANDALONE_PROFILE")
    if mode == "qwen_cf050":
        if (not adapter.is_file() or adapter.stat().st_size != 34908456
                or hashlib.sha256(adapter.read_bytes()).hexdigest() != CF_SHA):
            raise LocalTTSFailure("QWEN_CF50_ADAPTER_INVALID")
    from .config import config_path
    cache = config_path().parent / "runtime"
    return python, immutable_worker(worker, cache,
                                    allow_git_fallback=_standalone() is None and "VOXBRIDGE_QWEN_WORKER" not in os.environ)

def child_env(mode, token, name):
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "PATH",
               "LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME", "HOMEDRIVE",
               "HOMEPATH", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
               "TRANSFORMERS_CACHE", "CUDA_PATH", "CUDA_VISIBLE_DEVICES"}
    env = {k:v for k,v in os.environ.items() if k.upper() in allowed}
    # Translate explicit user configuration into the reviewed worker's
    # existing asset contract. Never forward arbitrary provider credentials.
    standalone = _standalone()
    if standalone is not None:
        reference, adapter = voice_assets(standalone)
        env.update(LOCALAPPDATA=str(standalone / "qwen-profile"),
                   OMOI_MINI_QWEN_REFERENCE=str(reference),
                   OMOI_MINI_QWEN_CF50_ADAPTER=str(adapter))
    else:
        for key, target in (("VOXBRIDGE_QWEN_REFERENCE", "OMOI_MINI_QWEN_REFERENCE"),
                            ("VOXBRIDGE_QWEN_CF50_ADAPTER", "OMOI_MINI_QWEN_CF50_ADAPTER")):
            if key in os.environ:
                env[target] = str(_asset_path(key, None))
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               HF_HUB_DISABLE_IMPLICIT_TOKEN="1", PYTHONDONTWRITEBYTECODE="1",
               OMOI_MINI_QWEN_SIZE="1.7B",
               OMOI_MINI_QWEN_VARIANT="cf050" if mode == "qwen_cf050" else "base",
               VOXBRIDGE_QWEN_TOKEN=token, VOXBRIDGE_QWEN_JOB=name)
    return env

class QwenRuntime:
    def __init__(self, *, verify=verify_files, guard=require_free_memory,
                 owners=listener_owners, job_factory=KillOnCloseJob,
                 popen=subprocess.Popen, connection=http.client.HTTPConnection, reuse=False,
                 discover_shared=None):
        self._verify, self._guard, self._owners = verify, guard, owners
        self._job_factory, self._popen, self._connection = job_factory, popen, connection
        self._lock = threading.RLock()
        self._stream_lock = threading.Lock()
        self._job = self._child = self._token = self._mode = None
        self._ready = False
        self._peer = None
        self._disconnect_safe = False
        self._reuse = reuse
        self._discover_shared = discover_shared or self._shared_peers
        self._activating = False
        self._epoch = 0
        self._cleanup_failed = False

    def ready(self, mode):
        # UI readiness is advisory. Never wait for a native spawn/cleanup
        # holding the ownership lock; stream() revalidates in its worker.
        if not self._lock.acquire(blocking=False):
            return False
        try:
            return self._is_ready(mode)
        finally:
            self._lock.release()

    def _is_ready(self, mode):
        return bool(self._ready and self._mode == mode and
                    (self._peer is not None or (self._child and self._child.poll() is None)))

    def _identity(self):
        with self._lock:
            if self._peer is not None:
                self._peer.verify()
                return None
            if self._job is None or self._child is None or self._child.poll() is not None:
                raise LocalTTSFailure("QWEN_NOT_RUNNING")
            owners = self._owners(PORT)
            if len(owners) != 1 or owners[0][0] != "127.0.0.1" or not self._job.contains_pid(owners[0][1]):
                raise LocalTTSFailure("QWEN_LISTENER_IDENTITY")
            return self._token

    def _call(self, path, cancel, *, timeout, unauthorized=False):
        deadline = time.monotonic() + timeout

        def check():
            if cancel.is_set():
                raise LocalTTSFailure("QWEN_CANCELLED")
            if time.monotonic() >= deadline:
                raise LocalTTSFailure("QWEN_REQUEST_TIMEOUT")

        check()
        key = self._identity()
        headers = {"Connection": "close"}
        peer = self._peer
        if not unauthorized and key is not None:
            headers["Authorization"] = "Bearer " + key
        try:
            with loopback_response(peer.port if peer is not None else PORT, "POST" if path == "/warmup" else "GET", path,
                                   headers=headers, check=check,
                                   connect_timeout=min(CONNECT_SECONDS, timeout),
                                   connection_factory=self._connection,
                                   verify_connected=peer.verify if peer is not None else None) as response:
                self._identity()
                if unauthorized:
                    if response.status != 401:
                        raise LocalTTSFailure("QWEN_AUTH_NOT_ENFORCED")
                    return {}
                if response.status == 429:
                    raise LocalTTSFailure("LOCAL_TTS_BUSY")
                if response.status != 200:
                    if path == "/warmup" and response.status == 503:
                        raw = response.read(8193)
                        if len(raw) <= 8192:
                            try:
                                data = json.loads(raw)
                                code = data.get("detail") if isinstance(data, dict) else None
                            except (ValueError, UnicodeError):
                                code = None
                            if code in ("CUDA_UNAVAILABLE", "CUDA_OUT_OF_MEMORY",
                                        "PRIVATE_VOICE_REFERENCE_MISSING", "CF050_ADAPTER_INVALID",
                                        "OFFLINE_MODEL_MISSING", "DEPENDENCY_MISSING",
                                        "LOCAL_QWEN_WARMUP_FAILED"):
                                raise LocalTTSFailure("QWEN_" + code)
                    raise LocalTTSFailure("QWEN_SERVICE_FAILED")
                raw = response.read(8193)
                if len(raw) > 8192:
                    raise LocalTTSFailure("QWEN_RESPONSE_LIMIT")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise LocalTTSFailure("QWEN_BAD_RESPONSE") from None
                if not isinstance(data, dict):
                    raise LocalTTSFailure("QWEN_BAD_RESPONSE")
                check()
                return data
        except LocalTTSFailure:
            raise
        except Exception:
            check()
            raise LocalTTSFailure("QWEN_SERVICE_FAILED") from None

    @staticmethod
    def _check_health(data, mode, *, loaded):
        if (data.get("service") != "omoi-mini-local-tts" or data.get("model") != MODELS[mode]
                or data.get("offline") is not True
                or (loaded and (data.get("loaded") is not True or data.get("state") != "ready"))):
            raise LocalTTSFailure("QWEN_MODEL_NOT_READY")

    @staticmethod
    def _shared_peers():
        python, worker, _ = runtime_files()
        return mini_qwen_peers(python, worker, BORROWED_WORKER_SHAS)

    def _borrow(self, mode, cancel, epoch):
        """Use a verified Mini server with the same selected voice and model."""
        if not self._reuse:
            return False
        _, _, base = runtime_files()
        reference = voice_assets(base)[0].resolve()
        voice = hashlib.sha256(str(reference).encode("utf-8")).hexdigest()[:12]
        for peer in self._discover_shared():
            if cancel.is_set():
                raise LocalTTSFailure("QWEN_CANCELLED")
            try:
                data = peer.get_json("/health", cancel)
            except LocalServiceError:
                raise LocalTTSFailure("QWEN_SHARED_NOT_READY") from None
            if (data.get("service") != "omoi-mini-local-tts" or data.get("model") != MODELS[mode]
                    or data.get("offline") is not True or data.get("voice_profile") != voice):
                continue
            with self._lock:
                if cancel.is_set() or epoch != self._epoch:
                    raise LocalTTSFailure("QWEN_CANCELLED")
                self._peer, self._mode = peer, mode
                self._disconnect_safe = True
            if data.get("loaded") is not True or data.get("state") != "ready":
                # No duplicate model if the trusted service is still cold.
                try:
                    self._guard()
                except GpuBusy as exc:
                    raise LocalTTSFailure(str(exc)) from None
                self._check_health(self._call("/warmup", cancel, timeout=180), mode, loaded=True)
            with self._lock:
                if cancel.is_set() or epoch != self._epoch or self._peer is not peer:
                    raise LocalTTSFailure("QWEN_CANCELLED")
                self._ready = True
            return True
        return False

    def activate(self, mode, cancel, *, approved=False):
        if approved is not True or mode not in MODELS:
            raise LocalTTSFailure("QWEN_EXPLICIT_ACTIVATION_REQUIRED")
        with self._lock:
            if self._cleanup_failed:
                raise LocalTTSFailure("QWEN_CLEANUP_FAILED")
            if self._activating or self._stream_lock.locked():
                raise LocalTTSFailure("QWEN_ALREADY_ACTIVE")
            if self._peer is not None:
                self.close()
            if self._job is not None or self._child is not None:
                if self._child is not None and self._child.poll() is None:
                    raise LocalTTSFailure("QWEN_ALREADY_ACTIVE")
                # A crashed worker must not permanently block an explicit retry.
                self.close()
            self._activating = True
            epoch = self._epoch
        try:
            if cancel.is_set():
                raise LocalTTSFailure("QWEN_CANCELLED")
            if self._borrow(mode, cancel, epoch):
                return
            python, worker = self._verify(mode)
            disconnect_safe = worker.name == "qwen_server_" + DISCONNECT_SAFE_SHA + ".py"
            try:
                self._guard()
            except GpuBusy as exc:
                raise LocalTTSFailure(str(exc)) from None
            if cancel.is_set():
                raise LocalTTSFailure("QWEN_CANCELLED")
            if self._owners(PORT):
                raise LocalTTSFailure("QWEN_PORT_BUSY_CLOSE_OTHER_TTS")
            token = secrets.token_urlsafe(32)
            name = "Local\\VoxBridge-Qwen-" + secrets.token_hex(16)
            with self._lock:
                if cancel.is_set() or epoch != self._epoch:
                    raise LocalTTSFailure("QWEN_CANCELLED")
                self._job = self._job_factory(name)
                self._mode, self._token = mode, token
                self._child = self._popen(
                    [str(python), "-I", "-B", str(Path(__file__).with_name("qwen_local_worker.py")), str(worker)],
                    cwd=str(Path(__file__).parent), env=child_env(mode, token, name),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self._job.assign(self._child)
                self._child.stdin.write(b"GO\n")
                self._child.stdin.close()
            deadline = time.monotonic() + 25
            while not cancel.is_set() and time.monotonic() < deadline:
                with self._lock:
                    if self._child is None or self._child.poll() is not None:
                        raise LocalTTSFailure("QWEN_WORKER_EXITED")
                if self._owners(PORT):
                    self._identity()
                    break
                cancel.wait(.1)
            else:
                raise LocalTTSFailure("QWEN_START_TIMEOUT")
            self._call("/health", cancel, timeout=3, unauthorized=True)
            self._check_health(self._call("/health", cancel, timeout=3), mode, loaded=False)
            self._check_health(self._call("/warmup", cancel, timeout=180), mode, loaded=True)
            with self._lock:
                if cancel.is_set() or epoch != self._epoch or self._job is None:
                    raise LocalTTSFailure("QWEN_CANCELLED")
                self._disconnect_safe = disconnect_safe
                self._ready = True
        except Exception as exc:
            self.close()
            if isinstance(exc, LocalTTSFailure):
                raise
            raise LocalTTSFailure("QWEN_ACTIVATION_FAILED") from None
        finally:
            with self._lock:
                self._activating = False

    def stream(self, mode, text, language, callback, stop):
        """One synthesis at a time; an interrupted request cannot leave a warm server busy."""
        if not self._stream_lock.acquire(blocking=False):
            raise LocalTTSFailure("QWEN_SYNTHESIS_BUSY")
        try:
            with self._lock:
                if not self._is_ready(mode) or stop.is_set():
                    raise LocalTTSFailure("QWEN_ENABLE_MODEL_FIRST")
                epoch = self._epoch
            try:
                self._check_health(self._call("/health", stop, timeout=3), mode, loaded=True)
                key = self._identity()
                peer = self._peer
                adapter = SpeechCoreTTS(port=peer.port if peer is not None else PORT, token=key, model=MODELS[mode],
                                        connection_factory=self._connection, peer=peer)
                # After a cancelled request the reviewed worker keeps its
                # generation lock until the native iteration has finished.
                # Retry only explicit 429, which proves no PCM was produced.
                until = time.monotonic() + 8
                while True:
                    try:
                        return adapter.stream(text, language, callback, authorized=True, stop=stop)
                    except LocalTTSFailure as exc:
                        if (str(exc) != "LOCAL_TTS_BUSY" or not self._disconnect_safe
                                or time.monotonic() >= until or stop.wait(.1)):
                            raise
            except Exception as exc:
                # Disconnect alone does not prove that the server stopped CUDA
                # work. Reap this exact owned model before permitting another
                # request; successful synthesis still keeps it warm.
                with self._lock:
                    if self._cleanup_failed:
                        raise LocalTTSFailure("QWEN_CLEANUP_FAILED") from None
                    safe_stop = self._disconnect_safe and isinstance(exc, LocalTTSFailure) and (
                        str(exc) == "LOCAL_TTS_BUSY" or (stop.is_set() and str(exc) in
                            ("LOCAL_TTS_CANCELLED", "QWEN_CANCELLED")))
                    if self._epoch == epoch and not safe_stop:
                        self.close()
                raise
        finally:
            self._stream_lock.release()

    def close(self):
        """Worker-only teardown; retain exact handles until verified exit."""
        with self._lock:
            self._epoch += 1
            self._ready = False
            self._disconnect_safe = False
            self._mode = self._token = None
            self._peer = None  # borrowed service belongs to another program
            try:
                if self._job is not None:
                    self._job.terminate_and_close()
                    self._job = None
                if self._child is not None:
                    child = self._child
                    # Assignment may have failed. Close its unopened gate and
                    # terminate only this exact Popen child, never by PID.
                    if child.stdin and not child.stdin.closed:
                        child.stdin.close()
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=2)
                    self._child = None
            except Exception:
                self._cleanup_failed = True
                raise LocalTTSFailure("QWEN_CLEANUP_FAILED") from None
            self._cleanup_failed = False
