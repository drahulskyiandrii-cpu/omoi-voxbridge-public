"""Own a single Qt audio subprocess and supervise it off the GUI thread.

One bounded command is in flight at a time. The child alone owns QAudioSink.
No text, model, API, or disk audio is needed in the child.
"""
from __future__ import annotations

from dataclasses import dataclass
import multiprocessing as mp
import queue
import struct
import threading
import time

from .audio_output import BLOCK_BYTES, BUFFER_BYTES

POLL_SECONDS = .05
TICK_SECONDS = .02
REPLY_SECONDS = 8.0
IDLE_SECONDS = 45.0
GRACE_SECONDS = .35
JOIN_SECONDS = 1.0
MAX_REPLY = 128
_STATUS = struct.Struct(">IBB")  # free pending bytes, recovery count, queued
_lock = threading.Lock()
_failed_child = None

_SAFE_ERRORS = {
    "AUDIO_OUTPUT_CANCELLED", "AUDIO_OUTPUT_PROTOCOL", "AUDIO_OUTPUT_FAILED",
    "AUDIO_OUTPUT_CLEANUP_FAILED", "AUDIO_FORMAT_NOT_SUPPORTED",
    "AUDIO_WRITE_FAILED", "TTS_STREAM_NO_AUDIO",
    "AUDIO_DEVICE_FAILED: AUDIO_DEVICE_UNAVAILABLE",
    "AUDIO_DEVICE_FAILED: AUDIO_OPEN_ERROR",
    "AUDIO_DEVICE_FAILED: AUDIO_WRITE_FAILED",
    "AUDIO_DEVICE_FAILED: AUDIO_PLAYBACK_STALLED",
    "AUDIO_DEVICE_FAILED: STOPPED", "AUDIO_DEVICE_FAILED: OpenError",
    "AUDIO_DEVICE_FAILED: IOError", "AUDIO_DEVICE_FAILED: FatalError",
    "PCM_BACKPRESSURE_OVERFLOW",
}


@dataclass(frozen=True)
class OutputResult:
    error: str = ""
    recoveries: int = 0
    cleanup_failed: bool = False


class OutputFailure(RuntimeError):
    pass


def output_block_reason():
    if _failed_child is not None:
        return "AUDIO_OUTPUT_CHILD_CLEANUP_FAILED"
    if _lock.locked():
        return "AUDIO_OUTPUT_BUSY"
    return ""


def _reply(state):
    if state.done:
        if state.error:
            code = state.error if state.error in _SAFE_ERRORS else "AUDIO_OUTPUT_FAILED"
            return b"E" + code.encode("ascii")
        return b"D" + bytes([state.recovery_count])
    return b"R" + _STATUS.pack(state.capacity, state.recovery_count, int(state.queued))


def _audio_child(pipe, engine_factory=None):
    """Spawn entry point: Qt event loop and all native calls in this process."""
    from PySide6.QtCore import QCoreApplication, QTimer
    from .audio_output import PlaybackEngine

    app = QCoreApplication([])
    engine = (engine_factory or PlaybackEngine)()
    timer = QTimer()
    timer.setInterval(int(TICK_SECONDS * 1000))

    def quit_child():
        timer.stop()
        app.quit()

    def step():
        try:
            if not pipe.poll(0):
                # Keep Qt playback progressing even while the parent is idle.
                # The parent gets state on its next bounded command.
                if not engine.done:
                    engine.tick()
                return
            command = pipe.recv_bytes(BLOCK_BYTES + 1)
            if command == b"S":
                engine.cancel()
            elif not engine.done:
                if command == b"D":
                    engine.finish()
                elif command.startswith(b"P") and 3 <= len(command) <= BLOCK_BYTES + 1:
                    engine.feed(command[1:])
                elif command != b"T":
                    raise OutputFailure("AUDIO_OUTPUT_PROTOCOL")
                engine.tick()
            state = engine.state()
            pipe.send_bytes(_reply(state))
            if state.done:
                quit_child()
        except (EOFError, OSError):
            engine.cancel()
            quit_child()
        except Exception:
            try:
                engine.cancel()
                pipe.send_bytes(b"EAUDIO_OUTPUT_FAILED")
            except Exception:
                pass
            quit_child()

    try:
        pipe.send_bytes(_reply(engine.state()))
        timer.timeout.connect(step)
        timer.start()
        app.exec()
    finally:
        pipe.close()


def _reap(child):
    """Return whether exit was graceful; quarantine an unverified child."""
    global _failed_child
    try:
        child.join(GRACE_SECONDS)
        graceful = not child.is_alive()
        if not graceful:
            child.terminate()
            child.join(JOIN_SECONDS)
        if child.is_alive():
            child.kill()
            child.join(JOIN_SECONDS)
        if child.is_alive():
            raise OutputFailure("AUDIO_OUTPUT_CHILD_CLEANUP_FAILED")
        exitcode = child.exitcode
        child.close()
        return graceful, exitcode
    except Exception:
        _failed_child = child
        raise OutputFailure("AUDIO_OUTPUT_CHILD_CLEANUP_FAILED") from None


def _receive(pipe, child, stop):
    deadline = time.monotonic() + REPLY_SECONDS
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutputFailure("AUDIO_OUTPUT_CHILD_TIMEOUT")
        if not pipe.poll(min(POLL_SECONDS, remaining)):
            if not child.is_alive():
                raise OutputFailure("AUDIO_OUTPUT_CHILD_EXITED")
            continue
        if stop.is_set():
            return None
        try:
            packet = pipe.recv_bytes(MAX_REPLY)
        except EOFError:
            raise OutputFailure("AUDIO_OUTPUT_CHILD_EXITED") from None
        except OSError:
            raise OutputFailure("AUDIO_OUTPUT_PROTOCOL") from None
        if stop.is_set():
            return None
        if time.monotonic() >= deadline:
            raise OutputFailure("AUDIO_OUTPUT_CHILD_TIMEOUT")
        return packet
    return None


def _decode(packet):
    if packet is None:
        return None
    if packet.startswith(b"E"):
        code = packet[1:].decode("ascii", errors="replace")
        if code not in _SAFE_ERRORS:
            raise OutputFailure("AUDIO_OUTPUT_PROTOCOL")
        raise OutputFailure(code)
    if len(packet) == 2 and packet.startswith(b"D") and packet[1] <= 2:
        return OutputResult(recoveries=packet[1])
    if len(packet) != 1 + _STATUS.size or not packet.startswith(b"R"):
        raise OutputFailure("AUDIO_OUTPUT_PROTOCOL")
    capacity, recoveries, queued = _STATUS.unpack(packet[1:])
    if capacity > BUFFER_BYTES or capacity % 2 or recoveries > 2 or queued not in (0, 1):
        raise OutputFailure("AUDIO_OUTPUT_PROTOCOL")
    return capacity, recoveries, bool(queued)


def _exchange(pipe, child, incoming, finished, stop, on_recovery):
    packet = _receive(pipe, child, stop)
    sent_done = False
    recoveries = 0
    idle_since = time.monotonic()
    while not stop.is_set():
        state = _decode(packet)
        if isinstance(state, OutputResult):
            if not sent_done or state.recoveries < recoveries:
                raise OutputFailure("AUDIO_OUTPUT_PROTOCOL")
            return state
        if state is None:
            break
        capacity, count, queued = state
        if count < recoveries:
            raise OutputFailure("AUDIO_OUTPUT_PROTOCOL")
        if count != recoveries:
            recoveries = count
            on_recovery(count)
        if queued:
            idle_since = time.monotonic()
        command = b"T"
        if not sent_done:
            if capacity >= BLOCK_BYTES:
                try:
                    data = incoming.get_nowait()
                except queue.Empty:
                    if finished.is_set() and incoming.empty():
                        command = b"D"
                        sent_done = True
                else:
                    if not isinstance(data, bytes) or not 2 <= len(data) <= BLOCK_BYTES or len(data) % 2:
                        raise OutputFailure("AUDIO_OUTPUT_PROTOCOL")
                    command = b"P" + data
                    idle_since = time.monotonic()
            if not queued and not finished.is_set() and time.monotonic() - idle_since > IDLE_SECONDS:
                raise OutputFailure("TTS_NEXT_AUDIO_TIMEOUT")
        if stop.is_set():
            break
        # One message < Windows pipe buffer, sent only after the previous
        # reply; no unbounded command queue or simultaneous writer.
        pipe.send_bytes(command)
        packet = _receive(pipe, child, stop)
        if command == b"T" and stop.wait(TICK_SECONDS):
            break
    return OutputResult()


def play_output(incoming, finished, stop, on_recovery=lambda _: None, *, _worker=_audio_child, _engine_factory=None):
    """Run in a supervisor thread. Cancellation never joins from the GUI."""
    if stop.is_set():
        return OutputResult()
    if not _lock.acquire(blocking=False):
        return OutputResult("AUDIO_OUTPUT_BUSY")
    child = parent = peer = None
    result = OutputResult()
    started = False
    try:
        if _failed_child is not None:
            return OutputResult("AUDIO_OUTPUT_CHILD_CLEANUP_FAILED", cleanup_failed=True)
        if stop.is_set():
            return result
        ctx = mp.get_context("spawn")
        parent, peer = ctx.Pipe(duplex=True)
        child = ctx.Process(target=_worker, args=(peer, _engine_factory),
                            name="voxbridge-audio-output", daemon=True)
        if stop.is_set():
            raise OutputFailure("AUDIO_OUTPUT_CANCELLED")
        child.start()
        started = True
        peer.close()
        result = _exchange(parent, child, incoming, finished, stop, on_recovery)
    except OutputFailure as exc:
        result = OutputResult(str(exc))
    except Exception:
        result = OutputResult("AUDIO_OUTPUT_CHILD_FAILED")
    finally:
        # EOF is cooperative Stop. A blocked native call is handled by the
        # exact child termination below; never kill by process name/PID.
        try:
            for pipe in (parent, peer):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        if not result.error:
                            result = OutputResult("AUDIO_OUTPUT_CHILD_FAILED")
        finally:
            try:
                if started:
                    graceful, exitcode = _reap(child)
                    if not stop.is_set() and not result.error:
                        if not graceful:
                            result = OutputResult("AUDIO_OUTPUT_CHILD_EXIT_TIMEOUT")
                        elif exitcode != 0:
                            result = OutputResult("AUDIO_OUTPUT_CHILD_EXITED")
                elif child is not None:
                    child.close()
            except Exception:
                result = OutputResult("AUDIO_OUTPUT_CHILD_CLEANUP_FAILED", cleanup_failed=True)
            finally:
                _lock.release()
    if stop.is_set() and not result.cleanup_failed:
        return OutputResult()
    return result
