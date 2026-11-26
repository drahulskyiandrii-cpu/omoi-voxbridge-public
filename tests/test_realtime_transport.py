"""WebSocket/turn simulations: zero real network, audio devices, API or GPU."""
import json
import threading
import unittest
from collections import deque
from types import SimpleNamespace as NS
from unittest.mock import Mock, MagicMock

try:
    import numpy as np
except ImportError:
    np = None

from voxbridge.captions_core import CaptionCoordinator
from voxbridge.wasapi_capture import capture_loopback
from voxbridge.capture_isolation import CAPTURE_IDLE
from voxbridge.session_limits import RealtimeUsage
from voxbridge.realtime_transport import (
    EnergyTurnDetector, RealtimeTransportFailure,
    run_realtime_transcription,
)


class FakeWebsocket:
    def __init__(self, *, ready=True, fail_on_send=False):
        self.received = deque()
        self.sent = []
        self.closed = False
        self.commit_number = 0
        self.append_count = 0
        self.fail_on_send = fail_on_send
        if ready:
            self.received.append({"type": "session.updated", "session": {"type": "transcription"}})

    def send(self, raw):
        if self.fail_on_send:
            raise RuntimeError("private error secret")
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "input_audio_buffer.append":
            self.append_count += 1
        if event["type"] == "input_audio_buffer.commit":
            item = "item_" + str(self.commit_number)
            self.commit_number += 1
            self.received.extend((
                {"type": "input_audio_buffer.committed", "item_id": item},
                {"type": "conversation.item.input_audio_transcription.delta",
                 "item_id": item, "delta": "DHCP"},
                {"type": "conversation.item.input_audio_transcription.completed",
                 "item_id": item, "transcript": "DHCP verteilt IP-Adressen."},
            ))

    def recv(self, timeout):
        if self.received:
            return json.dumps(self.received.popleft())
        raise TimeoutError()

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, socket=None):
        self.socket = socket if socket is not None else FakeWebsocket()
        self.calls = []
    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.socket


@unittest.skipUnless(np is not None, "NumPy required for test signal")
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.core = CaptionCoordinator()
        self.gen = self.core.begin(source_language="de", target_language=None)
        self.fake = Factory()
        self.stop = threading.Event()

    @staticmethod
    def voiced():
        return np.full(4800, 8000, dtype="<i2").tobytes()

    @staticmethod
    def silent():
        return bytes(9600)

    def run_session(self, chunks, **overrides):
        return run_realtime_transcription(
            self.core, self.gen, chunks, approved=True, stop=self.stop,
            socket_factory=self.fake, api_key="test-only",
            **overrides,
        )

    def test_consent_denial_no_generator_iteration_or_network(self):
        generator = MagicMock()
        factory = Mock()
        with self.assertRaisesRegex(RealtimeTransportFailure, "CONSENT_REQUIRED"):
            run_realtime_transcription(
                self.core, self.gen, generator, approved=False,
                stop=self.stop, socket_factory=factory, api_key="test-only")
        factory.assert_not_called()
        generator.__iter__.assert_not_called()

    def test_socket_url_headers_and_one_manual_commit(self):
        n = self.run_session([self.voiced()] * 2 + [self.silent()] * 3)
        self.assertEqual(n, 1)
        self.assertEqual(self.fake.socket.append_count, 5)
        self.assertEqual(self.fake.socket.commit_number, 1)
        self.assertTrue(self.fake.socket.closed)
        self.assertEqual(len(self.core.snapshot().lines), 1)
        self.assertEqual(self.core.snapshot().lines[0].original,
                         "DHCP verteilt IP-Adressen.")
        url, args = self.fake.calls[0]
        self.assertEqual(url, "wss://api.openai.com/v1/realtime?intent=transcription")
        self.assertEqual(args["additional_headers"],
                         {"Authorization": "Bearer test-only"})
        self.assertIsNone(args["proxy"])
        self.assertEqual(self.fake.socket.sent[0]["type"], "session.update")

    def test_cleanup_failure_survives_transport_error_and_socket_closes(self):
        from voxbridge.wasapi_capture import CaptureFailure
        def source():
            try:
                yield self.voiced()
                yield self.voiced()
            finally:
                raise CaptureFailure("WASAPI_CHILD_CLEANUP_FAILED")
        real_send = self.fake.socket.send
        def send(raw):
            if json.loads(raw)["type"] == "input_audio_buffer.append":
                raise RuntimeError("private")
            real_send(raw)
        self.fake.socket.send = send
        with self.assertRaisesRegex(CaptureFailure,"^WASAPI_CHILD_CLEANUP_FAILED$"):
            self.run_session(source())
        self.assertTrue(self.fake.socket.closed)

    def test_socket_close_failure_is_not_silently_ignored(self):
        self.fake.socket.close = Mock(side_effect=OSError("private"))
        with self.assertRaisesRegex(RealtimeTransportFailure, "^REALTIME_CLEANUP_FAILED$"):
            self.run_session([self.silent()])

    def test_leading_silence_is_not_sent_or_charged(self):
        n = self.run_session([self.silent()] * 2)
        self.assertEqual(n, 0)
        self.assertEqual(self.fake.socket.append_count, 0)
        self.assertEqual(self.fake.socket.commit_number, 0)
        self.assertTrue(self.fake.socket.closed)

    def test_final_partial_turn_committed_on_stream_end(self):
        n = self.run_session([self.voiced()] * 2)
        self.assertEqual(n, 1)
        self.assertEqual(self.fake.socket.commit_number, 1)

    def test_turn_force_commit_at_bounded_chunk_count(self):
        n = self.run_session([self.voiced()] * 12)
        self.assertEqual(n, 1)

    def test_runtime_audio_cap(self):
        with self.assertRaisesRegex(RealtimeTransportFailure, "AUDIO_LIMIT"):
            self.run_session([self.voiced()] * 6, max_seconds=1)
        self.assertTrue(self.fake.socket.closed)

    def test_stop_before_network(self):
        self.stop.set()
        with self.assertRaisesRegex(RealtimeTransportFailure, "CANCELLED"):
            self.run_session([self.voiced()])
        self.assertEqual(self.fake.calls, [])

    def test_invalid_generation_before_network(self):
        self.core.stop()
        with self.assertRaisesRegex(RealtimeTransportFailure, "SESSION_MISMATCH"):
            self.run_session([self.voiced()])
        self.assertEqual(self.fake.calls, [])

    def test_no_key_before_network(self):
        with self.assertRaisesRegex(RealtimeTransportFailure, "API_KEY_MISSING"):
            run_realtime_transcription(
                self.core, self.gen, [self.voiced()], approved=True,
                stop=self.stop, socket_factory=self.fake, api_key="")
        self.assertEqual(self.fake.calls, [])

    def test_socket_error_sanitized_and_closed(self):
        self.fake = Factory(FakeWebsocket(fail_on_send=True))
        with self.assertRaisesRegex(RealtimeTransportFailure, "WEBSOCKET_FAILED") as error:
            self.run_session([self.voiced()])
        self.assertNotIn("private", str(error.exception))
        self.assertTrue(self.fake.socket.closed)

    def test_server_error_sanitized(self):
        self.fake = Factory(FakeWebsocket(ready=False))
        self.fake.socket.received.append(
            {"type": "error", "error": {"message": "private audio secret"}}
        )
        with self.assertRaisesRegex(RealtimeTransportFailure, "SESSION_REJECTED") as err:
            self.run_session([self.voiced()])
        self.assertNotIn("private", str(err.exception))

    def test_invalid_server_session_refused_before_audio_upload(self):
        self.fake = Factory(FakeWebsocket(ready=False))
        self.fake.socket.received.append({
            "type": "session.updated",
            "session": {"type": "realtime"},
        })
        with self.assertRaisesRegex(RealtimeTransportFailure, "WRONG_SESSION_TYPE"):
            self.run_session([self.voiced()])
        self.assertEqual(self.fake.socket.append_count, 0)
        self.assertTrue(self.fake.socket.closed)

    def test_synthetic_wasapi_to_fake_realtime_pipeline(self):
        """Real CPU resampler -> fake SSL transport; never a physical device."""
        class Mic:
            id = "mock_speaker_id"
            name = "Fake speakers"
            isloopback = True

            def recorder(self, samplerate):
                assert samplerate == 48000
                class Recorder:
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        return None
                    def record(self, numframes):
                        return np.full((numframes, 2), 0.45, dtype="float32")
                return Recorder()

        mic = Mic()
        class Card:
            def all_microphones(self, include_loopback):
                assert include_loopback
                return [mic]
            def get_microphone(self, name, include_loopback):
                assert name == mic.id and include_loopback
                return mic

        captured = capture_loopback(
            mic.id, self.stop, seconds=1, backend=Card()
        )
        result = self.run_session(captured, max_seconds=1)
        self.assertGreaterEqual(result, 1)
        self.assertTrue(self.fake.socket.closed)
        self.assertGreaterEqual(self.fake.socket.append_count, 5)
        self.assertEqual(self.core.snapshot().lines[0].original,
                         "DHCP verteilt IP-Adressen.")

    def test_audio_source_generator_closed_after_success(self):
        state = {"closed": False}

        def source():
            try:
                yield self.voiced()
                yield self.silent()
            finally:
                state["closed"] = True

        self.run_session(source())
        self.assertTrue(state["closed"])

    def test_audio_source_generator_closed_on_websocket_failure(self):
        state = {"closed": False}
        class FailingOnAudio(FakeWebsocket):
            def send(self, raw):
                if json.loads(raw)["type"] == "input_audio_buffer.append":
                    raise RuntimeError("User-secret error not for logs")
                return super().send(raw)
        self.fake = Factory(FailingOnAudio())

        def source():
            try:
                yield self.voiced()
                yield self.voiced()
            finally:
                state["closed"] = True

        with self.assertRaisesRegex(RealtimeTransportFailure, "WEBSOCKET_FAILED"):
            self.run_session(source())
        self.assertTrue(state["closed"])
        self.assertTrue(self.fake.socket.closed)

    def test_audio_source_closed_on_invalid_pcm(self):
        state = {"closed": False}

        def source():
            try:
                yield self.voiced()
                yield b"\x00"  # invalid signed16LE
            finally:
                state["closed"] = True

        with self.assertRaisesRegex(RealtimeTransportFailure, "AUDIO_LIMIT"):
            self.run_session(source())
        self.assertTrue(state["closed"])

    def test_short_voiced_onset_not_averaged_away(self):
        pcm=np.zeros(4800,dtype="<i2")
        pcm[-480:]=500
        self.assertEqual(EnergyTurnDetector().feed(pcm.tobytes()),(True,False))
        self.assertEqual(EnergyTurnDetector().feed(bytes(9600)),(False,False))

    def test_preview_received_during_audio_before_commit(self):
        socket=self.fake.socket
        def source():
            yield self.voiced()
            socket.received.append({"type":"conversation.item.input_audio_transcription.delta",
                                    "item_id":"item_0","delta":"Early"})
            yield CAPTURE_IDLE
            self.assertEqual(self.core.snapshot().partial,"Early")
            self.assertEqual(self.core.snapshot().lines,())
            self.assertEqual(socket.commit_number,0)
            yield self.voiced()
        self.assertEqual(self.run_session(source()),1)
        self.assertEqual(self.core.snapshot().partial,"")

    def test_detector_on_chunk_silence_and_forced_commits(self):
        det = EnergyTurnDetector()
        self.assertEqual(det.feed(self.silent()), (False, False))
        self.assertEqual(det.feed(self.voiced()), (True, False))
        self.assertEqual(det.feed(self.silent()), (True, False))
        self.assertEqual(det.feed(self.silent()), (True, False))
        self.assertEqual(det.feed(self.silent()), (True, True))
        self.assertFalse(det.finish())

    def test_long_session_does_not_drain_after_all_turns_settle(self):
        socket = self.fake.socket
        original_recv = socket.recv
        def recv(timeout):
            if timeout == 0.15:
                self.fail("Unnecessary drain after settled turns")
            return original_recv(timeout)
        socket.recv = recv
        self.assertEqual(self.run_session(([self.voiced()] + [self.silent()] * 3) * 8), 8)
        self.assertEqual(len(self.core.snapshot().lines), 6)

    def test_delayed_final_still_drains_until_received(self):
        socket = self.fake.socket
        original_recv = socket.recv
        drained = []
        def recv(timeout):
            if timeout == 0.001 and socket.received:
                if socket.received[0]["type"].endswith(".completed"):
                    raise TimeoutError()
            if timeout == 0.15:
                drained.append(True)
            return original_recv(timeout)
        socket.recv = recv
        self.assertEqual(self.run_session([self.voiced()] * 2), 1)
        self.assertEqual(len(drained), 3)
        self.assertEqual(len(self.core.snapshot().lines), 1)

    def test_idle_ticks_service_server_errors_without_audio_upload(self):
        socket = self.fake.socket
        state = {"closed": False}
        def source():
            try:
                socket.received.append({"type": "error", "error": {"message": "private"}})
                yield CAPTURE_IDLE
                self.fail("Capture should have stopped on server error")
            finally:
                state["closed"] = True
        with self.assertRaisesRegex(RealtimeTransportFailure, "REALTIME_REMOTE_ERROR"):
            self.run_session(source())
        self.assertEqual(socket.append_count, 0)
        self.assertEqual(socket.commit_number, 0)
        self.assertTrue(socket.closed)
        self.assertTrue(state["closed"])

    def test_idle_ticks_do_not_count_as_pcm_or_vad_silence(self):
        pcm = [self.voiced(), CAPTURE_IDLE, CAPTURE_IDLE, CAPTURE_IDLE,
               self.voiced()]
        self.assertEqual(self.run_session(pcm), 1)
        self.assertEqual(self.fake.socket.append_count, 2)

    def test_unsettled_final_timeout_reports_failure(self):
        from unittest.mock import patch
        socket = self.fake.socket
        original_recv = socket.recv
        def recv(timeout):
            if socket.received and socket.received[0]["type"].endswith(".completed"):
                raise TimeoutError()
            return original_recv(timeout)
        socket.recv = recv
        # Real time is not required for a deterministic deadline test.
        ticks = iter(range(100))
        with patch("voxbridge.realtime_transport.time.monotonic",
                   side_effect=lambda: next(ticks)):
            with self.assertRaisesRegex(RealtimeTransportFailure, "REALTIME_FINAL_TIMEOUT"):
                self.run_session([self.voiced()])
        self.assertTrue(socket.closed)

    def test_usage_counts_attempt_before_failed_send(self):
        usage = RealtimeUsage(60)
        socket = self.fake.socket
        original_send = socket.send
        def send(raw):
            if json.loads(raw)["type"] == "input_audio_buffer.append":
                raise OSError("private")
            return original_send(raw)
        socket.send = send
        with self.assertRaisesRegex(RealtimeTransportFailure, "WEBSOCKET_FAILED"):
            self.run_session([self.voiced()], usage=usage)
        self.assertEqual(usage.snapshot().attempted_audio_samples, 4800)
        self.assertEqual(usage.snapshot().attempted_connections, 1)

    def test_unacknowledged_turns_cannot_grow_unbounded(self):
        socket = self.fake.socket
        original_send = socket.send
        def send(raw):
            if json.loads(raw)["type"] == "input_audio_buffer.commit":
                socket.commit_number += 1
                return
            return original_send(raw)
        socket.send = send
        with self.assertRaisesRegex(RealtimeTransportFailure, "UNSETTLED_LIMIT"):
            self.run_session(([self.voiced()] + [self.silent()] * 3) * 13)
        self.assertEqual(socket.commit_number, 12)

    def test_attempt_budget_cannot_be_reused_for_second_connection(self):
        usage = RealtimeUsage(60)
        self.assertEqual(self.run_session([self.voiced()], usage=usage), 1)
        count = len(self.fake.calls)
        with self.assertRaisesRegex(RealtimeTransportFailure, "USAGE_ALREADY_CLAIMED"):
            self.run_session([self.voiced()], usage=usage)
        self.assertEqual(len(self.fake.calls), count)

    def test_source_language_and_delay_used_before_connect(self):
        self.gen = self.core.begin(source_language="ru", target_language="de")
        with self.assertRaisesRegex(RealtimeTransportFailure, "INVALID_DELAY"):
            self.run_session([], delay="bad")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.run_session([], delay="medium"), 0)
        trans = self.fake.socket.sent[0]["session"]["audio"]["input"]["transcription"]
        self.assertEqual(trans["languages"], ["ru"])
        self.assertEqual(trans["delay"], "medium")

    def test_invalid_audio_not_sent(self):
        with self.assertRaisesRegex(RealtimeTransportFailure, "AUDIO_LIMIT"):
            self.run_session([b"\x00"])
        self.assertEqual(self.fake.socket.append_count, 0)

    def test_stop_during_connect_prevents_session_update_and_audio(self):
        socket = self.fake.socket
        def connect(*args, **kwargs):
            self.stop.set()
            return socket
        self.fake = connect
        source = MagicMock()
        self.assertEqual(self.run_session(source), 0)
        self.assertEqual(socket.sent, [])
        source.__iter__.assert_not_called()
        self.assertTrue(socket.closed)

    def test_stop_during_handshake_prevents_capture(self):
        socket = self.fake.socket
        original_recv = socket.recv
        def recv(timeout):
            self.stop.set()
            return original_recv(timeout)
        socket.recv = recv
        source = MagicMock()
        self.assertEqual(self.run_session(source), 0)
        source.__iter__.assert_not_called()
        self.assertEqual(socket.append_count, 0)
        self.assertTrue(socket.closed)

    def test_stop_after_append_does_not_read_next_chunk_or_commit(self):
        socket = self.fake.socket
        original_send = socket.send
        def send(raw):
            original_send(raw)
            if json.loads(raw)["type"] == "input_audio_buffer.append":
                self.stop.set()
        socket.send = send
        state = {"reads": 0, "closed": False}
        def source():
            try:
                while True:
                    state["reads"] += 1
                    yield self.voiced()
            finally:
                state["closed"] = True
        self.assertEqual(self.run_session(source()), 0)
        self.assertEqual(state, {"reads": 1, "closed": True})
        self.assertEqual(socket.commit_number, 0)
        self.assertTrue(socket.closed)

    def test_distinct_iterator_closed_after_send_failure(self):
        state = {"closed": False}
        class Source:
            def __iter__(inner):
                try:
                    yield self.voiced()
                    yield self.voiced()
                finally:
                    state["closed"] = True
        socket = self.fake.socket
        original_send = socket.send
        def send(raw):
            if json.loads(raw)["type"] == "input_audio_buffer.append":
                raise OSError("private socket error")
            original_send(raw)
        socket.send = send
        with self.assertRaisesRegex(RealtimeTransportFailure, "WEBSOCKET_FAILED"):
            self.run_session(Source())
        self.assertTrue(state["closed"])
        self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()
