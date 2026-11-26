"""Explicit non-sensitive desktop preferences, applied at next manual launch."""
from dataclasses import replace
import threading

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (QDialog, QComboBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QSpinBox, QPushButton, QVBoxLayout, QScrollArea, QWidget, QCheckBox, QTabWidget)

from .config import Settings, load_settings, save_settings, layout_safe_hotkeys
from .microphone_devices import selectable_inputs


class DeviceSignals(QObject):
    ready = Signal(object, int, str)


class DesktopSettingsDialog(QDialog):
    def __init__(self, active: Settings, parent=None, *, loader=load_settings, saver=save_settings, discover=selectable_inputs, live_microphone=False):
        super().__init__(parent)
        self.setWindowTitle("OMOI VoxBridge — Einstellungen / Настройки")
        self._loader, self._saver = loader, saver
        self._discover = discover
        self._live_microphone = live_microphone
        self._discovering = False
        self._devices = ()
        self._loaded_on_show = False
        self._prewarm_edited = False
        self._saved_mic_label = ""
        self._signals = DeviceSignals(self)
        self._signals.ready.connect(self._devices_ready)
        self.resize(720, 650)
        layout = QVBoxLayout(self)
        info = QLabel(
            "Mikrofon: nach Speichern sofort aktiv; Hotkeys und Aufnahmelimit: nach Neustart.\n"
            "Микрофон применяется сразу после сохранения; горячие клавиши и лимит записи — после перезапуска."
            if live_microphone else
            "Hotkeys, Mikrofon und Aufnahmelimit gelten nach dem nächsten manuellen Programmstart.\n"
            "Настройки применяются после перезапуска.")
        info.setWordWrap(True)
        layout.addWidget(info)
        active_label = QLabel(f"Aktiv / Сейчас: {active.hotkey}, {active.selection_hotkey}, "
                              f"{active.max_record_seconds} s")
        active_label.setWordWrap(True)
        layout.addWidget(active_label)
        self.tabs = QTabWidget(self)
        def tab(label):
            scroll = QScrollArea(self.tabs)
            scroll.setWidgetResizable(True)
            content = QWidget(scroll)
            form = QFormLayout(content)
            scroll.setWidget(content)
            self.tabs.addTab(scroll, label)
            return form

        form = tab("Diktat / Диктовка")
        hotkeys = tab("Hotkeys / Клавиши")
        presets = tab("Bildaufgaben / Экран")
        advanced = tab("Erweitert / Расширенные")
        self.dictation = QLineEdit()
        self.selection = QLineEdit()
        self.region_analysis = QLineEdit()
        self.region_context = QLineEdit()
        self.region_task = QLineEdit()
        self.region_task2_hotkey = QLineEdit()
        self.region_task2 = QLineEdit()
        self.region_task3_hotkey = QLineEdit()
        self.region_task3 = QLineEdit()
        for field in (self.region_task, self.region_task2, self.region_task3):
            field.setMaxLength(600)
            field.setPlaceholderText("Например: переведи текст на русский, только перевод")
        self.region_task2_hotkey.setMaxLength(48)
        self.region_task3_hotkey.setMaxLength(48)
        self.region_read = QLineEdit()
        self.read_selection = QLineEdit()
        self.copy_read = QLineEdit()
        for edit in (self.dictation, self.selection, self.region_analysis, self.region_context, self.region_read, self.read_selection, self.copy_read):
            edit.setMaxLength(48)
            edit.setPlaceholderText("Ctrl+Alt+Space")
        self.reuse_services = QCheckBox("Lokale OMOI-Dienste wiederverwenden / Использовать уже запущенные локальные модели")
        self.reuse_services.setToolTip("Nur gepr?fte lokale Prozesse; Legacy-Dienste ohne HTTP-Authentifizierung. Kein Beenden fremder Prozesse.")
        advanced.addRow(self.reuse_services)
        self.prewarm_tts = QCheckBox("Qwen beim Start vorwärmen / Загружать Qwen при запуске")
        self.prewarm_tts.setToolTip("Nur ausgew?hlte lokale TTS, mit VRAM-Schutz. Kein Cloud-Aufruf.")
        self.prewarm_tts.clicked.connect(self._mark_prewarm_edited)
        advanced.addRow(self.prewarm_tts)
        self.model_dirs = QLineEdit()
        self.model_dirs.setMaxLength(4096)
        self.model_dirs.setToolTip("Absolute Modellordner, getrennt durch Semikolon. Keine Downloads.")
        advanced.addRow("GGUF-Ordner:", self.model_dirs)
        self.llama_server = QLineEdit()
        self.llama_server.setMaxLength(1024)
        self.llama_server.setToolTip("Optionaler absoluter Pfad zur vorhandenen llama-server.exe.")
        advanced.addRow("llama-server.exe:", self.llama_server)
        self.local_partial = QCheckBox("Live-Vorschau / Промежуточный текст (Mini CPU/CUDA)")
        advanced.addRow(self.local_partial)
        self.enhanced_cpu = QCheckBox("OpenVINO CPU: native streaming / новый движок")
        self.enhanced_cpu.setToolTip("Lange Diktate: ein fr\u00fcher Entwurf nach 6-10 s Audio. Extra CPU-Arbeit m\u00f6glich. / \u0414\u043b\u0438\u043d\u043d\u0430\u044f \u0434\u0438\u043a\u0442\u043e\u0432\u043a\u0430: \u0440\u0430\u043d\u043d\u0438\u0439 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a \u043f\u043e\u0441\u043b\u0435 6-10 \u0441 \u0430\u0443\u0434\u0438\u043e. \u0412\u043e\u0437\u043c\u043e\u0436\u043d\u0430 \u0434\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u0430\u044f \u043d\u0430\u0433\u0440\u0443\u0437\u043a\u0430 CPU.")
        self.hotwords_de = QLineEdit()
        self.hotwords_de.setMaxLength(192)
        self.hotwords_de.setPlaceholderText("Firewall, Netzwerkkarte, VLAN, Arbeitsspeicher")
        self.hotwords_ru = QLineEdit()
        self.hotwords_ru.setMaxLength(192)
        self.hotwords_ru.setPlaceholderText("Сервер, OpenVINO, VLAN")
        self.hotwords = QLineEdit()
        self.hotwords.setMaxLength(192)
        self.hotwords.setPlaceholderText("OMOI, OpenVINO, GitHub, VLAN (max. 192 UTF-8 bytes)")
        self.term_correction = QCheckBox("IT-Begriffe / Контекстное исправление IT-терминов")
        self.audio_conditioning = QCheckBox("Audio: DC + gain / Нормализация уровня (эксперимент)")
        advanced.addRow(self.enhanced_cpu)
        advanced.addRow("CPU/CUDA Hotwords / Общий словарь:",self.hotwords)
        advanced.addRow("Deutsch / Немецкий словарь:",self.hotwords_de)
        advanced.addRow("Russisch / Русский словарь:",self.hotwords_ru)
        hint = QLabel("Optional. Sprachspezifisch ersetzt den allgemeinen Wortschatz. / Необязательно. Словарь языка заменяет общий. Подсказки могут ухудшить результат.")
        hint.setWordWrap(True)
        advanced.addRow(hint)
        advanced.addRow(self.term_correction)
        advanced.addRow(self.audio_conditioning)
        self.stt_port = QSpinBox()
        self.stt_port.setRange(1024, 65535)
        self.vram = QSpinBox()
        self.vram.setRange(2000, 16000)
        self.vram.setSuffix(" MiB")
        self.seconds = QSpinBox()
        from .audio import MAX_RECORD_SECONDS
        self.seconds.setRange(3, MAX_RECORD_SECONDS)
        self.seconds.setSuffix(" s")
        self.seconds.setToolTip("31–120 s: längere Diktate in Abschnitten; lokale Vorschau bei aktivierter Teiltranskription. / 31–120 с: диктовка по фрагментам, локальный предпросмотр при включённом потоковом режиме.")
        form.addRow("Diktieren / Диктовка:", self.dictation)
        self.layout_safe = QPushButton("DE/RU/EN: F-Tasten / Независимые от раскладки")
        self.layout_safe.clicked.connect(self._layout_safe_hotkeys)
        hotkeys.addRow(self.layout_safe)
        hint = QLabel("Ctrl+Shift+F1-F9 statt Buchstaben und AltGr. "
                      "Vorschlag; erst speichern und neu starten.")
        hint.setWordWrap(True)
        hotkeys.addRow(hint)
        hotkeys.addRow("Auswahl / Выделенный текст:", self.selection)
        presets.addRow("Bildanalyse / Анализ области:", self.region_analysis)
        presets.addRow("Aufgabe 1 / Задание 1:", self.region_task)
        presets.addRow("Hotkey 2 / Комбинация 2:", self.region_task2_hotkey)
        presets.addRow("Aufgabe 2 / Задание 2:", self.region_task2)
        presets.addRow("Hotkey 3 / Комбинация 3:", self.region_task3_hotkey)
        presets.addRow("Aufgabe 3 / Задание 3:", self.region_task3)
        presets.addRow("Bild + Frage / Область + вопрос:", self.region_context)
        presets.addRow("Bild vorlesen / Озвучить область:", self.region_read)
        hotkeys.addRow("Text vorlesen / Озвучить текст:", self.read_selection)
        hotkeys.addRow("Kopierten Text vorlesen / Скопированный текст:", self.copy_read)
        form.addRow("Max. Aufnahme / Лимит записи:", self.seconds)
        advanced.addRow(self.stt_port)
        self.stt_port.hide()
        advanced.addRow("CUDA min. frei / Свободная VRAM:", self.vram)
        self.microphone = QComboBox()
        self.microphone.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.microphone.setMinimumContentsLength(28)
        self.refresh_mics = QPushButton("Geräte laden / Обновить список")
        self.refresh_mics.clicked.connect(self._refresh_microphones)
        mic_row = QHBoxLayout()
        mic_row.addWidget(self.microphone, 1)
        mic_row.addWidget(self.refresh_mics)
        form.addRow("Mikrofon / Микрофон:", mic_row)
        self.mic_status = QLabel("Geräteliste manuell laden; keine Aufnahme. / Обновление списка не включает запись.")
        self.mic_status.setWordWrap(True)
        form.addRow(self.mic_status)
        layout.addWidget(self.tabs, 1)
        supported = QLabel("Ctrl / Alt / Shift / Win + A–Z, 0–9, Space, F8–F12. "
                           "Windows kann reservierte Kombinationen ablehnen.")
        supported.setWordWrap(True)
        layout.addWidget(supported)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.save = QPushButton("Speichern / Сохранить")
        self.defaults = QPushButton("Standard / По умолчанию")
        cancel = QPushButton("Schließen / Закрыть")
        self.save.clicked.connect(self._save)
        self.defaults.clicked.connect(self._defaults)
        cancel.clicked.connect(self.reject)
        for button in (self.save, self.defaults, cancel):
            row.addWidget(button)
        layout.addLayout(row)
        try:
            saved = loader().validate()
        except (OSError, ValueError, TypeError):
            self._fill(active)
            self.status.setText("SETTINGS_READ_FAILED; bestehende Datei wird nicht überschrieben.")
            self.save.setEnabled(False)
        else:
            self._fill(saved)

    def _fill(self, settings):
        self.dictation.setText(settings.hotkey)
        self.selection.setText(settings.selection_hotkey)
        self.region_analysis.setText(settings.region_analysis_hotkey)
        self.region_context.setText(settings.region_context_hotkey)
        self.region_task.setText(settings.region_task)
        self.region_task2_hotkey.setText(settings.region_task2_hotkey)
        self.region_task2.setText(settings.region_task2)
        self.region_task3_hotkey.setText(settings.region_task3_hotkey)
        self.region_task3.setText(settings.region_task3)
        self.region_read.setText(settings.region_read_hotkey)
        self.read_selection.setText(settings.read_selection_hotkey)
        self.copy_read.setText(settings.copy_read_hotkey)
        self.seconds.setValue(settings.max_record_seconds)
        self.reuse_services.setChecked(settings.reuse_local_services)
        self.prewarm_tts.setChecked(settings.prewarm_local_tts)
        self.model_dirs.setText(settings.local_model_dirs)
        self.llama_server.setText(settings.local_llama_server)
        self.local_partial.setChecked(settings.local_partial_enabled)
        self.enhanced_cpu.setChecked(settings.enhanced_cpu_stt)
        self.hotwords.setText(settings.stt_hotwords)
        self.hotwords_de.setText(settings.stt_hotwords_de)
        self.hotwords_ru.setText(settings.stt_hotwords_ru)
        self.term_correction.setChecked(settings.stt_term_correction)
        self.audio_conditioning.setChecked(settings.stt_audio_conditioning)
        self.stt_port.setValue(settings.stt_port)
        self.vram.setValue(settings.min_free_vram_mib)
        self._saved_mic_label = settings.microphone_label
        self._populate_microphones(settings.microphone_id)

    def _populate_microphones(self, selected):
        self.microphone.clear()
        self.microphone.addItem("Systemstandard / Системный по умолчанию", "")
        for device in self._devices:
            self.microphone.addItem(device.label, device.device_id)
        if selected and self.microphone.findData(selected) < 0:
            label = self._saved_mic_label or "Gespeichertes Mikrofon / Сохранённый микрофон"
            self.microphone.addItem(label + " (nicht geprüft)", selected)
        self.microphone.setCurrentIndex(self.microphone.findData(selected))

    def showEvent(self, event):
        super().showEvent(event)
        # Opening settings explicitly requests device metadata, never capture.
        if not self._loaded_on_show:
            self._loaded_on_show = True
            self._refresh_microphones()

    @Slot()
    def _refresh_microphones(self):
        if self._discovering:
            return
        self._discovering = True
        self.refresh_mics.setEnabled(False)
        self.mic_status.setText("Geräte werden gesucht / Поиск устройств…")

        def worker():
            try:
                devices, ambiguous = self._discover()
                error = ""
            except Exception:
                devices, ambiguous, error = (), 0, "MIC_DEVICE_DISCOVERY_FAILED"
            try:
                self._signals.ready.emit(devices, ambiguous, error)
            except RuntimeError:
                pass
        try:
            threading.Thread(target=worker, name="voxbridge-microphone-discovery", daemon=True).start()
        except RuntimeError:
            self._devices_ready((), 0, "MIC_DEVICE_DISCOVERY_FAILED")

    @Slot(object, int, str)
    def _devices_ready(self, devices, ambiguous, error):
        if not self._discovering:
            return
        self._discovering = False
        self.refresh_mics.setEnabled(True)
        selected = self.microphone.currentData()
        old = next((d for d in self._devices if d.device_id == selected), None)
        if old is not None:
            self._saved_mic_label = old.label
        self._devices = tuple(devices)
        matched = next((d for d in self._devices if d.matches(selected)), None)
        if matched is not None:
            self._saved_mic_label = matched.label
            selected = matched.device_id
        self._populate_microphones(selected)
        if error:
            self.mic_status.setText(error + " - Auswahl bleibt gespeichert.")
        elif selected and not any(d.device_id == selected for d in self._devices):
            self.mic_status.setText("MIC_DEVICE_UNAVAILABLE: Systemstandard oder ein verfügbares Mikrofon wählen, dann speichern. / Выберите системный или доступный микрофон и сохраните.")
        elif selected and matched is not None:
            self.mic_status.setText("Ausgewaehlt / Selected: " + matched.label)
        else:
            self.mic_status.setText(f"{len(devices)} Eingänge / Входов; {ambiguous} nicht eindeutig / неоднозначных. Neustart ggf. nötig.")

    @Slot()
    def _layout_safe_hotkeys(self):
        # Only change shortcut drafts, never modify microphone/provider/tasks.
        try:
            preset = layout_safe_hotkeys(replace(Settings(),
            region_task2=self.region_task2.text().strip(),
            region_task2_hotkey="Ctrl+Shift+F8" if self.region_task2.text().strip() else "",
            region_task3=self.region_task3.text().strip(),
            region_task3_hotkey="Ctrl+Shift+F9" if self.region_task3.text().strip() else ""))
        except ValueError:
            self.status.setText("HOTKEY_PRESET_INVALID_TASK")
            return
        for widget, value in (
            (self.dictation, preset.hotkey), (self.selection, preset.selection_hotkey),
            (self.region_analysis, preset.region_analysis_hotkey),
            (self.region_context, preset.region_context_hotkey),
            (self.region_read, preset.region_read_hotkey),
            (self.read_selection, preset.read_selection_hotkey),
            (self.copy_read, preset.copy_read_hotkey),
            (self.region_task2_hotkey, preset.region_task2_hotkey),
            (self.region_task3_hotkey, preset.region_task3_hotkey),
        ):
            widget.setText(value)
        self.status.setText("F-Tasten vorgeschlagen. Speichern und neu starten.")

    @Slot()
    def _defaults(self):
        self._fill(Settings())
        self._prewarm_edited = True

    @Slot(bool)
    def _mark_prewarm_edited(self, checked):
        self._prewarm_edited = True

    @Slot()
    def _save(self):
        try:
            selected = self.microphone.currentData()
            matched = next((d for d in self._devices if d.device_id == selected), None)
            label = matched.label if matched is not None else (self._saved_mic_label if selected else "")
            # Reload to preserve voice, language and enablement changed elsewhere.
            current = self._loader()
            updated = replace(current,
                              hotkey=self.dictation.text().strip(),
                              selection_hotkey=self.selection.text().strip(),
                              region_analysis_hotkey=self.region_analysis.text().strip(),
                              region_context_hotkey=self.region_context.text().strip(),
                              region_task=self.region_task.text().strip(),
                              region_task2_hotkey=self.region_task2_hotkey.text().strip(),
                              region_task2=self.region_task2.text().strip(),
                              region_task3_hotkey=self.region_task3_hotkey.text().strip(),
                              region_task3=self.region_task3.text().strip(),
                              region_read_hotkey=self.region_read.text().strip(),
                              read_selection_hotkey=self.read_selection.text().strip(),
                              copy_read_hotkey=self.copy_read.text().strip(),
                              max_record_seconds=self.seconds.value(),
                              local_partial_enabled=self.local_partial.isChecked(),
                              reuse_local_services=self.reuse_services.isChecked(),
                              prewarm_local_tts=(self.prewarm_tts.isChecked() if self._prewarm_edited
                                                 else current.prewarm_local_tts),
                              local_model_dirs=self.model_dirs.text().strip(),
                              local_llama_server=self.llama_server.text().strip(),
                              enhanced_cpu_stt=self.enhanced_cpu.isChecked(),
                              stt_hotwords=self.hotwords.text().strip(),
                              stt_hotwords_de=self.hotwords_de.text().strip(),
                              stt_hotwords_ru=self.hotwords_ru.text().strip(),
                              stt_term_correction=self.term_correction.isChecked(),
                              stt_audio_conditioning=self.audio_conditioning.isChecked(),
                              min_free_vram_mib=self.vram.value(),
                              microphone_id=selected,
                              microphone_label=label).validate()
            self._saver(updated)
            self._saved_mic_label = label
            self.prewarm_tts.setChecked(updated.prewarm_local_tts)
            self._prewarm_edited = False
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText("SETTINGS_SAVE_FAILED: " + (
                str(exc) if isinstance(exc, ValueError) and str(exc).isupper()
                and len(str(exc)) < 80 else "INVALID_OR_UNAVAILABLE"))
            return
        self.status.setText("Mikrofon aktiv; übrige Einstellungen: Neustart / Микрофон активен; остальные настройки — после перезапуска."
                            if self._live_microphone else
                            "Gespeichert; Neustart erforderlich / Сохранено, нужен перезапуск.")
