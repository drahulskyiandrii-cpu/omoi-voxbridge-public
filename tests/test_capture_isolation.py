"""Real Windows spawn/pipe tests, synthetic workers ONLY; no audio or network."""
import multiprocessing as mp
import threading
import time
import unittest

from voxbridge.capture_isolation import CAPTURE_IDLE, capture_isolated
from voxbridge.wasapi_capture import CaptureFailure


def worker_packets(sender, cancel, device_id, seconds):
    with sender:
        sender.send_bytes(b"P" + bytes(9600))
        sender.send_bytes(b"P" + bytes((1, 0)) * 4800)
        sender.send_bytes(b"D")


def worker_blocked(sender, cancel, device_id, seconds):
    with sender:
        sender.send_bytes(b"P" + bytes(9600))
        # Simulate a native call that ignores cancellation indefinitely.
        time.sleep(60)


def worker_silent(sender, cancel, device_id, seconds):
    with sender:
        time.sleep(60)


def worker_error(sender, cancel, device_id, seconds):
    with sender:
        sender.send_bytes(b"EWASAPI_CAPTURE_FAILED")


def worker_private_error(sender, cancel, device_id, seconds):
    with sender:
        sender.send_bytes(b"Eprivate endpoint name")


def worker_malformed(sender, cancel, device_id, seconds):
    with sender:
        sender.send_bytes(b"P" + b"X")


def worker_oversize(sender, cancel, device_id, seconds):
    with sender:
        try:
            sender.send_bytes(b"P" + bytes(9602))
        except OSError:
            pass


def worker_abrupt(sender, cancel, device_id, seconds):
    sender.close()


def worker_excess(sender, cancel, device_id, seconds):
    with sender:
        for _ in range(6):
            sender.send_bytes(b"P" + bytes(9600))


class IsolationTests(unittest.TestCase):
    def stream(self, worker, stop=None, seconds=1):
        return capture_isolated("synthetic", stop or threading.Event(),
                                seconds=seconds, _worker=worker)

    def assert_no_capture_child(self):
        self.assertFalse(any(p.name == "voxbridge-wasapi-capture"
                             for p in mp.active_children()))

    def test_real_spawn_binary_pcm_and_normal_exit(self):
        self.assertEqual(list(self.stream(worker_packets)),
                         [bytes(9600), bytes((1, 0)) * 4800])
        self.assert_no_capture_child()

    def test_stop_reaps_blocked_owned_child(self):
        stop = threading.Event()
        source = self.stream(worker_blocked, stop)
        try:
            self.assertEqual(next(source), bytes(9600))
            start = time.monotonic()
            stop.set()
            self.assertEqual(list(source), [])
            self.assertLess(time.monotonic() - start, 3)
        finally:
            source.close()
        self.assert_no_capture_child()

    def test_close_reaps_blocked_child_after_transport_failure(self):
        source = self.stream(worker_blocked)
        try:
            next(source)
            start = time.monotonic()
            source.close()
            self.assertLess(time.monotonic() - start, 3)
        finally:
            source.close()
        self.assert_no_capture_child()

    def test_stop_while_waiting_for_first_packet(self):
        stop = threading.Event()
        timer = threading.Timer(0.5, stop.set)
        timer.start()
        try:
            start = time.monotonic()
            self.assertEqual(list(self.stream(worker_silent, stop)), [])
            self.assertLess(time.monotonic() - start, 4)
        finally:
            timer.cancel()
            timer.join()
        self.assert_no_capture_child()

    def test_deadline_reaps_child_without_packets(self):
        from unittest.mock import patch
        with patch("voxbridge.capture_isolation.STARTUP_ALLOWANCE", 0.5):
            with self.assertRaisesRegex(CaptureFailure, "CHILD_TIMEOUT"):
                list(self.stream(worker_silent))
        self.assert_no_capture_child()

    def test_idle_heartbeat_without_audio_and_close_reaps_child(self):
        source = capture_isolated("synthetic", threading.Event(), seconds=1,
                                  poll_idle=True, _worker=worker_silent)
        try:
            self.assertIs(next(source), CAPTURE_IDLE)
        finally:
            source.close()
        self.assert_no_capture_child()

    def test_precancel_never_starts_child(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(list(self.stream(worker_blocked, stop)), [])
        self.assert_no_capture_child()

    def test_bad_messages_and_crash_fail_closed(self):
        for worker, code in ((worker_error, "WASAPI_CAPTURE_FAILED"),
                             (worker_private_error, "WASAPI_CHILD_PROTOCOL"),
                             (worker_malformed, "WASAPI_CHILD_PROTOCOL"),
                             (worker_oversize, "WASAPI_CHILD_PROTOCOL"),
                             (worker_abrupt, "WASAPI_CHILD_EXITED"),
                             (worker_excess, "WASAPI_CHILD_PROTOCOL")):
            with self.subTest(worker=worker.__name__):
                with self.assertRaisesRegex(CaptureFailure, "^" + code + "$"):
                    list(self.stream(worker))
                self.assert_no_capture_child()


    def test_concurrent_capture_blocked_until_owned_source_closed(self):
        source = self.stream(worker_blocked)
        try:
            self.assertEqual(next(source),bytes(9600))
            with self.assertRaisesRegex(CaptureFailure,"WASAPI_CAPTURE_BUSY"):
                list(self.stream(worker_packets))
        finally:
            source.close()
        self.assertEqual(len(list(self.stream(worker_packets))),2)
        self.assert_no_capture_child()

    def test_unreaped_child_is_quarantined_without_unbounded_join(self):
        from unittest.mock import Mock, patch, call
        from voxbridge import capture_isolation as capture
        child = Mock()
        child.is_alive.return_value = True
        with patch.object(capture,"_failed_child",None), patch.object(capture.mp,"get_context") as spawn:
            with self.assertRaisesRegex(CaptureFailure,"WASAPI_CHILD_CLEANUP_FAILED"):
                capture._reap_child(child)
            self.assertIs(capture._failed_child,child)
            self.assertEqual(child.join.call_args_list,[call(capture.GRACE_SECONDS),
                              call(capture.JOIN_SECONDS),call(capture.JOIN_SECONDS)])
            child.terminate.assert_called_once()
            child.kill.assert_called_once()
            child.close.assert_not_called()
            self.assertEqual(capture.capture_block_reason(),"WASAPI_CHILD_CLEANUP_FAILED")
            with self.assertRaisesRegex(CaptureFailure,"WASAPI_CHILD_CLEANUP_FAILED"):
                list(self.stream(worker_packets))
            spawn.assert_not_called()

    def test_failed_terminate_keeps_exact_handle(self):
        from unittest.mock import Mock, patch
        from voxbridge import capture_isolation as capture
        child = Mock()
        child.is_alive.return_value = True
        child.terminate.side_effect = OSError("private")
        with patch.object(capture,"_failed_child",None):
            with self.assertRaisesRegex(CaptureFailure,"^WASAPI_CHILD_CLEANUP_FAILED$"):
                capture._reap_child(child)
            self.assertIs(capture._failed_child,child)
            child.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
