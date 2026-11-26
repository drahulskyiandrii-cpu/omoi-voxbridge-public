"""Explicit Cartesia owned-voice selector. No TTS calls, model loading or audio uploads."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from .analysis_providers import ProviderFailure
from .cartesia_voices import OwnedVoice, list_owned_voices, VOICE_CLEANUP_FAILED
from .config import load_settings, save_settings
from .private_dialog import PrivateDialog
from .voice_cache import load_voice_cache, save_voice_cache


class VoiceSignals(QObject):
    complete = Signal(int, object, str)


class VoiceDialog(PrivateDialog):
    """Read-only discovery is initiated with Refresh. A different user voice is
    saved only after Save; secrets/voice embeddings are never persisted.
    """

    def __init__(self, parent=None, fetcher=list_owned_voices, *, cache_loader=None, cache_saver=None):
        super().__init__(parent)
        self.setWindowTitle("OMOI VoxBridge — Cartesia: Eigene Stimmen")
        self.resize(490, 210)
        self._fetcher = fetcher
        self._cache_loader = cache_loader or (load_voice_cache if fetcher is list_owned_voices else lambda: [])
        self._cache_saver = cache_saver or (save_voice_cache if fetcher is list_owned_voices else lambda voices: None)
        self._generation = 0
        self._worker_generation = None
        self._cancel = None
        self._cleanup_failed = False
        self._voices: dict[str, OwnedVoice] = {}
        self.signals = VoiceSignals(self)
        self.signals.complete.connect(self._loaded)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Nur eigene Cartesia-Stimmen / Только твои голоса Cartesia.\n"
            "Загрузка списка не синтезирует аудио и не клонирует голос."
        ))
        self.choices = QComboBox(self)
        self.choices.setEnabled(False)
        layout.addWidget(self.choices)
        self.status = QLabel("Liste nicht geladen / Список ещё не загружен.")
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        self.refresh = QPushButton("Eigene Stimmen laden / Обновить")
        self.refresh.clicked.connect(self._refresh)
        controls.addWidget(self.refresh)
        self.save = QPushButton("Auswählen / Выбрать")
        self.save.setEnabled(False)
        self.save.clicked.connect(self._save)
        controls.addWidget(self.save)
        layout.addLayout(controls)

    def showEvent(self, event):
        super().showEvent(event)
        if self._cleanup_failed:
            self.status.setText(VOICE_CLEANUP_FAILED)
        if self._worker_generation is None and not self._voices and not self._cleanup_failed:
            try:
                voices = self._cache_loader()
            except Exception:
                voices = []
            self._populate(voices)
            if voices:
                self.status.setText("Lokaler Stimmen-Cache / Сохранённый список. Обновление необязательно.")
            elif self.choices.count():
                self.status.setText("Gespeicherte Stimme / Голос уже сохранён. Обновление необязательно.")

    @Slot()
    def _refresh(self):
        if self._cleanup_failed or self._worker_generation is not None or not self.refresh.isEnabled():
            return
        self._generation += 1
        generation = self._generation
        self._worker_generation = generation
        cancel = self._cancel = threading.Event()
        self.choices.setEnabled(False)
        self.save.setEnabled(False)
        self.refresh.setEnabled(False)
        self.status.setText("Lade Stimmen-Metadaten ...")

        def worker():
            try:
                found = self._fetcher(cancel_event=cancel)
                voices, error = found, ""
            except ProviderFailure as exc:
                voices, error = [], str(exc)
            except Exception:
                voices, error = [], "CARTESIA_VOICES_ERROR"
            try:
                self.signals.complete.emit(generation, voices, error)
            except RuntimeError:
                pass
        try:
            threading.Thread(target=worker, name="voxbridge-voices", daemon=True).start()
        except RuntimeError:
            self._loaded(generation, [], "CARTESIA_VOICES_WORKER_START_FAILED")

    @Slot(int, object, str)
    def _loaded(self, generation, voices, error):
        if generation != self._worker_generation:
            return
        self._worker_generation = None
        self._cancel = None
        if error == VOICE_CLEANUP_FAILED:
            self._cleanup_failed = True
            self.refresh.setEnabled(False)
            self.choices.setEnabled(False)
            self.save.setEnabled(False)
            self.status.setText(error)
            return
        self.refresh.setEnabled(True)
        if generation != self._generation:
            self.status.setText("Liste erneut laden / Обновите список.")
            return
        if not isinstance(voices, list) or len(voices) > 500 or not all(isinstance(v, OwnedVoice) for v in voices):
            error = "CARTESIA_VOICES_INVALID_RESULT"
        if error:
            self.choices.setEnabled(bool(self._voices))
            self.save.setEnabled(bool(self._voices))
            self.status.setText(error + " — Cache bleibt erhalten / Сохранённые голоса остаются.")
            return
        self._populate(voices)
        try:
            self._cache_saver(voices)
        except (OSError, ValueError, TypeError):
            self.status.setText("VOICE_CACHE_SAVE_FAILED")
            return
        self.status.setText(f"{len(voices)} eigene Stimmen / Список сохранён." if voices else "Keine eigenen Stimmen im Account gefunden.")

    def _populate(self, voices):
        try:
            selected = load_settings().cartesia_voice_id
        except (ValueError, OSError):
            selected = ""
        self._voices = {v.voice_id: v for v in voices}
        self.choices.clear()
        for voice in voices:
            self.choices.addItem(f"{voice.name} ({voice.language.upper()})", voice.voice_id)
        self.choices.setEnabled(bool(voices))
        self.save.setEnabled(bool(voices))
        if selected in self._voices:
            self.choices.setCurrentIndex(self.choices.findData(selected))
        elif selected:
            self.choices.addItem("Gespeicherte Stimme / Сохранённый голос (не проверен)", selected)
            self.choices.setCurrentIndex(self.choices.count() - 1)
        else:
            # User's known own name is an optional suggestion, never a persisted default.
            suggested = next((v.voice_id for v in voices if v.name.casefold() == "nika"), None)
            if suggested:
                self.choices.setCurrentIndex(self.choices.findData(suggested))
        self.choices.setEnabled(bool(self._voices))
        self.save.setEnabled(bool(self._voices))

    @Slot()
    def _save(self):
        from dataclasses import replace
        selected = self.choices.currentData()
        if selected not in self._voices:
            self.status.setText("VOICE_NOT_OWNED")
            return
        try:
            settings = replace(load_settings(), cartesia_voice_id=selected).validate()
            save_settings(settings)
        except (ValueError, OSError):
            self.status.setText("VOICE_SAVE_FAILED")
            return
        self.status.setText("Stimme gespeichert / Голос выбран.")
        self.accept()

    def _dismiss_private(self):
        self._generation += 1  # ignore late API metadata
        if self._cancel is not None:
            self._cancel.set()
        self._voices.clear()
        self.choices.clear()
        self.choices.setEnabled(False)
        self.refresh.setEnabled(self._worker_generation is None and not self._cleanup_failed)
        self.save.setEnabled(False)
        self.status.setText("Liste nicht geladen / Список ещё не загружен.")
