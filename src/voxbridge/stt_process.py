"""Own STT process trees without PID-based termination or GUI waits."""
from pathlib import Path
import secrets
import subprocess
import sys
import threading

from .windows_owned_job import KillOnCloseJob
from .stt import SpeechFailure

STT_CLEANUP_FAILED = "STT_OWNED_CLEANUP_FAILED"
_failed_owner = None
_owner_lock = threading.Lock()


def quarantine_stt(owner):
    """Retain exact handles on unverified cleanup; no new owned STT until restart."""
    global _failed_owner
    with _owner_lock:
        if _failed_owner is None:
            _failed_owner = owner


def stt_block_reason():
    with _owner_lock:
        return STT_CLEANUP_FAILED if _failed_owner is not None else ""



def spawn_owned_stt(command, worker, env, cancel):
    """Create job, gate launcher, then allow inference; return process and job."""
    if stt_block_reason():
        raise SpeechFailure(STT_CLEANUP_FAILED)
    if cancel.is_set():
        raise SpeechFailure("STT_CANCELLED")
    name = "Local\\VoxBridge-STT-" + secrets.token_hex(16)
    job = KillOnCloseJob(name)
    proc = None
    assigned = False
    try:
        if cancel.is_set():
            raise RuntimeError("STT_CANCELLED")
        # Use the base interpreter rather than a Windows venv redirector.
        launcher = Path(__file__).with_name("owned_stt_launcher.py")
        proc = subprocess.Popen(
            [sys._base_executable, "-I", "-B", "-u", str(launcher), *command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", cwd=str(worker.parent),
            env={**env, "VOXBRIDGE_STT_JOB": name},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        job.assign(proc)
        assigned = True
        if cancel.is_set():
            raise RuntimeError("STT_CANCELLED")
        proc.stdin.write("GO\n")
        proc.stdin.flush()
        return proc, job
    except BaseException:
        try:
            if assigned:
                job.terminate_and_close()
            else:
                # The launcher cannot start its target before the gate.
                if proc is not None and proc.poll() is None:
                    proc.kill()
                job.close()
            if proc is not None:
                proc.wait(timeout=2)
                for pipe in (proc.stdin, proc.stdout):
                    if pipe is not None:
                        pipe.close()
        except Exception:
            quarantine_stt((proc, job))
            raise SpeechFailure(STT_CLEANUP_FAILED) from None
        raise
