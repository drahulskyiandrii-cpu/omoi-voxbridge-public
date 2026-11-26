"""User-mediated analysis dialog. Manual paste now; Windows selection hotkey later.

The dialog uses no model unless the user presses Analyze. API mode requires a
one-time checkbox or provider-specific saved consent. The worker never blocks the Qt GUI.
"""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, Signal, Slot, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QVBoxLayout,
)

from .analysis import AnalysisRequest, AnalysisError, supported_operations
from .analysis_providers import (
    AnthropicAPI, ProviderFailure, execute_analysis,
)
from .cloud_providers import OpenAIResponses, close_provider, PROVIDER_CLEANUP_FAILED
from .cloud_permissions import approved
from .analysis_stream import AnalysisPreview
from .private_dialog import PrivateDialog
from .local_llm import LocalGemma, model_selection
from .config import save_analysis_selection


class AnalysisSignals(QObject):
    complete = Signal(int, str, str)


class AnalysisDialog(PrivateDialog):
    def __init__(self, parent=None, *, permission_checker=None, on_speak=None,
                 local_runtime=None, local_models=(), preferences=None, can_start_local=None):
        super().__init__(parent)
        self._permission_checker = permission_checker
        self._on_speak = on_speak
        self._can_start_local = can_start_local or (lambda: True)
        self._local_runtime = local_runtime
        self._local_models = tuple(local_models)
        self._preferences = preferences
        self._answer_language = None
        self._request_language = None
        self.setWindowTitle("OMOI VoxBridge — KI-Analyse / ИИ-анализ")
        self.resize(700, 610)
        self._generation = 0
        self._busy = False
        self._provider_cleanup_failed = False
        self._worker_generation = 0
        self._cancel_event = threading.Event()
        self.signals = AnalysisSignals(self)
        self.signals.complete.connect(self._on_complete)
        self._preview = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(50)
        self._preview_timer.timeout.connect(self._drain_preview)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Markierten Text manuell einfügen / Вставьте выделенный текст вручную.\n"
            "Optional: experimenteller Auswahl-Hotkey im Tray (nur markierter Text)."
        ))
        self.text = QTextEdit()
        self.text.setPlaceholderText("Text / Aufgabe / Test mit Antworten ...")
        layout.addWidget(self.text)
        form = QFormLayout()
        self.operation = QComboBox()
        for key, label in supported_operations().items():
            self.operation.addItem(label, key)
        form.addRow("Aktion:", self.operation)
        self.question = QLineEdit()
        self.question.setPlaceholderText("Необязательно: свой вопрос к выделенному")
        form.addRow("Frage:", self.question)
        self.provider = QComboBox()
        self.provider.addItem("Gemma (local / локально)", "local")
        self.provider.model().item(0).setEnabled(self._local_runtime is not None and bool(self._local_models))
        self.provider.addItem("OpenAI API", "openai")
        self.provider.addItem("Anthropic API", "anthropic")
        initial = getattr(self._preferences, "analysis_provider", "openai")
        self.provider.setCurrentIndex(max(0, self.provider.findData(initial)))
        form.addRow("LLM:", self.provider)
        self.local_model = QComboBox()
        if self._local_models:
            self.local_model.addItem("Auto (bereits geladen / уже загруженная)", "")
        for model in self._local_models:
            self.local_model.addItem(model.label, model.key)
        self.local_model.setCurrentIndex(self.local_model.findData(
            getattr(self._preferences, "local_analysis_model", "")))
        form.addRow("Gemma:", self.local_model)
        if self._preferences is not None:
            self.provider.activated.connect(self._save_provider)
            self.local_model.activated.connect(self._save_provider)
        self.model = QLineEdit(os.environ.get("VOXBRIDGE_OPENAI_MODEL", "gpt-6-luna"))
        self.model.setMaxLength(128)
        form.addRow("OpenAI Model ID:", self.model)
        self.effort = QComboBox()
        self.effort.addItem("Luna Fast — low", "low")
        self.effort.addItem("Luna Deep — high", "high")
        form.addRow("Reasoning:", self.effort)
        self.provider.currentIndexChanged.connect(self._update_model_controls)
        self.language = QComboBox()
        for label, code in (("Русский", "ru"), ("Deutsch", "de"), ("English", "en")):
            self.language.addItem(label, code)
        form.addRow("Antwortsprache:", self.language)
        layout.addLayout(form)
        self.consent = QCheckBox(
            "Ich stimme der Übermittlung dieses Texts an den gewählten Cloud-Dienst zu.\n"
            "Я разрешаю отправить этот текст выбранному облачному API (возможна оплата)."
        )
        layout.addWidget(self.consent)
        layout.addWidget(QLabel("Oder dauerhafte Freigabe im Tray / Или постоянное разрешение в трее."))
        self.provider.currentIndexChanged.connect(self._update_consent)
        self._update_consent()
        self._update_model_controls()
        for signal in (self.text.textChanged, self.question.textChanged,
                       self.model.textChanged, self.operation.currentIndexChanged,
                       self.effort.currentIndexChanged, self.language.currentIndexChanged):
            signal.connect(self._invalidate_consent)
        controls = QHBoxLayout()
        self.run_button = QPushButton("Analysieren / Анализировать")
        self.run_button.clicked.connect(self._on_analyze)
        controls.addWidget(self.run_button)
        self.cancel_button = QPushButton("Abbrechen / Отмена")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_analysis)
        controls.addWidget(self.cancel_button)
        self.copy_button = QPushButton("Antwort kopieren / Копировать ответ")
        self.copy_button.clicked.connect(self._copy_answer)
        self.copy_button.setEnabled(False)
        controls.addWidget(self.copy_button)
        layout.addLayout(controls)
        self.message = QLabel("Bereit. Keine Hintergrundübertragung.")
        layout.addWidget(self.message)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Die Antwort erscheint hier.")
        layout.addWidget(self.output)
        speak_row = QHBoxLayout()
        self.speak_source = QPushButton("Original vorlesen / Озвучить оригинал")
        self.speak_answer = QPushButton("Antwort vorlesen / Озвучить ответ")
        self.speak_source.clicked.connect(self._speak_source)
        self.speak_answer.clicked.connect(self._speak_answer)
        self.speak_source.setEnabled(False)
        self.speak_answer.setEnabled(False)
        self.text.textChanged.connect(self._update_speech_buttons)
        self.output.textChanged.connect(self._update_speech_buttons)
        speak_row.addWidget(self.speak_source)
        speak_row.addWidget(self.speak_answer)
        layout.addLayout(speak_row)

    def _update_speech_buttons(self):
        self.speak_source.setEnabled(self._on_speak is not None and bool(self.text.toPlainText().strip()))
        final_answer = not self._busy and bool(self.output.toPlainText().strip())
        self.speak_answer.setEnabled(self._on_speak is not None and final_answer)
        self.copy_button.setEnabled(final_answer)

    @Slot()
    def _speak_source(self):
        self._prepare_speech(self.text, None)

    @Slot()
    def _speak_answer(self):
        if self._busy:
            return
        self._prepare_speech(self.output, self._answer_language)

    def _prepare_speech(self, edit, language):
        if self._on_speak is None:
            return
        cursor = edit.textCursor()
        text = cursor.selectedText().replace("\u2029", "\n") if cursor.hasSelection() else edit.toPlainText()
        if not text.strip():
            return
        try:
            self._on_speak(text, language)
        except ProviderFailure as exc:
            self.message.setText(str(exc)[:100])

    def _invalidate_consent(self, *args):
        self.consent.setChecked(False)

    def _update_consent(self):
        cloud = self.provider.currentData() != "local"
        self.consent.setVisible(cloud)
        self.consent.setChecked(False)

    def _save_provider(self, *_):
        try:
            save_analysis_selection("analysis", self.provider.currentData(), self.local_model.currentData() or "")
        except (ValueError, OSError, TypeError):
            self.message.setText("SETTINGS_SAVE_FAILED")

    def _update_model_controls(self):
        is_openai = self.provider.currentData() == "openai"
        local = self.provider.currentData() == "local"
        self.local_model.setVisible(local)
        self.model.setEnabled(is_openai)
        self.effort.setEnabled(is_openai)

    def _make_provider(self, key: str):
        if key == "local":
            model = model_selection(self._local_models, self.local_model.currentData() or "")
            if self._local_runtime is None or self.local_model.currentIndex() < 0 or model is None:
                raise ProviderFailure("LOCAL_MODEL_NOT_CONFIGURED")
            return LocalGemma(self._local_runtime, model)
        if key == "openai":
            return OpenAIResponses(model=self.model.text().strip(), effort=self.effort.currentData())
        if key == "anthropic":
            return AnthropicAPI(os.environ.get("VOXBRIDGE_ANTHROPIC_MODEL", ""))
        raise ProviderFailure("UNKNOWN_PROVIDER")

    @Slot()
    def _on_analyze(self):
        if self._provider_cleanup_failed:
            self.message.setText(PROVIDER_CLEANUP_FAILED)
            return
        if self._busy or not self.run_button.isEnabled():
            return
        provider_name = self.provider.currentData()
        is_cloud = provider_name != "local"
        if not is_cloud and not self._can_start_local():
            self.message.setText("LOCAL_LLM_BUSY")
            return
        allow_cloud = bool(is_cloud and (self.consent.isChecked() or
                           approved(self._permission_checker, "analysis_" + provider_name)))
        if is_cloud and not allow_cloud:
            self.message.setText("Vor der Cloud-Übertragung bitte ausdrücklich zustimmen.")
            return
        try:
            request = AnalysisRequest(
                text=self.text.toPlainText(),
                operation=self.operation.currentData(),
                question=self.question.text(),
                response_language=self.language.currentData(),
                target_language=self.language.currentData(),
            ).validate()
            provider = self._make_provider(provider_name)
        except (AnalysisError, ProviderFailure, ValueError) as error:
            self.message.setText(str(error)[:100])
            return
        self.consent.setChecked(False)  # no blanket approval for later requests
        self._generation += 1
        generation = self._generation
        self._worker_generation = generation
        self._request_language = request.response_language
        self._answer_language = None
        self._busy = True
        self._cancel_event = threading.Event()
        cancelled = self._cancel_event
        self.run_button.setEnabled(False)
        self.provider.setEnabled(False)
        self.local_model.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.message.setText(("Lokale Gemma / Локальная Gemma" if not is_cloud else "API") + ": Verarbeitung läuft ...")
        self.output.clear()
        self._update_speech_buttons()
        preview = AnalysisPreview(cancelled) if isinstance(provider, (OpenAIResponses, LocalGemma)) else None
        self._preview = preview
        if preview is not None:
            self._preview_timer.start()

        def worker():
            try:
                if cancelled.is_set():
                    raise ProviderFailure("ANALYSIS_CANCELLED")
                result = execute_analysis(request, provider, cloud_approved=allow_cloud,
                                          on_delta=preview.push if preview is not None else None,
                                          cancel_event=cancelled)
                answer, error = result.answer, ""
            except (ProviderFailure, AnalysisError) as exc:
                answer, error = "", str(exc)
            except Exception:
                answer, error = "", "ANALYSIS_ERROR"
            finally:
                error = close_provider(provider) or error
            try:
                self.signals.complete.emit(generation, answer, error)
            except RuntimeError:
                pass
        try:
            threading.Thread(target=worker, name="voxbridge-analysis", daemon=True).start()
        except RuntimeError:
            error = close_provider(provider) or "ANALYSIS_WORKER_START_FAILED"
            self._on_complete(generation, "", error)

    @Slot()
    def _drain_preview(self):
        if not self._busy or self._preview is None or self._worker_generation != self._generation:
            return
        text = self._preview.drain()
        if text:
            cursor = self.output.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text)
            self.output.setTextCursor(cursor)
            self.message.setText("Antwort wird empfangen / Ответ поступает…")

    def _discard_preview(self):
        self._preview_timer.stop()
        if self._preview is not None:
            self._preview.close()
            self._preview = None

    @Slot(int, str, str)
    def _on_complete(self, generation: int, answer: str, error: str):
        if generation != self._worker_generation or not self._busy:
            return
        self._discard_preview()
        self._busy = False
        self.provider.setEnabled(True)
        self.local_model.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if error == PROVIDER_CLEANUP_FAILED:
            self._provider_cleanup_failed = True
            self.output.clear()
            self._answer_language = None
            self.run_button.setEnabled(False)
            self.message.setText(error)
            self._update_speech_buttons()
            return
        self.run_button.setEnabled(True)
        if generation != self._generation:
            self.message.setText("Bereit / Готово к новому запросу.")
            return
        self.message.setText(error or "Fertig / Готово")
        if not error:
            self._answer_language = self._request_language
            self.output.setPlainText(answer)
        else:
            self.output.clear()  # incomplete previews are never presented as answers
        self._update_speech_buttons()

    def _cancel_analysis(self):
        if not self._busy:
            return
        self._generation += 1
        self._cancel_event.set()
        self._discard_preview()
        self._answer_language = None
        self.output.clear()
        self.consent.setChecked(False)
        self.cancel_button.setEnabled(False)
        self.message.setText("Abgebrochen; laufende Anfrage kann noch berechnet werden.")

    def _copy_answer(self):
        if not self._busy and self.output.toPlainText().strip():
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(self.output.toPlainText())

    def _dismiss_private(self):
        self._generation += 1  # ignore results returned after closing
        self._cancel_event.set()
        self._discard_preview()
        self.cancel_button.setEnabled(False)
        self._answer_language = None
        self.text.clear()
        self.question.clear()
        self.output.clear()
        self.consent.setChecked(False)
        self.run_button.setEnabled(not self._busy and not self._provider_cleanup_failed)
        if self._busy:
            self.message.setText("Warte auf Ende der vorherigen API-Anfrage.")
