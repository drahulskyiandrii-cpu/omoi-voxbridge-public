"""Focused editable controls, including browser contenteditable and Word.

UI Automation is queried for metadata/range attributes only, never text or
clipboard contents. COM runs in an owned, bounded worker process. Insertion
uses Unicode SendInput at the current caret of the same verified control.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import multiprocessing
import sys
import threading
import time

from .injection import InjectionError, _native_edit_style, insert_text, key_events, replace_native_text
from .selection import SelectionTarget, SelectionFailure, capture_target, _require_normal_process

EDIT, DOCUMENT = 50004, 50030
IS_PASSWORD, VALUE_READONLY, TEXT_READONLY = 30019, 30046, 40015
TEXT_PATTERN = 10014
TEXT_PATTERN2 = 10024

# These are compatibility failures, not positive evidence of a protected field.
# The app may offer local, manual-copy dictation only; never automatic insertion.
MANUAL_LOCAL_CODES = frozenset({
    "UIA_TARGET_UNVERIFIED", "UIA_NO_EDITABLE_FIELD",
    "UIA_IDENTITY_UNVERIFIED", "UIA_CARET_UNVERIFIED",
})


@dataclass(frozen=True)
class EditIdentity:
    target: SelectionTarget
    runtime_id: tuple[int, ...] = ()
    element_pid: int = 0


def _require_current(target):
    try:
        actual = capture_target(target.hwnd, target.owner_pid)
    except SelectionFailure as exc:
        raise InjectionError(str(exc)) from None
    if actual != target:
        raise InjectionError("TEXT_FIELD_CHANGED")


def _runtime_id(element):
    value = element.GetRuntimeId()
    if (not isinstance(value, (tuple, list)) or not 2 <= len(value) <= 64
            or any(type(n) is not int or not -(2**31) <= n < 2**31 for n in value)):
        raise InjectionError("UIA_IDENTITY_UNVERIFIED")
    return tuple(value)


def _not_password(element):
    explicit = element.GetCurrentPropertyValueEx(IS_PASSWORD, True)
    if explicit is True:
        return False
    if explicit is False:
        return True
    # UIA documents IsPassword=False as its default; conforming password
    # edit providers MUST report True. Still require positive editable proof.
    default = element.CurrentIsPassword
    return type(default) in (bool, int) and default == 0


def focused_edit_identity(automation, target, *, verify_current, require_normal,
                          text_interface, expected=None, native_link=None):
    """Pure UIA contract. No GetText, CurrentValue, SetValue or focus changes."""
    try:
        verify_current()
        element = automation.GetFocusedElement()
        if not element:
            raise InjectionError("UIA_NO_EDITABLE_FIELD")
        if (not _not_password(element)
                or not element.CurrentHasKeyboardFocus or not element.CurrentIsEnabled):
            raise InjectionError("UIA_PROTECTED_OR_UNVERIFIED")
        if element.CurrentControlType not in (EDIT, DOCUMENT):
            raise InjectionError("UIA_NO_EDITABLE_FIELD")
        pid = element.CurrentProcessId
        if type(pid) is not int or pid <= 0 or pid == target.owner_pid:
            raise InjectionError("UIA_PROTECTED_OR_UNVERIFIED")
        require_normal(pid)
        # Browser accessibility elements may belong to a renderer process.
        # Require a bounded ancestry link to the actual native focus or root.
        walker = automation.RawViewWalker
        ancestor = element
        for _ in range(32):
            if not ancestor:
                raise InjectionError("UIA_TARGET_UNVERIFIED")
            if not _not_password(ancestor):
                raise InjectionError("UIA_PROTECTED_OR_UNVERIFIED")
            handle = ancestor.CurrentNativeWindowHandle
            if ancestor.CurrentProcessId == target.process_id and (
                    handle in (target.focus, target.hwnd) or
                    (native_link is not None and native_link(handle, target))):
                break
            ancestor = walker.GetParentElement(ancestor)
        else:
            raise InjectionError("UIA_TARGET_UNVERIFIED")
        identity = EditIdentity(target, _runtime_id(element), pid)
        if expected is not None and identity != expected:
            raise InjectionError("TEXT_FIELD_CHANGED")

        # Standard editable inputs (including browser contenteditable) may
        # expose Value.IsReadOnly. Never read the actual Value.
        readonly = element.GetCurrentPropertyValueEx(VALUE_READONLY, True)
        if element.CurrentControlType == EDIT and readonly is False:
            pass
        elif readonly is True:
            raise InjectionError("READONLY_FIELD_BLOCKED")
        else:
            # Word/document editors: check the current selection/caret range's
            # read-only attribute, not document text or the whole document.
            unknown = element.GetCurrentPattern(TEXT_PATTERN)
            if not unknown:
                unknown = element.GetCurrentPattern(TEXT_PATTERN2)
            if not unknown:
                raise InjectionError("UIA_NO_EDITABLE_FIELD")
            pattern = unknown.QueryInterface(text_interface)
            ranges = pattern.GetSelection()
            if not ranges or ranges.Length != 1:
                raise InjectionError("UIA_CARET_UNVERIFIED")
            current_range = ranges.GetElement(0)
            if not current_range or current_range.GetAttributeValue(TEXT_READONLY) is not False:
                raise InjectionError("READONLY_OR_UNVERIFIED_FIELD")
        verify_current()
        current = automation.GetFocusedElement()
        if (not current or not automation.CompareElements(element, current)
                or _runtime_id(current) != identity.runtime_id
                or not _not_password(current)
                or not current.CurrentHasKeyboardFocus or not current.CurrentIsEnabled):
            raise InjectionError("TEXT_FIELD_CHANGED")
        return identity
    except InjectionError:
        raise
    except SelectionFailure:
        raise InjectionError("UIA_PROTECTED_OR_UNVERIFIED") from None
    except Exception:
        raise InjectionError("UIA_CHECK_FAILED") from None


def _automation():
    sys.coinit_flags = 0
    try:
        from comtypes.client import CreateObject, GetModule
    except ImportError:
        raise InjectionError("UIA_SDK_NOT_INSTALLED") from None
    uia = GetModule("UIAutomationCore.dll")
    return CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation), uia.IUIAutomationTextPattern


def _related_native_anchor(user, handle, target):
    """A real same-process HWND on the captured focus branch, not a sibling.

    HWND metadata from UIA alone is insufficient. Verify the live Win32
    process and ancestry before accepting a provider's intermediate anchor.
    """
    if type(handle) is not int or handle <= 0:
        return False
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    user.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    user.IsChild.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    if not user.IsWindow(handle) or not user.IsChild(target.hwnd, handle):
        return False
    pid = wintypes.DWORD()
    if not user.GetWindowThreadProcessId(handle, ctypes.byref(pid)) or pid.value != target.process_id:
        return False
    return bool(handle == target.focus or target.focus == target.hwnd
                or user.IsChild(handle, target.focus) or user.IsChild(target.focus, handle))


def _native_or_uia(target, expected=None, stop=None, text=None):
    def check_cancel():
        if stop is not None and stop.is_set():
            raise InjectionError("EDIT_CANCELLED")
        _require_current(target)

    check_cancel()
    user = ctypes.WinDLL("user32", use_last_error=True)
    try:
        _native_edit_style(user, target.focus)
        native = True
    except InjectionError as exc:
        if str(exc) != "UNVERIFIED_TEXT_CONTROL":
            raise
        native = False
    if native:
        identity = EditIdentity(target)
        if expected is not None and identity != expected:
            raise InjectionError("TEXT_FIELD_CHANGED")
        check_cancel()
        if text is not None:
            replace_native_text(target.hwnd, target.focus, text, cancel_check=check_cancel)
        return identity
    automation, interface = _automation()

    def verify():
        return focused_edit_identity(
            automation, target, verify_current=check_cancel,
            require_normal=_require_normal_process, text_interface=interface, expected=expected,
            native_link=lambda handle, target: _related_native_anchor(user, handle, target))

    identity = verify()
    if text is not None:
        # Recheck the same logical element in the sending process before
        # every batch, not merely the browser's native window handle.
        insert_text(target.hwnd, text, target.focus, logical_verify=verify,
                    cancel_check=check_cancel)
    return identity


def _edit_child(sender, stop, target, expected, text):
    try:
        identity = _native_or_uia(target, expected, stop, text)
        packet = {"runtime_id": list(identity.runtime_id), "element_pid":identity.element_pid}
    except Exception as exc:
        code = str(exc) if isinstance(exc, InjectionError) else "EDIT_CHECK_FAILED"
        packet = {"error":code if code.isascii() and code.isupper() and len(code) <= 80 else "EDIT_CHECK_FAILED"}
    try:
        sender.send_bytes(json.dumps(packet).encode("ascii"))
    except OSError:
        pass
    finally:
        sender.close()


def run_edit_job(target: SelectionTarget, cancel: threading.Event, *,
                 expected: EditIdentity | None = None, text: str | None = None,
                 timeout: float = 6.0, _worker=_edit_child) -> EditIdentity:
    """Verify or insert in an owned Windows spawn child, returning only metadata."""
    if (not isinstance(target, SelectionTarget) or not isinstance(cancel, threading.Event)
            or not 0.1 <= timeout <= 30 or (text is not None and
                (not isinstance(expected, EditIdentity) or expected.target != target))):
        raise InjectionError("EDIT_INVALID_JOB")
    if text is not None:
        # Dictation must not activate Enter/Tab behavior in web chats.
        key_events(text)  # bound and validate before creating a child
        text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if cancel.is_set():
        raise InjectionError("EDIT_CANCELLED")
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    stop = ctx.Event()
    child = ctx.Process(target=_worker, args=(sender, stop, target, expected, text),
                        name="voxbridge-edit-target", daemon=True)
    started = False
    deadline = time.monotonic() + timeout
    uncertain = "PARTIAL_INSERT_POSSIBLE" if text is not None else "EDIT_CHECK_TIMEOUT"
    try:
        child.start()
        started = True
        sender.close()
        while True:
            if cancel.is_set():
                raise InjectionError("EDIT_CANCELLED")
            if time.monotonic() >= deadline:
                raise InjectionError(uncertain)
            if not receiver.poll(0.025):
                if not child.is_alive():
                    raise InjectionError(uncertain)
                continue
            try:
                data = json.loads(receiver.recv_bytes(2048))
            except (ValueError, OSError, EOFError):
                raise InjectionError(uncertain) from None
            if cancel.is_set():
                raise InjectionError("EDIT_CANCELLED")
            if time.monotonic() >= deadline:
                raise InjectionError(uncertain)
            if isinstance(data, dict) and set(data) == {"error"}:
                code = data["error"]
                if isinstance(code, str) and code.isascii() and code.isupper() and len(code) <= 80:
                    raise InjectionError(code)
            if not isinstance(data, dict) or set(data) != {"runtime_id", "element_pid"}:
                raise InjectionError(uncertain)
            runtime, pid = data["runtime_id"], data["element_pid"]
            if (not isinstance(runtime, list) or len(runtime) > 64
                    or any(type(n) is not int or not -(2**31) <= n < 2**31 for n in runtime)
                    or type(pid) is not int or pid < 0
                    or (runtime and (len(runtime) < 2 or not pid))
                    or (not runtime and pid)):
                raise InjectionError(uncertain)
            identity = EditIdentity(target, tuple(runtime), pid)
            if expected is not None and identity != expected:
                raise InjectionError(uncertain)
            return identity
    except InjectionError:
        raise
    except Exception:
        raise InjectionError(uncertain if started else "EDIT_WORKER_START_FAILED") from None
    finally:
        stop.set()
        sender.close()
        receiver.close()
        if started:
            child.join(0.3)
            if child.is_alive():
                child.terminate()
                child.join(1)
            if child.is_alive():
                child.kill()
                child.join(1)
            if not child.is_alive():
                child.close()
