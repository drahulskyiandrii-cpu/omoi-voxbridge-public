"""Native Windows process-tree tests: standard-library children only."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from voxbridge.audio import pcm_to_wav
from voxbridge.local_partial import ResidentMiniSTT
from voxbridge.stt import SpeechFailure
from voxbridge.stt_process import spawn_owned_stt


@unittest.skipUnless(os.name == "nt", "Windows Job Objects required")
class OwnedProcessTests(unittest.TestCase):
    def test_failed_assignment_never_opens_gate(self):
        job = Mock()
        job.assign.side_effect = RuntimeError("assignment failed")
        child = Mock()
        child.poll.return_value = None
        with patch("voxbridge.stt_process.KillOnCloseJob", return_value=job), patch(
                "voxbridge.stt_process.subprocess.Popen", return_value=child):
            with self.assertRaises(RuntimeError):
                spawn_owned_stt(["unused"], Path("worker.py"), {}, threading.Event())
        child.stdin.write.assert_not_called()
        child.kill.assert_called_once()
        job.close.assert_called_once()

    def test_failed_spawn_cleanup_retains_exact_owner_and_blocks_next_spawn(self):
        from voxbridge.stt_process import stt_block_reason, STT_CLEANUP_FAILED
        job, child = Mock(), Mock()
        job.assign.side_effect = RuntimeError("assignment failed")
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired("private", 2)
        with patch("voxbridge.stt_process._failed_owner", None), patch(
                "voxbridge.stt_process.KillOnCloseJob", return_value=job), patch(
                "voxbridge.stt_process.subprocess.Popen", return_value=child) as launch:
            with self.assertRaisesRegex(SpeechFailure, STT_CLEANUP_FAILED):
                spawn_owned_stt(["unused"], Path("worker.py"), {}, threading.Event())
            self.assertEqual(stt_block_reason(), STT_CLEANUP_FAILED)
            from voxbridge import stt_process
            self.assertEqual(stt_process._failed_owner, (child, job))
            with self.assertRaisesRegex(SpeechFailure, STT_CLEANUP_FAILED):
                spawn_owned_stt(["unused"], Path("worker.py"), {}, threading.Event())
            launch.assert_called_once()
            child.stdin.write.assert_not_called()
            child.stdin.close.assert_not_called()

    def test_tree_cleanup_failure_keeps_handles_and_never_touches_live_pipes(self):
        from voxbridge.stt_process import stt_block_reason, STT_CLEANUP_FAILED
        backend = ResidentMiniSTT(engine="openvino")
        proc, job = Mock(), Mock()
        backend._process, backend._job = proc, job
        job.terminate_and_close.side_effect = OSError("private")
        with patch("voxbridge.stt_process._failed_owner", None):
            for _ in range(2):
                with self.assertRaisesRegex(SpeechFailure, STT_CLEANUP_FAILED):
                    backend.close()
            self.assertIs(backend._job, job)
            self.assertIs(backend._process, proc)
            self.assertEqual(stt_block_reason(), STT_CLEANUP_FAILED)
            job.terminate_and_close.assert_called_once()
            proc.wait.assert_not_called()
            proc.stdin.close.assert_not_called()
            proc.stdout.close.assert_not_called()
            other = ResidentMiniSTT(engine="openvino")
            with patch.object(other, "_launch_spec") as launch:
                with self.assertRaisesRegex(SpeechFailure, STT_CLEANUP_FAILED):
                    other.ensure()
                launch.assert_not_called()

    def test_pipe_thread_failure_quarantines_before_pipe_close(self):
        from voxbridge.stt_process import stt_block_reason, STT_CLEANUP_FAILED
        backend = ResidentMiniSTT(engine="openvino")
        backend._process, backend._job = Mock(), Mock()
        backend._writer = Mock()
        backend._writer.is_alive.return_value = True
        with patch("voxbridge.stt_process._failed_owner", None):
            with self.assertRaisesRegex(SpeechFailure, STT_CLEANUP_FAILED):
                backend.close()
            self.assertEqual(stt_block_reason(), STT_CLEANUP_FAILED)
            backend._writer.join.assert_called_once_with(1)
            backend._process.stdin.close.assert_not_called()
            self.assertIsNone(backend._job)

    def test_pre_cancelled_spawn_never_launches(self):
        stop = threading.Event()
        stop.set()
        with patch("voxbridge.stt_process.subprocess.Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "STT_CANCELLED"):
                spawn_owned_stt(["unused"], Path("worker.py"), {}, stop)
        launch.assert_not_called()

    def test_invalid_ready_packet_closes_owned_worker(self):
        script = "import time; print('{\"event\":\"unexpected\"}',flush=True); time.sleep(60)"
        backend = ResidentMiniSTT(engine="openvino")
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                backend, "_launch_spec", return_value=(
                    [sys._base_executable, "-u", "-c", script], Path(tmp)/"worker.py", os.environ.copy())):
            try:
                with self.assertRaisesRegex(SpeechFailure, "LOCAL_PARTIAL_NOT_READY"):
                    backend.ensure()
                self.assertIsNone(backend._process)
                self.assertIsNone(backend._job)
                self.assertFalse(backend._ready)
                with self.assertRaisesRegex(SpeechFailure, "STT_CANCELLED"):
                    backend.ensure()
            finally:
                backend.close()

    def test_verified_job_termination_reaps_exact_tree_not_foreign_process(self):
        from voxbridge.windows_owned_job import KillOnCloseJob
        job = KillOnCloseJob()
        foreign = child = None
        grandchild_handle = None
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        # The child waits for the gate before spawning anything.
        code = ("import sys,subprocess,time; sys.stdin.readline(); "
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
                "print(p.pid,flush=True); time.sleep(60)")
        try:
            foreign = subprocess.Popen([sys._base_executable, "-c", "import time;time.sleep(60)"])
            child = subprocess.Popen([sys._base_executable, "-u", "-c", code],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            job.assign(child)
            child.stdin.write("GO\n")
            child.stdin.flush()
            # Bounded reader instead of waiting forever for a child response.
            import queue
            replies = queue.Queue()
            reader = threading.Thread(target=lambda: replies.put(child.stdout.readline()), daemon=True)
            reader.start()
            pid = int(replies.get(timeout=5))
            self.assertTrue(job.contains_pid(child.pid))
            self.assertTrue(job.contains_pid(pid))
            grandchild_handle = api.OpenProcess(0x00100000, False, pid)
            self.assertTrue(grandchild_handle)
            job.terminate_and_close()
            self.assertIsNone(job._handle)
            self.assertIsNotNone(child.wait(timeout=2))
            self.assertEqual(api.WaitForSingleObject(grandchild_handle, 2000), 0)
            self.assertIsNone(foreign.poll())
        finally:
            job.close()
            for proc in (child, foreign):
                if proc is not None:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=3)
                    for pipe in (proc.stdin, proc.stdout):
                        if pipe is not None:
                            pipe.close()
            if grandchild_handle:
                api.CloseHandle(grandchild_handle)

    def test_verified_job_failure_keeps_handle(self):
        from voxbridge.windows_owned_job import KillOnCloseJob
        from voxbridge.captions_core import CaptionError
        job = KillOnCloseJob.__new__(KillOnCloseJob)
        job._handle = 123
        job._api = Mock()
        job._api.TerminateJobObject.return_value = False
        with self.assertRaisesRegex(CaptionError, "CPU_RUNTIME_JOB_TERMINATE"):
            job.terminate_and_close()
        self.assertEqual(job._handle,123)
        job._api.CloseHandle.assert_not_called()
        job._api.TerminateJobObject.return_value = True
        job._api.QueryInformationJobObject.return_value = True
        job._api.CloseHandle.return_value = False
        with self.assertRaisesRegex(CaptionError, "CPU_RUNTIME_JOB_CLOSE"):
            job.terminate_and_close()
        self.assertEqual(job._handle,123)
        job._api.CloseHandle.return_value = True
        job.terminate_and_close()
        self.assertIsNone(job._handle)

    def test_native_cancel_releases_child_and_grandchild(self):
        # Worker creates its own grandchild inside the job, then stalls decoding.
        # A distinct unrelated process must remain alive after closing our job.
        script = (
            "import sys,json,subprocess,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "print(json.dumps({'event':'ready','worker':__import__('os').getpid(),'grandchild':p.pid}),flush=True); "
            "sys.stdin.readline(); time.sleep(60)"
        )
        foreign = subprocess.Popen([sys._base_executable, "-c", "import time;time.sleep(60)"])
        backend = ResidentMiniSTT(engine="openvino")
        errors = []
        worker = None
        handles = []
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.object(
                    backend, "_launch_spec", return_value=(
                        [sys._base_executable,"-u","-c",script], Path(tmp)/"worker.py", os.environ.copy())):
                original = backend._receive
                packets = []
                def record(timeout):
                    packet = original(timeout)
                    packets.append(packet)
                    return packet
                with patch.object(backend, "_receive", side_effect=record):
                    backend.ensure()
                proc, job = backend._process, backend._job
                self.assertTrue(job.contains_pid(proc.pid))
                for pid in (packets[0]["worker"], packets[0]["grandchild"]):
                    self.assertTrue(job.contains_pid(pid))
                    handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                    self.assertTrue(handle)
                    handles.append(handle)
                def decode():
                    try:
                        backend.transcribe(pcm_to_wav(bytes(32000)), "de")
                    except SpeechFailure as exc:
                        errors.append(str(exc))
                    finally:
                        backend.close()
                worker = threading.Thread(target=decode)
                worker.start()
                time.sleep(.1)
                start = time.monotonic()
                backend.cancel()
                self.assertLess(time.monotonic()-start, .1)
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, ["STT_CANCELLED"])
                self.assertIsNotNone(proc.poll())
                self.assertIsNone(backend._process)
                self.assertIsNone(foreign.poll())
                for handle in handles:
                    self.assertEqual(api.WaitForSingleObject(handle, 2000), 0)
        finally:
            backend.cancel()
            if worker is not None:
                worker.join(5)
            if backend._process is not None:
                backend.close()
            for handle in handles:
                api.CloseHandle(handle)
            foreign.kill()
            foreign.wait(timeout=3)
