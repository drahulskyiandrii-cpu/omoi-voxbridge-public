"""Application coordinator: tray commands, session lifecycle and feature routing."""
from __future__ import annotations

import concurrent.futures
import os
import threading
import time
from functools import partial

from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtWidgets import (
    QApplication, QMenu, QStyle, QSystemTrayIcon, QMessageBox,
)

from .audio import Microphone, AudioQuality, MAX_SEGMENT_SECONDS
from .microphone_devices import MicrophoneError
from .microphone_lifecycle import MicrophoneLifecycle
from .analysis_ui import AnalysisDialog
from .analysis_providers import ProviderFailure
from .voice_ui import VoiceDialog
from .tts_ui import CloudTTSDialog, desktop_tts_mode
from .qwen_local_runtime import MODELS as LOCAL_TTS_MODELS, QwenRuntime
from .local_llm import LocalModelRuntime, discover_models
from .vision_ui import VisionDialog
from .captions_core import CaptionCoordinator
from .captions_overlay import CaptionsOverlay, populate_synthetic_demo
from .live_captions_ui import LiveCaptionsDialog
from .config import load_settings, save_settings
from .cloud_permissions import standing_permission
from .cloud_permissions_ui import CloudPermissionsDialog
from .caption_preferences import load_caption_preferences, save_caption_preferences
from .hotkeys import HotkeyListener
from .injection import InjectionError
from .edit_target import EditIdentity, run_edit_job, MANUAL_LOCAL_CODES
from .session import Session, State
from .stt import SpeechFailure, SpeechCoreHTTP, MiniOneShotSTT
from .local_partial import LocalPartialTurn, ResidentMiniSTT
from .stt_process import stt_block_reason, STT_CLEANUP_FAILED
from .long_dictation import LongDictationTurn, ChunkedRecording, IncompleteDictation
from .cuda_warm import WarmCudaController
from .dictation_quality import inspect_transcript, MAX_RECORD_TEXT, MAX_SEGMENT_TEXT
from .cloud_providers import OpenAIFileSTT, close_provider, PROVIDER_CLEANUP_FAILED
from .selection_controller import SelectionController
from .selection import SelectionFailure, capture_target, capture_readable_selection
from .copied_text import capture_copy_target, capture_copied_text
from .desktop_settings_ui import DesktopSettingsDialog
from .control_center import ControlCenter, STT_CHOICES, LANGUAGE_CHOICES
from .status_ui import StatusWidget, show_manual_fallback


class Bridges(QObject):
    hotkey = Signal(object)
    completed = Signal(int, str, str)
    partial_ready = Signal(int, str)
    edit_ready = Signal(int, object, str)
    insertion_ready = Signal(int, str, str)
    region_action = Signal(str, object)


class VoxBridge(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.settings = load_settings()
        self.session = Session()
        self.signals = Bridges()
        self.signals.hotkey.connect(self.on_hotkey)
        self.signals.completed.connect(self.on_completed)
        self.signals.partial_ready.connect(self._partial_ready)
        self.signals.edit_ready.connect(self._edit_ready)
        self.signals.insertion_ready.connect(self._insertion_ready)
        self.signals.region_action.connect(self._region_action)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="voxbridge-stt")
        self.cuda_warm = WarmCudaController(self.executor)
        self.local_llm = LocalModelRuntime(reuse=self.settings.reuse_local_services, server=self.settings.local_llama_server)
        self.recorder = Microphone(self.settings.max_record_seconds, microphone_id=self.settings.microphone_id)
        self.recorder.conditioning = self.settings.stt_audio_conditioning
        self.mic = MicrophoneLifecycle(self.recorder, self)
        self.mic.ready.connect(self._microphone_ready)
        self.mic.stopped.connect(self._microphone_stopped)
        self.mic_deadline = QTimer(self)
        self.mic_deadline.setSingleShot(True)
        self.mic_deadline.timeout.connect(self._microphone_timeout)
        self.active_backend = None
        # A saved local engine choice authorizes manual dictation only.
        # Startup never loads a model; CUDA still checks free VRAM at launch.
        self._local_stt_armed = self.settings.stt_backend != "openai_cloud"
        self._partial_turn = None
        self._dictation_worker_generation = None
        self._stt_cleanup_failed = False
        self._edit_worker_generation = None
        self._edit_cancel = threading.Event()
        self._edit_identity = None
        self._dictation_target = None
        self._manual_insert_reason = ""
        self._last_transcript_check = "NOT_TESTED"
        self._last_audio_info = "MIC_NOT_TESTED"
        self._last_partial_info = "Partial: not tested"
        self._diagnose_next = False
        self._shortcut_tts_text = None
        self._approval_dialog = None
        self._shutting_down = False
        self._exit_requested = False
        self._exit_deadline = 0.0
        self._exit_timer = QTimer(self)
        self._exit_timer.setInterval(100)
        self._exit_timer.timeout.connect(self._poll_exit)
        self.control_center = None
        self.analysis_dialog = None
        self.voice_dialog = None
        self.tts_dialog = None
        self.vision_dialog = None
        self.caption_demo_core = None
        self.caption_demo_overlay = None
        self.live_captions_dialog = None
        self.status = StatusWidget()
        self.selection = SelectionController(
            can_start=self._selection_allowed, on_preview=self._show_selection,
            notify=self.notify, parent=self)
        self.read_selection = SelectionController(
            can_start=self._read_selection_allowed, on_preview=self._read_aloud,
            notify=self._notify_read_selection, parent=self, listener_factory=HotkeyListener,
            reader=capture_readable_selection, on_repeat=self._stop_read_aloud)
        self.read_copy = SelectionController(can_start=self._copy_read_allowed,
            on_preview=self._read_aloud, notify=self.notify, parent=self,
            listener_factory=HotkeyListener, target_factory=capture_copy_target,
            reader=capture_copied_text, available=lambda: True, on_repeat=self._stop_read_aloud)
        self._region_hotkeys = {}
        self.tray = QSystemTrayIcon(app.style().standardIcon(QStyle.SP_ComputerIcon), app)
        self.tray.setToolTip("OMOI VoxBridge — " + self.settings.hotkey)
        self.tray.setContextMenu(self._make_menu())
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        self.hotkeys = HotkeyListener(self.settings.hotkey, self.signals.hotkey.emit)
        self.app.aboutToQuit.connect(self.shutdown)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self.on_max_duration)
        self._hotkey_ready = False
        self._start_hotkey()
        self._start_region_hotkeys()
        self._refresh_menu()
        if self.settings.selection_hotkey_enabled:
            try:
                self.selection.enable(self.settings.selection_hotkey)
            except SelectionFailure as exc:
                self.notify(str(exc))
            self._refresh_menu()
        if self.settings.prewarm_local_tts:
            QTimer.singleShot(0, self._prewarm_tts_at_start)

    def _new_tts_dialog(self, transport):
        return CloudTTSDialog(permission_checker=standing_permission,
            can_start=self._can_play_tts, transport_mode=transport,
            shortcut_notify=self.notify, voice_opener=self.open_voices,
            local_runtime=QwenRuntime(reuse=getattr(self.settings, "reuse_local_services", False)),
            keep_warm=self.settings.prewarm_local_tts, tts_enabled=self.settings.tts_enabled,
            fallback_language=self.settings.language, state_changed=self._refresh_menu)

    def _can_start_local_analysis(self):
        return (self._dictation_idle() and self._tts_idle()
                and (self.live_captions_dialog is None or not self.live_captions_dialog._busy))

    def _new_vision_dialog(self):
        return VisionDialog(permission_checker=standing_permission,
                            on_speak=self._prepare_speech, on_read=self._read_aloud,
                            local_runtime=self.local_llm, local_models=discover_models(extra_dirs=self.settings.local_model_dirs),
                            preferences=load_settings(), can_start_local=self._can_start_local_analysis)

    def _prewarm_tts_at_start(self):
        if self._shutting_down or not self.settings.tts_enabled or not self.settings.prewarm_local_tts:
            return
        mode = desktop_tts_mode(load_settings().tts_transport)
        if mode not in LOCAL_TTS_MODELS:
            return
        if self.tts_dialog is None:
            self.tts_dialog = self._new_tts_dialog(mode)
        self.tts_dialog.ensure_prewarm()

    @Slot(bool)
    def _set_tts_enabled(self, enabled):
        if self._shutting_down:
            return
        from dataclasses import replace
        try:
            current = load_settings()
            updated = replace(current, tts_enabled=bool(enabled),
                              prewarm_local_tts=True if enabled and desktop_tts_mode(current.tts_transport) in LOCAL_TTS_MODELS
                              else current.prewarm_local_tts)
            save_settings(updated)
        except (OSError, ValueError, TypeError):
            self.notify("TTS_SETTINGS_SAVE_FAILED")
            self._refresh_menu()
            return
        self.settings = replace(self.settings, tts_enabled=updated.tts_enabled,
                                prewarm_local_tts=updated.prewarm_local_tts)
        if not enabled:
            self.read_selection.cancel()
            self.read_copy.cancel()
        if self.tts_dialog is not None:
            self.tts_dialog.set_tts_enabled(enabled, keep_warm=updated.prewarm_local_tts)
        elif enabled:
            self._prewarm_tts_at_start()
        self._refresh_menu()

    def _start_hotkey(self):
        try:
            self.hotkeys.start()
        except (RuntimeError, OSError):
            self.hotkeys.stop()
            self._hotkey_ready = False
            self.notify("HOTKEY_UNAVAILABLE / Tastenkombination in Einstellungen ändern.")
        else:
            self._hotkey_ready = True
            self.notify("VoxBridge bereit: " + self.settings.hotkey)

    def _start_region_hotkeys(self):
        for mode, combo in (("analyze", self.settings.region_analysis_hotkey), ("context", self.settings.region_context_hotkey), ("read", self.settings.region_read_hotkey), ("task2", self.settings.region_task2_hotkey), ("task3", self.settings.region_task3_hotkey)):
            if not combo:
                continue
            listener = HotkeyListener(combo, lambda hwnd, action=mode: self.signals.region_action.emit(action, hwnd))
            try:
                listener.start()
            except (RuntimeError, OSError):
                listener.stop()
                self.notify("REGION_HOTKEY_UNAVAILABLE: " + combo)
            else:
                self._region_hotkeys[mode] = listener
        try:
            self.read_selection.enable(self.settings.read_selection_hotkey)
        except SelectionFailure as exc:
            self.notify(str(exc) + ": " + self.settings.read_selection_hotkey)
        try:
            self.read_copy.enable(self.settings.copy_read_hotkey)
        except SelectionFailure as exc:
            self.notify(str(exc) + ": " + self.settings.copy_read_hotkey)

    @Slot(str, object)
    def _region_action(self, mode, hwnd=None):
        del hwnd
        if self._shutting_down:
            return
        if mode == "read" and not self.settings.tts_enabled:
            self.notify("TTS_DISABLED")
            return
        if mode == 'read' and self._stop_read_aloud():
            return
        if not self._dictation_idle() or not self._tts_idle():
            self.notify("REGION_BUSY")
            return
        if self.live_captions_dialog is not None and self.live_captions_dialog._busy:
            self.notify("REGION_BUSY_CAPTIONS")
            return
        if self.vision_dialog is None:
            self.vision_dialog = self._new_vision_dialog()
        tasks = {"analyze": self.settings.region_task, "task2": self.settings.region_task2, "task3": self.settings.region_task3}
        if not self.vision_dialog.start_shortcut("analyze" if mode in tasks else mode, task=tasks.get(mode, "")):
            self.notify("REGION_BUSY")

    def _read_selection_allowed(self):
        if not getattr(self.settings, "tts_enabled", True):
            self.notify("TTS_DISABLED")
            return False
        return (self._dictation_idle() and VoxBridge._tts_accepts_request(self)
                and (self.live_captions_dialog is None or not self.live_captions_dialog._busy))

    @Slot(bool)
    def _read_copied_from_menu(self, checked=False):
        self.read_copy.on_hotkey(None)

    def _copy_read_allowed(self):
        try:
            mode = desktop_tts_mode(load_settings().tts_transport)
        except (OSError, ValueError, TypeError):
            self.notify("TTS_SETTINGS_INVALID")
            return False
        if mode not in LOCAL_TTS_MODELS and not standing_permission('tts_cartesia'):
            self.notify('TTS_PERMISSION_REQUIRED')
            return False
        return self._read_selection_allowed()

    def _notify_read_selection(self, code):
        if code in ('SELECTION_NOT_SUPPORTED','SELECTION_EMPTY_OR_MULTIPLE','SELECTION_EMBEDDED_CONTROL','SELECTION_NATIVE_FAILED','SELECTION_TIMEOUT'):
            self.notify(code + ' | Ctrl+C, ' + self.settings.copy_read_hotkey)
        else:
            self.notify(code)

    def _stop_read_aloud(self):
        for name in ('read_selection','read_copy'):
            controller = getattr(self,name,None)
            if controller is not None and controller.busy:
                controller.cancel()
                return True
        dialog = getattr(self,"tts_dialog",None)
        if dialog is not None and getattr(dialog,"_shortcut",False) and not self._tts_idle():
            dialog._stop()
            self.notify("Vorlesen: Stop angefordert.")
            return True
        return False

    def _read_aloud(self, text):
        if not getattr(self.settings, "tts_enabled", True):
            self.notify("TTS_DISABLED")
            return
        if self._shutting_down:
            raise ProviderFailure("TTS_UNAVAILABLE")
        try:
            transport = desktop_tts_mode(load_settings().tts_transport)
        except (OSError, ValueError, TypeError):
            self.notify("TTS_SETTINGS_INVALID")
            return
        if transport not in LOCAL_TTS_MODELS and not standing_permission("tts_cartesia"):
            self.notify("TTS_PERMISSION_REQUIRED")
            return
        if not VoxBridge._tts_accepts_request(self) or not self._can_play_tts():
            self.notify("TTS_BUSY")
            return
        if self.tts_dialog is None:
            self.tts_dialog = self._new_tts_dialog(transport)
        # Keep manual TTS draft untouched; selection/region text is transient.
        if self.tts_dialog.start_shortcut(text, self.settings.language, transport):
            self.notify("Vorlesen / ???????????: " + self.settings.read_selection_hotkey + " = Stop")

    def _make_menu(self) -> QMenu:
        menu = QMenu()
        def action(parent, label, callback):
            item = parent.addAction(label)
            item.triggered.connect(callback)
            return item

        if self._shutting_down:
            action(menu, "Beenden erneut pruefen / Retry exit", self.request_exit)
            return menu

        action(menu, "Kontrollzentrum / Центр управления…", self.open_control_center)
        menu.addSeparator()
        dictate = menu.addMenu("Diktieren / Диктовка")
        hint = dictate.addAction("Hotkey: " + self.settings.hotkey +
            ("" if getattr(self, "_hotkey_ready", False) else " (OFF)"))
        hint.setEnabled(False)
        engines = dictate.addMenu("STT / Распознавание")
        for mode, label in STT_CHOICES:
            item = action(engines, label, partial(self.set_backend, mode))
            item.setCheckable(True)
            item.setChecked(self.settings.stt_backend == mode)
        languages = dictate.addMenu("Sprache / Язык")
        for code, label in LANGUAGE_CHOICES:
            item = action(languages, label, partial(self.set_language, code))
            item.setCheckable(True)
            item.setChecked(self.settings.language == code)
        diagnose = action(dictate, "STT-Test / Проверка без вставки", self._toggle_stt_test)
        diagnose.setCheckable(True)
        diagnose.setChecked(self._diagnose_next)
        speak = menu.addMenu("Vorlesen / Озвучивание")
        enabled = getattr(self.settings, "tts_enabled", True)
        toggle = action(speak, "TTS aktiv / TTS on", self._set_tts_enabled)
        toggle.setCheckable(True)
        toggle.setChecked(enabled)
        dialog = self.tts_dialog
        state = dialog.model_status if dialog is not None else ("Ready on request" if enabled else "OFF")
        speak.addAction("Status: " + state).setEnabled(False)
        action(speak, "Modell & Stimme / Модель и голос…", self.open_tts)
        copied = action(speak, "Kopierten Text lesen / Озвучить скопированное",
                        self._read_copied_from_menu)
        copied.setEnabled(enabled and self.read_copy.enabled)
        read = speak.addAction("Auswahl / Выделение: " + self.settings.read_selection_hotkey +
                               ("" if self.read_selection.enabled else " (OFF)"))
        read.setEnabled(False)
        action(menu, "Live-Untertitel / Субтитры…", self.open_live_captions)
        ai = menu.addMenu("KI / ИИ-инструменты")
        action(ai, "Textanalyse / Анализ текста…", self.open_analysis)
        selection = action(ai, "Auswahl-Hotkey / Анализ выделения: " + self.settings.selection_hotkey,
                           self.toggle_selection_hotkey)
        selection.setCheckable(True)
        selection.setChecked(self.selection.enabled)
        action(ai, "Bildanalyse / Анализ изображения…", self.open_vision)
        for mode, combo, label in (
                ("analyze", self.settings.region_analysis_hotkey, "Bereich / Область"),
                ("context", self.settings.region_context_hotkey, "Bild + Frage / Область + вопрос"),
                ("read", self.settings.region_read_hotkey, "Bild lesen / Озвучить область"),
                ("task2", self.settings.region_task2_hotkey, "Aufgabe / Задание 2"),
                ("task3", self.settings.region_task3_hotkey, "Aufgabe / Задание 3")):
            if combo:
                action(ai, label + " — " + combo, partial(self._region_action, mode))
        settings = menu.addMenu("Einstellungen / Настройки")
        action(settings, "Tastenkürzel & Mikrofon / Клавиши и микрофон…", self.open_desktop_settings)
        action(settings, "Cloud-Freigaben / Облачные разрешения…", self.open_cloud_permissions)
        action(settings, "Cartesia-Stimmen / Облачные голоса…", self.open_voices)
        action(settings, "Untertitel-Demo / Демо субтитров…", self.open_captions_demo)
        menu.addSeparator()
        action(menu, "Alles stoppen / Остановить всё", self.stop_all)
        action(menu, "Beenden / Выход", self.request_exit)
        return menu

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_control_center()

    def _dashboard_status(self):
        def busy(dialog):
            return dialog is not None and bool(dialog._busy)

        state = self.session.snapshot().state.value
        mic = getattr(self, "mic", None)
        if state == "idle":
            if getattr(mic, "cleanup_failed", False):
                state = "MIC_CLEANUP_FAILED"
            elif (self._dictation_worker_generation is not None or
                  getattr(self, "_edit_worker_generation", None) is not None or
                  getattr(mic, "busy", False)):
                state = "cancelling"
        key = self.settings.hotkey if self._hotkey_ready else "HOTKEY_UNAVAILABLE"
        selection = self.settings.selection_hotkey if self.selection.enabled else "OFF"
        if self.selection.busy:
            selection += " / busy"
        # Never display API keys, inspect audio devices, or test external services.
        configured = [label for label, name in (
            ("OpenAI", "OPENAI_API_KEY"), ("Cartesia", "CARTESIA_API_KEY"),
            ("Anthropic", "ANTHROPIC_API_KEY")) if os.environ.get(name)]
        active = "Aktiv / Активно"
        idle = "Bereit / Готово"
        return {
            "dictation": key + " | " + self.settings.stt_backend + " / " + self.settings.language + " | " + state + " / " + getattr(self, "_last_transcript_check", "NOT_TESTED") + " | " + getattr(self, "_last_audio_info", "MIC_NOT_TESTED"),
            "stt_partial": getattr(self, "_last_partial_info", "Partial: not tested"),
            "selection": selection,
            "analysis": active if busy(self.analysis_dialog) else idle,
            "vision": active if (busy(self.vision_dialog) or
                (self.vision_dialog is not None and
                 (self.vision_dialog._overlay is not None or self.vision_dialog._capture_pending))) else idle,
            "tts": active if not self._tts_idle() else idle,
            "captions": active if busy(self.live_captions_dialog) else idle,
            "cloud": (", ".join(configured) if configured else "Keine / Нет") +
                     " — Schlüssel konfiguriert, Zugriff ungeprüft",
            "local": ("STT armed: " + self.settings.stt_backend if getattr(self, "_local_stt_armed", False) else "OFF — no local inference"),
        }

    def open_control_center(self, checked=False):
        del checked
        if self._shutting_down:
            return
        if self.control_center is None:
            self.control_center = ControlCenter(
                status_provider=self._dashboard_status, stop_all=self.stop_all,
                settings_provider=lambda: self.settings, set_backend=self.set_backend,
                set_language=self.set_language,
                cuda_status=self.cuda_warm.status, toggle_cuda=self._toggle_cuda_warm,
                actions={
                    "analysis": self.open_analysis, "vision": self.open_vision,
                    "tts": self.open_tts, "captions": self.open_live_captions,
                    "demo": self.open_captions_demo, "voices": self.open_voices,
                    "settings": self.open_desktop_settings, "permissions": self.open_cloud_permissions,
                })
        self.control_center.show()
        self.control_center.raise_()
        self.control_center.activateWindow()

    def stop_all(self, checked=False):
        del checked
        self.cancel()
        warm = getattr(self, "cuda_warm", None)
        if warm is not None:
            warm.deactivate()
        if getattr(self, "read_selection", None) is not None:
            self.read_selection.cancel()
        if getattr(self, 'read_copy', None) is not None:
            self.read_copy.cancel()
        if self.analysis_dialog is not None and self.analysis_dialog._busy:
            self.analysis_dialog._cancel_analysis()
        if self.vision_dialog is not None:
            if self.vision_dialog._overlay is not None or self.vision_dialog._capture_pending:
                self.vision_dialog.close()
            elif self.vision_dialog._busy:
                self.vision_dialog._cancel()
        if self.tts_dialog is not None:
            self.tts_dialog.stop_all()
        llm = getattr(self, "local_llm", None)
        if llm is not None:
            llm.request_stop()
        voice = getattr(self, "voice_dialog", None)
        if voice is not None and voice._worker_generation is not None:
            voice.reject()
        demo = getattr(self, "caption_demo_overlay", None)
        if demo is not None:
            demo.close()
        if self.live_captions_dialog is not None:
            if self.live_captions_dialog._busy:
                self.live_captions_dialog._stop_session()
            elif self.live_captions_dialog._refreshing:
                self.live_captions_dialog.reject()
        self.notify("Stopp angefordert / Остановка запрошена.")

    def _refresh_menu(self):
        old = self.tray.contextMenu()
        self.tray.setContextMenu(self._make_menu())
        if old is not None:
            old.deleteLater()

    def _toggle_stt_test(self, checked=False):
        if not self._dictation_idle():
            self.notify("STT_TEST_BUSY")
            self._refresh_menu()
            return
        self._diagnose_next = bool(checked)
        self.notify("STT_TEST_ARMED: next dictation displays raw recognition, no insertion" if checked else "STT_TEST_OFF")

    def _toggle_cuda_warm(self, checked=False):
        state, error = self.cuda_warm.status()
        if state == "closing":
            return
        if state != "off":
            if self.session.snapshot().state != State.IDLE:
                self.notify("STT_BUSY")
                return
            self.cuda_warm.deactivate()
            return
        if (self.settings.stt_backend != "mini_worker" or not self._dictation_idle()
                or not self._tts_idle()
                or (self.live_captions_dialog is not None and self.live_captions_dialog._busy)):
            self.notify("CUDA_SELECT_ENGINE_AND_STOP_AUDIO")
            return
        try:
            self.cuda_warm.activate(self.settings.min_free_vram_mib,
                                    hotwords=self.settings.vocabulary(), approved=True)
        except SpeechFailure as exc:
            self.notify(str(exc))

    def set_backend(self, mode: str, checked=False):
        del checked
        if not self._dictation_idle():
            self.notify("Während der Aufnahme kann die Engine nicht gewechselt werden.")
            self._refresh_menu()
            return
        if mode not in ("openai_cloud", "mini_cpu", "mini_worker"):
            self.notify("INVALID_STT_ENGINE")
            self._refresh_menu()
            return
        if self.settings.language == "en" and mode in ("mini_cpu", "mini_worker"):
            self.notify("MINI_STT_SUPPORTS_DE_RU_ONLY")
            self._refresh_menu()
            return
        from dataclasses import replace
        try:
            updated = replace(load_settings(), stt_backend=mode).validate()
            save_settings(updated)
        except (ValueError, OSError, TypeError):
            self.notify("SETTINGS_SAVE_FAILED")
            self._refresh_menu()
            return
        if mode != "mini_worker" and getattr(self, "cuda_warm", None) is not None:
            self.cuda_warm.deactivate()
        self.settings = replace(self.settings, stt_backend=updated.stt_backend)
        self._local_stt_armed = mode != "openai_cloud"
        self._refresh_menu()
        self.notify("STT gespeichert / Saved: " + mode + " — " + self.settings.hotkey)

    def set_language(self, lang: str, checked=False):
        del checked
        if self.session.snapshot().state != State.IDLE:
            self.notify("Sprache erst nach Abschluss ändern.")
            return
        if lang == "en" and self.settings.stt_backend in ("mini_worker", "mini_cpu"):
            self.notify("Mini CUDA unterstützt derzeit nur Deutsch und Russisch.")
            return
        from dataclasses import replace
        try:
            updated = replace(load_settings(), language=lang).validate()
            save_settings(updated)
        except (ValueError, OSError, TypeError):
            self.notify("SETTINGS_SAVE_FAILED")
            return
        self.settings = replace(self.settings, language=updated.language)
        self._refresh_menu()

    def open_desktop_settings(self, checked=False):
        del checked
        DesktopSettingsDialog(self.settings, saver=self._save_desktop_settings,
                              live_microphone=True).exec()

    def _save_desktop_settings(self, updated):
        """Apply an explicitly chosen microphone immediately when capture is idle."""
        from dataclasses import replace
        updated.validate()
        changed = updated.microphone_id != self.settings.microphone_id
        if changed and not self._dictation_idle():
            raise ValueError("MIC_CHANGE_BUSY")
        save_settings(updated)
        self.settings = replace(self.settings, microphone_id=updated.microphone_id,
                                microphone_label=updated.microphone_label)
        self.recorder.microphone_id = updated.microphone_id

    def open_cloud_permissions(self, checked=False):
        del checked
        CloudPermissionsDialog().exec()

    def _selection_allowed(self):
        return (self._dictation_idle()
                and not (self.live_captions_dialog is not None and self.live_captions_dialog._busy)
                and (self.analysis_dialog is None
                     or (not self.analysis_dialog._busy
                         and not self.analysis_dialog.text.toPlainText().strip())))

    def _show_selection(self, text: str):
        if not self._selection_allowed():
            return
        self.open_analysis()
        self.analysis_dialog.output.clear()
        self.analysis_dialog.text.setPlainText(text)
        self.analysis_dialog.consent.setChecked(False)
        self.analysis_dialog.message.setText(
            "Auswahl übernommen. Keine Übertragung; Analyse manuell starten."
        )

    def toggle_selection_hotkey(self, checked=False):
        from dataclasses import replace
        if self._shutting_down:
            return
        if not checked:
            self.selection.disable()
        try:
            updated = replace(load_settings(), selection_hotkey_enabled=bool(checked)).validate()
            runtime = replace(self.settings, selection_hotkey_enabled=bool(checked)).validate()
            if checked:
                self.selection.enable(runtime.selection_hotkey)
            save_settings(updated)
            self.settings = runtime
        except (SelectionFailure, OSError, TypeError, ValueError):
            if checked:
                self.selection.disable()
            self.notify("SELECTION_HOTKEY_SETTINGS_FAILED / Nicht gespeichert.")
        self._refresh_menu()

    def open_analysis(self, checked=False):
        del checked
        if self.analysis_dialog is None:
            self.analysis_dialog = AnalysisDialog(permission_checker=standing_permission, on_speak=self._prepare_speech,
                local_runtime=self.local_llm, local_models=discover_models(extra_dirs=self.settings.local_model_dirs), preferences=load_settings(), can_start_local=self._can_start_local_analysis)
        self.analysis_dialog.show()
        self.analysis_dialog.raise_()
        self.analysis_dialog.activateWindow()

    def open_voices(self, checked=False):
        del checked
        if self._shutting_down:
            return
        if self.voice_dialog is None:
            self.voice_dialog = VoiceDialog()
        self.voice_dialog.show()
        self.voice_dialog.raise_()
        self.voice_dialog.activateWindow()

    def _prepare_speech(self, text: str, language: str | None = None):
        if not getattr(getattr(self, "settings", None), "tts_enabled", True):
            raise ProviderFailure("TTS_DISABLED")
        if self._shutting_down:
            raise ProviderFailure("TTS_UNAVAILABLE")
        if self.tts_dialog is None:
            self.tts_dialog = self._new_tts_dialog(load_settings().tts_transport)
        self._shortcut_tts_text = None
        self.tts_dialog.prepare_text(text, language)
        self.open_tts()

    def open_tts(self, checked=False):
        del checked
        if self.tts_dialog is None:
            self.tts_dialog = self._new_tts_dialog(load_settings().tts_transport)
        self.tts_dialog.show()
        self.tts_dialog.raise_()
        self.tts_dialog.activateWindow()

    def open_vision(self, checked=False):
        del checked
        if self.vision_dialog is None:
            self.vision_dialog = self._new_vision_dialog()
        self.vision_dialog.show()
        self.vision_dialog.raise_()
        self.vision_dialog.activateWindow()

    def open_captions_demo(self, checked=False):
        del checked
        if self.caption_demo_overlay is not None and self.caption_demo_overlay.isVisible():
            self.caption_demo_overlay.close()
            return
        if self.caption_demo_core is None:
            self.caption_demo_core = CaptionCoordinator()
            self.caption_demo_overlay = CaptionsOverlay(self.caption_demo_core)
            self.caption_demo_overlay.title_label.setText("DEMO / No live audio")
        populate_synthetic_demo(self.caption_demo_core)
        self.caption_demo_overlay.show()
        self.caption_demo_overlay.raise_()

    def open_live_captions(self, checked=False):
        del checked
        if self.live_captions_dialog is None:
            self.live_captions_dialog = LiveCaptionsDialog(
                can_start=self._can_start_captions,
                preference_loader=load_caption_preferences,
                preference_saver=save_caption_preferences,
                permission_checker=standing_permission,
            )
        self.live_captions_dialog.show()
        self.live_captions_dialog.raise_()
        self.live_captions_dialog.activateWindow()

    def _tts_idle(self):
        dialog = getattr(self, "tts_dialog", None)
        return dialog is None or not dialog.busy

    def _tts_accepts_request(self):
        dialog = getattr(self, "tts_dialog", None)
        if dialog is None:
            return True
        # A request may be queued during optional background prewarming.
        return not dialog.busy or getattr(dialog, "accepts_read_request", False) is True

    def _can_play_tts(self):
        return (self._dictation_idle() and
                (self.live_captions_dialog is None or not self.live_captions_dialog._busy))

    def _can_start_captions(self):
        return self._dictation_idle() and self._tts_idle()

    def _dictation_idle(self):
        warm = getattr(self, "cuda_warm", None)
        return (not self._shutting_down
                and not getattr(getattr(self, "local_llm", None), "pending", False)
                and (warm is None or warm.status()[0] not in ("loading", "closing"))
                and self._dictation_worker_generation is None
                and not getattr(getattr(self, "mic", None), "busy", False)
                and getattr(self, "_edit_worker_generation", None) is None
                and not getattr(getattr(self, "selection", None), "busy", False)
                and not getattr(getattr(self, "read_selection", None), "busy", False)
                and not getattr(getattr(self, "read_copy", None), "busy", False)
                and self.session.snapshot().state == State.IDLE)

    def _approve_dictation(self) -> bool:
        """Standing opt-in or fresh approval for this recording."""
        if standing_permission("dictation_openai"):
            return True
        dialog = QMessageBox()
        dialog.setWindowTitle("OMOI VoxBridge - Cloud STT")
        dialog.setText(
            "Diese Mikrofonaufnahme an OpenAI zur Transkription senden? "
            "Kosten sind möglich. / Отправить эту запись в OpenAI? Возможна оплата."
        )
        if getattr(self.settings, "max_record_seconds", 25) > MAX_SEGMENT_SECONDS:
            dialog.setInformativeText("Lange Aufnahme: mehrere Anfragen möglich. / Длинная запись: возможны несколько запросов.")
        dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        dialog.setDefaultButton(QMessageBox.No)
        dialog.setEscapeButton(QMessageBox.No)
        self._approval_dialog = dialog
        try:
            return dialog.exec() == QMessageBox.Yes
        finally:
            self._approval_dialog = None

    def notify(self, message: str):
        self.tray.showMessage("OMOI VoxBridge", message, QSystemTrayIcon.Information, 2500)

    @Slot(object)
    def on_hotkey(self, hwnd: int):
        if getattr(self, "_shutting_down", False) or not getattr(self, "_hotkey_ready", True):
            return
        state = self.session.snapshot().state
        if state == State.IDLE:
            reason = stt_block_reason()
            if reason or getattr(self, "_stt_cleanup_failed", False):
                self.notify(reason or PROVIDER_CLEANUP_FAILED)
                return
            if getattr(getattr(self, "settings", None), "stt_backend", None) == "shared":
                self.notify("STT wählen / Выберите CPU, CUDA или OpenAI в центре управления")
                return
            warm = getattr(self, "cuda_warm", None)
            if warm is not None:
                warm_state, warm_error = warm.status()
                if warm_state in ("loading", "closing") or (
                        self.settings.stt_backend == "mini_worker" and warm_state == "failed"):
                    self.notify(warm_error or "CUDA_MODEL_" + warm_state.upper())
                    return
            if getattr(getattr(self, "local_llm", None), "pending", False):
                self.notify("LOCAL_LLM_BUSY")
                return
            if not VoxBridge._tts_idle(self):
                self.notify("Sprachausgabe läuft noch. Diktieren ist gesperrt.")
                return
            if (getattr(getattr(self, "selection", None), "busy", False) or
                    getattr(getattr(self, "read_selection", None), "busy", False) or
                    getattr(getattr(self, "read_copy", None), "busy", False)):
                self.notify("Textauswahl läuft noch; Diktieren gesperrt.")
                return
            if (self.live_captions_dialog is not None and
                    self.live_captions_dialog._busy):
                self.notify("Cloud-Untertitel laufen noch. Diktieren ist gesperrt.")
                return
            if getattr(getattr(self, "mic", None), "busy", False):
                self.notify("MIC_CLEANUP_PENDING")
                return
            if (getattr(self, "_dictation_worker_generation", None) is not None
                    or getattr(self, "_edit_worker_generation", None) is not None):
                self.notify("Vorherige Cloud-Anfrage läuft noch; Diktieren gesperrt.")
                return
            if self.settings.stt_backend != "openai_cloud" and not getattr(self, "_local_stt_armed", False):
                self.notify("LOCAL_STT_NOT_CONFIGURED")
                return
            if hwnd <= 0:
                self.notify("Kein gültiges Zielfenster.")
                return
            try:
                target = capture_target(hwnd)
                generation = self.session.begin(hwnd, target.focus)
                if not self.session.advance(generation, State.RECORDING, State.VERIFYING_TARGET):
                    return
            except SelectionFailure as exc:
                self.notify(str(exc))
                return
            self._edit_identity = None
            self._dictation_target = target
            self._manual_insert_reason = ""
            self._edit_cancel = threading.Event()
            self._edit_worker_generation = generation
            self.status.update_state("Textfeld wird geprüft / Проверка поля…")
            cancelled = self._edit_cancel

            def worker():
                identity, error = None, ""
                try:
                    identity = run_edit_job(target, cancelled)
                except InjectionError as exc:
                    error = str(exc)
                except Exception:
                    error = "EDIT_CHECK_FAILED"
                try:
                    self.signals.edit_ready.emit(generation, identity, error)
                except RuntimeError:
                    pass

            try:
                threading.Thread(target=worker, name="voxbridge-edit-check", daemon=True).start()
            except RuntimeError:
                self._edit_ready(generation, None, "EDIT_WORKER_START_FAILED")
        elif state == State.RECORDING:
            self.finish_recording()
        else:
            # Second hotkey cancels even during recognition; no stale insertion.
            self.cancel()

    @Slot(int, object, str)
    def _edit_ready(self, generation, identity, error):
        if generation != self._edit_worker_generation:
            return
        self._edit_worker_generation = None
        snap = self.session.snapshot()
        if self._shutting_down or snap.generation != generation or snap.state != State.VERIFYING_TARGET:
            return
        try:
            # Local speech recognition and automatic text insertion are separate.
            # An unsupported UIA provider must not prevent a user-triggered
            # local recording. Never grant an unverified target SendInput.
            manual = (error in MANUAL_LOCAL_CODES
                      and getattr(self.settings, "stt_backend", "") in ("mini_worker", "mini_cpu", "shared")
                      and getattr(self, "_local_stt_armed", False)
                      and identity is None)
            if manual:
                target = getattr(self, "_dictation_target", None)
                if target is None or capture_target(snap.target_hwnd) != target:
                    raise InjectionError("TEXT_FIELD_CHANGED")
                self._manual_insert_reason = error
            else:
                if error or not isinstance(identity, EditIdentity):
                    raise InjectionError(error or "EDIT_CHECK_FAILED")
                if capture_target(snap.target_hwnd) != identity.target:
                    raise InjectionError("TEXT_FIELD_CHANGED")
                self._manual_insert_reason = ""
            if not self.session.advance(generation, State.VERIFYING_TARGET, State.STARTING_MIC):
                return
            self._edit_identity = identity
            self.mic_deadline.start(8000)
            self.status.update_state("MIC_STARTING")
            self.mic.start(generation)
        except (InjectionError, MicrophoneError, SelectionFailure) as exc:
            self.mic_deadline.stop()
            self._edit_identity = None
            self.session.finish(generation)
            self.status.hide()
            self.notify(str(exc))
            return
        except Exception:
            self.mic_deadline.stop()
            self._edit_identity = None
            self.session.finish(generation)
            self.status.hide()
            self.notify("MIC_START_FAILED")
            return

    @Slot(int, str)
    def _microphone_ready(self, generation, error):
        snap = self.session.snapshot()
        if self._shutting_down or snap.generation != generation or snap.state != State.STARTING_MIC:
            return
        self.mic_deadline.stop()
        if error:
            self._microphone_failed(generation, error)
            return
        if not self.session.advance(generation, State.STARTING_MIC, State.RECORDING):
            return
        self._last_audio_info = "MIC_RECORDING"
        self._last_partial_info = "Partial: off or not started"
        self._last_transcript_check = "STT_RECORDING"
        self.status.update_state("Aufnahme / Запись: " + getattr(self.settings, "stt_backend", "") + " — " + getattr(self.settings, "language", "") + " | " + self.settings.hotkey + " = Stopp")
        if getattr(self, "_manual_insert_reason", ""):
            self.notify("Lokale Diktierung: Text zum Kopieren / Локальная диктовка: текст для копирования")
        self.deadline.start(self.settings.max_record_seconds * 1000)
        if (getattr(self.settings, "local_partial_enabled", False) and getattr(self, "_local_stt_armed", False)
                and self.settings.stt_backend in ("mini_worker", "mini_cpu")):
            self._start_local_partial(generation)

    def _start_local_partial(self, generation):
        settings = self.settings
        engine = "openvino" if settings.stt_backend == "mini_cpu" else "cuda"
        warm = getattr(self, "cuda_warm", None)
        cached = warm.current() if engine == "cuda" and warm is not None else None
        if engine == "cuda" and warm is not None and warm.status()[0] != "off" and cached is None:
            self.deadline.stop()
            try:
                self.mic.cancel()
            except Exception:
                pass
            self.session.finish(generation)
            self._edit_identity = None
            self.status.hide()
            self.notify("CUDA_STT_WORKER_EXITED")
            return
        terms = settings.vocabulary() if hasattr(settings, "vocabulary") else getattr(settings, "stt_hotwords", "")
        if cached is not None:
            cached.hotwords = terms
        long_mode = getattr(settings, 'max_record_seconds', 25) > MAX_SEGMENT_SECONDS
        backend = cached if cached is not None else ResidentMiniSTT(settings.min_free_vram_mib, engine=engine,
            enhanced=getattr(settings,'enhanced_cpu_stt',False), hotwords=terms,
            on_delta=None if long_mode else lambda text:self.signals.partial_ready.emit(generation,text))
        if long_mode:
            turn = LongDictationTurn(backend, self.recorder.raw_snapshot, settings.language,
                lambda text: self.signals.partial_ready.emit(generation, text),
                keep_resident=cached is not None, conditioning=settings.stt_audio_conditioning,
                native_preview=engine == "openvino" and settings.enhanced_cpu_stt)
        else:
            turn = LocalPartialTurn(backend, self.recorder.snapshot, settings.language,
                lambda text: self.signals.partial_ready.emit(generation, text),
                interval=2.5 if engine == "openvino" else 2.0, keep_resident=cached is not None)
        self._partial_turn = turn
        self._last_partial_info = "Partial: warm CUDA" if cached is not None else "Partial: warming up"
        self._dictation_worker_generation = generation
        self.status.update_state("Local STT: " + engine + (" — recording / ready" if cached is not None else " — recording / warmup"))
        try:
            future = self.executor.submit(turn.run)
        except RuntimeError:
            turn.cancel()
            self.on_completed(generation, "", "STT_WORKER_START_FAILED")
            return
        future.add_done_callback(partial(self._complete_stt_future, generation))

    @Slot(int, str)
    def _partial_ready(self, generation, text):
        snap = self.session.snapshot()
        if (not self._shutting_down and generation == self._dictation_worker_generation
                and generation == snap.generation and snap.state == State.RECORDING
                and inspect_transcript(text, max_chars=MAX_RECORD_TEXT) == "STT_TEXT_OK"):
            self.status.update_state(getattr(getattr(self, "settings", None), "stt_backend", "local") + " | " + "Vorschau / Черновик:\n" + ("..." if len(text)>240 else "") + text[-240:])

    def _complete_stt_future(self, generation, future):
        try:
            text, error = future.result(), ""
        except Exception as exc:
            text = exc.text if isinstance(exc, IncompleteDictation) else ""
            error = str(exc)[:80] if isinstance(exc, SpeechFailure) else "STT_ERROR"
        try:
            self.signals.completed.emit(generation, text, error)
        except RuntimeError:
            pass

    def on_max_duration(self):
        if self.session.snapshot().state == State.RECORDING:
            self.finish_recording()

    def finish_recording(self):
        snap = self.session.snapshot()
        if snap.state != State.RECORDING:
            return
        if not self.session.advance(snap.generation, State.RECORDING, State.STOPPING_MIC):
            return
        self.deadline.stop()
        self.mic_deadline.start(8000)
        self.status.update_state("MIC_STOPPING")
        try:
            self.mic.finish()
        except MicrophoneError as exc:
            self._microphone_failed(snap.generation, str(exc))

    def _microphone_timeout(self):
        snap = self.session.snapshot()
        if snap.state in (State.STARTING_MIC, State.STOPPING_MIC):
            self._microphone_failed(snap.generation, "MIC_DRIVER_TIMEOUT")

    def _microphone_failed(self, generation, error):
        self.mic_deadline.stop()
        self.deadline.stop()
        self.mic.cancel()
        turn = getattr(self, "_partial_turn", None)
        prefix = turn.confirmed_text() if isinstance(turn, LongDictationTurn) else ""
        if turn is not None:
            turn.cancel()
        self.session.finish(generation)
        self.status.hide()
        self._edit_identity = None
        self._dictation_target = None
        quality = getattr(self.recorder, "last_quality", None)
        self._last_audio_info = quality.summary() if isinstance(quality, AudioQuality) else error
        self._last_transcript_check = error
        if error == "MIC_DEVICE_UNAVAILABLE":
            self.notify("MIC_DEVICE_UNAVAILABLE: Mikrofon in Einstellungen wählen und speichern / Выберите доступный микрофон в настройках и сохраните.")
        else:
            self.notify(error)
        if prefix:
            show_manual_fallback(prefix, "INCOMPLETE_DICTATION: " + error)

    @Slot(int, bytes, str)
    def _microphone_stopped(self, generation, wav, error):
        snap = self.session.snapshot()
        if self._shutting_down or snap.generation != generation or snap.state != State.STOPPING_MIC:
            return
        self.mic_deadline.stop()
        if error:
            self._microphone_failed(generation, error)
            return
        quality = getattr(self.recorder, "last_quality", None)
        if isinstance(quality, AudioQuality):
            self._last_audio_info = quality.summary()
        if not self.session.advance(generation, State.STOPPING_MIC, State.AWAITING_APPROVAL):
            return
        settings = self.settings
        self.status.hide()
        if self._shutting_down or (settings.stt_backend != "openai_cloud" and not getattr(self, "_local_stt_armed", False)):
            self.session.finish(snap.generation)
            return
        if settings.stt_backend == "openai_cloud" and not self._approve_dictation():
            self.session.finish(snap.generation)
            self.notify("Aufnahme verworfen; kein Cloud-Upload.")
            return
        # The modal dialog runs a nested Qt event loop: Stop may already
        # have invalidated this recording. Never restore an old generation.
        if self._shutting_down or not self.session.advance(
                snap.generation, State.AWAITING_APPROVAL, State.RECOGNIZING):
            return
        self.status.update_state("STT: " + settings.stt_backend)
        if getattr(self, "_partial_turn", None) is not None:
            try:
                if not self._partial_turn.finalize(wav):
                    raise SpeechFailure("STT_CANCELLED")
            except SpeechFailure as exc:
                self._microphone_failed(generation, str(exc))
            return
        self._dictation_worker_generation = snap.generation

        def process():
            state = self.session.snapshot()
            if state.generation != snap.generation or state.state != State.RECOGNIZING:
                raise RuntimeError("STT_CANCELLED")
            warm = getattr(self, "cuda_warm", None)
            cached = warm.current() if settings.stt_backend == "mini_worker" and warm is not None else None
            if settings.stt_backend == "mini_worker" and warm is not None and warm.status()[0] != "off" and cached is None:
                raise SpeechFailure("CUDA_STT_WORKER_EXITED")
            terms = settings.vocabulary() if hasattr(settings, "vocabulary") else getattr(settings, "stt_hotwords", "")
            if cached is not None:
                cached.hotwords = terms
            long_mode = getattr(settings, "max_record_seconds", 25) > MAX_SEGMENT_SECONDS
            if settings.stt_backend == "openai_cloud":
                backend = OpenAIFileSTT()
            elif not getattr(self, "_local_stt_armed", False):
                raise SpeechFailure("LOCAL_MODE_NOT_AUTHORIZED")
            elif settings.stt_backend == "shared":
                backend = SpeechCoreHTTP(port=settings.stt_port)
            elif settings.stt_backend in ("mini_worker", "mini_cpu"):
                if long_mode and cached is None:
                    backend = ResidentMiniSTT(settings.min_free_vram_mib,
                        engine="openvino" if settings.stt_backend == "mini_cpu" else "cuda",
                        enhanced=getattr(settings,"enhanced_cpu_stt",False), hotwords=terms)
                elif settings.stt_backend == 'mini_cpu' and getattr(settings,'enhanced_cpu_stt',False):
                    backend = ResidentMiniSTT(settings.min_free_vram_mib, engine='openvino', enhanced=True, hotwords=terms)
                elif settings.stt_backend == "mini_worker":
                    backend = cached if cached is not None else MiniOneShotSTT(settings.min_free_vram_mib, engine="cuda", hotwords=terms)
                else:
                    backend = MiniOneShotSTT(settings.min_free_vram_mib, engine="openvino")
            else:
                raise SpeechFailure("INVALID_STT_ENGINE")
            self.active_backend = backend
            success = False
            result = ""
            try:
                state = self.session.snapshot()
                if state.generation != snap.generation or state.state != State.RECOGNIZING:
                    if hasattr(backend, "cancel"):
                        backend.cancel()
                    raise RuntimeError("STT_CANCELLED")
                if long_mode:
                    def cancelled():
                        state = self.session.snapshot()
                        return state.generation != snap.generation or state.state != State.RECOGNIZING
                    decode = backend.transcribe_final if settings.stt_backend in ("mini_cpu", "mini_worker") else backend.transcribe
                    chunks = ChunkedRecording(decode, settings.language, cancelled,
                                              conditioning=settings.stt_audio_conditioning)
                    result = chunks.consume(wav, final=True)
                else:
                    result = backend.transcribe_final(wav, settings.language) if cached is not None else backend.transcribe(wav, settings.language)
                success = True
                return result
            except IncompleteDictation as exc:
                result = exc.text
                raise
            finally:
                cleanup_error = (close_provider(backend)
                                 if not (cached is not None and success) else "")
                if self.active_backend is backend:
                    self.active_backend = None
                if cleanup_error:
                    raise IncompleteDictation(stt_block_reason() or cleanup_error, result) from None

        try:
            future = self.executor.submit(process)
        except RuntimeError:
            self.on_completed(snap.generation, "", "STT_WORKER_START_FAILED")
            return
        def done(f):
            try:
                result, error = f.result(), ""
            except Exception as exc:
                result = exc.text if isinstance(exc, IncompleteDictation) else ""
                # Only typed error codes, never content/voice.
                error = str(exc) if isinstance(exc, SpeechFailure) else "STT_ERROR"
                error = error[:80]
            try:
                self.signals.completed.emit(snap.generation, result, error)
            except RuntimeError:
                pass
        future.add_done_callback(done)

    @Slot(int, str, str)
    def on_completed(self, generation: int, text: str, error: str):
        if generation != self._dictation_worker_generation:
            return
        self._dictation_worker_generation = None
        if error in (PROVIDER_CLEANUP_FAILED, STT_CLEANUP_FAILED):
            self._stt_cleanup_failed = True
            if not self._shutting_down and generation != self.session.snapshot().generation:
                self.notify(error)
        turn = getattr(self, "_partial_turn", None)
        was_partial = turn is not None
        if isinstance(turn, (LocalPartialTurn, LongDictationTurn)):
            self._last_partial_info = turn.summary()
        self._partial_turn = None
        snap = self.session.snapshot()
        if was_partial and not self._shutting_down and snap.generation == generation and snap.state in (State.RECORDING, State.STOPPING_MIC):
            self.deadline.stop()
            self.mic_deadline.stop()
            try:
                self.mic.cancel()
            except Exception:
                pass
            self.session.finish(generation)
            self.status.hide()
            self._edit_identity = None
            self._dictation_target = None
            self._last_transcript_check = error or "LOCAL_PARTIAL_ENDED_EARLY"
            self.notify(self._last_transcript_check)
            if error and isinstance(turn, LongDictationTurn):
                prefix = text or turn.confirmed_text()
                if inspect_transcript(prefix, max_chars=MAX_RECORD_TEXT) == "STT_TEXT_OK":
                    show_manual_fallback(prefix, "INCOMPLETE_DICTATION: " + error)
            return
        if self._shutting_down or not self.session.advance(generation, State.RECOGNIZING, State.INSERTING):
            return
        self.status.hide()
        if error or not text:
            self.session.finish(generation)
            self._last_transcript_check = error or "NO_SPEECH"
            self.notify("Spracherkennung: " + self._last_transcript_check)
            if error and text:
                signal = getattr(getattr(self, "recorder", None), "last_quality", None)
                seconds = signal.seconds if isinstance(signal, AudioQuality) else None
                if inspect_transcript(text, seconds, max_chars=MAX_RECORD_TEXT) == "STT_TEXT_OK":
                    show_manual_fallback(text, "INCOMPLETE_DICTATION: " + error)
            return
        signal = getattr(getattr(self,'recorder',None),'last_quality',None)
        quality = inspect_transcript(text,signal.seconds if isinstance(signal,AudioQuality) else None,
                                     max_chars=MAX_RECORD_TEXT if getattr(getattr(self,"settings",None),"max_record_seconds",25) > MAX_SEGMENT_SECONDS else MAX_SEGMENT_TEXT)
        self._last_transcript_check = quality
        if getattr(self, "_diagnose_next", False):
            self._diagnose_next = False
            self._edit_identity = None
            self.session.finish(generation)
            self._refresh_menu()
            show_manual_fallback(text[:MAX_RECORD_TEXT], "STT TEST (no insertion) | " + self.settings.stt_backend + " | " + quality + "\n" + self._last_audio_info + "\n" + getattr(self, "_last_partial_info", "Partial: off"))
            return
        if quality != "STT_TEXT_OK":
            self.session.finish(generation)
            self._edit_identity = None
            self.notify(quality + " — Aufnahme wiederholen / Повторите запись.")
            return
        if getattr(getattr(self,'settings',None),'stt_term_correction',False):
            from .stt_enhancements import correct_terms
            text, rules = correct_terms(text)
            if rules:
                corrected_quality = inspect_transcript(text, signal.seconds if isinstance(signal, AudioQuality) else None,
                    max_chars=MAX_RECORD_TEXT if getattr(self.settings, "max_record_seconds", 25) > MAX_SEGMENT_SECONDS else MAX_SEGMENT_TEXT)
                if corrected_quality != "STT_TEXT_OK":
                    self.session.finish(generation)
                    self._edit_identity = None
                    self._last_transcript_check = corrected_quality
                    self.notify(corrected_quality)
                    return
                self._last_transcript_check = 'STT_TEXT_OK / terms=' + ','.join(rules)
        # Let Ctrl/Alt from global hotkey release. Focus must remain the original HWND.
        QTimer.singleShot(175, lambda: self.try_insert(generation, text))

    def try_insert(self, generation: int, text: str):
        snap = self.session.snapshot()
        if snap.generation != generation or snap.state != State.INSERTING:
            return
        identity = self._edit_identity
        if len(text) > MAX_SEGMENT_TEXT or identity is None or self._edit_worker_generation is not None:
            self.session.finish(generation)
            reason = "LONG_TEXT_MANUAL_COPY" if len(text) > MAX_SEGMENT_TEXT else (getattr(self, "_manual_insert_reason", "") or "EDIT_TARGET_UNVERIFIED")
            self._manual_insert_reason = ""
            self._dictation_target = None
            self._edit_identity = None
            self._last_transcript_check = "STT_TEXT_OK / MANUAL_COPY"
            show_manual_fallback(text, reason)
            return
        self._edit_worker_generation = generation
        cancelled = self._edit_cancel

        def worker():
            error = ""
            try:
                run_edit_job(identity.target, cancelled, expected=identity, text=text, timeout=15)
            except InjectionError as exc:
                error = str(exc)
            except Exception:
                error = "EDIT_INSERT_FAILED"
            try:
                self.signals.insertion_ready.emit(generation, text, error)
            except RuntimeError:
                pass

        try:
            threading.Thread(target=worker, name="voxbridge-edit-insert", daemon=True).start()
        except RuntimeError:
            self._insertion_ready(generation, text, "EDIT_WORKER_START_FAILED")

    @Slot(int, str, str)
    def _insertion_ready(self, generation, text, error):
        if generation != self._edit_worker_generation:
            return
        self._edit_worker_generation = None
        self._edit_identity = None
        snap = self.session.snapshot()
        if self._shutting_down or snap.generation != generation or snap.state != State.INSERTING:
            return
        # Release the session before displaying the explicit fallback.
        self.session.finish(generation)
        if error:
            self._last_transcript_check = "INSERT: " + error
            show_manual_fallback(text, error)
        else:
            self._last_transcript_check = "STT_TEXT_OK / INSERT_SENT"
            self.notify("Texteingabe gesendet; Ergebnis bitte prüfen.")

    def cancel(self):
        cancelled = getattr(self, "_edit_cancel", None)
        if cancelled is not None:
            cancelled.set()
        self._edit_identity = None
        self._dictation_target = None
        self._manual_insert_reason = ""
        self.deadline.stop()
        self.mic_deadline.stop()
        self.session.cancel()
        self.mic.cancel()
        if getattr(self, "_diagnose_next", False):
            self._diagnose_next = False
            self._refresh_menu()
        selection = getattr(self, "selection", None)
        if selection is not None:
            selection.cancel()
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        turn = getattr(self, "_partial_turn", None)
        if turn is not None:
            turn.cancel()
        warm = getattr(self, "cuda_warm", None)
        if warm is not None and warm.status()[0] == "failed":
            warm.deactivate()
        backend = self.active_backend
        if backend is not None and hasattr(backend, "cancel"):
            backend.cancel()  # cancel our request/worker, never stop the shared service
        self.status.hide()
        self.notify("Abgebrochen; alte Ergebnisse werden verworfen.")

    def _exit_pending(self):
        """Poll actual outstanding work; never join native workers on Qt."""
        if (getattr(getattr(self, "local_llm", None), "pending", False)
                or self._dictation_worker_generation is not None
                or self._edit_worker_generation is not None
                or self.mic.pending
                or self.cuda_warm.status()[0] in ("loading", "closing")):
            return True
        if any(controller.shutdown_pending for controller in
               (self.selection, self.read_selection, self.read_copy)):
            return True
        if any(listener.running is True for listener in
               (self.hotkeys, *self._region_hotkeys.values())):
            return True
        if any(dialog is not None and dialog._busy for dialog in
               (self.analysis_dialog, self.vision_dialog, self.live_captions_dialog)):
            return True
        return (self.tts_dialog is not None and self.tts_dialog.shutdown_pending
                or self.voice_dialog is not None and self.voice_dialog._worker_generation is not None
                or self.live_captions_dialog is not None and self.live_captions_dialog._refreshing)

    @Slot()
    def request_exit(self, checked=False):
        """Normal tray exit: stop work, then keep Qt alive for completion signals."""
        del checked
        if self._exit_timer.isActive():
            return
        self._exit_requested = True
        self.shutdown()
        self._exit_deadline = time.monotonic() + 15
        self._exit_timer.start()
        self._poll_exit()

    @Slot()
    def _poll_exit(self):
        if not self._exit_pending():
            self._exit_timer.stop()
            self.tray.hide()
            self.app.quit()
        elif time.monotonic() >= self._exit_deadline:
            # Do not force an exit while a worker may own audio or clipboard.
            # A stalled native driver may require OS intervention. The tray
            # remains accessible for an explicit retry, with other actions disabled.
            self._exit_timer.stop()
            self.tray.setToolTip("OMOI VoxBridge: SHUTDOWN_CLEANUP_PENDING")
            self.notify("SHUTDOWN_CLEANUP_PENDING - Retry exit")
        else:
            self.tray.setToolTip("OMOI VoxBridge: stopping workers")

    def shutdown(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        self._hotkey_ready = False
        self.cancel()
        self.hotkeys.stop()
        for listener in self._region_hotkeys.values():
            listener.stop()
        self.read_selection.disable()
        if getattr(self,'read_copy',None) is not None:
            self.read_copy.disable()
        self.selection.disable()
        if self.tts_dialog is not None:
            self.tts_dialog.shutdown_local()
        for dialog in (self.control_center, self.live_captions_dialog, self.caption_demo_overlay,
                       self.analysis_dialog, self.vision_dialog,
                       self.tts_dialog, self.voice_dialog):
            if dialog is not None:
                dialog.close()
                dialog.setEnabled(False)
        self.cuda_warm.deactivate()
        llm = getattr(self, "local_llm", None)
        if llm is not None:
            llm.request_stop()
        self.executor.shutdown(wait=False, cancel_futures=False)
        if self._exit_requested:
            self._refresh_menu()
        else:
            # aboutToQuit / test teardown: Qt cannot postpone an external exit.
            self.tray.hide()


def main() -> int:
    if os.name != "nt":
        raise SystemExit("VoxBridge MVP1 requires Windows")
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    try:
        bridge = VoxBridge(app)
    except Exception as exc:
        # No personal input has been captured yet.
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "VoxBridge", "Start fehlgeschlagen: " + str(exc)[:120])
        return 2
    return app.exec()


def main_standalone() -> int:
    """Installed launcher: independent runtime paths, without creating assets."""
    from .config import config_path
    os.environ.setdefault("VOXBRIDGE_RUNTIME_ROOT",
                          str(config_path().parent / "local-runtime"))
    return main()


if __name__ == "__main__":
    raise SystemExit(main())
