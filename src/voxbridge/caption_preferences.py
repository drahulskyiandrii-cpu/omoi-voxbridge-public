"""Local non-sensitive caption preferences. Never persist consent or content."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from .config import config_path, write_json_atomic


@dataclass(frozen=True)
class CaptionPreferences:
    source: str = "de"
    target: str = "ru"
    translate: bool = False
    seconds: int = 30
    delay: str = "low"
    translation_limit: int = 12
    display_mode: str = "both"
    font_size: int = 15
    visible_lines: int = 6
    opacity: int = 100
    translation_engine: str = "cloud"
    group_sentences: bool = False
    stt_provider: str = "openai"

    def validate(self):
        if (self.source not in ("de", "ru", "en")
                or self.target not in ("de", "ru", "en")
                or self.source == self.target):
            raise ValueError("CAPTION_PREF_LANGUAGE")
        if type(self.translate) is not bool or type(self.group_sentences) is not bool:
            raise ValueError("CAPTION_PREF_TRANSLATE")
        if self.stt_provider not in ("openai", "cartesia_preview"):
            raise ValueError("CAPTION_PREF_STT_PROVIDER")
        if self.translation_engine not in ("cloud", "gemma_cpu", "specialist"):
            raise ValueError("CAPTION_PREF_ENGINE")
        if self.delay not in ("minimal", "low", "medium", "high", "xhigh"):
            raise ValueError("CAPTION_PREF_DELAY")
        if self.display_mode not in ("both", "original", "translation"):
            raise ValueError("CAPTION_PREF_DISPLAY")
        for value, lo, hi in ((self.seconds, 5, 60), (self.translation_limit, 1, 12),
                              (self.font_size, 12, 32), (self.visible_lines, 1, 6),
                              (self.opacity, 50, 100)):
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError("CAPTION_PREF_RANGE")
        return self


def preferences_path() -> Path:
    return config_path().with_name("captions.json")


def load_caption_preferences(path: Path | None = None) -> CaptionPreferences:
    path = path if path is not None else preferences_path()
    if not path.exists():
        return CaptionPreferences()
    with path.open("rb") as reader:
        raw = reader.read(4097)
    if len(raw) > 4096:
        raise ValueError("CAPTION_PREF_TOO_LARGE")
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) - set(CaptionPreferences.__dataclass_fields__):
        raise ValueError("CAPTION_PREF_UNKNOWN_FIELD")
    return CaptionPreferences(**data).validate()


def save_caption_preferences(prefs: CaptionPreferences, path: Path | None = None):
    prefs.validate()
    write_json_atomic(path if path is not None else preferences_path(), asdict(prefs))
