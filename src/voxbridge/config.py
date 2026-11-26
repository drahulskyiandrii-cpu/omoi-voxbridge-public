"""Versioned, non-sensitive local configuration."""
from __future__ import annotations
import json
import os
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

CLOUD_STT_BACKENDS = frozenset({"openai_cloud", "groq_cloud"})


@dataclass(frozen=True)
class Settings:
    language: str = "de"
    hotkey: str = "Ctrl+Alt+Space"
    stt_backend: str = "openai_cloud"
    local_partial_enabled: bool = True
    enhanced_cpu_stt: bool = False
    stt_hotwords: str = ""
    stt_hotwords_de: str = ""
    stt_hotwords_ru: str = ""
    stt_term_correction: bool = False
    stt_audio_conditioning: bool = False
    cartesia_voice_id: str = ""
    tts_transport: str = "sse"
    stt_port: int = 18194
    min_free_vram_mib: int = 8000
    max_record_seconds: int = 25
    selection_hotkey: str = "Ctrl+Alt+A"
    selection_hotkey_enabled: bool = False
    reuse_local_services: bool = False
    prewarm_local_tts: bool = False
    tts_enabled: bool = True
    analysis_provider: str = "openai"
    vision_provider: str = "openai"
    local_model_dirs: str = ""
    local_llama_server: str = ""
    local_analysis_model: str = ""
    local_vision_model: str = ""
    microphone_id: str = ""
    microphone_label: str = ""
    region_analysis_hotkey: str = "Ctrl+Alt+Q"
    region_context_hotkey: str = "Ctrl+Alt+W"
    region_read_hotkey: str = "Ctrl+Alt+R"
    read_selection_hotkey: str = "Ctrl+Alt+S"
    region_task: str = "Return only the useful result, without preamble or technical commentary."
    region_task2_hotkey: str = ""
    region_task2: str = ""
    region_task3_hotkey: str = ""
    region_task3: str = ""
    copy_read_hotkey: str = "Ctrl+Alt+Shift+S"

    def vocabulary(self, language=None):
        language = self.language if language is None else language
        specific = self.stt_hotwords_de if language == "de" else self.stt_hotwords_ru if language == "ru" else ""
        return specific or self.stt_hotwords

    def validate(self):
        from .microphone_devices import valid_microphone_id
        if type(self.reuse_local_services) is not bool or type(self.prewarm_local_tts) is not bool or type(self.tts_enabled) is not bool:
            raise ValueError("INVALID_LOCAL_SERVICE_SETTINGS")
        if self.analysis_provider not in ("local", "openai", "anthropic") or self.vision_provider not in ("local", "local_ocr", "openai"):
            raise ValueError("INVALID_ANALYSIS_PROVIDER")
        if (not isinstance(self.local_model_dirs, str) or len(self.local_model_dirs) > 4096
                or any(not Path(value).is_absolute() for value in self.local_model_dirs.split(os.pathsep) if value)
                or not isinstance(self.local_llama_server, str) or len(self.local_llama_server) > 1024
                or (self.local_llama_server and (not Path(self.local_llama_server).is_absolute()
                    or Path(self.local_llama_server).name.lower() != "llama-server.exe"))):
            raise ValueError("INVALID_LOCAL_MODEL_PATHS")
        if not valid_microphone_id(self.local_analysis_model) or not valid_microphone_id(self.local_vision_model):
            raise ValueError("INVALID_LOCAL_MODEL_SELECTION")
        if not valid_microphone_id(self.microphone_id):
            raise ValueError("INVALID_MICROPHONE_ID")
        if (not isinstance(self.microphone_label, str) or len(self.microphone_label) > 2051
                or any(ord(c) < 32 for c in self.microphone_label)
                or (self.microphone_label and not self.microphone_id)):
            raise ValueError("INVALID_MICROPHONE_LABEL")
        from .hotkeys import parse_hotkey
        main_hotkey = parse_hotkey(self.hotkey)
        selection_hotkey = parse_hotkey(self.selection_hotkey)
        if type(self.selection_hotkey_enabled) is not bool:
            raise ValueError("INVALID_SELECTION_HOTKEY_SETTING")
        if selection_hotkey == main_hotkey:
            raise ValueError("SELECTION_HOTKEY_CONFLICT")
        shortcuts = [main_hotkey, selection_hotkey, *(parse_hotkey(combo) for combo in (self.region_analysis_hotkey, self.region_context_hotkey, self.region_read_hotkey, self.read_selection_hotkey, self.copy_read_hotkey))]
        for index, (hotkey, task) in enumerate(((self.region_analysis_hotkey, self.region_task), (self.region_task2_hotkey, self.region_task2), (self.region_task3_hotkey, self.region_task3))):
            if not isinstance(hotkey, str) or not isinstance(task, str) or len(task) > 600 or "\x00" in task:
                raise ValueError("INVALID_REGION_TASK")
            try:
                task.encode("utf-8")
            except UnicodeError:
                raise ValueError("INVALID_REGION_TASK") from None
            if bool(hotkey.strip()) != bool(task.strip()):
                raise ValueError("REGION_TASK_REQUIRES_HOTKEY_AND_TEXT")
            if index and hotkey:
                shortcuts.append(parse_hotkey(hotkey))
        if len(set(shortcuts)) != len(shortcuts):
            raise ValueError("GLOBAL_HOTKEY_CONFLICT")
        if not isinstance(self.language, str) or self.language not in {"de", "ru", "en"}:
            raise ValueError("UNSUPPORTED_LANGUAGE")
        from .stt_enhancements import validate_hotwords
        for value in (self.stt_hotwords, self.stt_hotwords_de, self.stt_hotwords_ru):
            validate_hotwords(value)
        if any(type(x) is not bool for x in (self.enhanced_cpu_stt,self.stt_term_correction,self.stt_audio_conditioning)):
            raise ValueError("INVALID_STT_ENHANCEMENT")
        if type(self.local_partial_enabled) is not bool:
            raise ValueError("INVALID_LOCAL_PARTIAL_SETTING")
        if not isinstance(self.stt_backend, str) or self.stt_backend not in CLOUD_STT_BACKENDS | {"shared", "mini_worker", "mini_cpu"}:
            raise ValueError("INVALID_BACKEND")
        if self.tts_transport not in ("sse", "bytes", "qwen_base", "qwen_cf050"):
            raise ValueError("INVALID_TTS_TRANSPORT")
        from .cartesia_voices import valid_voice_id
        if not isinstance(self.cartesia_voice_id, str) or (self.cartesia_voice_id and not valid_voice_id(self.cartesia_voice_id)):
            raise ValueError("INVALID_CARTESIA_VOICE_ID")
        if type(self.stt_port) is not int or not 1024 <= self.stt_port <= 65535:
            raise ValueError("INVALID_PORT")
        if type(self.min_free_vram_mib) is not int or not 2000 <= self.min_free_vram_mib <= 16000:
            raise ValueError("INVALID_VRAM_BUDGET")
        from .audio import MAX_RECORD_SECONDS
        if type(self.max_record_seconds) is not int or not 3 <= self.max_record_seconds <= MAX_RECORD_SECONDS:
            raise ValueError("INVALID_AUDIO_LIMIT")
        return self

def layout_safe_hotkeys(settings: Settings) -> Settings:
    """Draft preset. No change to files or registration until explicit save/restart.

    Function keys avoid character-layout changes and AltGr. Windows or another
    program may still reserve a combination. F12 is intentionally not assigned.
    """
    from dataclasses import replace
    return replace(settings,
        hotkey="Ctrl+Shift+F1",
        selection_hotkey="Ctrl+Shift+F2",
        region_analysis_hotkey="Ctrl+Shift+F3",
        region_context_hotkey="Ctrl+Shift+F4",
        region_read_hotkey="Ctrl+Shift+F5",
        read_selection_hotkey="Ctrl+Shift+F6",
        copy_read_hotkey="Ctrl+Shift+F7",
        region_task2_hotkey="Ctrl+Shift+F8" if settings.region_task2.strip() else "",
        region_task3_hotkey="Ctrl+Shift+F9" if settings.region_task3.strip() else "",
    ).validate()


def config_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    return (Path(root) / "OMOI-VoxBridge" / "settings.json") if root else (Path.home() / ".omoi-voxbridge" / "settings.json")

def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    if not path.exists():
        return Settings().validate()
    if path.stat().st_size > 32768:
        raise ValueError("CONFIG_TOO_LARGE")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - set(Settings.__dataclass_fields__):
        raise ValueError("INVALID_CONFIG")
    return Settings(**data).validate()

def save_settings(settings: Settings, path: Path | None = None) -> None:
    settings.validate()
    path = path or config_path()
    write_json_atomic(path, asdict(settings))


def write_json_atomic(path: Path, data: dict) -> None:
    """A unique temp file in the same directory; failed writes preserve old settings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp",
                                         delete=False) as handle:
            temp = Path(handle.name)
            json.dump(data, handle, indent=2)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def save_analysis_selection(kind, provider, model_key):
    """Persist only an explicitly changed provider and local model selection."""
    from dataclasses import replace
    if kind not in ("analysis", "vision"):
        raise ValueError("INVALID_ANALYSIS_KIND")
    save_settings(replace(load_settings(), **{
        kind + "_provider": provider, "local_" + kind + "_model": model_key,
    }).validate())
