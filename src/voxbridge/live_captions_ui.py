"""Manual start/stop Windows loopback -> OpenAI Realtime -> caption overlay.

Opening this dialog NEVER starts capture or an API session. Devices are
enumerated only via Refresh. A Start click requires one-time or saved paid-cloud consent;
caption translation needs independent consent. Local factories are opt-in.
"""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from .captions_core import CaptionCoordinator
from .captions_overlay import CaptionsOverlay
from .captions_translate import translate_committed, translate_locally
from .session_limits import (
    MAX_SESSION_TRANSLATION_CALLS, MAX_TRANSLATION_OUTPUT_TOKENS,
    TranslationQuota, RealtimeUsage,
)
from .cloud_providers import OpenAIResponses, close_provider, PROVIDER_CLEANUP_FAILED
from .realtime_transport import RealtimeTransportFailure, run_realtime_transcription
from .cartesia_realtime import run_ink_transcription
from .wasapi_capture import CaptureFailure, LoopbackDevice
from .capture_isolation import capture_live, capture_block_reason
from .device_discovery import discover_loopback_devices
from .caption_preferences import CaptionPreferences
from .cloud_permissions import approved
from .private_dialog import PrivateDialog

MAX_TRANSLATION_CALLS = MAX_SESSION_TRANSLATION_CALLS


class LiveSignals(QObject):
    complete = Signal(int, str)
    translation_complete = Signal(int, str)
    devices_ready = Signal(int, object, str)
    stage = Signal(int, str)


class LiveCaptionsDialog(PrivateDialog):
    def __init__(self, parent=None, *,
                 discover_devices=discover_loopback_devices,
                 capture=capture_live,
                 transcribe=run_realtime_transcription,
                 ink_transcribe=run_ink_transcription,
                 translation_factory=OpenAIResponses,
                 can_start=None, preference_loader=None, preference_saver=None,
                 permission_checker=None, local_translation_factories=None):
        super().__init__(parent)
        self.setWindowTitle("OMOI VoxBridge — Live-Untertitel (Cloud)")
        self.resize(760, 430)
        self._permission_checker = permission_checker
        self._preference_loader = preference_loader
        self._preference_saver = preference_saver
        self._discover = discover_devices
        self._capture = capture
        self._transcribe = transcribe
        self._ink_transcribe = ink_transcribe
        self._translation_factory = translation_factory
        # Only explicitly registered, identity-verified CPU factories are permitted.
        # Selecting an engine never launches it; Gemma production registration withdrawn.
        self._local_translation_factories = dict(local_translation_factories or {})
        self._can_start = can_start if can_start is not None else (lambda: True)
        self._generation = 0
        self._worker_generation = 0
        self._busy = False
        self._source_done = True
        self._translation_alive = False
        self._last_error = ""
        self._cleanup_error = ""
        self._quota = None
        self._usage = None
        self._refresh_generation = 0
        self._refreshing = False
        self._refresh_worker = None
        self._stop = threading.Event()
        self._source_finished = threading.Event()
        self._coordinator = CaptionCoordinator()
        self._overlay = CaptionsOverlay(self._coordinator)
        self._overlay.closed.connect(self._overlay_closed)
        self.signals = LiveSignals(self)
        self.signals.complete.connect(self._completed)
        self.signals.translation_complete.connect(self._translation_completed)
        self.signals.devices_ready.connect(self._devices_ready)
        self.signals.stage.connect(self._stage_changed)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Systemton (WASAPI) → Cloud STT.\n"
            "Звук всех программ выбранного устройства будет передаваться в облако "
            "только после Start и отдельного согласия."
        ))
        row = QHBoxLayout()
        self.devices = QComboBox(self)
        self.devices.setMinimumWidth(280)
        row.addWidget(self.devices)
        self.refresh = QPushButton("Geräte laden / Обновить устройства")
        self.refresh.clicked.connect(self._refresh)
        row.addWidget(self.refresh)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Max. Sitzungsdauer:"))
        self.seconds = QSpinBox(self)
        self.seconds.setMinimum(5)
        self.seconds.setMaximum(60)
        self.seconds.setValue(30)
        self.seconds.setSuffix(" Sekunden")
        row.addWidget(self.seconds)
        layout.addLayout(row)

        provider_row = QHBoxLayout()
        provider_row.addWidget(QLabel("STT / API:"))
        self.stt_provider = QComboBox(self)
        self.stt_provider.addItem("OpenAI gpt-live-transcribe", "openai")
        self.stt_provider.addItem("Cartesia Ink Preview (experimental)", "cartesia_preview")
        provider_row.addWidget(self.stt_provider, 1)
        layout.addLayout(provider_row)

        language_row = QHBoxLayout()
        language_row.addWidget(QLabel("Original / Язык речи:"))
        self.source_language = QComboBox(self)
        for code, label in (("de", "Deutsch"), ("ru", "Русский"), ("en", "English")):
            self.source_language.addItem(label, code)
        language_row.addWidget(self.source_language)
        language_row.addWidget(QLabel("Übersetzung / Перевод:"))
        self.target_language = QComboBox(self)
        language_row.addWidget(self.target_language)
        layout.addLayout(language_row)

        delay_row = QHBoxLayout()
        delay_row.addWidget(QLabel("STT Latenz / Задержка:"))
        self.delay = QComboBox(self)
        for value in ("minimal", "low", "medium", "high", "xhigh"):
            self.delay.addItem(value, value)
        self.delay.setCurrentIndex(1)
        delay_row.addWidget(self.delay)
        # Advanced controls are attached below the main controls.

        engine_row = QHBoxLayout()
        engine_row.addWidget(QLabel("Übersetzer / Переводчик:"))
        self.translation_engine = QComboBox(self)
        for label, key in (("Ohne / Без перевода", "off"),
                           ("OpenAI Cloud", "cloud"),
                           ("Gemma Local CPU (Referenz, zu langsam)", "gemma_cpu"),
                           ("Lokale Übersetzung (MT)", "specialist")):
            if key not in ("off", "cloud") and key not in self._local_translation_factories:
                label += " (nicht eingerichtet)"
            self.translation_engine.addItem(label, key)
        engine_row.addWidget(self.translation_engine, 1)
        layout.addLayout(engine_row)
        self.audio_consent = QCheckBox(
            "Ich erlaube die kostenpflichtige Cloud-Übertragung von Systemaudio "
            "für diese Sitzung. / Разрешаю передавать системный звук в OpenAI.",
            self,
        )
        layout.addWidget(self.audio_consent)
        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("Max. Übersetzungen / Лимит переводов:"))
        self.translation_limit = QSpinBox(self)
        self.translation_limit.setRange(1, MAX_TRANSLATION_CALLS)
        self.translation_limit.setValue(MAX_TRANSLATION_CALLS)
        limit_row.addWidget(self.translation_limit)
        # Cloud limit is in the advanced panel.
        self.translation_consent = QCheckBox(self)
        layout.addWidget(self.translation_consent)
        layout.addWidget(QLabel("Oder dauerhafte Freigaben im Tray / Или постоянные разрешения в трее."))
        self.stt_provider.currentIndexChanged.connect(self._stt_changed)
        self.translation_engine.currentIndexChanged.connect(self._engine_changed)
        self.translation_limit.valueChanged.connect(self._mode_changed)
        self.seconds.valueChanged.connect(self._invalidate_consent)
        self.devices.currentIndexChanged.connect(self._invalidate_consent)
        self.source_language.currentIndexChanged.connect(self._source_changed)
        self.target_language.currentIndexChanged.connect(self._invalidate_consent)
        self.delay.currentIndexChanged.connect(self._invalidate_consent)
        self._source_changed()
        self._mode_changed()
        self._stt_changed()

        controls = QHBoxLayout()
        self.start = QPushButton("Start / Начать")
        self.start.clicked.connect(self._start)
        self.stop = QPushButton("Stop / Остановить")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self._stop_session)
        controls.addWidget(self.start)
        controls.addWidget(self.stop)
        layout.addLayout(controls)
        self.status = QLabel("Bereit. Keine Aufnahme oder API-Sitzung gestartet.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.usage_label = QLabel("Lokale Nutzungszähler / Локальный учёт: —")
        self.usage_label.setWordWrap(True)
        # Usage counters are in the advanced panel.
        display_row = QHBoxLayout()
        display_row.addWidget(QLabel("Anzeige / Вид:"))
        self.display_mode = QComboBox(self)
        for label, mode in (("DE/RU/EN + Übersetzung", "both"),
                            ("Original", "original"), ("Übersetzung", "translation")):
            self.display_mode.addItem(label, mode)
        display_row.addWidget(self.display_mode)
        self.font_size = QSpinBox(self)
        self.font_size.setRange(12, 32)
        self.font_size.setValue(15)
        self.font_size.setSuffix(" px")
        display_row.addWidget(self.font_size)
        self.visible_lines = QSpinBox(self)
        self.visible_lines.setRange(1, 6)
        self.visible_lines.setValue(6)
        self.visible_lines.setSuffix(" Zeilen")
        display_row.addWidget(self.visible_lines)
        self.opacity = QSpinBox(self)
        self.opacity.setRange(50, 100)
        self.opacity.setValue(100)
        self.opacity.setSuffix(" %")
        display_row.addWidget(self.opacity)
        self.advanced_toggle = QPushButton("Erweiterte Einstellungen / Расширенные", self)
        self.advanced_toggle.setCheckable(True)
        self.advanced_panel = QWidget(self)
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addLayout(delay_row)
        advanced_layout.addLayout(limit_row)
        self.group_sentences = QCheckBox("Satzweise Übersetzung (bis 600 ms) / Перевод фразами")
        self.group_sentences.toggled.connect(self._invalidate_consent)
        advanced_layout.addWidget(self.group_sentences)
        advanced_layout.addLayout(display_row)
        advanced_layout.addWidget(self.usage_label)
        self.advanced_panel.hide()
        self.advanced_toggle.toggled.connect(self.advanced_panel.setVisible)
        layout.addWidget(self.advanced_toggle)
        layout.addWidget(self.advanced_panel)
        self.display_mode.currentIndexChanged.connect(self._display_changed)
        self.font_size.valueChanged.connect(self._display_changed)
        self.visible_lines.valueChanged.connect(self._display_changed)
        self.opacity.valueChanged.connect(self._display_changed)
        pref_row = QHBoxLayout()
        self.save_preferences = QPushButton("Einstellungen speichern / Сохранить настройки")
        self.reset_preferences = QPushButton("Standard / По умолчанию")
        self.save_preferences.clicked.connect(self._save_preferences)
        self.reset_preferences.clicked.connect(self._reset_preferences)
        pref_row.addWidget(self.save_preferences)
        pref_row.addWidget(self.reset_preferences)
        layout.addLayout(pref_row)
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(250)
        self._usage_timer.timeout.connect(self._refresh_usage)
        layout.addWidget(QLabel(
            "Keine automatische Wiederverbindung. Stop kann laufende API-Kosten "
            "nicht rückgängig machen. Lokale CPU-Modelle nur nach Zustimmung."
        ))

        if self._preference_loader is not None:
            try:
                self._apply_preferences(self._preference_loader())
            except (OSError, ValueError, TypeError):
                self.status.setText("CAPTION_SETTINGS_INVALID / Standardwerte verwendet.")

    def _current_preferences(self):
        return CaptionPreferences(
            source=self.source_language.currentData(), target=self.target_language.currentData(),
            translate=self.translation_engine.currentData() != "off", seconds=self.seconds.value(),
            translation_engine=self.translation_engine.currentData() if self.translation_engine.currentData() != "off" else "cloud",
            delay=self.delay.currentData(), translation_limit=self.translation_limit.value(),
            display_mode=self.display_mode.currentData(), font_size=self.font_size.value(),
            visible_lines=self.visible_lines.value(), opacity=self.opacity.value(),
            group_sentences=self.group_sentences.isChecked(),
            stt_provider=self.stt_provider.currentData(),
        ).validate()

    def _apply_preferences(self, prefs):
        prefs.validate()
        self.stt_provider.setCurrentIndex(self.stt_provider.findData(prefs.stt_provider))
        self.source_language.setCurrentIndex(self.source_language.findData(prefs.source))
        self.target_language.setCurrentIndex(self.target_language.findData(prefs.target))
        self.translation_engine.setCurrentIndex(self.translation_engine.findData(
            prefs.translation_engine if prefs.translate else "off"))
        self.seconds.setValue(prefs.seconds)
        self.delay.setCurrentIndex(self.delay.findData(prefs.delay))
        self.translation_limit.setValue(prefs.translation_limit)
        self.display_mode.setCurrentIndex(self.display_mode.findData(prefs.display_mode))
        self.font_size.setValue(prefs.font_size)
        self.visible_lines.setValue(prefs.visible_lines)
        self.opacity.setValue(prefs.opacity)
        self.group_sentences.setChecked(prefs.group_sentences)
        self._mode_changed()
        self._display_changed()
        self._invalidate_consent()

    def _save_preferences(self):
        if self._busy or self._preference_saver is None:
            return
        try:
            self._preference_saver(self._current_preferences())
        except (OSError, ValueError, TypeError):
            self.status.setText("CAPTION_SETTINGS_SAVE_FAILED")
        else:
            self.status.setText("Einstellungen lokal gespeichert / Настройки сохранены.")

    def _reset_preferences(self):
        if not self._busy:
            self._apply_preferences(CaptionPreferences())
            self.status.setText("Standardwerte wiederhergestellt; Speichern ist optional.")

    def _stt_changed(self, *args):
        ink = self.stt_provider.currentData() == "cartesia_preview"
        name = "Cartesia Ink Preview" if ink else "OpenAI"
        self.audio_consent.setText(
            f"Ich erlaube die ggf. kostenpflichtige Systemaudio-Uebertragung an {name}. "
            f"/ \u0420\u0430\u0437\u0440\u0435\u0448\u0430\u044e \u043f\u0435\u0440\u0435\u0434\u0430\u0447\u0443 \u0437\u0432\u0443\u043a\u0430 \u0432 {name}.")
        self.delay.setEnabled(not ink and not self._busy)
        self._invalidate_consent()

    def _engine_changed(self, *args):
        self._invalidate_consent()
        self._mode_changed()

    def _mode_changed(self, *args):
        del args
        engine = self.translation_engine.currentData()
        enabled = engine != "off" and not self._busy
        cloud = engine == "cloud"
        self.translation_limit.setEnabled(enabled and cloud)
        if hasattr(self, "group_sentences"):
            self.group_sentences.setEnabled(enabled)
        self.target_language.setEnabled(enabled)
        self.translation_consent.setEnabled(enabled)
        self.translation_consent.setVisible(engine != "off")
        limit = self.translation_limit.value()
        if cloud:
            self.translation_consent.setText(
                f"Ich erlaube bis zu {limit} kostenpflichtige OpenAI-Übersetzungen (max. 256 Tokens je Aufruf).")
        else:
            self.translation_consent.setText("Ich erlaube lokale CPU-Übersetzung für diese Sitzung.")
        self.translation_consent.setChecked(False)

    def _source_changed(self, *args):
        previous = self.target_language.currentData()
        source = self.source_language.currentData()
        self.target_language.clear()
        for code, label in (("ru", "Русский"), ("de", "Deutsch"), ("en", "English")):
            if code != source:
                self.target_language.addItem(label, code)
        index = self.target_language.findData(previous)
        self.target_language.setCurrentIndex(max(0, index))
        self._invalidate_consent()

    def _display_changed(self, *args):
        self._overlay.set_display_options(
            self.display_mode.currentData(), self.font_size.value(),
            self.visible_lines.value(), self.opacity.value())

    def _invalidate_consent(self, *args):
        self.audio_consent.setChecked(False)
        self.translation_consent.setChecked(False)

    def _refresh_usage(self):
        if self._usage is None:
            return
        usage = self._usage.snapshot()
        calls = self._quota.calls_reserved if self._quota is not None else 0
        chars = self._quota.input_chars_reserved if self._quota is not None else 0
        limit = self._quota.max_calls if self._quota is not None else 0
        self.usage_label.setText(
            f"Audio: {usage.attempted_audio_seconds:.1f} / {self._usage.seconds} s; "
            f"Verbindung: {usage.attempted_connections}; Turns: {usage.attempted_commits}; "
            f"Übersetzung: {calls}/{limit}, {chars} Zeichen. "
            "Lokale Versuche, keine Abrechnung / Не сумма оплаты."
        )

    @Slot()
    def _refresh(self):
        if self._busy or self._refreshing:
            return
        # Enumerating WASAPI endpoints can block on Windows audio services.
        # Run it off the Qt main thread; no audio is ever captured here.
        self._refresh_generation += 1
        request_id = self._refresh_generation
        self._refreshing = True
        self._refresh_worker = request_id
        self.devices.clear()
        self.refresh.setEnabled(False)
        self.start.setEnabled(False)
        self.status.setText("WASAPI-Geräte werden im Hintergrund gesucht ...")

        def worker():
            result, error = (), ""
            try:
                devices = self._discover()
                if (not isinstance(devices, list) or len(devices) > 64 or
                        not all(isinstance(d, LoopbackDevice) for d in devices)):
                    raise CaptureFailure("WASAPI_INVALID_DEVICE_LIST")
                result = tuple(devices)
            except CaptureFailure as exc:
                # The sole legitimate errors are our own sanitized codes.
                error = str(exc)[:100]
            except Exception:
                error = "WASAPI_DEVICE_ENUMERATION_FAILED"
            try:
                self.signals.devices_ready.emit(request_id, result, error)
            except RuntimeError:
                pass

        try:
            threading.Thread(
                target=worker, name="voxbridge-wasapi-discovery", daemon=True
            ).start()
        except RuntimeError:
            self._devices_ready(request_id, (), "WASAPI_DISCOVERY_WORKER_START_FAILED")

    @Slot(int, object, str)
    def _devices_ready(self, request_id: int, devices: object, error: str):
        if request_id != self._refresh_worker:
            return
        self._refresh_worker = None
        self._refreshing = False
        self.refresh.setEnabled(not self._busy)
        self.start.setEnabled(not self._busy and not self._cleanup_error)
        if self._cleanup_error:
            self.status.setText(self._cleanup_error)
            return
        if request_id != self._refresh_generation:
            self.status.setText("Ger?te erneut laden / ???????? ?????? ?????????.")
            return
        if error:
            self.status.setText(error)
            return
        for device in devices:
            self.devices.addItem(device.name + (" (Systemstandard)" if device.is_default else ""), device.id)
            if device.is_default:
                self.devices.setCurrentIndex(self.devices.count() - 1)
        self.status.setText(
            f"{len(devices)} Loopback-Ausgabegeräte gefunden; noch keine Aufnahme."
        )

    @Slot()
    def _start(self):
        if self._cleanup_error:
            self.status.setText(self._cleanup_error)
            return
        if self._busy or self._refreshing:
            return
        if self._capture is capture_live and (reason := capture_block_reason()):
            self.status.setText(reason)
            return
        try:
            allowed = self._can_start() is True
        except Exception:
            allowed = False
        if not allowed:
            self.status.setText("Andere VoxBridge-Aufnahme aktiv; zuerst beenden.")
            return
        device_id = self.devices.currentData()
        if not isinstance(device_id, str) or not device_id:
            self.status.setText("Bitte zuerst ein Loopback-Ausgabegerät wählen.")
            return
        stt_provider = self.stt_provider.currentData()
        if stt_provider not in ("openai", "cartesia_preview"):
            self.status.setText("CAPTION_PREF_STT_PROVIDER")
            return
        ink = stt_provider == "cartesia_preview"
        audio_scope = "captions_cartesia_audio" if ink else "captions_openai_audio"
        if not (self.audio_consent.isChecked() or
                approved(self._permission_checker, audio_scope)):
            self.status.setText("Cloud-Audio-Freigabe ist erforderlich.")
            return
        engine = self.translation_engine.currentData()
        translate = engine != "off"
        cloud_translation = engine == "cloud"
        local_factory = self._local_translation_factories.get(engine)
        if translate and not cloud_translation and local_factory is None:
            self.status.setText("LOCAL_TRANSLATOR_NOT_CONFIGURED - Anderen Übersetzer oder Ohne wählen.")
            return
        if translate and not (self.translation_consent.isChecked() or
                              (cloud_translation and approved(
                                  self._permission_checker, "captions_openai_translation"))):
            self.status.setText("Separate Zustimmung zur Übersetzung erforderlich.")
            return
        if ink and not os.environ.get("CARTESIA_API_KEY"):
            self.status.setText("CARTESIA_API_KEY_MISSING")
            return
        if (not ink or cloud_translation) and not os.environ.get("OPENAI_API_KEY"):
            self.status.setText("OPENAI_API_KEY_MISSING")
            return

        self._generation += 1
        generation = self._generation
        self._worker_generation = generation
        self._stop = threading.Event()
        self._source_finished = threading.Event()
        self._busy = True
        self._source_done = False
        self._translation_alive = translate
        self._last_error = ""
        self._quota = TranslationQuota(max_calls=self.translation_limit.value()) if cloud_translation else None
        seconds = int(self.seconds.value())
        self._usage = RealtimeUsage(seconds)
        self._refresh_usage()
        self._usage_timer.start()
        self.start.setEnabled(False)
        self.save_preferences.setEnabled(False)
        self.reset_preferences.setEnabled(False)
        self.stop.setEnabled(True)
        self.refresh.setEnabled(False)
        self.devices.setEnabled(False)
        self.seconds.setEnabled(False)
        self.stt_provider.setEnabled(False)
        self.translation_engine.setEnabled(False)
        self.source_language.setEnabled(False)
        self.delay.setEnabled(False)
        source_language = self.source_language.currentData()
        target_language = self.target_language.currentData() if translate else None
        delay = self.delay.currentData()
        self.audio_consent.setChecked(False)
        self.audio_consent.setEnabled(False)
        self.translation_consent.setChecked(False)
        self._mode_changed()
        caption_generation = self._coordinator.begin(
            source_language=source_language, target_language=target_language,
            group_sentences=self.group_sentences.isChecked(),
        )
        self._overlay.show()
        self.status.setText("REALTIME_CONNECTING - " + ("Cartesia" if ink else "OpenAI"))

        # Session references are immutable for worker lifetime; a new session
        # must not start until BOTH audio and in-flight LLM workers finish.
        stop = self._stop
        source_finished = self._source_finished
        coordinator = self._coordinator
        quota = self._quota
        usage = self._usage
        provider_factory = self._translation_factory
        transcribe = self._ink_transcribe if ink else self._transcribe

        def translate_worker(provider=None):
            try:
                while not stop.is_set():
                    # Clear BEFORE checking the queue: a concurrent final or source
                    # completion after this point cannot lose its wakeup.
                    coordinator.translation_ready.clear()
                    job = coordinator.take_translation()
                    if job is None:
                        if stop.is_set():
                            return
                        if not source_finished.is_set():
                            coordinator.translation_ready.wait(coordinator.translation_wait_seconds())
                            continue
                        # Source completion may have occurred AFTER the first empty read.
                        job = coordinator.take_translation()
                        if job is None:
                            return
                    try:
                        if stop.is_set():
                            return
                        if job.cancel_event.is_set():
                            coordinator.finish_translation(job, failed=True)
                            continue
                        if quota is not None and not quota.reserve(job, stop=stop):
                            coordinator.finish_translation(job, failed=True)
                            continue
                        if provider is None:
                            if cloud_translation:
                                provider = provider_factory(
                                    max_output_tokens=MAX_TRANSLATION_OUTPUT_TOKENS,
                                    timeout=15,
                                )
                            else:
                                provider = local_factory()
                        if stop.is_set():
                            coordinator.finish_translation(job, failed=True)
                            return
                        if job.cancel_event.is_set():
                            coordinator.finish_translation(job, failed=True)
                            continue
                        if cloud_translation:
                            if getattr(provider, "is_cloud", None) is not True:
                                raise ValueError("CAPTIONS_CLOUD_PROVIDER_REQUIRED")
                            answer = translate_committed(job, provider, cloud_approved=True)
                        else:
                            answer = translate_locally(job, provider, local_approved=True)
                        coordinator.finish_translation(job, answer)
                    except Exception:
                        coordinator.finish_translation(job, failed=True)
                        # A failed local engine is not restarted per caption.
                        if not cloud_translation and not job.cancel_event.is_set() and not stop.is_set():
                            coordinator.disable_translation(caption_generation)
                            self.signals.stage.emit(generation, "LOCAL_TRANSLATION_FAILED - Original bleibt aktiv.")
                            return
                        # Never expose API text, private transcript, token or error details.
            finally:
                cleanup_error = close_provider(provider)
                try:
                    self.signals.translation_complete.emit(generation, cleanup_error)
                except RuntimeError:
                    pass

        def run_worker():
            error = ""
            translation_started = False
            prepared_provider = None
            audio = None
            try:
                if translate and not cloud_translation:
                    self.signals.stage.emit(generation, "LOCAL_TRANSLATOR_PREPARING")
                    try:
                        prepared_provider = local_factory()
                        if getattr(prepared_provider, "is_cloud", None) is not False:
                            raise ValueError("CAPTIONS_LOCAL_PROVIDER_REQUIRED")
                        prepare = getattr(type(prepared_provider), "prepare", None)
                        if callable(prepare):
                            prepare(prepared_provider, stop)
                        if stop.is_set():
                            return
                    except Exception:
                        if not stop.is_set():
                            error = "LOCAL_TRANSLATOR_PREFLIGHT_FAILED - Keine Audioaufnahme gestartet."
                        return
                if translate:
                    threading.Thread(
                        target=translate_worker, args=(prepared_provider,),
                        name="voxbridge-live-ru", daemon=True
                    ).start()
                    translation_started = True
                if stop.is_set():
                    return
                # Capture begins only after explicit local preflight, if selected.
                # Transport separately validates cloud consent and authentication.
                audio = self._capture(device_id, stop, seconds=seconds)
                turns = transcribe(
                    coordinator, caption_generation, audio,
                    approved=True, stop=stop, max_seconds=seconds, usage=usage, delay=delay,
                    on_stage=lambda stage: self.signals.stage.emit(generation, stage),
                )
                if turns == 0 and not stop.is_set():
                    error = "CAPTIONS_NO_SPEECH — Wiedergabegerät und Lautstärke prüfen."
            except (CaptureFailure, RealtimeTransportFailure) as exc:
                error = str(exc)[:100]
            except Exception:
                error = "LIVE_CAPTIONS_SESSION_FAILED"
            finally:
                if audio is not None:
                    close = getattr(audio, "close", None)
                    if callable(close):
                        try:
                            close()
                        except CaptureFailure as exc:
                            error = str(exc)[:100]
                        except Exception:
                            error = "WASAPI_CHILD_FAILED"
                if not translation_started:
                    error = close_provider(prepared_provider) or error
                if error:
                    # Do not start more paid translations after source failure.
                    stop.set()
                    if quota is not None:
                        quota.close()
                    coordinator.abort(caption_generation)
                if not error and not stop.is_set():
                    coordinator.finish_source(caption_generation)
                source_finished.set()
                coordinator.translation_ready.set()
                try:
                    if translate and not translation_started:
                        self.signals.translation_complete.emit(generation, "")
                    self.signals.complete.emit(generation, error)
                except RuntimeError:
                    pass

        try:
            threading.Thread(
                target=run_worker, name="voxbridge-live-stt", daemon=True
            ).start()
        except RuntimeError:
            stop.set()
            if quota is not None:
                quota.close()
            coordinator.abort(caption_generation)
            source_finished.set()
            coordinator.translation_ready.set()
            self._translation_alive = False
            self._completed(generation, "LIVE_CAPTIONS_WORKER_START_FAILED")

    @Slot(int, str)
    def _stage_changed(self, generation, stage):
        if generation == self._worker_generation and self._busy and not self._stop.is_set():
            self.status.setText(stage)

    @Slot()
    def _overlay_closed(self):
        self._stop_session(hide_overlay=False)

    @Slot()
    def _stop_session(self, *, hide_overlay=True):
        if not self._busy or self._stop.is_set():
            return
        self._generation += 1  # queued old results cannot start a new session
        self._stop.set()
        if self._quota is not None:
            self._quota.close()
        self._coordinator.stop()
        if hide_overlay:
            self._overlay.hide()
        self.stop.setEnabled(False)
        self.status.setText(
            "Stopp angefordert. Warte auf Audio UND Übersetzung; "
            "bereits gestartete API-Kosten können weiterlaufen."
        )

    def _finish_when_all_workers_exited(self):
        if not self._busy or not self._source_done or self._translation_alive:
            if self._source_done and self._translation_alive:
                self.status.setText(
                    "Audio gestoppt. Warte auf laufende Übersetzung; "
                    "Neustart bis zum Abschluss gesperrt."
                )
            return
        if self._quota is not None:
            self._quota.close()
        self._busy = False
        # All workers have exited: preserve committed rows but fence stale events.
        self._coordinator.abort(self._coordinator.snapshot().generation)
        self._usage_timer.stop()
        self._refresh_usage()
        self.audio_consent.setEnabled(True)
        self._invalidate_consent()
        self.stop.setEnabled(False)
        self.start.setEnabled(not self._cleanup_error)
        self.save_preferences.setEnabled(True)
        self.reset_preferences.setEnabled(True)
        self.refresh.setEnabled(True)
        self.devices.setEnabled(True)
        self.seconds.setEnabled(True)
        self.stt_provider.setEnabled(True)
        self.translation_engine.setEnabled(True)
        self.source_language.setEnabled(True)
        self._stt_changed()
        self._mode_changed()
        if self._capture is capture_live and capture_block_reason() == "WASAPI_CHILD_CLEANUP_FAILED":
            self.start.setEnabled(False)
        if self._cleanup_error:
            self.start.setEnabled(False)
            self.status.setText(self._cleanup_error)
        elif self._last_error:
            self.status.setText(self._last_error)
        elif self._stop.is_set():
            self.status.setText("Gestoppt / Остановлено.")
        else:
            self.status.setText(
                "Sitzung beendet. Untertitel verbleiben bis zum Schließen im RAM."
            )

    @Slot(int, str)
    def _translation_completed(self, generation: int, error: str):
        if generation != self._worker_generation or not self._busy or not self._translation_alive:
            return
        self._translation_alive = False
        if error:
            self._cleanup_error = error
        self._finish_when_all_workers_exited()

    @Slot(int, str)
    def _completed(self, generation: int, error: str):
        if generation != self._worker_generation or not self._busy or self._source_done:
            return
        self._source_done = True
        self._last_error = error
        if error in (PROVIDER_CLEANUP_FAILED, "REALTIME_CLEANUP_FAILED", "WASAPI_CHILD_CLEANUP_FAILED"):
            self._cleanup_error = error
        self._finish_when_all_workers_exited()

    def _dismiss_private(self):
        self._refresh_generation += 1
        # Keep the enumeration interlock until the discarded worker exits.
        self._invalidate_consent()
        self._stop_session()
        self._overlay.close()   # clears private caption snapshots
