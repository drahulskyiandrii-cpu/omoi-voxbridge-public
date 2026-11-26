"""GUI-side lifecycle for an optional selection hotkey. No automatic requests."""
from __future__ import annotations

import importlib.util
import threading

from PySide6.QtCore import QObject, Signal, Slot

from .hotkeys import HotkeyListener
from .selection import capture_target, capture_selection, SelectionFailure, SAFE_CODES
from .analysis_providers import ProviderFailure


class SelectionController(QObject):
    hotkey = Signal(int, object)
    completed = Signal(int, object, str)

    def __init__(self, *, can_start, on_preview, notify, parent=None,
                 listener_factory=HotkeyListener, target_factory=capture_target,
                 reader=capture_selection, available=None, on_repeat=None):
        super().__init__(parent)
        self._on_repeat = on_repeat
        self._can_start = can_start
        self._preview = on_preview
        self._notify = notify
        self._listener_factory = listener_factory
        self._target_factory = target_factory
        self._reader = reader
        self._available = available or (lambda: importlib.util.find_spec("comtypes") is not None)
        self._listener = None
        self._retiring = None
        self._registration = 0
        self._generation = 0
        self._worker_generation = None
        self._target = None
        self._stop = threading.Event()
        self.hotkey.connect(self._registered_hotkey)
        self.completed.connect(self._completed)

    @property
    def enabled(self):
        return self._listener is not None

    @property
    def busy(self):
        return self._worker_generation is not None

    @property
    def shutdown_pending(self):
        return self.busy or (self._retiring is not None and self._retiring.running is True)

    def enable(self, combo: str):
        if self.enabled:
            return
        if not self._available():
            raise SelectionFailure("SELECTION_SDK_NOT_INSTALLED")
        if self._retiring is not None:
            if self._retiring.running:
                raise SelectionFailure("SELECTION_HOTKEY_CLEANUP_PENDING")
            self._retiring = None
        self._registration += 1
        registration = self._registration
        listener = self._listener_factory(combo, lambda hwnd: self.hotkey.emit(registration, hwnd))
        try:
            listener.start()
        except Exception:
            try:
                listener.stop()
            except Exception:
                pass
            self._retiring = listener
            raise SelectionFailure("SELECTION_HOTKEY_UNAVAILABLE") from None
        self._listener = listener

    def cancel(self):
        self._generation += 1
        self._stop.set()

    def disable(self):
        self.cancel()
        self._registration += 1
        listener, self._listener = self._listener, None
        if listener is not None:
            self._retiring = listener
            listener.stop()

    def _allowed(self):
        try:
            return self._can_start() is True
        except Exception:
            return False

    @Slot(int, object)
    def _registered_hotkey(self, registration, hwnd):
        if registration == self._registration:
            self.on_hotkey(hwnd)

    @Slot(object)
    def on_hotkey(self, hwnd):
        if not self.enabled:
            return
        if self._on_repeat is not None and self._on_repeat():
            return
        if self.busy:
            return
        if not self._allowed():
            self._notify("Auswahl nicht möglich: laufender Vorgang oder Text im Analysefenster.")
            return
        try:
            target = self._target_factory(hwnd)
        except SelectionFailure as exc:
            self._notify(str(exc) if str(exc) in SAFE_CODES else "SELECTION_NATIVE_FAILED")
            return
        except Exception:
            self._notify("SELECTION_NATIVE_FAILED")
            return
        self._generation += 1
        generation = self._generation
        self._worker_generation = generation
        self._target = target
        self._stop = threading.Event()
        stop = self._stop

        def worker():
            text, code = "", ""
            try:
                text = self._reader(target, stop)
            except SelectionFailure as exc:
                code = str(exc) if str(exc) in SAFE_CODES else "SELECTION_NATIVE_FAILED"
            except Exception:
                code = "SELECTION_NATIVE_FAILED"
            try:
                self.completed.emit(generation, text, code)
            except RuntimeError:
                pass

        try:
            threading.Thread(target=worker, name="voxbridge-selection-parent",
                             daemon=True).start()
        except RuntimeError:
            self._completed(generation, "", "SELECTION_WORKER_START_FAILED")

    @Slot(int, object, str)
    def _completed(self, generation, text, code):
        if generation != self._worker_generation:
            return
        self._worker_generation = None
        target, self._target = self._target, None
        if generation != self._generation or self._stop.is_set() or not self.enabled:
            return
        if code:
            self._notify(code)
            return
        if not self._allowed():
            return
        try:
            if self._target_factory(target.hwnd) != target:
                raise SelectionFailure("SELECTION_TARGET_CHANGED")
        except Exception:
            self._notify("SELECTION_TARGET_CHANGED")
            return
        try:
            self._preview(text)
        except ProviderFailure as exc:
            code = str(exc)
            self._notify(code if code.isascii() and code.isupper() and len(code) <= 80 else "SELECTION_ACTION_FAILED")
        except Exception:
            self._notify("SELECTION_ACTION_FAILED")
