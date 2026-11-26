"""Opt-in, selection-only Windows UI Automation boundary.

Never reads clipboard, document ranges, screenshots, passwords or full windows.
Native use is OFF by default; explicit experimental opt-in only.
Actual Windows application compatibility and native acceptance remain unverified. All content stays in RAM.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import multiprocessing
import os
import sys
import threading
import time

from .analysis import MAX_TEXT_CHARS, AnalysisRequest, AnalysisError
from .injection import capture_focus, InjectionError

UIA_IS_PASSWORD = 30019
UIA_TEXT_PATTERN = 10014
UIA_TEXT_PATTERN2 = 10024
MAX_WIRE_BYTES = MAX_TEXT_CHARS * 4 + 1
SAFE_CODES = frozenset({
    "SELECTION_INVALID_TARGET", "SELECTION_TARGET_CHANGED",
    "SELECTION_PROTECTED_OR_UNKNOWN", "SELECTION_NOT_SUPPORTED",
    "SELECTION_EMPTY_OR_MULTIPLE", "SELECTION_EMBEDDED_CONTROL",
    "SELECTION_INVALID_TEXT", "SELECTION_SDK_NOT_INSTALLED",
    "SELECTION_NATIVE_FAILED", "SELECTION_TIMEOUT", "SELECTION_CANCELLED",
    "SELECTION_CHILD_EXITED", "SELECTION_PROTOCOL", "SELECTION_HOTKEY_UNAVAILABLE",
    "CLIPBOARD_CHANGED", "CLIPBOARD_UNAVAILABLE", "CLIPBOARD_NO_TEXT",
})


class SelectionFailure(RuntimeError):
    """Only a stable error code, never selected text or COM diagnostics."""


@dataclass(frozen=True)
class SelectionTarget:
    hwnd: int
    focus: int
    process_id: int
    owner_pid: int


def _require_normal_process(pid: int) -> None:
    """Fail closed on inaccessible/elevated/high-integrity target processes."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    proc = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not proc:
        raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
    token = wintypes.HANDLE()
    try:
        if not advapi.OpenProcessToken(proc, 0x0008, ctypes.byref(token)):
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        elevation = wintypes.DWORD()
        needed = wintypes.DWORD()
        if (not advapi.GetTokenInformation(token, 20, ctypes.byref(elevation),
                                           ctypes.sizeof(elevation), ctypes.byref(needed))
                or elevation.value != 0):
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        # TokenIntegrityLevel: TOKEN_MANDATORY_LABEL starts with a SID pointer.
        advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        if not ctypes.sizeof(ctypes.c_void_p) <= needed.value <= 4096:
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        data = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 25, data, len(data), ctypes.byref(needed)):
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not sid:
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        count = advapi.GetSidSubAuthorityCount(sid)
        if not count or count.contents.value < 1:
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        rid = advapi.GetSidSubAuthority(sid, count.contents.value - 1)
        if not rid or rid.contents.value >= 0x3000:
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(proc)


def capture_target(hwnd: int, owner_pid: int | None = None) -> SelectionTarget:
    """Capture only native target metadata; never read text."""
    if os.name != "nt" or type(hwnd) is not int or hwnd <= 0:
        raise SelectionFailure("SELECTION_INVALID_TARGET")
    owner_pid = os.getpid() if owner_pid is None else owner_pid
    try:
        focus = capture_focus(hwnd)
    except InjectionError as exc:
        if str(exc) == "PASSWORD_FIELD_BLOCKED":
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN") from None
        raise SelectionFailure("SELECTION_TARGET_CHANGED") from None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    process = wintypes.DWORD()
    focus_process = wintypes.DWORD()
    if (not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
            or not user32.GetWindowThreadProcessId(focus, ctypes.byref(focus_process))
            or not process.value or process.value != focus_process.value
            or process.value == owner_pid):
        raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
    _require_normal_process(process.value)
    return SelectionTarget(hwnd, focus, process.value, owner_pid)


def _validated_text(text: object) -> str:
    try:
        return AnalysisRequest(text).validate().text
    except (AnalysisError, TypeError):
        raise SelectionFailure("SELECTION_INVALID_TEXT") from None


def read_selection(automation, target: SelectionTarget, *, verify_target,
                   text_pattern_interface) -> str:
    """Pure injectable UIA contract. No fallback to whole document or clipboard."""
    def require_current():
        if verify_target() != target:
            raise SelectionFailure("SELECTION_TARGET_CHANGED")

    def require_safe(element):
        # GetCurrentPropertyValueEx(ignoreDefaultValue=True) exposes unknown
        # support rather than silently treating it as IsPassword=False.
        password = element.GetCurrentPropertyValueEx(UIA_IS_PASSWORD, True)
        if (password is not False or element.CurrentProcessId != target.process_id
                or not element.CurrentHasKeyboardFocus):
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")

    def only_range(pattern):
        ranges = pattern.GetSelection()
        if ranges is None or ranges.Length != 1:
            raise SelectionFailure("SELECTION_EMPTY_OR_MULTIPLE")
        return ranges.GetElement(0)

    try:
        require_current()
        element = automation.GetFocusedElement()
        if element is None:
            raise SelectionFailure("SELECTION_NOT_SUPPORTED")
        require_safe(element)
        native_handle = element.CurrentNativeWindowHandle
        if native_handle and native_handle != target.focus:
            raise SelectionFailure("SELECTION_TARGET_CHANGED")
        unknown = element.GetCurrentPattern(UIA_TEXT_PATTERN)
        if not unknown:
            raise SelectionFailure("SELECTION_NOT_SUPPORTED")
        pattern = unknown.QueryInterface(text_pattern_interface)
        selected = only_range(pattern)
        enclosing = selected.GetEnclosingElement()
        if not enclosing or not automation.CompareElements(element, enclosing):
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        children = selected.GetChildren()
        if children is None or children.Length != 0:
            raise SelectionFailure("SELECTION_EMBEDDED_CONTROL")
        # No GetText until ALL initial identity/password/range checks pass.
        require_current()
        require_safe(element)
        text = _validated_text(selected.GetText(MAX_TEXT_CHARS + 1))
        require_current()
        current = automation.GetFocusedElement()
        if not current or not automation.CompareElements(element, current):
            raise SelectionFailure("SELECTION_TARGET_CHANGED")
        require_safe(current)
        if not selected.Compare(only_range(pattern)):
            raise SelectionFailure("SELECTION_TARGET_CHANGED")
        require_current()
        return text
    except SelectionFailure:
        raise
    except Exception:
        raise SelectionFailure("SELECTION_NATIVE_FAILED") from None


def _native_read(target: SelectionTarget) -> str:
    # This runs in an owned disposable process, not the Qt or hotkey thread.
    sys.coinit_flags = 0  # COM MTA
    try:
        from comtypes.client import CreateObject, GetModule
    except ImportError:
        raise SelectionFailure("SELECTION_SDK_NOT_INSTALLED") from None
    uia = GetModule("UIAutomationCore.dll")
    automation = CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    return read_selection(automation, target,
                          verify_target=lambda: capture_target(target.hwnd, target.owner_pid),
                          text_pattern_interface=uia.IUIAutomationTextPattern)


def _selection_child(sender, target):
    try:
        result = _native_read(target)
        sender.send_bytes(b"T" + result.encode("utf-8"))
    except Exception as exc:
        code = str(exc) if isinstance(exc, SelectionFailure) else "SELECTION_NATIVE_FAILED"
        if code not in SAFE_CODES:
            code = "SELECTION_NATIVE_FAILED"
        try:
            sender.send_bytes(b"E" + code.encode("ascii"))
        except OSError:
            pass
    finally:
        sender.close()


def capture_selection(target: SelectionTarget, stop: threading.Event, *,
                      timeout: float = 4.0, _worker=_selection_child) -> str:
    """Bounded binary IPC; only this function's owned child may be terminated."""
    if not isinstance(target, SelectionTarget):
        raise SelectionFailure("SELECTION_INVALID_TARGET")
    return _capture_text_job(target,stop,timeout=timeout,_worker=_worker)


def _capture_text_job(target,stop,*,timeout,_worker):
    if not isinstance(stop, threading.Event) or not 0.1 <= timeout <= 10:
        raise SelectionFailure("SELECTION_INVALID_TARGET")
    if stop.is_set():
        raise SelectionFailure("SELECTION_CANCELLED")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_worker, args=(sender, target),
                            name="voxbridge-selection", daemon=True)
    started = False
    deadline = time.monotonic() + timeout
    try:
        child.start()
        started = True
        sender.close()
        while True:
            if stop.is_set():
                raise SelectionFailure("SELECTION_CANCELLED")
            if time.monotonic() >= deadline:
                raise SelectionFailure("SELECTION_TIMEOUT")
            if not receiver.poll(0.025):
                if not child.is_alive():
                    raise SelectionFailure("SELECTION_CHILD_EXITED")
                continue
            try:
                packet = receiver.recv_bytes(MAX_WIRE_BYTES)
            except EOFError:
                raise SelectionFailure("SELECTION_CHILD_EXITED") from None
            except OSError:
                raise SelectionFailure("SELECTION_PROTOCOL") from None
            if stop.is_set():
                raise SelectionFailure("SELECTION_CANCELLED")
            if time.monotonic() >= deadline:
                raise SelectionFailure("SELECTION_TIMEOUT")
            try:
                if packet[:1] == b"T":
                    return _validated_text(packet[1:].decode("utf-8"))
                if packet[:1] == b"E":
                    code = packet[1:].decode("ascii")
                    if code in SAFE_CODES:
                        raise SelectionFailure(code)
            except UnicodeError:
                pass
            raise SelectionFailure("SELECTION_PROTOCOL")
    except SelectionFailure:
        raise
    except Exception:
        raise SelectionFailure("SELECTION_NATIVE_FAILED") from None
    finally:
        sender.close()
        receiver.close()
        if started:
            child.join(0.2)
            if child.is_alive():
                child.terminate()
                child.join(1)
            if child.is_alive():
                child.kill()
                child.join(1)
            if not child.is_alive():
                child.close()


def read_document_selection(automation, target, *, verify_target,
                            text_pattern_interface, require_normal=_require_normal_process):
    """Read an explicit selected range in an accessible editor/document only.

    Unlike analysis's strict focused-leaf reader, allow same-window document
    ancestry and ordinary noninteractive document children (links/headings).
    Never use DocumentRange, clipboard, screenshots or control Values.
    """
    def current():
        if verify_target() != target:
            raise SelectionFailure("SELECTION_TARGET_CHANGED")

    checked_pids = set()
    def safe(element):
        if not element:
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        pid = element.CurrentProcessId
        if type(pid) is not int or pid <= 0 or pid == target.owner_pid:
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        if pid not in checked_pids:
            require_normal(pid)
            checked_pids.add(pid)
        explicit = element.GetCurrentPropertyValueEx(UIA_IS_PASSWORD, True)
        if explicit is not False:
            default = element.CurrentIsPassword
            if (explicit is True or element.CurrentControlType == 50004
                    or type(default) not in (bool, int) or default != 0):
                raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")

    walker = automation.RawViewWalker
    def ancestry(element):
        chain=[]
        for _ in range(32):
            if not element:
                break
            safe(element)
            chain.append(element)
            if element.CurrentProcessId == target.process_id:
                if element.CurrentNativeWindowHandle == target.hwnd:
                    return chain
                if element.CurrentNativeWindowHandle == target.focus:
                    if element.CurrentControlType in (50004,50030):
                        return chain
            element=walker.GetParentElement(element)
        raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")

    def range_for(pattern):
        ranges=pattern.GetSelection()
        if not ranges or ranges.Length!=1:
            raise SelectionFailure("SELECTION_EMPTY_OR_MULTIPLE")
        selected=ranges.GetElement(0)
        if not selected:
            raise SelectionFailure("SELECTION_EMPTY_OR_MULTIPLE")
        # Equal start/end means caret only, not selected text.
        if selected.CompareEndpoints(0,selected,1)==0:
            raise SelectionFailure("SELECTION_EMPTY_OR_MULTIPLE")
        return selected

    # Read-only document descendants; reject form fields, embedded windows,
    # nested documents and other actionable controls except hyperlinks.
    passive={50005,50006,50007,50008,50017,50020,50026,50029,50034,50035,50036}
    def inspect_children(selected, container, editor):
        children=selected.GetChildren()
        if children is None or not 0<=children.Length<=256:
            raise SelectionFailure("SELECTION_EMBEDDED_CONTROL")
        pending=[children.GetElement(i) for i in range(children.Length)]
        seen=0
        while pending:
            node=pending.pop()
            safe(node)
            seen+=1
            if seen>512 or editor or node.CurrentControlType not in passive:
                raise SelectionFailure("SELECTION_EMBEDDED_CONTROL")
            if not any(automation.CompareElements(container,x) for x in ancestry(node)):
                raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
            child=walker.GetFirstChildElement(node)
            while child:
                if len(pending)+seen>=512:
                    raise SelectionFailure("SELECTION_EMBEDDED_CONTROL")
                pending.append(child)
                child=walker.GetNextSiblingElement(child)

    try:
        current()
        focused=automation.GetFocusedElement()
        safe(focused)
        if not focused.CurrentHasKeyboardFocus:
            raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
        chain=ancestry(focused)
        # An edit field must never fall back to an enclosing page selection.
        editor=focused.CurrentControlType==50004
        candidates=chain[:1] if editor else [e for e in chain if e.CurrentControlType in (50004,50030)]
        for container in candidates:
            unknown=container.GetCurrentPattern(UIA_TEXT_PATTERN)
            if not unknown:
                unknown=container.GetCurrentPattern(UIA_TEXT_PATTERN2)
            if not unknown:
                continue
            pattern=unknown.QueryInterface(text_pattern_interface)
            selected=range_for(pattern)
            enclosing=selected.GetEnclosingElement()
            if not any(automation.CompareElements(container,e) for e in ancestry(enclosing)):
                raise SelectionFailure("SELECTION_PROTECTED_OR_UNKNOWN")
            inspect_children(selected,container,editor)
            current()
            safe(focused)
            text=_validated_text(selected.GetText(MAX_TEXT_CHARS+1))
            current()
            now=automation.GetFocusedElement()
            safe(now)
            if not now.CurrentHasKeyboardFocus or not automation.CompareElements(focused,now) or not selected.Compare(range_for(pattern)):
                raise SelectionFailure("SELECTION_TARGET_CHANGED")
            current()
            return text
        raise SelectionFailure("SELECTION_NOT_SUPPORTED")
    except SelectionFailure:
        raise
    except Exception:
        raise SelectionFailure("SELECTION_NATIVE_FAILED") from None


def _document_selection_child(sender,target):
    try:
        sys.coinit_flags=0
        from comtypes.client import CreateObject,GetModule
        uia=GetModule("UIAutomationCore.dll")
        automation=CreateObject(uia.CUIAutomation,interface=uia.IUIAutomation)
        result=read_document_selection(automation,target,
            verify_target=lambda:capture_target(target.hwnd,target.owner_pid),
            text_pattern_interface=uia.IUIAutomationTextPattern)
        sender.send_bytes(b"T"+result.encode("utf-8"))
    except Exception as exc:
        code=str(exc) if isinstance(exc,SelectionFailure) else "SELECTION_NATIVE_FAILED"
        try:
            sender.send_bytes(b"E"+(code if code in SAFE_CODES else "SELECTION_NATIVE_FAILED").encode("ascii"))
        except OSError:
            pass
    finally:
        sender.close()


def capture_readable_selection(target, stop):
    return capture_selection(target,stop,timeout=6.0,_worker=_document_selection_child)
