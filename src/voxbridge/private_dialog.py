"""Consistent private-state cleanup for Escape, close, accept/reject and done."""
from PySide6.QtWidgets import QDialog


class PrivateDialog(QDialog):
    _dismissing = False

    def _dismiss_private(self):
        """Subclass clears/fences transient state without closing itself."""

    def done(self, result):
        if self._dismissing:
            return super().done(result)
        self._dismissing = True
        try:
            self._dismiss_private()
            super().done(result)
        finally:
            self._dismissing = False

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def accept(self):
        self.done(QDialog.DialogCode.Accepted)

    def closeEvent(self, event):
        if self._dismissing:
            return super().closeEvent(event)
        self._dismissing = True
        try:
            self._dismiss_private()
            super().closeEvent(event)
        finally:
            self._dismissing = False
