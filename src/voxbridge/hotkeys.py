"""Dedicated Win32 WM_HOTKEY thread: no global keystroke logging/hooks."""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
PM_REMOVE = 1
QS_HOTKEY = 0x0080
MWMO_INPUTAVAILABLE = 4
WAIT_TIMEOUT = 258
HOTKEY_POLL_MS = 100
HOTKEY_ID = 0xB17

_KEYS = {"SPACE": 0x20, **{f"F{i}": 0x6F + i for i in range(1, 13)}}
_MODS = {"CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL, "ALT": MOD_ALT,
         "SHIFT": MOD_SHIFT, "WIN": MOD_WIN, "WINDOWS": MOD_WIN}


def parse_hotkey(text: str) -> tuple[int, int]:
    if not isinstance(text, str):
        raise ValueError("INVALID_HOTKEY")
    bits = [part.strip().upper() for part in text.split("+")]
    if len(bits) < 2 or len(bits) > 5 or any(not x for x in bits):
        raise ValueError("INVALID_HOTKEY")
    modifiers = 0
    for name in bits[:-1]:
        if name not in _MODS or modifiers & _MODS[name]:
            raise ValueError("INVALID_HOTKEY_MODIFIER")
        modifiers |= _MODS[name]
    key = bits[-1]
    vk = _KEYS.get(key)
    if vk is None and len(key) == 1 and ("A" <= key <= "Z" or "0" <= key <= "9"):
        vk = ord(key)
    if not modifiers or vk is None:
        raise ValueError("INVALID_HOTKEY_KEY")
    return modifiers | MOD_NOREPEAT, vk


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD), ("pt", POINT), ("lPrivate", wintypes.DWORD),
    ]


class HotkeyListener:
    """Own one registration; cancellation never waits on the GUI thread."""

    def __init__(self, key_combo: str, callback: Callable[[int], None]):
        self.modifiers, self.vk = parse_hotkey(key_combo)
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._error = ""

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if os.name != "nt":
            raise RuntimeError("WINDOWS_ONLY")
        if self.running:
            raise RuntimeError("HOTKEY_ALREADY_RUNNING")
        self._ready.clear()
        self._error = ""
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._loop, name="voxbridge-hotkeys", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None
            raise RuntimeError("HOTKEY_THREAD_START_FAILED") from None
        if not self._ready.wait(3):
            self.stop()
            raise RuntimeError("HOTKEY_THREAD_TIMEOUT")
        if self._error:
            self.stop()
            raise RuntimeError(self._error)

    def _loop(self):
        user32 = None
        registered = False
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user32.PeekMessageW.restype = wintypes.BOOL
            user32.MsgWaitForMultipleObjectsEx.argtypes = [
                wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD]
            user32.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            msg = MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            if self._stop_requested.is_set():
                self._error = "HOTKEY_CANCELLED"
                return
            if not user32.RegisterHotKey(None, HOTKEY_ID, self.modifiers, self.vk):
                self._error = "HOTKEY_IN_USE_OR_UNAVAILABLE"
                return
            registered = True
            self._ready.set()
            # No cross-thread WM_QUIT or thread-ID reuse race. The native wait
            # wakes immediately for a hotkey and periodically checks cancellation.
            while not self._stop_requested.is_set():
                status = user32.MsgWaitForMultipleObjectsEx(
                    0, None, HOTKEY_POLL_MS, QS_HOTKEY, MWMO_INPUTAVAILABLE)
                if status == WAIT_TIMEOUT:
                    continue
                if status != 0:
                    self._error = "HOTKEY_MESSAGE_WAIT_FAILED"
                    break
                while not self._stop_requested.is_set() and user32.PeekMessageW(
                        ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
                    if msg.message == WM_QUIT:
                        return
                    if (msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID
                            and not self._stop_requested.is_set()):
                        hwnd = int(user32.GetForegroundWindow() or 0)
                        try:
                            self.callback(hwnd)
                        except Exception:
                            pass  # never log content from hotkey callbacks
        except Exception:
            self._error = "HOTKEY_NATIVE_FAILED"
        finally:
            try:
                if registered:
                    if not user32.UnregisterHotKey(None, HOTKEY_ID):
                        self._error = "HOTKEY_UNREGISTER_FAILED"
            except Exception:
                self._error = "HOTKEY_UNREGISTER_FAILED"
            finally:
                self._ready.set()

    def stop(self):
        self._stop_requested.set()
        if not self.running:
            self._thread = None

    def wait_closed(self, timeout=0.25):
        """Bounded join for explicit shutdown/CLI verification, not GUI toggles."""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))
        if not self.running:
            self._thread = None
            return True
        return False
