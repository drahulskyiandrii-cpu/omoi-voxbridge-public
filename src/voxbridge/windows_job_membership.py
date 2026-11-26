"""Child-side verification of membership in a named Windows Job Object."""
import ctypes
from ctypes import wintypes


def in_named_job(name):
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenJobObjectW.restype = wintypes.HANDLE
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    api.IsProcessInJob.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    job = api.OpenJobObjectW(4, False, name)
    if not job:
        return False
    try:
        member = wintypes.BOOL()
        return bool(api.IsProcessInJob(api.GetCurrentProcess(), job, ctypes.byref(member)) and member.value)
    finally:
        api.CloseHandle(job)
