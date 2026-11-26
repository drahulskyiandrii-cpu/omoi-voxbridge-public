"""Non-activating dictation status and explicit manual-copy fallback."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
)


class StatusWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.ToolTip | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("QWidget { background: #222833; color: white; border-radius: 10px; }")
        layout = QHBoxLayout(self)
        self.label = QLabel("VoxBridge")
        self.label.setTextFormat(Qt.PlainText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.resize(270, 60)

    def update_state(self, text: str):
        self.label.setText(text)
        self.label.setMaximumWidth(420)
        self.adjustSize()
        screen = QApplication.primaryScreen()
        if screen:
            r = screen.availableGeometry()
            self.move(max(r.left(), r.right() - self.width() - 20), max(r.top(), r.bottom() - self.height() - 40))
        self.show()


def show_manual_fallback(text: str, reason: str):
    """Display ephemeral text to copy manually; never copy without a click."""
    dialog = QDialog()
    dialog.setWindowTitle("VoxBridge — Text nicht eingefügt")
    layout = QVBoxLayout(dialog)
    note = reason + "\nText bleibt nur bis zum Schließen in diesem Fenster."
    if reason == "PARTIAL_INSERT_POSSIBLE":
        note += "\nEin Teil kann bereits eingefügt sein. Vor dem Kopieren prüfen. / Часть текста могла быть вставлена; сначала проверьте."
    if reason.startswith("INCOMPLETE_DICTATION:"):
        note += "\nNur bisher erkannte Abschnitte. / Только уже распознанные фрагменты; запись не завершена."
    elif reason == "LONG_TEXT_MANUAL_COPY":
        note += "\nLanger Text: vollständig manuell kopieren. / Длинный текст: скопируйте его полностью вручную."
    details = QLabel(note)
    details.setTextFormat(Qt.PlainText)
    details.setWordWrap(True)
    layout.addWidget(details)
    edit = QTextEdit()
    edit.setPlainText(text)
    layout.addWidget(edit)
    buttons = QHBoxLayout()
    copy = QPushButton("Kopieren")
    close = QPushButton("Schließen")
    def copy_and_close():
        QApplication.clipboard().setText(edit.toPlainText())
        dialog.accept()
    copy.clicked.connect(copy_and_close)
    close.clicked.connect(dialog.accept)
    buttons.addWidget(copy)
    buttons.addWidget(close)
    layout.addLayout(buttons)
    dialog.resize(520, 240)
    dialog.exec()
