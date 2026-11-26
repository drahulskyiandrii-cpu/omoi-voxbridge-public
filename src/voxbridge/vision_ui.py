"""User-mediated screenshot region preview and opt-in OpenAI Vision analysis.

Qt captures only the user-selected rectangle, after the selection overlay
disappears. The image is kept only in memory and never sent until an additional
one-time or saved cloud consent + Analyse click. No background screen monitoring.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QBuffer, QIODevice, QRect, QSize, Qt, QTimer, Signal, Slot, QObject
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from .analysis_providers import ProviderFailure
from .cloud_permissions import approved
from .cloud_providers import close_provider, PROVIDER_CLEANUP_FAILED
from .private_dialog import PrivateDialog
from .local_llm import LocalGemmaVision, LocalGemmaOCR, model_selection
from .config import save_analysis_selection
from .vision import OpenAIVision, png_dimensions, MAX_DIM, MAX_PIXELS


class RegionOverlay(QWidget):
    """An empty paint overlay; no screenshot is captured until mouse release."""

    selected = Signal(QRect)
    aborted = Signal()

    def __init__(self, screen, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(screen.geometry())
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._start = None
        self._region = QRect()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._start = event.position().toPoint()
        self._region = QRect(self._start, self._start)
        self.update()

    def mouseMoveEvent(self, event):
        if self._start is not None:
            self._region = QRect(self._start, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._start is None:
            return
        self._region = QRect(self._start, event.position().toPoint()).normalized()
        self._start = None
        if (self._region.width() < 40 or self._region.height() < 40 or
                self._region.width() > MAX_DIM or self._region.height() > MAX_DIM or
                self._region.width() * self._region.height() > MAX_PIXELS):
            self.aborted.emit()
        else:
            self.selected.emit(QRect(self._region))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.aborted.emit()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        super().closeEvent(event)
        self.aborted.emit()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(15, 19, 25, 75))
        if not self._region.isNull():
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(self._region, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor(99, 176, 255), 2))
            painter.drawRect(self._region)


class VisionSignals(QObject):
    complete = Signal(int, str, str)


class VisionDialog(PrivateDialog):
    def __init__(self, parent=None, provider_factory=OpenAIVision, *, permission_checker=None, on_speak=None, on_read=None,
                 local_runtime=None, local_models=(), preferences=None, can_start_local=None):
        super().__init__(parent)
        self._permission_checker = permission_checker
        self._on_speak = on_speak
        self._on_read = on_read
        self.setWindowTitle("OMOI VoxBridge — Bildanalyse / Анализ области экрана")
        self.resize(660, 660)
        self._provider_factory = provider_factory
        self._can_start_local = can_start_local or (lambda: True)
        self._local_runtime = local_runtime
        self._local_models = tuple(local_models)
        self._preferences = preferences
        self._provider_cleanup_failed = False
        self._png: bytes | None = None
        self._overlay = None
        self._overlays = []
        self._region_mode = "analyze"
        self._preset_task = ""
        self._shortcut_mode = False
        self._generation = 0
        self._busy = False
        self._worker_generation = None
        self._selection_generation = 0
        self._capture_pending = False
        self._cancel_event = threading.Event()
        self.signals = VisionSignals(self)
        self.signals.complete.connect(self._complete)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Bereich w?hlen, Modell ausw?hlen, Vorschau analysieren. Cloud nur mit Freigabe.\n"
            "Выберите область и модель. Локальный анализ без облака."
        ))
        screen_row = QHBoxLayout()
        self.screen_choice = QComboBox(self)
        for screen in QApplication.screens():
            rect = screen.geometry()
            self.screen_choice.addItem(
                f"{screen.name()} ({rect.width()} × {rect.height()})", screen
            )
        screen_row.addWidget(self.screen_choice)
        self.select = QPushButton("Bereich wählen / Выделить область")
        self.select.clicked.connect(self._start_selection)
        screen_row.addWidget(self.select)
        layout.addLayout(screen_row)

        self.preview = QLabel("Noch kein Bereich ausgewählt / Нет изображения")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(180)
        self.preview.setStyleSheet("border: 1px solid #999;")
        layout.addWidget(self.preview)
        self.question = QLineEdit(self)
        self.question.setMaxLength(1000)
        self.question.setPlaceholderText(
            "Meine Frage / Вопрос: например, объясни сеть VLAN на рисунке"
        )
        layout.addWidget(self.question)

        self.provider = QComboBox(self)
        self.provider.addItem("OpenAI API", "openai")
        self.provider.addItem("Gemma + Windows OCR (Text / текст)", "local_ocr")
        self.provider.addItem("Gemma Vision (Bild / изображение)", "local")
        self.provider.model().item(1).setEnabled(self._local_runtime is not None)
        self.provider.model().item(2).setEnabled(self._local_runtime is not None and any(m.vision for m in self._local_models))
        initial = getattr(preferences, "vision_provider", "openai")
        self.provider.setCurrentIndex(max(0, self.provider.findData(initial)))
        layout.addWidget(self.provider)
        self.local_model = QComboBox(self)
        layout.addWidget(self.local_model)
        self.ocr_language = QComboBox(self)
        for label, key in (("OCR Deutsch", "de"), ("OCR Русский", "ru"), ("OCR English (language pack)", "en")):
            self.ocr_language.addItem(label, key)
        self.ocr_language.setCurrentIndex(max(0, self.ocr_language.findData(getattr(preferences, "language", "de"))))
        layout.addWidget(self.ocr_language)
        self._preferred_model = getattr(preferences, "local_vision_model", "")
        self.consent = QCheckBox(
            "Ich bestätige die Übermittlung NUR dieser Vorschau an OpenAI "
            "(kostenpflichtige API). / Разрешаю отправить ТОЛЬКО этот фрагмент.",
            self,
        )
        layout.addWidget(self.consent)
        layout.addWidget(QLabel("Oder dauerhafte Freigabe im Tray / Или постоянное разрешение в трее."))
        self.provider.currentIndexChanged.connect(self._update_provider_controls)
        if preferences is not None:
            self.provider.activated.connect(self._save_provider)
            self.local_model.activated.connect(self._save_provider)
        self._update_provider_controls()
        self.question.textChanged.connect(lambda *_: self.consent.setChecked(False))
        self.question.returnPressed.connect(self._analyze_preview)
        controls = QHBoxLayout()
        self.analyze = QPushButton("Bild analysieren / Анализировать")
        self.analyze.clicked.connect(self._analyze_preview)
        self.read_region = QPushButton("Ausschnitt vorlesen / Озвучить область")
        self.read_region.clicked.connect(self._read_preview)
        self.read_region.setEnabled(False)
        controls.addWidget(self.read_region)
        self.cancel = QPushButton("Abbrechen / Отмена")
        self.cancel.clicked.connect(self._cancel)
        self.analyze.setEnabled(False)
        self.cancel.setEnabled(False)
        controls.addWidget(self.analyze)
        controls.addWidget(self.cancel)
        layout.addLayout(controls)

        self.message = QLabel("Bereit. Keine Bilder hochgeladen.")
        layout.addWidget(self.message)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output)
        self.speak_answer = QPushButton("Antwort vorlesen / Озвучить ответ")
        self.speak_answer.setEnabled(False)
        self.speak_answer.clicked.connect(self._prepare_speech)
        self.output.textChanged.connect(self._update_speech_button)
        layout.addWidget(self.speak_answer)

    def _save_provider(self, *_):
        try:
            save_analysis_selection("vision", self.provider.currentData(), self.local_model.currentData() or "")
        except (ValueError, OSError, TypeError):
            self.message.setText("SETTINGS_SAVE_FAILED")

    def _update_provider_controls(self, *_):
        key = self.provider.currentData()
        local = key in ("local", "local_ocr")
        chosen_key = self.local_model.currentData()
        if chosen_key is None:
            chosen_key = self._preferred_model
        self.local_model.clear()
        choices = [m for m in self._local_models if key != "local" or m.vision]
        if choices:
            self.local_model.addItem("Auto (bereits geladen / уже загруженная)", "")
        for model in choices:
            self.local_model.addItem(model.label, model.key)
        index = self.local_model.findData(chosen_key)
        if index < 0 and key == "local" and choices:
            index = 0
        self.local_model.setCurrentIndex(index)
        self.local_model.setVisible(local)
        self.ocr_language.setVisible(key == "local_ocr")
        self.consent.setVisible(not local)
        self.consent.setChecked(False)

    @Slot()
    def _update_speech_button(self):
        self.speak_answer.setEnabled(self._on_speak is not None and bool(self.output.toPlainText().strip()))

    @Slot()
    def _prepare_speech(self):
        if self._on_speak is None:
            return
        cursor = self.output.textCursor()
        text = cursor.selectedText().replace("\u2029", "\n") if cursor.hasSelection() else self.output.toPlainText()
        if text.strip():
            try:
                self._on_speak(text, "ru")
            except ProviderFailure as exc:
                self.message.setText(str(exc)[:100])

    def start_shortcut(self, mode, *, task=""):
        if mode not in ("analyze", "read", "context") or self._busy or self._overlay is not None or self._capture_pending:
            return False
        self.question.setText(task)
        self._preset_task = task
        self._region_mode = mode
        self._shortcut_mode = True
        self._begin_selection(QApplication.screens())
        return self._overlay is not None

    @Slot()
    def _analyze_preview(self):
        self._region_mode = "analyze"
        self._analyze()

    @Slot()
    def _read_preview(self):
        self._region_mode = "read"
        self._analyze()

    def _start_selection(self):
        if self._busy or self._overlay is not None or self._capture_pending:
            return
        screen = self.screen_choice.currentData()
        if screen is None:
            self.message.setText("NO_SCREEN")
            return
        self._region_mode = "analyze"
        self._preset_task = ""
        self._shortcut_mode = False
        self._begin_selection([screen])

    def _begin_selection(self, screens):
        self._selection_generation += 1
        self._forget_image()
        self.hide()
        for screen in screens:
            overlay = RegionOverlay(screen)
            self._overlays.append(overlay)
            overlay.selected.connect(lambda rect, source=screen: self._picked(source, rect))
            overlay.aborted.connect(self._aborted)
            overlay.show()
        if self._overlays:
            self._overlay = self._overlays[0]
            from PySide6.QtGui import QCursor
            active = next((o for o in self._overlays if o.geometry().contains(QCursor.pos())), self._overlay)
            active.activateWindow()
            active.setFocus()

    def _aborted(self):
        if self._overlay is None:
            return
        self._dispose_overlay()
        self.message.setText("Auswahl abgebrochen / Выбор отменён.")
        if not self._shortcut_mode:
            self.show()
        self._shortcut_mode = False

    def _picked(self, screen, rect: QRect):
        if self._overlay is None:
            return
        self._dispose_overlay()  # hide overlay BEFORE grabbing selected pixels
        generation = self._selection_generation
        self._capture_pending = True

        def capture():
            if generation != self._selection_generation or not self._capture_pending:
                return
            self._capture_pending = False
            try:
                # QScreen.grabWindow coordinates are relative to selected screen
                # on Windows. Multi-DPI monitor behavior needs a native smoke.
                pixmap = screen.grabWindow(0, rect.x(), rect.y(),
                                           rect.width(), rect.height())
                self._accept_pixmap(pixmap)
            except Exception:
                self._forget_image()
                self.message.setText("SCREEN_CAPTURE_FAILED")
            immediate = self._png is not None and self._shortcut_mode and self._region_mode in ("read", "analyze") and (self.provider.currentData() in ("local", "local_ocr") or approved(self._permission_checker, "vision_openai"))
            if self._region_mode != "read" or not immediate:
                self.show()
                self.raise_()
                self.activateWindow()
            if immediate:
                self._analyze()
            elif self._region_mode == "context":
                self.question.setFocus()
                self.message.setText("Kontext eingeben und Enter / Введи вопрос и нажми Enter.")

        QTimer.singleShot(160, capture)

    def _dispose_overlay(self):
        overlays = self._overlays or ([self._overlay] if self._overlay is not None else [])
        self._overlays = []
        self._overlay = None
        for overlay in overlays:
            overlay.hide()
            overlay.deleteLater()

    def _accept_pixmap(self, image: QPixmap):
        if not isinstance(image, QPixmap) or image.isNull():
            raise ProviderFailure("VISION_CAPTURE_EMPTY")
        # Qt might return DPR-scaled image; validate actual encoded pixels.
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
            raise ProviderFailure("VISION_PNG_ENCODING_FAILED")
        data = bytes(buffer.data())
        buffer.close()
        png_dimensions(data)
        self._png = data
        self.preview.setPixmap(image.scaled(
            QSize(560, 260), Qt.KeepAspectRatio, Qt.SmoothTransformation
        ))
        self.consent.setChecked(False)
        self.analyze.setEnabled(not self._provider_cleanup_failed)
        self.read_region.setEnabled(not self._provider_cleanup_failed)
        self.message.setText("Vorschau fertig. Kontext optional; Analyse oder Vorlesen wählen.")
        self.output.clear()

    def _forget_image(self):
        self._png = None
        self.preview.clear()
        self.preview.setText("Noch kein Bereich ausgewählt / Нет изображения")
        self.consent.setChecked(False)
        self.analyze.setEnabled(False)
        self.read_region.setEnabled(False)
        self.output.clear()

    @Slot()
    def _analyze(self):
        if self._provider_cleanup_failed:
            self.message.setText(PROVIDER_CLEANUP_FAILED)
            return
        if self._busy or self._png is None:
            return
        key = self.provider.currentData()
        local = key in ("local", "local_ocr")
        ocr_language = self.ocr_language.currentData()
        model = model_selection(self._local_models, self.local_model.currentData() or "", vision=key == "local") if local else None
        needs_model = not (key == "local_ocr" and self._region_mode == "read")
        if local and needs_model and not self._can_start_local():
            self.show()
            self.message.setText("LOCAL_LLM_BUSY")
            return
        if local and needs_model and (self._local_runtime is None or self.local_model.currentIndex() < 0 or model is None):
            self.show()
            self.message.setText("LOCAL_VISION_MODEL_NOT_CONFIGURED")
            return
        if not local and not (self.consent.isChecked() or approved(self._permission_checker, "vision_openai")):
            self.show()
            self.message.setText("Cloud-Zustimmung für den sichtbaren Ausschnitt erforderlich.")
            return
        png = self._png
        mode = self._region_mode
        preset = bool(self._preset_task)
        question = self.question.text()
        if mode == "read":
            question = ""
        self._generation += 1
        generation = self._generation
        self._worker_generation = generation
        self._busy = True
        self._cancel_event = threading.Event()
        cancelled = self._cancel_event
        self.consent.setChecked(False)
        self.analyze.setEnabled(False)
        self.select.setEnabled(False)
        self.provider.setEnabled(False)
        self.local_model.setEnabled(False)
        self.ocr_language.setEnabled(False)
        self.read_region.setEnabled(False)
        self.cancel.setEnabled(True)
        self.message.setText(("Gemma lokal" if local else "OpenAI") + " Bildanalyse läuft ...")
        self.output.clear()

        def worker():
            answer, error = "", ""
            provider = None
            try:
                if cancelled.is_set():
                    raise ProviderFailure("VISION_CANCELLED")
                if key == "local_ocr":
                    provider = LocalGemmaOCR(self._local_runtime, model, ocr_language, cancelled)
                elif key == "local":
                    provider = LocalGemmaVision(self._local_runtime, model, cancelled)
                else:
                    provider = self._provider_factory()
                if cancelled.is_set():
                    raise ProviderFailure("VISION_CANCELLED")
                from .cloud_budget import cloud_budget
                with cloud_budget(90, cancelled, max_bytes=2_000_000):
                    if mode == "read":
                        answer = provider.read_png(png)
                    elif preset:
                        answer = provider.analyze_task_png(png, question)
                    else:
                        answer = provider.analyze_png(png, question)
            except ProviderFailure as exc:
                error = str(exc)
            except Exception:
                error = "VISION_ERROR"
            finally:
                error = close_provider(provider) or error
            try:
                self.signals.complete.emit(generation, answer, error)
            except RuntimeError:
                pass

        try:
            threading.Thread(target=worker, name="voxbridge-vision", daemon=True).start()
        except RuntimeError:
            self._complete(generation, "", "VISION_WORKER_START_FAILED")

    @Slot(int, str, str)
    def _complete(self, generation, answer, error):
        if generation != self._worker_generation or not self._busy:
            return
        self._worker_generation = None
        self._busy = False
        self.provider.setEnabled(True)
        self.local_model.setEnabled(True)
        self.ocr_language.setEnabled(True)
        if error == PROVIDER_CLEANUP_FAILED:
            self._provider_cleanup_failed = True
            self.output.clear()
            self.select.setEnabled(True)
            self.analyze.setEnabled(False)
            self.read_region.setEnabled(False)
            self.cancel.setEnabled(False)
            self.message.setText(error)
            self._shortcut_mode = False
            return
        if generation != self._generation:
            self.read_region.setEnabled(self._png is not None)
            self.select.setEnabled(True)
            self.analyze.setEnabled(self._png is not None)
            self.cancel.setEnabled(False)
            return
        self.select.setEnabled(True)
        self.analyze.setEnabled(self._png is not None)
        self.read_region.setEnabled(self._png is not None)
        self.cancel.setEnabled(False)
        self.message.setText(error[:100] if error else "Fertig / Анализ готов.")
        if not error:
            self.output.setPlainText(answer)
            if self._region_mode == "read" and self._on_read is not None:
                try:
                    self._on_read(answer)
                except ProviderFailure as exc:
                    self.message.setText(str(exc)[:100])
                    self.show()
        elif self._shortcut_mode:
            self.show()
        self._shortcut_mode = False

    @Slot()
    def _cancel(self):
        self._generation += 1
        self._cancel_event.set()
        self.consent.setChecked(False)
        self.output.clear()
        self.cancel.setEnabled(False)
        self.select.setEnabled(not self._busy)
        self.analyze.setEnabled(not self._provider_cleanup_failed and not self._busy and self._png is not None)
        self.message.setText(
            "Abgebrochen; bereits gesendete API-Anfrage kann weiterlaufen."
        )

    def _dismiss_private(self):
        self._generation += 1
        self._selection_generation += 1
        self._capture_pending = False
        self._shortcut_mode = False
        self._cancel_event.set()
        self._dispose_overlay()
        self._forget_image()
        self._preset_task = ""
        self.question.clear()
