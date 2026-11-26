"""Deterministic DE/RU speech routing, independent of the dictation language."""
from __future__ import annotations

import re
import unicodedata

from .tts_stream import split_read_text
from .analysis_providers import ProviderFailure

# A Latin technical term inside Russian speech does not switch the voice.
# Sentence/paragraph boundaries can switch language without changing text.
_SENTENCES = re.compile(r".+?(?:[.!?]+(?=\s|$)|\n+|$)\s*|\s+", re.S)
_DE = set("der die das den dem des ein eine einer und oder ist sind nicht ich du wir sie mit auf f\u00fcr von zu im als wird werden bitte heute hier was wie kann muss danke guten tag hallo".split())
_EN = set("the a an and or is are not i you we they with on for from to in as will be please today here what how can must thanks hello".split())


def speech_language(text, fallback="de"):
    """Russian script is decisive; DE/EN use simple lexical hints, not an LLM."""
    if any(ch.isalpha() and "CYRILLIC" in unicodedata.name(ch, "") for ch in text):
        return "ru"
    if any(ch in "\u00e4\u00f6\u00fc\u00df\u00c4\u00d6\u00dc" for ch in text):
        return "de"
    words = set(re.findall(r"[a-z]+", text.casefold()))
    de, en = len(words & _DE), len(words & _EN)
    if de > en:
        return "de"
    if en > de:
        return "en"
    return fallback if fallback in ("de", "en") else "de"


def plan_speech(text, language="auto", *, fallback="de", local=False):
    """Preserve text and group adjacent same-language sentences before chunking."""
    if language not in ("auto", "de", "ru", "en"):
        raise ProviderFailure("TTS_INVALID_LANGUAGE")
    # Validate the entire input before generating even a single chunk.
    split_read_text(text, "de")
    if language != "auto":
        return tuple((part, language) for part in split_read_text(text, language, limit=280 if local else 1000))
    runs = []
    for match in _SENTENCES.finditer(text):
        part = match.group()
        lang = speech_language(part, fallback)
        if runs and (not any(ch.isalpha() for ch in part) or runs[-1][1] == lang):
            runs[-1] = (runs[-1][0] + part, runs[-1][1])
        else:
            runs.append((part, lang))
    if "".join(part for part, _ in runs) != text:
        raise ProviderFailure("TTS_LANGUAGE_PARTITION_FAILED")
    return tuple((chunk, lang) for part, lang in runs if part.strip()
                 for chunk in split_read_text(part, lang, limit=280 if local else 1000))
