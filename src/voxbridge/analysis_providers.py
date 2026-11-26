"""Analysis provider contract and bounded Anthropic API adapter.

Cloud requests require *per-request* explicit approval from the caller.
No supplied user material, requests, responses or API keys are logged.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol
from .cloud_budget import cloud_budget, abort_code


PROVIDER_CLEANUP_FAILED = "PROVIDER_CLEANUP_FAILED"


class ProviderFailure(RuntimeError):
    """Sanitized provider error without content or key leakage."""


class ModelProvider(Protocol):
    is_cloud: bool
    def generate(self, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    answer: str


def _parse_answer(answer: object) -> str:
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 24000:
        raise ProviderFailure("LLM_INVALID_RESULT")
    return answer.strip()


class AnthropicAPI:
    """One bounded, cancellable request; no persistent client or retries."""
    is_cloud = True
    URL = "https://api.anthropic.com/v1/messages"
    SECONDS = 60
    MAX_BYTES = 64000

    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if not isinstance(model, str) or not 0 < len(model.strip()) <= 128 or not self.api_key:
            raise ProviderFailure("ANTHROPIC_PROVIDER_NOT_CONFIGURED")
        self._cleanup_failed = False

    def _client(self):
        try:
            import httpx
            from .cloud_http import DeadlineTransport
        except ImportError:
            raise ProviderFailure("ANTHROPIC_HTTPX_NOT_INSTALLED") from None
        return httpx.Client(timeout=self.SECONDS, follow_redirects=False,
                            trust_env=False, transport=DeadlineTransport(
                                self.SECONDS, max_bytes=self.MAX_BYTES))

    def generate(self, system: str, user: str, *, cancel_event=None) -> str:
        if self._cleanup_failed:
            raise ProviderFailure(PROVIDER_CLEANUP_FAILED)
        if not isinstance(system, str) or not isinstance(user, str):
            raise ProviderFailure("LLM_INVALID_INPUT")
        try:
            payload = json.dumps({
                "model": self.model, "system": system, "max_tokens": 1200,
                "messages": [{"role": "user", "content": user}],
            }, ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            raise ProviderFailure("LLM_INVALID_INPUT") from None
        if len(payload) > 60000:
            raise ProviderFailure("LLM_INPUT_TOO_LARGE")
        client = None
        try:
            with cloud_budget(self.SECONDS, cancel_event, max_bytes=self.MAX_BYTES) as budget:
                client = self._client()
                with client.stream("POST", self.URL, content=payload, headers={
                    "x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json", "Cache-Control": "no-store",
                    "Accept-Encoding": "identity",
                }) as response:
                    if response.status_code != 200:
                        raise ProviderFailure("LLM_HTTP_ERROR")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ProviderFailure("LLM_INVALID_ENCODING")
                    raw = bytearray()
                    for chunk in response.iter_raw():
                        budget.check()
                        if len(raw) + len(chunk) > self.MAX_BYTES:
                            raise ProviderFailure("LLM_RESPONSE_TOO_LARGE")
                        raw.extend(chunk)
                budget.check()
                try:
                    obj = json.loads(raw)
                    if not isinstance(obj, dict) or not isinstance(obj.get("content"), list):
                        raise ValueError("invalid content")
                    result = "".join(part["text"] for part in obj["content"]
                                     if isinstance(part, dict) and part.get("type") == "text")
                except (KeyError, TypeError, ValueError, UnicodeError):
                    raise ProviderFailure("LLM_INVALID_RESULT") from None
                return _parse_answer(result)
        except ProviderFailure:
            raise
        except Exception as exc:
            code = abort_code(exc)
            if code == "CLEANUP_FAILED":
                self._cleanup_failed = True
                raise ProviderFailure(PROVIDER_CLEANUP_FAILED) from None
            raise ProviderFailure("ANTHROPIC_" + code if code else
                                  "LLM_CONNECTION_OR_HTTP_ERROR") from None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    self._cleanup_failed = True
                    raise ProviderFailure(PROVIDER_CLEANUP_FAILED) from None


def execute_analysis(request, provider: ModelProvider, *, cloud_approved: bool = False,
                     context: str = "", on_delta=None, cancel_event=None) -> ProviderResult:
    """Cloud permission is not persisted or inherited by a future request."""
    from .analysis import build_prompt
    prompt = build_prompt(request, context)
    if provider.is_cloud and cloud_approved is not True:
        raise ProviderFailure("CLOUD_APPROVAL_REQUIRED")
    if cancel_event is not None and cancel_event.is_set():
        raise ProviderFailure("ANALYSIS_CANCELLED")
    if on_delta is not None:
        stream = getattr(provider, "generate_stream", None)
        if not callable(on_delta) or not callable(stream):
            raise ProviderFailure("ANALYSIS_STREAM_UNSUPPORTED")
        answer = stream(prompt.system, prompt.user, on_delta, cancel_event)
    elif isinstance(provider, AnthropicAPI):
        answer = provider.generate(prompt.system, prompt.user, cancel_event=cancel_event)
    else:
        answer = provider.generate(prompt.system, prompt.user)
    if cancel_event is not None and cancel_event.is_set():
        raise ProviderFailure("ANALYSIS_CANCELLED")
    return ProviderResult(provider=type(provider).__name__, answer=_parse_answer(answer))
