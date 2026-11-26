"""Explicit read-aloud dialog: providers -> bounded isolated audio output.

No backend starts and no API calls occur during construction. Only the user's
Play click after one-time or saved consent starts a request. Stop invalidates all queued
generations; remote credits may still be charged after a cancellation.
"""
from __future__ import annotations

import queue
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from .analysis_providers import ProviderFailure
from .cloud_permissions import approved
from .cloud_providers import CartesiaTTSProvider, close_provider, PROVIDER_CLEANUP_FAILED
from .config import load_settings, save_settings
from dataclasses import replace
from .tts_stream import (
    stream_to_callback, stream_bytes_to_callback,
)
from .voice_ui import VoiceDialog
from .tts_language import plan_speech
from .private_dialog import PrivateDialog
from .qwen_local_runtime import QwenRuntime, MODELS as LOCAL_MODELS
from .speech_core_tts import LocalTTSFailure
from .audio_output import BLOCK_BYTES
from .output_isolation import OutputResult, play_output, output_block_reason


def desktop_tts_mode(mode):
    # The old Base selection becomes the preferred CF50 variant; it still
    # needs explicit model activation. Keep a user's working cloud transport.
    return "qwen_cf050" if mode == "qwen_base" else mode


class TTSSignals(QObject):
    complete = Signal(int, str)
    model_complete = Signal(str, str)
    model_closed = Signal(str)
    warm_ready = Signal(int)
    output_complete = Signal(int, object)
    output_recovery = Signal(int, int)


class CloudTTSDialog(PrivateDialog):
    """GUI coordinates provider and isolated output; never calls a native sink."""

    def __init__(self, parent=None, provider_factory=CartesiaTTSProvider, *, permission_checker=None, can_start=None, transport_mode="sse", shortcut_notify=None, local_runtime=None, output_runner=play_output, voice_opener=None, keep_warm=False, tts_enabled=True, fallback_language="de", state_changed=None):
        super().__init__(parent)
        self._permission_checker = permission_checker
        self._tts_enabled = tts_enabled
        self._keep_warm = keep_warm
        self._prewarm_requested = False
        self._fallback_language = fallback_language
        self._state_changed = state_changed or (lambda: None)
        self._shortcut = False
        self._shortcut_notify = shortcut_notify or (lambda _:None)
        self._pending_error = ''
        self._output_runner = output_runner
        self._output_generation = None
        self._output_finished = threading.Event()
        self._output_cleanup_failed = False
        self._provider_cleanup_failed = False
        self._local_playback = False
        self._stopping = False
        self._can_start = can_start if can_start is not None else (lambda: True)
        self.setWindowTitle("OMOI VoxBridge — Vorlesen / Озвучивание")
        self.resize(560, 400)
        self._provider_factory = provider_factory
        self._local_runtime = local_runtime if local_runtime is not None else QwenRuntime()
        self._local_loading = False
        self._queued_playback = None
        self._local_used = False
        self._local_closing = False
        self._local_cleanup_failed = False
        self._warming_generation = None
        self._local_cancel = None
        transport_mode = desktop_tts_mode(transport_mode)
        self._saved_transport = transport_mode
        self._voice_dialog = None
        self._voice_opener = voice_opener
        self._generation = 0
        self._busy = False
        self._remote_done = False
        self._cancel = None
        self._worker_generation = None
        self._incoming = queue.Queue(maxsize=8)
        self._progress_at = 0.0

        self.signals = TTSSignals(self)
        self.signals.complete.connect(self._completed)
        self.signals.model_complete.connect(self._model_completed)
        self.signals.model_closed.connect(self._model_closed)
        self.signals.warm_ready.connect(self._playback_warm_ready)
        self.signals.output_complete.connect(self._output_completed)
        self.signals.output_recovery.connect(self._output_reconnecting)

        layout = QVBoxLayout(self)
        self.backend_hint = QLabel()
        self.backend_hint.setWordWrap(True)
        layout.addWidget(self.backend_hint)
        self.text = QPlainTextEdit(self)
        self.text.setPlaceholderText("Text für Sprachausgabe / Текст для озвучивания...")
        layout.addWidget(self.text)
        language_row = QHBoxLayout()
        language_row.addWidget(QLabel("Sprache:"))
        self.language = QComboBox(self)
        self.language.addItem("Auto (DE / RU / EN)", "auto")
        self.language.addItem("Русский (eigene Stimmen: ru)", "ru")
        self.language.addItem("Deutsch (experimentell)", "de")
        self.language.addItem("English (experimental)", "en")
        language_row.addWidget(self.language)
        language_row.addStretch(1)
        self.voices = QPushButton("Eigene Stimmen wählen / Выбрать голос")
        self.voices.clicked.connect(self._choose_voice)
        language_row.addWidget(self.voices)
        layout.addLayout(language_row)
        self.transport = QComboBox(self)
        # One cloud provider, one preferred local voice model. A previously
        # saved PCM transport remains internal rather than exposed as a model.
        self.transport.addItem("Cartesia — Cloud / Облако", "bytes" if transport_mode == "bytes" else "sse")
        self.transport.addItem("Qwen3-TTS 1.7B + CF-LoRA 50% — Lokal / Локально", "qwen_cf050")
        self.transport.setCurrentIndex(max(0, self.transport.findData(transport_mode)))
        self.transport.activated.connect(self._save_transport)
        layout.addWidget(QLabel("Modell / Модель озвучивания"))
        layout.addWidget(self.transport)
        self.model_button = QPushButton("Modell vorladen / Предзагрузить модель")
        self.model_button.clicked.connect(self._toggle_model)
        layout.addWidget(self.model_button)
        self.transport.currentIndexChanged.connect(lambda *_: self.consent.setChecked(False))
        self.voice_quality_hint = QLabel("DE/EN: Qualität der eigenen Stimmen noch nicht geprüft / Качество голоса DE/EN ещё не проверено.")
        self.voice_quality_hint.setWordWrap(True)
        layout.addWidget(self.voice_quality_hint)
        self.consent = QCheckBox(
            "Ich erlaube diesen kostenpflichtigen Cloud-TTS-Aufruf / "
            "Разрешаю платную отправку этого текста в Cartesia.",
            self,
        )
        layout.addWidget(self.consent)
        self.cloud_hint = QLabel("Cloud-Freigaben in Einstellungen / Разрешения в настройках")
        layout.addWidget(self.cloud_hint)
        self.text.textChanged.connect(lambda: self.consent.setChecked(False))
        self.language.currentIndexChanged.connect(lambda *_: self.consent.setChecked(False))
        controls = QHBoxLayout()
        self.play = QPushButton("Play / Воспроизвести")
        self.stop = QPushButton("Stop / Остановить")
        self.stop.setEnabled(False)
        self.play.clicked.connect(self._play)
        self.stop.clicked.connect(self._stop)
        controls.addWidget(self.play)
        controls.addWidget(self.stop)
        layout.addLayout(controls)
        self.status = QLabel("Bereit. Kein Audio und kein Cloud-Aufruf gestartet.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.cost_hint = QLabel("Ein gesendeter Cloud-Auftrag kann trotz Stop berechnet werden.")
        self.cost_hint.setWordWrap(True)
        layout.addWidget(self.cost_hint)

        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._pump)
        self.transport.currentIndexChanged.connect(self._backend_changed)
        self._sync_model_controls()

    @property
    def playback_busy(self):
        return (self._busy or self._output_generation is not None
                or self._remote_done or self._output_cleanup_failed)

    @property
    def busy(self):
        return (self.playback_busy or self._local_loading or self._local_closing
                or self._local_cleanup_failed)

    @property
    def accepts_read_request(self):
        return not self.busy or (self._local_loading and self._queued_playback is None
                                 and not self.playback_busy and not self._local_closing
                                 and not self._local_cleanup_failed)

    @property
    def model_status(self):
        """Report cleanup separately from the desired enabled setting."""
        if self._local_cleanup_failed:
            return "Qwen cleanup failed"
        if self._local_closing or (not self._tts_enabled and self._local_loading):
            return "Qwen closing"
        if not self._tts_enabled:
            return "OFF (cleanup pending)" if self.playback_busy else "OFF"
        if self._local_loading:
            return "Qwen loading"
        if self._local_runtime.ready("qwen_cf050"):
            return "Qwen ready"
        return "Ready on request"

    @property
    def shutdown_pending(self):
        return (self._busy or self._output_generation is not None
                or self._local_loading or self._local_closing
                or (self._voice_dialog is not None
                    and self._voice_dialog._worker_generation is not None))

    def _sync_model_controls(self):
        mode = self.transport.currentData()
        local = mode in LOCAL_MODELS
        active = (local and not self._local_closing and not self._local_cleanup_failed
                  and self._local_runtime.ready(mode))
        self.model_button.setVisible(local and not self._keep_warm)
        self.model_button.setEnabled(self._tts_enabled and local and not self.playback_busy and not self._local_closing
                                     and not self._local_cleanup_failed)
        self.model_button.setText("Laden abbrechen / Отменить загрузку" if self._local_loading else
                                  "Modell deaktivieren / Выключить модель" if active else
                                  "Modell vorladen / Предзагрузить модель")
        self.backend_hint.setText(
            "Lokal / Локально. Модель загрузится при первом озвучивании. Текст не отправляется в облако."
            if local else "Cartesia Cloud / Облако. Только введённый текст отправляется при запуске.")
        self.consent.setVisible(not local)
        self.voice_quality_hint.setVisible(not local)
        self.cloud_hint.setVisible(not local)
        self.cost_hint.setVisible(not local)
        self.voices.setVisible(not local)
        self.play.setEnabled(self._tts_enabled and self.accepts_read_request and not self._provider_cleanup_failed)
        self.transport.setEnabled(not self.busy)
        self.voices.setEnabled(not self.busy)
        self.stop.setEnabled((self.playback_busy or self._queued_playback is not None) and not self._output_cleanup_failed
                             and (self._queued_playback is not None or self._cancel is None or not self._cancel.is_set()))

    @Slot(int)
    def _backend_changed(self, index):
        mode = self.transport.itemData(index)
        if mode != self._saved_transport and self._saved_transport in LOCAL_MODELS:
            self._cancel_model()
        self._sync_model_controls()

    def _cancel_model(self):
        self._queued_playback = None
        if self._local_cancel is not None:
            self._local_cancel.set()
        if self._local_closing:
            return
        self._local_closing = True
        self._sync_model_controls()
        runtime, signals = self._local_runtime, self.signals

        def worker():
            error = ""
            try:
                runtime.close()
            except Exception:
                error = "QWEN_CLEANUP_FAILED"
            try:
                signals.model_closed.emit(error)
            except RuntimeError:
                pass

        self.status.setText("QWEN_CLOSING")
        try:
            threading.Thread(target=worker, name="voxbridge-qwen-cleanup", daemon=False).start()
        except RuntimeError:
            self._model_closed("QWEN_CLEANUP_FAILED")

    @Slot(str)
    def _model_closed(self, error):
        if not self._local_closing:
            return
        self._local_closing = False
        self._local_cleanup_failed = bool(error)
        if error:
            self._report_failure(error)
        elif not self._busy and not self._local_loading:
            self.status.setText("QWEN_DEACTIVATED")
        self._after_stopped_worker()
        self._state_changed()

    def shutdown_local(self):
        self._prewarm_requested = False
        if self._cancel is not None:
            self._cancel.set()
        if self._local_used or self._local_cleanup_failed:
            self._cancel_model()

    def stop_all(self):
        self._prewarm_requested = False
        self._stop()
        self.shutdown_local()

    def set_tts_enabled(self, enabled, *, keep_warm=True):
        self._tts_enabled = bool(enabled)
        self._keep_warm = bool(keep_warm)
        if not enabled:
            self.stop_all()
        elif self._keep_warm:
            if self.busy:
                self._prewarm_requested = True
            else:
                self.ensure_prewarm()
        self._sync_model_controls()

    def ensure_prewarm(self):
        mode = self.transport.currentData()
        if (not self._tts_enabled or mode not in LOCAL_MODELS or self.busy
                or self._local_runtime.ready(mode)):
            return False
        if mode != self._saved_transport or not self._can_start():
            self.status.setText("QWEN_SAVE_SELECTION_OR_STOP_CAPTURE")
            return False
        self._begin_prewarm(mode)
        return True

    @Slot()
    def _toggle_model(self):
        mode = self.transport.currentData()
        if not self._tts_enabled or mode not in LOCAL_MODELS or self.playback_busy or self._local_closing or self._local_cleanup_failed:
            return
        if self._local_loading or self._local_runtime.ready(mode):
            self._cancel_model()
        else:
            self.ensure_prewarm()

    def _begin_prewarm(self, mode):
        self._local_used = True
        self._local_cancel = cancel = threading.Event()
        self._local_loading = True
        self.transport.setEnabled(False)
        self.status.setText("QWEN_LOADING / Modell wird lokal geladen")
        self._sync_model_controls()
        self._state_changed()

        def worker():
            error = ""
            try:
                self._local_runtime.activate(mode, cancel, approved=True)
            except LocalTTSFailure as exc:
                error = str(exc)
            except Exception:
                error = "QWEN_ACTIVATION_FAILED"
            try:
                self.signals.model_complete.emit(mode, error)
            except RuntimeError:
                self._local_runtime.close()
        try:
            threading.Thread(target=worker, name="voxbridge-qwen-activation", daemon=True).start()
        except RuntimeError:
            self._model_completed(mode, "QWEN_ACTIVATION_THREAD_FAILED")

    @Slot(str, str)
    def _model_completed(self, mode, error):
        self._local_loading = False
        if self._local_cleanup_failed or error == "QWEN_CLEANUP_FAILED":
            self._local_cleanup_failed = True
            error = "QWEN_CLEANUP_FAILED"
        elif self._local_cancel is not None and self._local_cancel.is_set():
            error = "QWEN_CLOSING" if self._local_closing else "QWEN_DEACTIVATED"
        self._local_cancel = None
        pending, self._queued_playback = self._queued_playback, None
        self.status.setText(error or "QWEN_READY / Модель включена")
        self._sync_model_controls()
        if self._prewarm_requested and not self.busy:
            self._prewarm_requested = False
            if self._tts_enabled and self._keep_warm:
                self.ensure_prewarm()
        self._state_changed()
        if (error and error not in ("QWEN_CLOSING", "QWEN_DEACTIVATED")
                and self._tts_enabled and self._keep_warm and pending is None):
            self._shortcut_notify(error)
        if pending is not None:
            text, language, transport, shortcut, fallback = pending
            self._shortcut = shortcut
            if error:
                self._report_failure(error)
            else:
                self._start_playback(text, language, transport, shortcut=shortcut, fallback=fallback)

    @Slot(int)
    def _save_transport(self, index):
        if self.playback_busy or self._local_loading or self._local_cleanup_failed:
            return
        try:
            mode = self.transport.itemData(index)
            save_settings(replace(load_settings(), tts_transport=mode).validate())
        except (OSError, ValueError, TypeError):
            self.transport.setCurrentIndex(max(0, self.transport.findData(self._saved_transport)))
            self.status.setText("TTS_TRANSPORT_SAVE_FAILED")
            self._sync_model_controls()
            return
        self._saved_transport = mode
        if self._keep_warm and mode in LOCAL_MODELS:
            self.ensure_prewarm()
        self._state_changed()
        if not self._local_closing and not self._local_loading:
            self.status.setText("TTS selected: " + mode + ("; Start beim Vorlesen / Запуск при озвучивании" if mode in LOCAL_MODELS else ""))
        self._sync_model_controls()

    def prepare_text(self, text: str, language: str | None = None):
        """Local preview only. Never start Play or overwrite a different draft."""
        if not self._tts_enabled:
            raise ProviderFailure("TTS_DISABLED")
        if not self.accepts_read_request:
            raise ProviderFailure("TTS_BUSY")
        plan_speech(text, "auto", fallback=language or self._fallback_language)
        existing = self.text.toPlainText()
        if existing.strip() and existing != text:
            raise ProviderFailure("TTS_DRAFT_EXISTS")
        self._shortcut = False
        self.language.setCurrentIndex(self.language.findData("auto"))
        self._fallback_language = language or self._fallback_language
        self.text.setPlainText(text)
        self.consent.setChecked(False)
        self.status.setText("Text übernommen; Play manuell starten / Текст готов, нажмите Play.")

    def _choose_voice(self):
        if self.busy:
            return
        self.consent.setChecked(False)
        if self._voice_opener is not None:
            self._voice_opener()
            return
        if self._voice_dialog is None:
            self._voice_dialog = VoiceDialog(self)
        dialog = self._voice_dialog
        if not dialog.isVisible():
            # Keep the same owner across dismissal; a cancelled metadata
            # request may still be cleaning up. Reopening cannot overlap it.
            dialog.exec()

    @Slot()
    def _play(self):
        self._start_playback(self.text.toPlainText(), self.language.currentData(),
                             self.transport.currentData(), shortcut=False)

    def start_shortcut(self,text,language,transport):
        """Explicit hotkey: separate transient text, no settings/draft display."""
        return self._start_playback(text, "auto", transport, shortcut=True, fallback=language)

    def _report_failure(self,code, *, shortcut=None):
        self._stopping = False
        if self._output_cleanup_failed:
            code = "AUDIO_OUTPUT_CHILD_CLEANUP_FAILED"
            if self.status.text() == code:
                return
        self.status.setText(code[:100])
        notify_shortcut = self._shortcut if shortcut is None else shortcut
        if notify_shortcut:
            self._shortcut_notify(code[:100])
        elif not self.isVisible():
            self.show()

    def _start_playback(self,text,language,transport,*,shortcut,fallback=None):
        if self.playback_busy:
            return False
        if not self._tts_enabled:
            self._report_failure("TTS_DISABLED", shortcut=shortcut)
            return False
        fallback = fallback or self._fallback_language
        if self._provider_cleanup_failed:
            self._report_failure(PROVIDER_CLEANUP_FAILED, shortcut=shortcut)
            return False
        if self._local_closing or self._local_cleanup_failed:
            self._report_failure("QWEN_CLEANUP_FAILED" if self._local_cleanup_failed else "QWEN_CLOSING", shortcut=shortcut)
            return False
        if self._local_loading:
            if (desktop_tts_mode(transport) not in LOCAL_MODELS or
                    desktop_tts_mode(transport) != self._saved_transport or
                    self._queued_playback is not None):
                self._report_failure("QWEN_LOADING", shortcut=shortcut)
                return False
            try:
                plan_speech(text, language, fallback=fallback, local=True)
            except ProviderFailure as exc:
                self._report_failure(str(exc), shortcut=shortcut)
                return False
            self._shortcut = shortcut
            self._queued_playback = (text, language, transport, shortcut, fallback)
            self.status.setText("QWEN_LOADING: Vorlesen vorgemerkt / Озвучивание начнётся после загрузки")
            if shortcut:
                self._shortcut_notify("QWEN_LOADING: queued")
            self._sync_model_controls()
            return True
        self._shortcut = shortcut
        blocked = output_block_reason()
        if blocked:
            self._report_failure(blocked)
            return False
        try:
            allowed = self._can_start() is True
        except Exception:
            allowed = False
        if not allowed:
            if shortcut:
                self._report_failure("TTS_BUSY")
            else:
                self.status.setText("Andere VoxBridge-Aufnahme aktiv; zuerst beenden.")
            return False
        transport = desktop_tts_mode(transport)
        local = transport in LOCAL_MODELS
        warm_needed = local and not self._local_runtime.ready(transport)
        if not local and not (approved(self._permission_checker,"tts_cartesia") or (not shortcut and self.consent.isChecked())):
            self.status.setText("Bitte Cloud-Freigabe f?r diesen Text best?tigen.")
            if shortcut:
                self._shortcut_notify("TTS_PERMISSION_REQUIRED")
            return False
        try:
            parts = plan_speech(text, language, fallback=fallback, local=local)
            voice_id = None
            if not local:
                voice_id = load_settings().cartesia_voice_id
                if not voice_id:
                    raise ProviderFailure("CARTESIA_VOICE_NOT_SELECTED")
                if transport not in ("sse","bytes"):
                    raise ProviderFailure("CARTESIA_INVALID_TRANSPORT")
        except (ProviderFailure, OSError, ValueError) as exc:
            self._report_failure(str(exc) if isinstance(exc,ProviderFailure) else "CARTESIA_SETTINGS_INVALID")
            return False
        if warm_needed and self._keep_warm:
            if not self.ensure_prewarm():
                self._report_failure("QWEN_PREWARM_UNAVAILABLE")
                return False
            if self._local_loading:
                self._queued_playback = (text, language, transport, shortcut, fallback)
                self.status.setText("QWEN_LOADING: queued")
                self._sync_model_controls()
                return True
            # A synchronous completion is possible in tests; re-evaluate.
            if not self._local_runtime.ready(transport):
                return False
            warm_needed = False
        self._pending_error = ""
        self._local_playback = local
        self._local_used = self._local_used or local
        self._stopping = False
        self._output_finished = threading.Event()
        self._generation += 1
        generation = self._generation
        self._cancel = threading.Event()
        self._worker_generation = generation
        self._warming_generation = generation if warm_needed else None
        self._incoming = queue.Queue(maxsize=8)
        incoming, cancelled = self._incoming, self._cancel
        self._remote_done = False
        self._busy = True
        self.consent.setChecked(False)  # no permission shared by multiple requests
        self.play.setEnabled(False)
        self.stop.setEnabled(True)
        self.voices.setEnabled(False)
        self.status.setText("QWEN_LOADING / Modell wird geladen / Загрузка модели" if warm_needed else
                            ("QWEN_CONNECTING" if local else "CARTESIA_CONNECTING_" + transport.upper()) + " / Warte auf Audio ...")
        if warm_needed and shortcut:
            self._shortcut_notify("Qwen: загрузка модели / Modell wird geladen")
        self._sync_model_controls()

        self.transport.setEnabled(False)
        self._progress_at = time.monotonic()
        self._timer.start()  # drain bounded queue even before an audio sink exists

        def send_chunk(data: bytes):
            # A fast provider must wait for playback, not flood Qt's event queue.
            if not isinstance(data, bytes) or len(data) % 2:
                raise ProviderFailure("TTS_INVALID_PCM")
            for offset in range(0, len(data), BLOCK_BYTES):
                block = data[offset:offset + BLOCK_BYTES]
                while not cancelled.is_set():
                    try:
                        incoming.put(block, timeout=0.05)
                        if cancelled.is_set():
                            raise ProviderFailure("CARTESIA_CANCELLED")
                        break
                    except queue.Full:
                        continue
                else:
                    raise ProviderFailure("CARTESIA_CANCELLED")

        def worker():
            code = ""
            provider = None
            try:
                if cancelled.is_set():
                    raise ProviderFailure("CARTESIA_CANCELLED")
                if warm_needed:
                    self._local_runtime.activate(transport, cancelled, approved=True)
                    if cancelled.is_set():
                        raise LocalTTSFailure("QWEN_CANCELLED")
                    self.signals.warm_ready.emit(generation)
                if not local:
                    provider = self._provider_factory(voice_id=voice_id)
                streamer = stream_bytes_to_callback if transport == "bytes" else stream_to_callback
                for part, part_language in parts:
                    if cancelled.is_set():
                        raise ProviderFailure('TTS_CANCELLED')
                    if local:
                        self._local_runtime.stream(transport, part, part_language, send_chunk, cancelled)
                    else:
                        streamer(provider, part, part_language, send_chunk, cancelled)
            except (ProviderFailure, LocalTTSFailure) as exc:
                code = str(exc)
            except Exception:
                code = "QWEN_TTS_ERROR" if local else "CARTESIA_TTS_ERROR"
            finally:
                code = close_provider(provider) or code
            try:
                self.signals.complete.emit(generation, code)
            except RuntimeError:
                pass

        try:
            threading.Thread(
                target=worker, name="voxbridge-cartesia-tts", daemon=True
            ).start()
        except RuntimeError:
            self._completed(generation, "CARTESIA_WORKER_START_FAILED")
            return False
        return True

    @Slot(int)
    def _playback_warm_ready(self, generation):
        if generation != self._generation or generation != self._warming_generation:
            return
        self._warming_generation = None
        self._progress_at = time.monotonic()
        self.status.setText("Qwen ready / Modell bereit")

    def _start_output(self):
        generation = self._generation
        self._output_generation = generation
        incoming, finished, stop = self._incoming, self._output_finished, self._cancel
        runner = self._output_runner
        signals = self.signals

        def worker():
            try:
                result = runner(incoming, finished, stop,
                                lambda count: signals.output_recovery.emit(generation, count))
            except Exception:
                result = OutputResult("AUDIO_OUTPUT_CHILD_FAILED")
            try:
                signals.output_complete.emit(generation, result)
            except RuntimeError:
                pass

        self.status.setText("Audioausgabe / Audio output")
        self._sync_model_controls()
        try:
            threading.Thread(target=worker, name="voxbridge-output-supervisor", daemon=False).start()
        except RuntimeError:
            self._output_completed(generation, OutputResult("AUDIO_OUTPUT_WORKER_START_FAILED"))

    @Slot(int, int)
    def _output_reconnecting(self, generation, count):
        if generation == self._generation == self._output_generation:
            self.status.setText(f"AUDIO_RECONNECTING {count}/2")

    def _pump(self):
        # The timer never performs native audio I/O. It starts the isolated
        # output only on first PCM, or enforces the provider startup deadline.
        if self._cancel is None or self._cancel.is_set() or self._output_cleanup_failed:
            return
        if self._output_generation is not None:
            return
        if not self._incoming.empty():
            self._start_output()
            return
        if self._remote_done:
            self._finish_playback(self._pending_error or "TTS_STREAM_NO_AUDIO")
            return
        warming = self._warming_generation == self._worker_generation and self._warming_generation is not None
        if self._busy and time.monotonic() - self._progress_at > (215 if warming else 25):
            self._stop()
            code = ("QWEN_LOAD_TIMEOUT" if warming else
                    "QWEN_FIRST_AUDIO_TIMEOUT" if self._local_playback else "CARTESIA_FIRST_AUDIO_TIMEOUT")
            self._report_failure(code)

    def _discard_audio(self):
        while True:
            try:
                self._incoming.get_nowait()
            except queue.Empty:
                return

    def _finish_playback(self, error="", recoveries=0):
        self._timer.stop()
        self._remote_done = False
        self._pending_error = ""
        self._discard_audio()
        self._sync_model_controls()
        self._state_changed()
        if error:
            self._report_failure(error)
        else:
            self.status.setText("Fertig / Vorlesen abgeschlossen." +
                                (f" Audio reconnects: {recoveries}" if recoveries else ""))

    @Slot(int, object)
    def _output_completed(self, generation, result):
        if generation != self._output_generation:
            return
        self._output_generation = None
        if result.cleanup_failed:
            self._output_cleanup_failed = True
            self._stop()
            self._report_failure("AUDIO_OUTPUT_CHILD_CLEANUP_FAILED")
            return
        if generation != self._generation:
            # Old supervisor has finally reaped the child. Do not overwrite
            # the Stop/error message or unlock an outstanding provider.
            self._after_stopped_worker()
            return
        if result.error or not self._remote_done:
            code = result.error or "AUDIO_OUTPUT_PROTOCOL"
            self._stop()
            self._report_failure(code)
            return
        self._finish_playback(self._pending_error, result.recoveries)

    @Slot(int, str)
    def _completed(self, generation: int, error: str):
        if generation != self._worker_generation or not self._busy:
            return
        self._worker_generation = None
        self._warming_generation = None
        self._busy = False
        if error == PROVIDER_CLEANUP_FAILED:
            self._provider_cleanup_failed = True
            self._stop()
            return
        if error == "QWEN_CLEANUP_FAILED":
            self._local_cleanup_failed = True
            self._report_failure(error)
        if generation != self._generation:
            self._discard_audio()
            self._after_stopped_worker()
            return
        self._pending_error = error
        self._remote_done = True
        # The producer has finished all queue writes. Let received PCM drain
        # even on a provider error; preserve that error for final reporting.
        self._output_finished.set()
        self._pump()

    def _after_stopped_worker(self):
        self._sync_model_controls()
        if self._prewarm_requested and not self.busy:
            self._prewarm_requested = False
            if self._tts_enabled and self._keep_warm:
                self.ensure_prewarm()
        if self._stopping and not self.busy:
            self._stopping = False
            self.status.setText("Gestoppt." if self._tts_enabled else "TTS OFF")
        self._state_changed()

    @Slot()
    def _stop(self):
        self._queued_playback = None
        self._generation += 1
        self._stopping = True
        self.consent.setChecked(False)
        if self._cancel is not None:
            self._cancel.set()
        self._timer.stop()
        self._discard_audio()
        self._remote_done = False
        self._pending_error = ""
        if self._warming_generation is not None:
            self._warming_generation = None
            self._cancel_model()
        if self._local_loading and not self._keep_warm:
            self._cancel_model()
        self._sync_model_controls()
        if self._output_cleanup_failed:
            self._report_failure("AUDIO_OUTPUT_CHILD_CLEANUP_FAILED")
        elif self._local_cleanup_failed:
            self._report_failure("QWEN_CLEANUP_FAILED")
        elif self._provider_cleanup_failed:
            self._report_failure(PROVIDER_CLEANUP_FAILED)
        else:
            self._stopping = self.busy
            self.status.setText("Stop angefordert / cleanup pending." if self._stopping else "Gestoppt.")

    def _dismiss_private(self):
        if self._voice_dialog is not None:
            self._voice_dialog.reject()
        if not self._shortcut:
            self._stop()
        elif not self._keep_warm and self._local_loading:
            self._stop()
        self.text.clear()
        self.consent.setChecked(False)
