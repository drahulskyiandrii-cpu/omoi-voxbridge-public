"""Explicit read-only discovery of voices owned by the user's Cartesia account.

Fetching metadata is only performed after a user click, never at import/startup.
No voice recordings, samples, embeddings or cloned voices are downloaded.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .analysis_providers import ProviderFailure
from .cloud_budget import CloudAbort, cloud_budget

_VOICE_UUID = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
_URL = "https://api.cartesia.ai/voices"
_API_VERSION = "2026-08-14"
_MAX_PAGES = 4
_MAX_BODY = 150_000
_REQUEST_SECONDS = 30
VOICE_CLEANUP_FAILED = "CARTESIA_VOICES_CLEANUP_FAILED"


@dataclass(frozen=True)
class OwnedVoice:
    name: str
    voice_id: str
    language: str


def valid_voice_id(voice_id: str) -> bool:
    return isinstance(voice_id, str) and _VOICE_UUID.fullmatch(voice_id) is not None


def list_owned_voices(*, api_key: str | None = None, cancel_event=None,
                      transport=None) -> list[OwnedVoice]:
    """Explicit metadata discovery, with one deadline across all pages.

    The optional HTTPX transport is for offline contract tests only.
    """
    key = api_key if api_key is not None else os.environ.get("CARTESIA_API_KEY", "")
    if not key:
        raise ProviderFailure("CARTESIA_API_KEY_MISSING")
    try:
        import httpx
        from .cloud_http import DeadlineTransport
    except ImportError:
        raise ProviderFailure("CARTESIA_SDK_NOT_INSTALLED") from None
    try:
        client = httpx.Client(
            transport=transport if transport is not None else DeadlineTransport(
                _REQUEST_SECONDS, max_bytes=_MAX_BODY),
            timeout=15, follow_redirects=False, trust_env=False,
            headers={"Authorization": "Bearer " + key,
                     "Cartesia-Version": _API_VERSION,
                     "Cache-Control": "no-store", "Accept-Encoding": "identity"},
        )
        try:
            with cloud_budget(_REQUEST_SECONDS, cancel_event, max_bytes=_MAX_BODY) as budget:
                return _fetch_pages(client, budget)
        finally:
            try:
                client.close()
            except Exception:
                raise ProviderFailure(VOICE_CLEANUP_FAILED) from None
    except CloudAbort as exc:
        raise ProviderFailure("CARTESIA_VOICES_" + str(exc)) from None
    except httpx.HTTPError:
        raise ProviderFailure("CARTESIA_VOICES_NETWORK_ERROR") from None
    except (ValueError, UnicodeError):
        raise ProviderFailure("CARTESIA_VOICES_INVALID_RESPONSE") from None


def _fetch_pages(client, budget):
    selected: dict[str, OwnedVoice] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(_MAX_PAGES):
        params = {"limit": "100", "is_owner": "true"}
        if cursor:
            params["starting_after"] = cursor
        budget.check()
        with client.stream("GET", _URL, params=params) as response:
            if response.status_code in (401, 403):
                raise ProviderFailure("CARTESIA_VOICES_AUTH")
            if response.status_code == 429:
                raise ProviderFailure("CARTESIA_VOICES_RATE_LIMIT")
            if response.status_code != 200:
                raise ProviderFailure("CARTESIA_VOICES_HTTP_ERROR")
            # No compressed metadata: avoid an unbounded decompression expansion.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ProviderFailure("CARTESIA_VOICES_INVALID_RESPONSE")
            body = bytearray()
            for chunk in response.iter_bytes():
                budget.check()
                if len(body) + len(chunk) > _MAX_BODY:
                    raise ProviderFailure("CARTESIA_VOICES_RESPONSE_TOO_LARGE")
                body.extend(chunk)
            obj = json.loads(body)
        budget.check()
        if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
            raise ProviderFailure("CARTESIA_VOICES_INVALID_RESPONSE")
        for item in obj["data"]:
            if not isinstance(item, dict) or item.get("is_owner") is not True:
                continue
            identity = item.get("id")
            name = item.get("name")
            language = item.get("language")
            if (valid_voice_id(identity) and isinstance(name, str)
                    and 0 < len(name) <= 100 and isinstance(language, str)
                    and 1 <= len(language) <= 20):
                selected[identity] = OwnedVoice(name=name, voice_id=identity, language=language)
        if type(obj.get("has_more")) is not bool:
            raise ProviderFailure("CARTESIA_VOICES_INVALID_RESPONSE")
        if not obj["has_more"]:
            break
        cursor = obj.get("next_page")
        if not valid_voice_id(cursor) or cursor in seen_cursors:
            raise ProviderFailure("CARTESIA_VOICES_INVALID_CURSOR")
        seen_cursors.add(cursor)
    else:
        raise ProviderFailure("CARTESIA_VOICES_TOO_MANY_PAGES")
    return sorted(selected.values(), key=lambda v: (v.name.casefold(), v.voice_id))
