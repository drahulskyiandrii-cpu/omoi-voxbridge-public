"""Isolate a possibly blocking WASAPI driver in a VoxBridge-owned child.

The parent polls a bounded binary pipe; it never calls a foreign driver.
No audio/device access occurs until an approved caller advances the generator.
No process other than the child created for this capture is terminated.
"""
from __future__ import annotations

import multiprocessing as mp
import threading
import time
from typing import Iterator

from .wasapi_capture import CaptureFailure, MAX_SECONDS, SOURCE_FRAMES, capture_loopback

POLL_SECONDS = 0.05
GRACE_SECONDS = 0.35
JOIN_SECONDS = 1.0
STARTUP_ALLOWANCE = 5.0
MAX_PACKET = SOURCE_FRAMES + 1  # one protocol tag + one PCM block
CAPTURE_IDLE = object()  # parent-side heartbeat, never an audio packet
_capture_lock = threading.Lock()
_failed_child = None  # retain exact handle if termination could not be verified


def capture_block_reason():
    """Fail closed until restart if an owned child could not be reaped."""
    if _failed_child is not None:
        return "WASAPI_CHILD_CLEANUP_FAILED"
    if _capture_lock.locked():
        return "WASAPI_CAPTURE_BUSY"
    return ""


def _reap_child(child):
    """All waits are bounded; never discard an unverified live handle."""
    global _failed_child
    try:
        child.join(GRACE_SECONDS)
        if child.is_alive():
            child.terminate()
            child.join(JOIN_SECONDS)
        if child.is_alive():
            child.kill()
            child.join(JOIN_SECONDS)
        if child.is_alive():
            raise CaptureFailure("WASAPI_CHILD_CLEANUP_FAILED")
        child.close()
    except Exception:
        _failed_child = child
        raise CaptureFailure("WASAPI_CHILD_CLEANUP_FAILED") from None


_SAFE_ERRORS = {
    "WASAPI_INVALID_DEVICE", "WASAPI_CANCEL_EVENT_REQUIRED",
    "WASAPI_INVALID_DURATION", "WASAPI_WINDOWS_REQUIRED",
    "SOUNDCARD_NOT_INSTALLED", "WASAPI_DEVICE_ENUMERATION_FAILED",
    "WASAPI_NOT_LOOPBACK_DEVICE", "WASAPI_INVALID_FRAMES",
    "WASAPI_NONFINITE_AUDIO", "WASAPI_FRAME_OVERRUN",
    "WASAPI_CAPTURE_TIMEOUT", "WASAPI_CAPTURE_FAILED",
}


def _capture_child(sender, cancel, device_id, seconds):
    """Entry point for Windows spawn; no Qt, network or models."""
    local_stop = threading.Event()
    threading.Thread(target=lambda: (cancel.wait(), local_stop.set()),
                     daemon=True).start()
    try:
        if cancel.is_set():
            return
        for pcm in capture_loopback(device_id, local_stop, seconds=seconds):
            if cancel.is_set():
                return
            sender.send_bytes(b"P" + pcm)
        sender.send_bytes(b"D")
    except CaptureFailure as exc:
        code = str(exc)
        if code not in _SAFE_ERRORS:
            code = "WASAPI_CAPTURE_FAILED"
        try:
            sender.send_bytes(b"E" + code.encode("ascii"))
        except (OSError, EOFError):
            pass
    except Exception:
        try:
            sender.send_bytes(b"EWASAPI_CAPTURE_FAILED")
        except (OSError, EOFError):
            pass
    finally:
        sender.close()


def capture_isolated(device_id: str, stop: threading.Event, *,
                     seconds: int = 60, poll_idle: bool = False,
                     _worker=_capture_child) -> Iterator[bytes | object]:
    """Lazy capture with a dedicated spawn process and a hard parent deadline.

    With poll_idle=True, yield CAPTURE_IDLE on a pipe polling timeout so
    the caller can service the socket without waiting for an audio packet.
    The internal worker override exists solely for synthetic IPC tests.
    Stop is cooperative first, then terminates only the owned child. No
    capture restart until cleanup completes; no automatic endpoint fallback.
    """
    if not isinstance(device_id, str) or not device_id or len(device_id) > 512:
        raise CaptureFailure("WASAPI_INVALID_DEVICE")
    if not isinstance(stop, threading.Event):
        raise CaptureFailure("WASAPI_CANCEL_EVENT_REQUIRED")
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise CaptureFailure("WASAPI_INVALID_DURATION")
    if stop.is_set():
        return

    if not _capture_lock.acquire(blocking=False):
        raise CaptureFailure("WASAPI_CAPTURE_BUSY")
    try:
        if _failed_child is not None:
            raise CaptureFailure("WASAPI_CHILD_CLEANUP_FAILED")
        yield from _capture_packets(device_id, stop, seconds, poll_idle, _worker)
    finally:
        _capture_lock.release()


def _capture_packets(device_id, stop, seconds, poll_idle, worker):
    ctx = mp.get_context("spawn")
    try:
        receiver, sender = ctx.Pipe(duplex=False)
    except Exception:
        raise CaptureFailure("WASAPI_CHILD_FAILED") from None
    child = None
    cancel = None
    started = False
    deadline = time.monotonic() + seconds + STARTUP_ALLOWANCE
    remaining = seconds * 24000 * 2
    try:
        cancel = ctx.Event()
        child = ctx.Process(target=worker, args=(sender, cancel, device_id, seconds),
                            name="voxbridge-wasapi-capture", daemon=True)
        if stop.is_set():
            return
        child.start()
        started = True
        sender.close()
        while not stop.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                raise CaptureFailure("WASAPI_CHILD_TIMEOUT")
            if not receiver.poll(min(POLL_SECONDS, left)):
                if not child.is_alive():
                    raise CaptureFailure("WASAPI_CHILD_EXITED")
                if poll_idle and not stop.is_set():
                    yield CAPTURE_IDLE
                continue
            if stop.is_set():
                return
            try:
                packet = receiver.recv_bytes(MAX_PACKET)
            except EOFError:
                raise CaptureFailure("WASAPI_CHILD_EXITED") from None
            except OSError:
                raise CaptureFailure("WASAPI_CHILD_PROTOCOL") from None
            if stop.is_set():
                return
            if time.monotonic() >= deadline:
                raise CaptureFailure("WASAPI_CHILD_TIMEOUT")
            if packet == b"D":
                return
            if packet.startswith(b"E"):
                code = packet[1:].decode("ascii", errors="replace")
                raise CaptureFailure(code if code in _SAFE_ERRORS else "WASAPI_CHILD_PROTOCOL")
            pcm = packet[1:]
            if (not packet.startswith(b"P") or not 2 <= len(pcm) <= SOURCE_FRAMES
                    or len(pcm) % 2 or len(pcm) > remaining):
                raise CaptureFailure("WASAPI_CHILD_PROTOCOL")
            remaining -= len(pcm)
            yield pcm
    except CaptureFailure:
        raise
    except Exception:
        raise CaptureFailure("WASAPI_CHILD_FAILED") from None
    finally:
        # Closing the read end also unblocks a child waiting to write.
        try:
            if cancel is not None:
                cancel.set()
        finally:
            try:
                try:
                    receiver.close()
                finally:
                    sender.close()
            finally:
                if started:
                    _reap_child(child)
                elif child is not None:
                    child.close()


def capture_live(device_id: str, stop: threading.Event, *, seconds: int = 60):
    """Production source: PCM packets and local idle notifications."""
    return capture_isolated(device_id, stop, seconds=seconds, poll_idle=True)
