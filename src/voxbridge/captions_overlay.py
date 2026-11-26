"""Read-only two-lane Qt subtitle overlay; no microphone, API or model startup.

A separate user-approved audio/STT integration will feed the coordinator.
No network, screen capture, sound card or clipboard use occurs in this module.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QPoint, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget, QHBoxLayout, QPushButton

from .captions_core import CaptionCoordinator, CaptionSnapshot


_STATUS_LABELS = {
    "pending": "…",
    "running": "…",
    "skipped": "[перевод пропущен]",
    "failed": "[ошибка перевода]",
    "original_only": "",
    "grouped": "\u2193",
}


def format_caption(snapshot: CaptionSnapshot, max_lines: int = 6) -> tuple[str, str]:
    """Generate plain strings; never interpret captured speech as rich HTML."""
    if type(max_lines) is not int or not 1 <= max_lines <= 6:
        raise ValueError("INVALID_CAPTION_LINE_COUNT")
    original = []
    translated = []
    # Keep the same finalized rows in both lanes. A tentative line is extra.
    for line in snapshot.lines[-max_lines:]:
        original.append(line.original)
        if line.status == "complete" and line.translation:
            translated.append(line.translation)
        else:
            translated.append(_STATUS_LABELS.get(line.status, ""))
    if snapshot.partial:
        original.append("≈ " + snapshot.partial)
    original_text = "\n".join(original) or "Original / Исходная речь…"
    labels = {"ru": "Русский перевод…", "de": "Deutsche Übersetzung…",
              "en": "English translation…"}
    translated_text = "\n".join(translated) or labels.get(snapshot.target_language, "")
    return original_text, translated_text


def populate_synthetic_demo(coordinator: CaptionCoordinator) -> None:
    """Manual UX preview only; no STT, translator API, network or device."""
    generation = coordinator.begin(source_language="de", target_language="ru")
    samples = (
        ("Ein VLAN trennt logische Netze.", "VLAN разделяет логические сети."),
        ("DHCP verteilt automatisch IP-Adressen.", "DHCP автоматически выдаёт IP-адреса."),
    )
    for sequence, (source, translation) in enumerate(samples):
        coordinator.final(generation, sequence, source)
        job = coordinator.take_translation()
        if job is None or not coordinator.finish_translation(job, translation):
            raise RuntimeError("CAPTION_DEMO_FAILED")


class CaptionsOverlay(QWidget):
    """Always-on-top display driven by snapshots only, with no external I/O."""

    closed = Signal()

    def __init__(self, coordinator: CaptionCoordinator, parent=None):
        if not isinstance(coordinator, CaptionCoordinator):
            raise TypeError("CAPTION_COORDINATOR_REQUIRED")
        super().__init__(parent)
        self.controller = coordinator
        self.setWindowTitle("OMOI VoxBridge — Untertitel / Субтитры")
        self.setWindowFlags(
            Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMinimumSize(420, 130)
        self.resize(740, 235)
        self.setStyleSheet(
            "QFrame#captionFrame { background: #19212d; border-radius: 12px; }"
            "QLabel { color: #f8fafc; padding: 3px; }"
            "QLabel#ruLabel { color: #b7d9ff; }"
        )
        frame = QFrame(self)
        frame.setObjectName("captionFrame")
        main = QVBoxLayout(self)
        main.addWidget(frame)
        lay = QVBoxLayout(frame)
        header = QHBoxLayout()
        self.title_label = QLabel("Untertitel / Subtitles")
        header.addWidget(self.title_label, 1)
        self.close_button = QPushButton("X / Close")
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.clicked.connect(self.close)
        header.addWidget(self.close_button)
        lay.addLayout(header)
        self.de_label = QLabel("Original / Немецкие субтитры…")
        self.de_label.setTextFormat(Qt.PlainText)
        self.de_label.setWordWrap(True)
        self.de_label.setObjectName("deLabel")
        self.ru_label = QLabel("Русский перевод…")
        self.ru_label.setTextFormat(Qt.PlainText)
        self.ru_label.setWordWrap(True)
        self.ru_label.setObjectName("ruLabel")
        lay.addWidget(self.de_label)
        lay.addWidget(self.ru_label)

        self._display_mode = "both"
        self._max_lines = 6
        self.set_display_options("both", 15, 6, 100)
        self._drag_origin: QPoint | None = None
        self._rendered_generation = -1
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.refresh)
        # Timer starts only after a user opens the overlay (not on import).

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        self.refresh()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def set_display_options(self, mode: str, font_px: int, max_lines: int, opacity: int):
        if (mode not in ("both", "original", "translation") or
                type(font_px) is not int or not 12 <= font_px <= 32 or
                type(max_lines) is not int or not 1 <= max_lines <= 6 or
                type(opacity) is not int or not 50 <= opacity <= 100):
            raise ValueError("INVALID_CAPTION_DISPLAY_OPTIONS")
        self._display_mode, self._max_lines = mode, max_lines
        font = QFont(self.de_label.font())
        font.setPixelSize(font_px)
        self.de_label.setFont(font)
        self.ru_label.setFont(font)
        self.setWindowOpacity(opacity / 100)
        self.refresh()

    def refresh(self):
        snapshot = self.controller.snapshot()
        original, translation = format_caption(snapshot, self._max_lines)
        self.de_label.setText(original)
        self.ru_label.setText(translation)
        translated = snapshot.target_language is not None
        self.de_label.setVisible(self._display_mode != "translation" or not translated)
        self.ru_label.setVisible(translated and self._display_mode != "original")
        self._rendered_generation = snapshot.generation

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        self._timer.stop()
        self.controller.stop()  # cancel and clear all transient private captions
        self.de_label.clear()
        self.ru_label.clear()
        super().closeEvent(event)
        self.closed.emit()  # live owner must also stop the capture transport
