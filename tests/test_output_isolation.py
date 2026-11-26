"""Windows spawn/IPC and Qt child event loop with synthetic audio only."""
import multiprocessing as mp
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

from voxbridge.audio_output import BLOCK_BYTES, PlaybackEngine, OutputState
from voxbridge.output_isolation import (
    OutputFailure, OutputResult, _reply, _reap, output_block_reason, play_output,
)


# The real child runs this fake sink on its own Qt event loop. No device
# enumeration, sound, inference, HTTP, disk PCM or private text.
EXPECTED = b"\x01\x00" * (BLOCK_BYTES//2) + b"\x02\x00" * 17


def synthetic_engine():
    from PySide6.QtMultimedia import QAudio
    output = bytearray()
    device = Mock()
    device.isNull.return_value = False
    device.isFormatSupported.return_value = True
    device.id.return_value = b"synthetic"
    media = Mock()
    media.defaultAudioOutput.return_value = device
    sink = Mock()
    sink.state.return_value = QAudio.State.IdleState
    sink.error.return_value = QAudio.Error.NoError
    sink.processedUSecs.return_value = 0
    sink.bytesFree.return_value = BLOCK_BYTES
    sink.start.return_value.write.side_effect = lambda data: (output.extend(data), len(data))[1]
    def verify():
        if output != EXPECTED:
            raise RuntimeError("synthetic PCM mismatch")
    sink.stop.side_effect = verify
    return PlaybackEngine(media=media, sink_factory=Mock(return_value=sink))


def ready(pipe):
    pipe.send_bytes(_reply(OutputState(192000, 0, False)))


def blocked_worker(pipe, _):
    with pipe:
        ready(pipe)
        pipe.recv_bytes(BLOCK_BYTES+1)
        time.sleep(60)



def signalled_blocked_worker(pipe, entered):
    with pipe:
        ready(pipe)
        pipe.recv_bytes(BLOCK_BYTES+1)
        entered.set()
        time.sleep(60)


def silent_worker(pipe, _):
    with pipe:
        time.sleep(60)


def malformed_worker(pipe, _):
    with pipe:
        pipe.send_bytes(b"Rbad")


def premature_done_worker(pipe, _):
    with pipe:
        pipe.send_bytes(b"D\x00")


def bad_exit_worker(pipe, _):
    with pipe:
        ready(pipe)
        while True:
            command = pipe.recv_bytes(BLOCK_BYTES+1)
            if command == b"D":
                pipe.send_bytes(b"D\x00")
                raise SystemExit(3)
            ready(pipe)


def late_exit_worker(pipe, _):
    with pipe:
        ready(pipe)
        while True:
            command = pipe.recv_bytes(BLOCK_BYTES+1)
            if command == b"D":
                pipe.send_bytes(b"D\x00")
                time.sleep(60)
            ready(pipe)


def recovery_worker(pipe, _):
    with pipe:
        ready(pipe)
        received = 0
        try:
            while True:
                command = pipe.recv_bytes(BLOCK_BYTES+1)
                if command.startswith(b"P"):
                    received += len(command)-1
                if command == b"D":
                    pipe.send_bytes(b"D\x01" if received == len(EXPECTED) else b"EAUDIO_OUTPUT_PROTOCOL")
                    return
                pipe.send_bytes(_reply(OutputState(192000, 1, False)))
        except (EOFError, OSError):
            pass



class OutputIsolationTests(unittest.TestCase):
    def incoming(self):
        pcm = queue.Queue(maxsize=8)
        pcm.put(EXPECTED[:BLOCK_BYTES])
        pcm.put(EXPECTED[BLOCK_BYTES:])
        return pcm

    def run_output(self, worker=None, *, stop=None, finished=True, on_recovery=lambda _:None):
        done = threading.Event()
        if finished:
            done.set()
        kwargs = {"_worker": worker} if worker else {"_engine_factory": synthetic_engine}
        result = play_output(self.incoming(), done, stop or threading.Event(), on_recovery, **kwargs)
        self.assertFalse(any(p.name == "voxbridge-audio-output" for p in mp.active_children()))
        self.assertEqual(output_block_reason(), "")
        return result

    def test_real_qt_child_exact_pcm_and_verified_exit(self):
        self.assertEqual(self.run_output(), OutputResult())

    def test_recovery_report_is_bounded_and_count_preserved(self):
        updates = []
        result = self.run_output(recovery_worker, on_recovery=updates.append)
        self.assertEqual(result, OutputResult(recoveries=1))
        self.assertEqual(updates, [1])

    def test_blocked_native_call_cancel_reaps_only_owned_child(self):
        stop = threading.Event()
        timer = threading.Timer(.8, stop.set)
        timer.start()
        try:
            start = time.monotonic()
            self.assertEqual(self.run_output(blocked_worker, stop=stop), OutputResult())
            self.assertLess(time.monotonic()-start, 4)
        finally:
            timer.cancel()
            timer.join()

    def test_stop_before_first_reply(self):
        stop = threading.Event()
        timer = threading.Timer(.5, stop.set)
        timer.start()
        try:
            self.assertEqual(self.run_output(silent_worker, stop=stop), OutputResult())
        finally:
            timer.cancel()
            timer.join()

    def test_precancel_does_not_spawn(self):
        stop = threading.Event()
        stop.set()
        with patch("voxbridge.output_isolation.mp.get_context") as ctx:
            self.assertEqual(self.run_output(blocked_worker, stop=stop), OutputResult())
            ctx.assert_not_called()

    def test_reply_deadline_reaps_hung_child(self):
        with patch("voxbridge.output_isolation.REPLY_SECONDS", 1.0):
            start = time.monotonic()
            self.assertEqual(self.run_output(silent_worker).error, "AUDIO_OUTPUT_CHILD_TIMEOUT")
            self.assertLess(time.monotonic()-start, 4)

    def test_malformed_premature_success_and_crash_rejected(self):
        for worker, code in ((malformed_worker, "AUDIO_OUTPUT_PROTOCOL"),
                             (premature_done_worker, "AUDIO_OUTPUT_PROTOCOL"),
                             (bad_exit_worker, "AUDIO_OUTPUT_CHILD_EXITED"),
                             (late_exit_worker, "AUDIO_OUTPUT_CHILD_EXIT_TIMEOUT")):
            with self.subTest(worker=worker.__name__):
                self.assertEqual(self.run_output(worker).error, code)

    def test_concurrent_output_rejected_until_child_reaped(self):
        stop = threading.Event()
        result = []
        done = threading.Event()
        worker = threading.Thread(target=lambda: result.append(
            play_output(self.incoming(), done, stop, _worker=silent_worker)))
        worker.start()
        try:
            deadline = time.monotonic()+2
            while output_block_reason() == "" and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(output_block_reason(), "AUDIO_OUTPUT_BUSY")
            self.assertEqual(play_output(self.incoming(), done, threading.Event(),
                                         _worker=blocked_worker).error, "AUDIO_OUTPUT_BUSY")
        finally:
            stop.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [OutputResult()])
        self.assertEqual(output_block_reason(), "")

    def test_cleanup_failure_quarantines_exact_handle(self):
        from voxbridge import output_isolation as iso
        child = Mock()
        child.is_alive.return_value = True
        with patch.object(iso, "_failed_child", None), patch.object(iso.mp, "get_context") as ctx:
            with self.assertRaisesRegex(OutputFailure, "AUDIO_OUTPUT_CHILD_CLEANUP_FAILED"):
                _reap(child)
            self.assertIs(iso._failed_child, child)
            child.terminate.assert_called_once()
            child.kill.assert_called_once()
            child.close.assert_not_called()
            self.assertEqual(output_block_reason(), "AUDIO_OUTPUT_CHILD_CLEANUP_FAILED")
            self.assertEqual(play_output(self.incoming(), threading.Event(), threading.Event()),
                             OutputResult("AUDIO_OUTPUT_CHILD_CLEANUP_FAILED", cleanup_failed=True))
            ctx.assert_not_called()

    def test_idle_provider_timeout_not_device_recovery(self):
        with patch("voxbridge.output_isolation.IDLE_SECONDS", .25):
            self.assertEqual(self.run_output(recovery_worker, finished=False).error,
                             "TTS_NEXT_AUDIO_TIMEOUT")

    def test_gui_event_loop_remains_responsive_while_real_child_is_blocked(self):
        import os
        from functools import partial
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from voxbridge.tts_ui import CloudTTSDialog
        app = QApplication.instance() or QApplication([])
        entered = mp.get_context("spawn").Event()
        dlg = CloudTTSDialog(provider_factory=Mock(),
                             output_runner=partial(play_output,
                                 _worker=signalled_blocked_worker, _engine_factory=entered))
        dlg._generation = dlg._worker_generation = 11
        dlg._busy = True
        dlg._cancel = threading.Event()
        dlg._incoming.put(bytes(2))
        beats = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: beats.append(time.monotonic()))
        timer.start()
        def until(predicate, seconds):
            deadline = time.monotonic()+seconds
            while not predicate() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
            return predicate()
        try:
            dlg._pump()
            self.assertTrue(until(entered.is_set, 5))
            before = len(beats)
            start = time.monotonic()
            dlg._stop()
            self.assertLess(time.monotonic()-start, .2)
            dlg._completed(11, "TTS_CANCELLED")
            self.assertTrue(dlg.busy)
            self.assertFalse(dlg.play.isEnabled())
            self.assertTrue(until(lambda: not dlg.busy, 4))
            self.assertGreater(len(beats), before)
            self.assertTrue(dlg.play.isEnabled())
            self.assertEqual(dlg.status.text(), "Gestoppt.")
            dlg._provider_factory.assert_not_called()
        finally:
            dlg._stop()
            until(lambda: dlg._output_generation is None, 4)
            timer.stop()
            dlg.close()
        self.assertFalse(any(p.name == "voxbridge-audio-output" for p in mp.active_children()))
