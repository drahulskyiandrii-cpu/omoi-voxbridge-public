"""Read-only Win32 TCP listener ownership checks for local services."""
from __future__ import annotations

import ctypes
import os
import socket

from .captions_core import CaptionError


def listening_endpoints() -> tuple[tuple[str, int, int], ...]:
    """Windows IPv4 listening endpoint inventory. Fail closed on query failure."""
    if os.name != "nt":
        raise CaptionError("CPU_RUNTIME_WINDOWS_REQUIRED")
    from ctypes import wintypes
    table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    table.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                      wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    table.restype = wintypes.DWORD
    size = wintypes.DWORD()
    if table(None, ctypes.byref(size), False, 2, 3, 0) != 122:
        raise CaptionError("CPU_RUNTIME_LISTENER_QUERY")
    for _ in range(3):
        if not 4 <= size.value <= 16 * 1024 * 1024:
            raise CaptionError("CPU_RUNTIME_LISTENER_QUERY")
        buf = ctypes.create_string_buffer(size.value)
        status = table(buf, ctypes.byref(size), False, 2, 3, 0)
        if status == 122:
            continue
        if status != 0:
            break
        import struct
        count = struct.unpack_from("<I", buf)[0]
        if 4 + count * 24 > len(buf):
            break
        result = []
        for offset in range(4, 4 + count * 24, 24):
            state, addr, local_port, _, _, pid = struct.unpack_from("<6I", buf, offset)
            if state == 2:
                result.append((socket.inet_ntoa(struct.pack("<I", addr)),
                               socket.ntohs(local_port & 65535), pid))
        return tuple(result)
    raise CaptionError("CPU_RUNTIME_LISTENER_QUERY")


def listener_owners(port: int) -> tuple[tuple[str, int], ...]:
    return tuple((address, pid) for address, number, pid in listening_endpoints()
                 if number == port)


def process_identity(pid):
    """Exact image path and creation time; no process termination or injection."""
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return None
    from ctypes import wintypes as w
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    api.OpenProcess.restype = w.HANDLE
    api.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    api.QueryFullProcessImageNameW.restype = w.BOOL
    api.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    api.GetProcessTimes.restype = w.BOOL
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        length = w.DWORD(32768)
        image = ctypes.create_unicode_buffer(length.value)
        times = [w.FILETIME() for _ in range(4)]
        if (not api.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length))
                or not api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return os.path.normcase(os.path.realpath(image.value)), created
    finally:
        api.CloseHandle(handle)


def process_arguments(pid):
    """Read Windows argv for a candidate local service, never log its contents."""
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return ()
    from ctypes import wintypes as w
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    api.OpenProcess.restype = w.HANDLE
    api.CloseHandle.argtypes = [w.HANDLE]
    api.LocalFree.argtypes = [ctypes.c_void_p]
    api.LocalFree.restype = ctypes.c_void_p
    query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
    query.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)]
    query.restype = ctypes.c_long
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return ()
    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", w.USHORT), ("capacity", w.USHORT), ("buffer", ctypes.c_void_p)]
    try:
        size = w.ULONG()
        query(handle, 60, None, 0, ctypes.byref(size))
        if not ctypes.sizeof(UnicodeString) <= size.value <= 131072:
            return ()
        raw = ctypes.create_string_buffer(size.value)
        if query(handle, 60, raw, size, ctypes.byref(size)) != 0:
            return ()
        value = UnicodeString.from_buffer(raw)
        start = ctypes.addressof(raw)
        if (not value.buffer or value.length % 2 or value.buffer < start
                or value.buffer + value.length > start + len(raw)):
            return ()
        command = ctypes.wstring_at(value.buffer, value.length // 2)
        parse = ctypes.WinDLL("shell32").CommandLineToArgvW
        parse.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        parse.restype = ctypes.POINTER(w.LPWSTR)
        count = ctypes.c_int()
        argv = parse(command, ctypes.byref(count))
        if not argv:
            return ()
        try:
            return tuple(argv[i] for i in range(count.value))
        finally:
            api.LocalFree(argv)
    finally:
        api.CloseHandle(handle)
