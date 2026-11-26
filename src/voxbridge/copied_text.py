"""Explicit read-copied-text fallback, never a clipboard monitor or writer."""
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
from .selection import SelectionFailure, SAFE_CODES, _validated_text, _capture_text_job
from .analysis import MAX_TEXT_CHARS

CF_UNICODETEXT = 13
COPY_READ_HOTKEY = "Ctrl+Alt+Shift+S"

@dataclass(frozen=True)
class CopiedTextTarget:
    sequence: int

def _sequence():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetClipboardSequenceNumber.restype = wintypes.DWORD
    return int(user.GetClipboardSequenceNumber())

def capture_copy_target(_hwnd=None):
    sequence = _sequence()
    if not sequence:
        raise SelectionFailure("CLIPBOARD_UNAVAILABLE")
    return CopiedTextTarget(sequence)

def _read_text(target):
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.OpenClipboard.restype = wintypes.BOOL
    user.CloseClipboard.restype = wintypes.BOOL
    user.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user.GetClipboardData.argtypes = [wintypes.UINT]
    user.GetClipboardData.restype = wintypes.HANDLE
    kernel.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel.GlobalSize.restype = ctypes.c_size_t
    kernel.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HANDLE]
    if _sequence() != target.sequence:
        raise SelectionFailure("CLIPBOARD_CHANGED")
    if not user.OpenClipboard(None):
        raise SelectionFailure("CLIPBOARD_UNAVAILABLE")
    try:
        if _sequence() != target.sequence:
            raise SelectionFailure("CLIPBOARD_CHANGED")
        if not user.IsClipboardFormatAvailable(CF_UNICODETEXT):
            raise SelectionFailure("CLIPBOARD_NO_TEXT")
        handle = user.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            raise SelectionFailure("CLIPBOARD_NO_TEXT")
        size = kernel.GlobalSize(handle)
        if size < 2:
            raise SelectionFailure("CLIPBOARD_NO_TEXT")
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise SelectionFailure("CLIPBOARD_UNAVAILABLE")
        try:
            # Bounded read, including one possible terminator; reject rather
            # than silently speak a truncated selection. UTF-16LE on Windows.
            raw = ctypes.string_at(pointer, min(size-size%2, (MAX_TEXT_CHARS*2+1)*2))
            end = next((i for i in range(0,len(raw),2) if raw[i:i+2] == b"\x00\x00"), -1)
            if end < 0:
                raise SelectionFailure("SELECTION_INVALID_TEXT")
            try:
                text = _validated_text(raw[:end].decode("utf-16-le"))
            except UnicodeError:
                raise SelectionFailure("SELECTION_INVALID_TEXT") from None
        finally:
            kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()
    if _sequence() != target.sequence:
        raise SelectionFailure("CLIPBOARD_CHANGED")
    return text

def _clipboard_child(sender, target):
    try:
        sender.send_bytes(b"T"+_read_text(target).encode("utf-8"))
    except Exception as exc:
        code = str(exc) if isinstance(exc,SelectionFailure) else "CLIPBOARD_UNAVAILABLE"
        try:
            sender.send_bytes(b"E"+(code if code in SAFE_CODES else "CLIPBOARD_UNAVAILABLE").encode("ascii"))
        except OSError:
            pass
    finally:
        sender.close()

def capture_copied_text(target, stop):
    if not isinstance(target,CopiedTextTarget):
        raise SelectionFailure("CLIPBOARD_UNAVAILABLE")
    return _capture_text_job(target, stop, timeout=4.0, _worker=_clipboard_child)
