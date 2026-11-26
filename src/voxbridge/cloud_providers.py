"""Opt-in cloud STT/LLM/TTS adapters. Importing this file makes no API calls.

Clients are injected in unit tests. Production uses official OpenAI and Cartesia
Python SDKs; no credentials, request text or audio are logged.
"""
from __future__ import annotations

import os
import re
import io
import wave
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .analysis_providers import ProviderFailure, _parse_answer, PROVIDER_CLEANUP_FAILED
from .stt import SpeechFailure, _validate_wav
from .cloud_budget import cloud_budget, abort_code

_ALLOWED_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LANGUAGES = {"de", "ru", "en"}
_LUNA_PRICE_PER_MILLION = (0.10, 0.50)  # USD, standard text input/output, Nov 2026


def _cloud_error(exc: Exception, service: str) -> str:
    """Never include cloud exception text (may contain user material/headers)."""
    aborted = abort_code(exc)
    if aborted is not None:
        return service + "_" + aborted
    kind = type(exc).__name__
    known = {
        "AuthenticationError": "AUTH",
        "PermissionDeniedError": "PERMISSION",
        "RateLimitError": "RATE_LIMIT",
        "BadRequestError": "BAD_REQUEST",
        "APITimeoutError": "TIMEOUT",
        "APIConnectionError": "NETWORK",
        "TimeoutException": "TIMEOUT",
        "ConnectError": "NETWORK",
    }
    status = getattr(exc, "status_code", None)
    by_status = {400:"BAD_REQUEST",401:"AUTH",402:"PAYMENT_REQUIRED",403:"PERMISSION",404:"NOT_FOUND",408:"TIMEOUT",422:"INVALID_REQUEST",429:"RATE_LIMIT",500:"SERVER_ERROR",502:"SERVER_ERROR",503:"SERVER_ERROR",504:"TIMEOUT"}
    return service + "_" + known.get(kind, by_status.get(status, "UNAVAILABLE") if type(status) is int else "UNAVAILABLE")


def _openai_client(api_key: str, timeout: float):
    try:
        from openai import OpenAI
    except ImportError:
        raise ProviderFailure("OPENAI_SDK_NOT_INSTALLED") from None
    # Prevent environment base-URL overrides and cross-origin redirects.
    import httpx
    from .cloud_http import DeadlineTransport
    transport = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False,
                             transport=DeadlineTransport(2*timeout, max_bytes=2_000_000))
    try:
        return OpenAI(api_key=api_key, base_url="https://api.openai.com/v1",
                      timeout=timeout, max_retries=0, http_client=transport)
    except Exception:
        transport.close()
        raise


def _cartesia_client(api_key: str):
    try:
        from cartesia import Cartesia
    except ImportError:
        raise ProviderFailure("CARTESIA_SDK_NOT_INSTALLED") from None
    import httpx
    from .cloud_http import DeadlineTransport
    transport = httpx.Client(timeout=45, follow_redirects=False, trust_env=False,
                             transport=DeadlineTransport(240))
    try:
        return Cartesia(api_key=api_key, base_url="https://api.cartesia.ai",
                        timeout=45, max_retries=0, http_client=transport)
    except Exception:
        transport.close()
        raise


class _OwnedSDK:
    """Close only the SDK instance created by this adapter, not an injected client."""
    _owns_client = False
    _cleanup_failed = False

    def close(self):
        if self._cleanup_failed:
            raise ProviderFailure(PROVIDER_CLEANUP_FAILED)
        if self._owns_client:
            try:
                self.client.close()
            except Exception:
                # SDKs may mark themselves closed before transport teardown
                # finishes. Never interpret a later no-op close as recovery.
                self._cleanup_failed = True
                raise ProviderFailure(PROVIDER_CLEANUP_FAILED) from None
            self._owns_client = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        error = close_provider(self)
        if error:
            raise ProviderFailure(error) from None


def close_provider(provider) -> str:
    """Return a stable cleanup code; callers must propagate it to completion.

    Keeping cleanup non-throwing here lets a worker always publish its final
    signal and release its UI interlock, without leaking provider diagnostics.
    """
    try:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    except Exception:
        return PROVIDER_CLEANUP_FAILED
    return ""


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_usd: float = 0.0


class OpenAIResponses(_OwnedSDK):
    """Responses API adapter; manual Fast/Deep selection, no automatic fallback."""

    is_cloud = True

    def __init__(self, model: str = "gpt-6-luna", effort: str = "low", *,
                 api_key: str | None = None, client: Any = None,
                 max_output_tokens: int = 1800, timeout: float = 45):
        if not isinstance(model, str) or not _ALLOWED_MODEL.fullmatch(model):
            raise ProviderFailure("OPENAI_INVALID_MODEL_ID")
        if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ProviderFailure("OPENAI_INVALID_EFFORT")
        if not 64 <= max_output_tokens <= 8192:
            raise ProviderFailure("OPENAI_INVALID_OUTPUT_LIMIT")
        self.model, self.effort = model, effort
        self.max_output_tokens = max_output_tokens
        self._request_seconds = 2*timeout
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if not self.api_key and client is None:
            raise ProviderFailure("OPENAI_API_KEY_MISSING")
        self._owns_client = client is None
        self.client = client if client is not None else _openai_client(self.api_key, timeout)
        self.last_usage = TokenUsage()

    def check_model_access(self) -> bool:
        """User-triggered API model access check; never automatically charged inference."""
        try:
            result = self.client.models.retrieve(self.model)
            return getattr(result, "id", None) == self.model
        except Exception as exc:
            raise ProviderFailure(_cloud_error(exc, "OPENAI_MODEL_CHECK")) from None

    def _params(self, system: str, user: str) -> dict:
        if not isinstance(system, str) or not isinstance(user, str):
            raise ProviderFailure("OPENAI_INVALID_INPUT")
        if len(system) > 12000 or len(user) > 30000:
            raise ProviderFailure("OPENAI_INPUT_TOO_LARGE")
        return {
            "model": self.model, "instructions": system, "input": user,
            "reasoning": {"effort": self.effort},
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }

    def _usage(self, response: Any):
        usage = getattr(response, "usage", None)
        inp = max(0, int(getattr(usage, "input_tokens", 0) or 0))
        out = max(0, int(getattr(usage, "output_tokens", 0) or 0))
        estimate = 0.0
        if self.model == "gpt-6-luna":
            estimate = (inp * _LUNA_PRICE_PER_MILLION[0]
                        + out * _LUNA_PRICE_PER_MILLION[1]) / 1_000_000
        self.last_usage = TokenUsage(inp, out, estimate)

    def generate(self, system: str, user: str) -> str:
        try:
            response = self.client.responses.create(**self._params(system, user))
            self._usage(response)
            if getattr(response, "status", None) != "completed":
                raise ProviderFailure("OPENAI_RESPONSE_INCOMPLETE")
            return _parse_answer(getattr(response, "output_text", None))
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(_cloud_error(exc, "OPENAI")) from None

    def generate_stream(self, system: str, user: str, on_delta: Callable[[str], None],
                        cancel_event: threading.Event | None = None) -> str:
        """Collect only response text; on_delta runs in the caller's worker thread."""
        if not callable(on_delta):
            raise ProviderFailure("OPENAI_INVALID_CALLBACK")
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderFailure("OPENAI_CANCELLED")
        total_chars = events = 0
        try:
            with cloud_budget(self._request_seconds, cancel_event, max_bytes=2_000_000):
                with self.client.responses.stream(**self._params(system, user)) as stream:
                    for event in stream:
                        events += 1
                        if events > 32768:
                            raise ProviderFailure("OPENAI_STREAM_EVENT_LIMIT")
                        if cancel_event is not None and cancel_event.is_set():
                            raise ProviderFailure("OPENAI_CANCELLED")
                        kind = getattr(event, "type", "")
                        if kind in ("response.failed", "response.incomplete", "error"):
                            raise ProviderFailure("OPENAI_STREAM_INCOMPLETE")
                        if kind == "response.output_text.delta":
                            part = getattr(event, "delta", "")
                            if isinstance(part, str) and part:
                                total_chars += len(part)
                                if total_chars > 24000:
                                    raise ProviderFailure("LLM_RESPONSE_TOO_LARGE")
                                on_delta(part)
                    if cancel_event is not None and cancel_event.is_set():
                        raise ProviderFailure("OPENAI_CANCELLED")
                    final = stream.get_final_response()
                    if getattr(final, "status", None) != "completed":
                        raise ProviderFailure("OPENAI_STREAM_INCOMPLETE")
                    self._usage(final)
                return _parse_answer(getattr(final, "output_text", None))
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(_cloud_error(exc, "OPENAI")) from None


class OpenAIFileSTT(_OwnedSDK):
    """Completed recording transcription; realtime subtitle transport is separate."""

    is_cloud = True

    def __init__(self, *, api_key: str | None = None, client: Any = None,
                 timeout: float = 50):
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if not key and client is None:
            raise SpeechFailure("OPENAI_API_KEY_MISSING")
        self._cancel = threading.Event()
        self._request_seconds = 2*timeout
        self._owns_client = client is None
        self.client = client if client is not None else _openai_client(key, timeout)

    def cancel(self):
        self._cancel.set()

    def transcribe(self, wav: bytes, language: str = "auto") -> str:
        _validate_wav(wav)
        if language not in _LANGUAGES | {"auto"}:
            raise SpeechFailure("UNSUPPORTED_LANGUAGE")
        languages = [language] if language != "auto" else ["de", "ru", "en"]
        try:
            with cloud_budget(self._request_seconds, self._cancel, max_bytes=2_000_000):
                result = self.client.audio.transcriptions.create(
                    model="gpt-transcribe",
                    file=("recording.wav", wav, "audio/wav"),
                    languages=languages,
                    prompt={"ru": "\u0422\u043e\u0447\u043d\u043e \u0437\u0430\u043f\u0438\u0448\u0438 \u043f\u0440\u043e\u0438\u0437\u043d\u0435\u0441\u0451\u043d\u043d\u044b\u0435 \u0441\u043b\u043e\u0432\u0430. \u041d\u0435 \u0434\u043e\u0431\u0430\u0432\u043b\u044f\u0439 \u0441\u043b\u043e\u0432\u0430 \u0438\u043b\u0438 \u0431\u0443\u043a\u0432\u044b.", "de": "Gesprochene Worte wortgetreu transkribieren, nichts hinzuf\u00fcgen.", "en": "Transcribe spoken words faithfully, without additions."}.get(language, ""),
                )
                transcript = getattr(result, "text", None)
                if not isinstance(transcript, str) or len(transcript) > 24000:
                    raise SpeechFailure("OPENAI_STT_INVALID_RESULT")
                return transcript.strip()
        except SpeechFailure:
            raise
        except Exception as exc:
            raise SpeechFailure(_cloud_error(exc, "OPENAI_STT")) from None


class CartesiaTTSProvider(_OwnedSDK):
    """Sonic-latest bounded WAV generation; no voice cloning."""

    is_cloud = True

    def __init__(self, voice_id: str | None = None, *, api_key: str | None = None,
                 client: Any = None, model: str = "sonic-latest"):
        self.voice_id = voice_id or os.environ.get("CARTESIA_VOICE_ID", "")
        key = api_key if api_key is not None else os.environ.get("CARTESIA_API_KEY", "")
        if not key and client is None:
            raise ProviderFailure("CARTESIA_API_KEY_MISSING")
        if not isinstance(model, str) or not _ALLOWED_MODEL.fullmatch(model):
            raise ProviderFailure("CARTESIA_INVALID_MODEL")
        if not self.voice_id or len(self.voice_id) > 128:
            raise ProviderFailure("CARTESIA_VOICE_ID_MISSING")
        self.model = model
        self._owns_client = client is None
        if client is None:
            client = _cartesia_client(key)
        self.client = client

    def synthesize(self, text: str, language: str = "de") -> bytes:
        if language not in _LANGUAGES:
            raise ProviderFailure("CARTESIA_INVALID_LANGUAGE")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ProviderFailure("CARTESIA_TEXT_LIMIT")
        try:
            data = bytearray()
            with self.client.tts.with_streaming_response.generate(
                model_id=self.model,
                transcript=text,
                voice=self.voice_id,
                language=language,
                output_format={"container": "wav", "encoding": "pcm_s16le", "sample_rate": 24000},
            ) as response:
                for chunk in response.iter_bytes():
                    if not isinstance(chunk, bytes) or len(data) + len(chunk) > 10_000_000:
                        raise ProviderFailure("CARTESIA_INVALID_AUDIO")
                    data.extend(chunk)
            if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                raise ProviderFailure("CARTESIA_INVALID_AUDIO")
            try:
                with wave.open(io.BytesIO(data), "rb") as wav:
                    if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
                        raise ProviderFailure("CARTESIA_INVALID_AUDIO")
                    count = wav.getnframes()
                    if not count or len(wav.readframes(count)) != count * 2:
                        raise ProviderFailure("CARTESIA_INVALID_AUDIO")
            except (wave.Error, EOFError):
                raise ProviderFailure("CARTESIA_INVALID_AUDIO") from None
            return bytes(data)
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(_cloud_error(exc, "CARTESIA")) from None
