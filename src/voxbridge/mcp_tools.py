"""Functions exposed to external MCP hosts, intentionally without screen access.

MCP is a *tool* protocol, not a standalone LLM. The host model can process the
selected text after the user explicitly supplies it. No automatic clipboard,
screenshot, file scanning or cloud calls occur in these tools.
"""
from __future__ import annotations

from .analysis import AnalysisRequest, build_prompt, supported_operations
from .captions_core import TranslationJob
from .captions_translate import caption_translation_prompt
import threading


def capability_manifest() -> dict:
    return {
        "service": "OMOI VoxBridge",
        "mode": "explicit-text-only",
        "operations": supported_operations(),
        "can_read_screen": False,
        "can_read_clipboard": False,
        "can_execute_commands": False,
        "can_read_files": False,
        "can_capture_audio": False,
        "can_send_cloud_requests": False,
        "can_read_live_selection": False,
        "cloud_default": False,
        "note": "Host must supply user-authorized text. Local selection bridge is not implemented.",
    }


def prepare_study_prompt(text: str, operation: str = "explain",
                         question: str = "", response_language: str = "ru",
                         target_language: str = "ru") -> dict:
    prompt = build_prompt(AnalysisRequest(
        text=text, operation=operation, question=question,
        response_language=response_language,
        target_language=target_language,
    ))
    return {"system": prompt.system, "user": prompt.user,
            "note": "These prompts are prepared only; the external MCP host's model must answer them."}


def prepare_caption_prompt(text: str, source_language: str = "de",
                           target_language: str = "ru") -> dict:
    """Prepare one explicit segment, with no access to the live caption buffer."""
    job = TranslationJob(0, 0, source_language, target_language,
                         text, (), threading.Event())
    system, user = caption_translation_prompt(job)
    return {"system": system, "user": user,
            "note": "Explicit text only; the external host model must translate it. No local or cloud translation was run."}
