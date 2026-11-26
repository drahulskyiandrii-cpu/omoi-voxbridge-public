"""Owned Windows Job Object with kill-on-last-handle-close.

The caller creates the job before spawning, then assigns its own Popen child
immediately. No attachment to arbitrary PIDs or unrelated processes.
"""
import ctypes
from ctypes import wintypes
import os
import time

from .captions_core import CaptionError


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime",
        "ThisPeriodTotalKernelTime")] + [(name, wintypes.DWORD) for name in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


class KillOnCloseJob:
    def __init__(self, name=None):
        if os.name != "nt":
            raise CaptionError("CPU_RUNTIME_WINDOWS_REQUIRED")
        self._handle = None
        self._api = api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self._handle = api.CreateJobObjectW(None, name)  # noninheritable
        if not self._handle:
            raise CaptionError("CPU_RUNTIME_JOB_CREATE")
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise CaptionError("CPU_RUNTIME_JOB_LIMIT")

    def assign(self, child):
        # Popen's handle is bound to the exact child, never re-open by PID.
        if not self._handle or not self._api.AssignProcessToJobObject(self._handle, int(child._handle)):
            raise CaptionError("CPU_RUNTIME_JOB_ASSIGN")

    def contains_pid(self, pid):
        if not self._handle:
            return False
        api = self._api
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        api.IsProcessInJob.restype = wintypes.BOOL
        process = api.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            member = wintypes.BOOL()
            return bool(api.IsProcessInJob(process, self._handle, ctypes.byref(member)) and member.value)
        finally:
            api.CloseHandle(process)

    def terminate_and_close(self, timeout=2.0):
        """Terminate this exact job, verify zero active processes, then release.

        Keep the handle on failure so the owner cannot forget an unreaped job.
        Native OS calls themselves are not bounded by the polling deadline.
        """
        handle = self._handle
        if not handle:
            return
        api = self._api
        if not api.TerminateJobObject(handle, 1):
            raise CaptionError("CPU_RUNTIME_JOB_TERMINATE")
        deadline = time.monotonic() + timeout
        accounting = _BasicAccounting()
        while True:
            if not api.QueryInformationJobObject(handle, 1, ctypes.byref(accounting),
                                                  ctypes.sizeof(accounting), None):
                raise CaptionError("CPU_RUNTIME_JOB_QUERY")
            if accounting.ActiveProcesses == 0:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptionError("CPU_RUNTIME_JOB_CLEANUP_TIMEOUT")
            time.sleep(min(.05, remaining))
        if not api.CloseHandle(handle):
            raise CaptionError("CPU_RUNTIME_JOB_CLOSE")
        self._handle = None

    def close(self):
        handle = self._handle
        if handle:
            if not self._api.CloseHandle(handle):
                raise CaptionError("CPU_RUNTIME_JOB_CLOSE")
            self._handle = None
