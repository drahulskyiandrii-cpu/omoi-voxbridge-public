"""Single local control centre. Construction and status refresh never start I/O."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QVBoxLayout,
)

STT_CHOICES = (
    ("mini_cpu", "Whisper Medium — CPU / Qualität"),
    ("mini_worker", "Faster-Whisper Small — CUDA / GPU"),
    ("openai_cloud", "OpenAI — Cloud / API"),
)
LANGUAGE_CHOICES = (("de", "Deutsch"), ("ru", "Русский"), ("en", "English"))

STATUS_LABELS = {
    "dictation": "Diktieren / Диктовка",
    "stt_partial": "STT Partial / Потоковая обработка",
    "selection": "Auswahl / Выделение",
    "analysis": "KI-Analyse / Анализ",
    "vision": "Bildanalyse / Изображение",
    "tts": "Vorlesen / Озвучивание",
    "captions": "Untertitel / Субтитры",
    "cloud": "Cloud / API",
    "local": "Lokale Modelle / Локальные модели",
}

ACTION_LABELS = {
    "analysis": "KI-Analyse / Анализ текста",
    "vision": "Bildanalyse / Анализ изображения",
    "tts": "Vorlesen: Modell & Stimme / Озвучивание: модель и голос",
    "captions": "Live-Untertitel / Субтитры",
    "demo": "Untertitel-Demo / Демонстрация",
    "voices": "Cartesia-Stimmen / Голоса",
    "settings": "Einstellungen / Настройки",
    "permissions": "Cloud-Freigaben / Разрешения",
}


class ControlCenter(QDialog):
    def __init__(self, *, status_provider, actions, stop_all, parent=None,
                 settings_provider=None, set_backend=None, set_language=None,
                 cuda_status=None, toggle_cuda=None):
        super().__init__(parent)
        self.setWindowTitle("OMOI VoxBridge — Kontrollzentrum / Центр управления")
        self.resize(720, 500)
        self._status_provider = status_provider
        self._settings_provider = settings_provider
        self._cuda_status = cuda_status
        layout = QVBoxLayout(self)
        heading = QLabel("OMOI VoxBridge")
        font = heading.font()
        font.setPointSize(17)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        if settings_provider is not None:
            dictation = QGroupBox("Diktieren / Диктовка")
            form = QFormLayout(dictation)
            self.dictation_key = QLabel()
            form.addRow("Hotkey / Клавиша", self.dictation_key)
            self.engine = QComboBox()
            self.engine.setPlaceholderText("STT wählen / Выберите распознавание")
            for mode, label in STT_CHOICES:
                self.engine.addItem(label, mode)
            self.language = QComboBox()
            for code, label in LANGUAGE_CHOICES:
                self.language.addItem(label, code)
            form.addRow("STT", self.engine)
            form.addRow("Sprache / Язык", self.language)
            if cuda_status is not None:
                self.cuda_button = QPushButton()
                self.cuda_button.clicked.connect(toggle_cuda)
                self.cuda_info = QLabel()
                self.cuda_info.setWordWrap(True)
                form.addRow(self.cuda_button)
                form.addRow(self.cuda_info)
            self.engine.activated.connect(lambda i: (set_backend(self.engine.itemData(i)), self.refresh_status()))
            self.language.activated.connect(lambda i: (set_language(self.language.itemData(i)), self.refresh_status()))
            layout.addWidget(dictation)
        group = QGroupBox("Status / Состояние")
        form = QFormLayout(group)
        self.status_rows = {}
        for key, label in STATUS_LABELS.items():
            field = QLabel("—")
            field.setTextFormat(Qt.PlainText)
            field.setWordWrap(True)
            form.addRow(label, field)
            self.status_rows[key] = field
        buttons = QGroupBox("Funktionen / Функции")
        grid = QGridLayout(buttons)
        self.buttons = {}
        for index, key in enumerate(("tts", "captions", "analysis", "vision", "settings", "permissions", "voices", "demo")):
            label = ACTION_LABELS[key]
            button = QPushButton(label)
            button.clicked.connect(actions[key])
            grid.addWidget(button, index // 2, index % 2)
            self.buttons[key] = button
            if key in ("voices", "demo"):
                button.hide()
        layout.addWidget(buttons)
        self.details = QPushButton("Status / Diagnose", self)
        self.details.setCheckable(True)
        group.hide()
        self.details.toggled.connect(group.setVisible)
        self.status_group = group
        layout.addWidget(self.details)
        layout.addWidget(group)
        controls = QHBoxLayout()
        self.stop_all = QPushButton("Alles stoppen / Остановить всё")
        self.stop_all.clicked.connect(stop_all)
        self.refresh = QPushButton("Status aktualisieren / Обновить")
        self.refresh.clicked.connect(self.refresh_status)
        close = QPushButton("Schließen / Закрыть")
        close.clicked.connect(self.close)
        for button in (self.stop_all, self.refresh, close):
            controls.addWidget(button)
        layout.addLayout(controls)
        self._timer = QTimer(self)
        self._timer.setInterval(750)
        self._timer.timeout.connect(self.refresh_status)
        self.refresh_status()

    @Slot()
    def refresh_status(self):
        if self._settings_provider is not None:
            settings = self._settings_provider()
            self.dictation_key.setText(settings.hotkey)
            self.engine.setCurrentIndex(self.engine.findData(settings.stt_backend))
            self.language.setCurrentIndex(self.language.findData(settings.language))
            if self._cuda_status is not None:
                state, error = self._cuda_status()
                show = settings.stt_backend == "mini_worker" or state != "off"
                self.cuda_button.setVisible(show)
                self.cuda_info.setVisible(show)
                self.cuda_button.setEnabled(state != "closing")
                self.cuda_button.setText({
                    "off": "CUDA vorwärmen / Активировать CUDA",
                    "loading": "Laden abbrechen / Отменить загрузку",
                    "ready": "CUDA deaktivieren / Выгрузить CUDA",
                    "closing": "CUDA wird beendet / Выгрузка",
                    "failed": "Fehler zurücksetzen / Сбросить ошибку",
                }[state])
                self.cuda_info.setText(error or {
                    "off": "Einmal laden, dann mehrere Diktate / Одна загрузка на несколько диктовок",
                    "loading": "Modell wird geladen / Загрузка модели",
                    "ready": "Bereit / Модель готова",
                    "closing": "Bitte erneut prüfen / Выгрузка",
                    "failed": "Modellfehler / Ошибка модели",
                }[state])
        try:
            values = self._status_provider()
        except Exception:
            values = {}
        for key, field in self.status_rows.items():
            value = values.get(key, "—") if isinstance(values, dict) else "—"
            field.setText(value[:420] if isinstance(value, str) else "—")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_status()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)
