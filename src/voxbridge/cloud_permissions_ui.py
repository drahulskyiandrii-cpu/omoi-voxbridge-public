"""Explicit opt-in/revocation for future user-triggered cloud actions."""
from PySide6.QtWidgets import (QCheckBox, QDialog, QLabel, QPushButton,
                               QHBoxLayout, QVBoxLayout)
from .cloud_permissions import CloudPermissions, load_permissions, save_permissions


LABELS = {
    "dictation_openai": "Diktieren / Диктовка — OpenAI (Mikrofon / микрофон)",
    "analysis_openai": "Textanalyse / Анализ текста — OpenAI",
    "analysis_anthropic": "Textanalyse / Анализ текста — Anthropic",
    "vision_openai": "Bildanalyse / Анализ изображений — OpenAI",
    "tts_cartesia": "Vorlesen / Озвучивание — Cartesia",
    "captions_openai_audio": "Live-Untertitel / Субтитры — OpenAI (Systemton / системный звук)",
    "captions_openai_translation": "Untertitel-Übersetzung / Перевод субтитров — OpenAI",
}


class CloudPermissionsDialog(QDialog):
    def __init__(self, parent=None, *, loader=load_permissions, saver=save_permissions):
        super().__init__(parent)
        self.setWindowTitle("Cloud-Freigaben / Облачные разрешения")
        self._saver = saver
        layout = QVBoxLayout(self)
        info = QLabel(
            "Aktivierte Freigaben gelten dauerhaft für manuell gestartete, ggf. "
            "kostenpflichtige Anfragen an den genannten Anbieter. "
            "Keine automatischen Aufnahmen oder Anfragen.\n"
            "Отмеченные функции не требуют повторного подтверждения. "
            "Данные отправляются только после вашего запуска функции; возможна оплата. "
            "Снимите галочку и сохраните, чтобы отозвать разрешение для будущих запросов."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        self.choices = {}
        try:
            current = loader().validate()
            error = ""
        except (OSError, ValueError, TypeError, AttributeError):
            current = CloudPermissions()
            error = "CLOUD_PERMISSION_INVALID — keine dauerhafte Freigabe aktiv."
        for scope, label in LABELS.items():
            check = QCheckBox(label)
            check.setChecked(getattr(current, scope))
            self.choices[scope] = check
            layout.addWidget(check)
        controls = QHBoxLayout()
        self.save = QPushButton("Speichern / Сохранить")
        self.revoke = QPushButton("Alle abwählen / Отозвать все")
        self.cancel = QPushButton("Abbrechen / Отмена")
        self.save.clicked.connect(self._save)
        self.revoke.clicked.connect(self._revoke)
        self.cancel.clicked.connect(self.reject)
        for button in (self.save, self.revoke, self.cancel):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.status = QLabel(error)
        layout.addWidget(self.status)

    def _revoke(self):
        for check in self.choices.values():
            check.setChecked(False)

    def _save(self):
        value = CloudPermissions(**{scope: check.isChecked()
                                   for scope, check in self.choices.items()})
        try:
            self._saver(value)
        except (OSError, ValueError, TypeError):
            self.status.setText("CLOUD_PERMISSION_SAVE_FAILED")
            return
        self.accept()
