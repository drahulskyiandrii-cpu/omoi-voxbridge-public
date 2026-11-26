"""Clipboard-free SendInput Unicode injection into an unchanged Win32 target."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
ES_PASSWORD = 0x0020
ES_READONLY = 0x0800
# Native edit controls use Win32 style checks; other editors require a
# verified UI Automation logical focus, not just a matching window handle.
NATIVE_EDIT_CLASSES = frozenset({"edit", "richedit20w", "richedit50w", "richeditd2dpt"})  # Windows 11 Notepad
GWL_STYLE = -16


class InjectionError(RuntimeError):
    pass


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def key_events(text: str) -> list[INPUT]:
    if (not isinstance(text, str) or not text.strip() or len(text) > 3000
            or any(ord(ch) < 32 and ch not in "\n\r\t" for ch in text)):
        raise InjectionError("INVALID_INSERT_TEXT")
    # UTF-16 code units include surrogate pairs for emoji / astral characters.
    try:
        raw = text.encode("utf-16-le")
    except UnicodeError:
        raise InjectionError("INVALID_UNICODE_TEXT") from None
    codes = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    events = []
    for c in codes:
        for flag in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            event = INPUT()
            event.type = INPUT_KEYBOARD
            event.u.ki = KEYBDINPUT(0, c, flag, 0, 0)
            events.append(event)
    return events


def _focused_is_password(user32, hwnd: int) -> tuple[bool, int]:
    # The native check catches Win32 edit/password fields, not all browser controls.
    # For other app types, a future UIA provider is required.
    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    thread_id = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
    if not thread_id:
        raise InjectionError("TARGET_NOT_AVAILABLE")
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        raise InjectionError("FOCUS_NOT_VERIFIED")
    focus = info.hwndFocus
    if not focus:
        raise InjectionError("FOCUS_NOT_VERIFIED")
    name = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    if not user32.GetClassNameW(wintypes.HWND(focus), name, 128):
        raise InjectionError("FOCUS_NOT_VERIFIED")
    if name.value.lower() in NATIVE_EDIT_CLASSES:
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        ctypes.set_last_error(0)
        style = user32.GetWindowLongW(wintypes.HWND(focus), GWL_STYLE)
        if not style and ctypes.get_last_error():
            raise InjectionError("FOCUS_NOT_VERIFIED")
        if style & ES_PASSWORD:
            return True, int(focus)
    return False, int(focus)


def capture_focus(hwnd: int) -> int:
    """Capture the keyboard-focus HWND before the recording indicator appears."""
    if os.name != "nt" or hwnd <= 0:
        raise InjectionError("INVALID_TARGET")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    if int(user32.GetForegroundWindow() or 0) != hwnd:
        raise InjectionError("TARGET_FOCUS_CHANGED")
    password, focus = _focused_is_password(user32, hwnd)
    if password:
        raise InjectionError("PASSWORD_FIELD_BLOCKED")
    return focus


def _native_edit_style(user32, focus: int) -> int:
    name = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    if (not user32.GetClassNameW(wintypes.HWND(focus), name, 128)
            or name.value.lower() not in NATIVE_EDIT_CLASSES):
        raise InjectionError("UNVERIFIED_TEXT_CONTROL")
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    ctypes.set_last_error(0)
    style = user32.GetWindowLongW(wintypes.HWND(focus), GWL_STYLE)
    if not style and ctypes.get_last_error():
        raise InjectionError("UNVERIFIED_TEXT_CONTROL")
    if style & ES_PASSWORD:
        raise InjectionError("PASSWORD_FIELD_BLOCKED")
    if style & ES_READONLY:
        raise InjectionError("READONLY_FIELD_BLOCKED")
    return style


def _require_normal_target(user32, hwnd: int, focus: int) -> int:
    # Reuse the selection module's fail-closed process token guard without
    # introducing a module-import cycle.
    from .selection import _require_normal_process, SelectionFailure
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    owner, field = wintypes.DWORD(), wintypes.DWORD()
    if (not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            or not user32.GetWindowThreadProcessId(focus, ctypes.byref(field))
            or not owner.value or owner.value != field.value or owner.value == os.getpid()):
        raise InjectionError("UNVERIFIED_TARGET_PROCESS")
    try:
        _require_normal_process(owner.value)
    except (SelectionFailure, OSError):
        raise InjectionError("UNVERIFIED_TARGET_PROCESS") from None
    return owner.value


def _verify_target(user32, hwnd: int, expected_focus: int, logical_verify=None) -> int:
    if not hwnd or not user32.IsWindow(hwnd) or int(user32.GetForegroundWindow() or 0) != hwnd:
        raise InjectionError("TARGET_FOCUS_CHANGED")
    password, focus = _focused_is_password(user32, hwnd)
    if password:
        raise InjectionError("PASSWORD_FIELD_BLOCKED")
    if not expected_focus or focus != expected_focus:
        raise InjectionError("TEXT_FIELD_CHANGED")
    if logical_verify is None:
        _native_edit_style(user32, focus)
    else:
        logical_verify()
    owner = _require_normal_target(user32, hwnd, focus)
    for key in (0x10, 0x11, 0x12, 0x5B, 0x5C):
        if user32.GetAsyncKeyState(key) & 0x8000:
            raise InjectionError("MODIFIER_STILL_PRESSED")
    if int(user32.GetForegroundWindow() or 0) != hwnd:
        raise InjectionError("TARGET_FOCUS_CHANGED")
    return owner


def insert_text(hwnd: int, text: str, expected_focus: int = 0, *, logical_verify=None, cancel_check=None) -> int:
    if os.name != "nt":
        raise InjectionError("WINDOWS_ONLY")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    data = key_events(text)
    inserted = 0
    owner = None
    # Each batch repeats focus, native edit style, process integrity and modifier
    # checks. SendInput still has an unavoidable OS-level focus race.
    offset = 0
    while offset < len(data):
        try:
            if cancel_check is not None:
                cancel_check()
            current_owner = _verify_target(user32, hwnd, expected_focus, logical_verify)
            if owner is not None and owner != current_owner:
                raise InjectionError("TARGET_FOCUS_CHANGED")
            owner = current_owner
            if cancel_check is not None:
                cancel_check()
        except InjectionError:
            if inserted:
                raise InjectionError("PARTIAL_INSERT_POSSIBLE") from None
            raise
        end = min(offset + 64, len(data))
        # Never separate a UTF-16 high surrogate from its low surrogate.
        if end < len(data) and 0xD800 <= data[end - 2].u.ki.wScan <= 0xDBFF:
            end -= 2
        block = data[offset:end]
        array = (INPUT * len(block))(*block)
        sent = int(user32.SendInput(len(block), array, ctypes.sizeof(INPUT)))
        if sent != len(block):
            raise InjectionError("PARTIAL_INSERT_POSSIBLE" if inserted or sent else "WINDOWS_SENDINPUT_FAILED")
        inserted += sent
        offset = end
    return inserted // 2

def replace_native_text(hwnd: int, focus: int, text: str, *, cancel_check=None) -> int:
    """Single undoable native edit insertion. No emulated character repeats."""
    if os.name != "nt":
        raise InjectionError("WINDOWS_ONLY")
    events = key_events(text)  # validates Unicode and bounds; no keyboard events sent
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.IsWindowUnicode.argtypes = [wintypes.HWND]
    user32.IsWindowUnicode.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    if cancel_check is not None:
        cancel_check()
    _verify_target(user32, hwnd, focus)
    if not user32.IsWindowUnicode(focus):
        raise InjectionError("NATIVE_UNICODE_UNSUPPORTED")
    value = ctypes.create_unicode_buffer(text)
    result = ctypes.c_size_t()
    if cancel_check is not None:
        cancel_check()
    # EM_REPLACESEL is a system message below WM_USER; Windows marshals its
    # string to the target process. No WM_SETTEXT, no full-document replacement.
    # Do not retry on timeout: execution may already have begun.
    if not user32.SendMessageTimeoutW(focus, 0x00C2, 1, ctypes.addressof(value),
                                      0x0001 | 0x0002, 1500, ctypes.byref(result)):
        raise InjectionError("PARTIAL_INSERT_POSSIBLE")
    return len(events) // 2
