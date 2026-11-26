"""Offline contract tests. No local model or HTTP server is contacted."""
import io
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import Mock

from voxbridge.speech_core_tts import SpeechCoreTTS, LocalTTSFailure, MODEL


def frame(data):
    return struct.pack(">I", len(data)) + data


class Response:
    status = 200

    def __init__(self, data, headers=None):
        self.data = io.BytesIO(data)
        self.headers = {
            "Content-Type": "application/x-omoi-pcm-stream",
            "X-OMOI-PCM-Rate": "24000",
            "X-OMOI-PCM-Format": "s16le",
            "X-OMOI-TTS-Model": MODEL,
        }
        self.headers.update(headers or {})

    def close(self):
        self.data.close()

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, n):
        return self.data.read(min(n, 3))  # deliberately fragmented


class LocalTTSContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = Mock()
        self.conn.connect.side_effect = lambda: setattr(self.conn, 'sock', Mock())
        self.factory = Mock(return_value=self.conn)
        self.adapter = SpeechCoreTTS(port=18190, token="x" * 32,
                                     connection_factory=self.factory)
        self.stop = threading.Event()
        self.output = []

    def stream(self, data, headers=None, **kwargs):
        self.conn.getresponse.return_value = Response(data, headers)
        return self.adapter.stream("Test", "de", self.output.append,
                                   authorized=True, stop=self.stop, **kwargs)

    def test_authorization_and_cancel_before_any_connection(self):
        with self.assertRaisesRegex(LocalTTSFailure, "AUTHORIZATION_REQUIRED"):
            self.adapter.stream("Test", "de", self.output.append,
                                authorized=False, stop=self.stop)
        self.stop.set()
        with self.assertRaisesRegex(LocalTTSFailure, "CANCELLED"):
            self.stream(b"")
        self.factory.assert_not_called()

    def test_framing_identity_and_local_request(self):
        first, second = bytes(6), bytes((1, 0)) * 4
        self.assertEqual(self.stream(frame(first) + frame(second) + bytes(4)), 14)
        self.assertEqual(self.output, [first, second])
        self.factory.assert_called_once_with("127.0.0.1", 18190, timeout=2.0)
        method, path = self.conn.request.call_args.args
        self.assertEqual((method, path), ("POST", "/v1/audio/stream"))
        body = json.loads(self.conn.request.call_args.kwargs["body"])
        self.assertEqual((body["model"], body["voice"], body["language"]),
                         (MODEL, "reference", "de"))
        self.conn.close.assert_called_once()

    def test_invalid_header_no_audio_and_closes(self):
        with self.assertRaisesRegex(LocalTTSFailure, "FORMAT_MISMATCH"):
            self.stream(frame(bytes(2)) + bytes(4), {"X-OMOI-PCM-Rate": "16000"})
        self.assertEqual(self.output, [])
        self.conn.close.assert_called_once()

    def test_malformed_truncated_remote_and_empty_frames(self):
        for data, code in ((bytes(4), "EMPTY_STREAM"),
                           (bytes((255,)) * 4, "REMOTE_ERROR"),
                           (struct.pack(">I", 3), "FRAME_LIMIT"),
                           (struct.pack(">I", 2**22), "FRAME_LIMIT"),
                           (struct.pack(">I", 8) + bytes(2), "INCOMPLETE_STREAM"),
                           (frame(bytes(2)), "INCOMPLETE_STREAM")):
            with self.subTest(code=code):
                self.output.clear()
                with self.assertRaisesRegex(LocalTTSFailure, code):
                    self.stream(data)
        self.assertEqual(self.conn.close.call_count, 6)

    def test_cancel_after_read_fences_audio(self):
        response = Response(frame(bytes(8)) + bytes(4))
        read = response.read
        def stopping_read(n):
            result = read(n)
            self.stop.set()
            return result
        response.read = stopping_read
        self.conn.getresponse.return_value = response
        with self.assertRaisesRegex(LocalTTSFailure, "CANCELLED"):
            self.adapter.stream("Test", "de", self.output.append,
                                authorized=True, stop=self.stop)
        self.assertEqual(self.output, [])
        self.conn.close.assert_called_once()

    def test_invalid_token_and_port_no_connection(self):
        for token in ("short", "x" * 32 + chr(10)):
            adapter = SpeechCoreTTS(port=18190, token=token,
                                    connection_factory=self.factory)
            with self.assertRaisesRegex(LocalTTSFailure, "TOKEN_REQUIRED"):
                adapter.stream("Test", "de", self.output.append,
                               authorized=True, stop=self.stop)
        with self.assertRaisesRegex(LocalTTSFailure, "INVALID_PORT"):
            SpeechCoreTTS(port="18190", connection_factory=self.factory)
        self.factory.assert_not_called()

    def test_callback_error_is_sanitized_and_connection_closed(self):
        self.conn.getresponse.return_value = Response(frame(bytes(2)) + bytes(4))
        def fail(data):
            raise RuntimeError("private text")
        with self.assertRaisesRegex(LocalTTSFailure, "^LOCAL_TTS_STREAM_FAILED$"):
            self.adapter.stream("Test", "de", fail, authorized=True, stop=self.stop)
        self.conn.close.assert_called_once()


class StreamingHandler(BaseHTTPRequestHandler):
    """Local synthetic endpoint; no inference or physical sound."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = frame(b"\x01\x00"*8) + frame(b"\x02\x00"*8) + bytes(4)
        mode = self.server.mode
        try:
            if mode == "headers":
                self.server.entered.set()
                self.server.release.wait(3)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-omoi-pcm-stream")
            self.send_header("X-OMOI-PCM-Rate", "24000")
            self.send_header("X-OMOI-PCM-Format", "s16le")
            self.send_header("X-OMOI-TTS-Model", MODEL)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if mode == "trickle":
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    self.server.entered.set()
                    if self.server.release.wait(.04):
                        return
            elif mode == "body":
                self.wfile.write(body[:22])
                self.wfile.flush()
                self.server.entered.set()
                self.server.release.wait(3)
            else:
                self.wfile.write(body[:5])
                self.wfile.flush()
                time.sleep(.15)
                self.wfile.write(body[5:])
        except OSError:
            pass


class RealLoopbackTTSTests(unittest.TestCase):
    def setUp(self):
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), StreamingHandler)
        self.http.mode = "ok"
        self.http.entered = threading.Event()
        self.http.release = threading.Event()
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.adapter = SpeechCoreTTS(port=self.http.server_port, token="x"*32)
        self.stop = threading.Event()
        self.output = []
        self.first_pcm = threading.Event()
        self.addCleanup(self.dispose)

    def dispose(self):
        self.http.release.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(1)

    def stream(self):
        def emit(pcm):
            self.output.append(pcm)
            self.first_pcm.set()
        return self.adapter.stream("Synthetic", "de", emit,
                                   authorized=True, stop=self.stop)

    def test_delayed_frame_preserves_exact_pcm(self):
        self.assertEqual(self.stream(), 32)
        self.assertEqual(self.output, [b"\x01\x00"*8, b"\x02\x00"*8])

    def test_dripping_body_has_absolute_timeout(self):
        self.http.mode = "trickle"
        self.adapter.STREAM_SECONDS = .3
        start = time.monotonic()
        with self.assertRaisesRegex(LocalTTSFailure, "^LOCAL_TTS_TIMEOUT$"):
            self.stream()
        self.assertLess(time.monotonic()-start, 2)
        self.assertTrue(self.http.entered.is_set())
        self.assertEqual(self.output, [])

    def cancel_blocked(self, mode):
        self.http.mode = mode
        errors = []
        def run():
            try:
                self.stream()
            except LocalTTSFailure as exc:
                errors.append(str(exc))
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(self.http.entered.wait(2))
            if mode == "body":
                self.assertTrue(self.first_pcm.wait(2))
            self.stop.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ["LOCAL_TTS_CANCELLED"])
            self.assertEqual(self.output, [b"\x01\x00"*8] if mode=="body" else [])
        finally:
            self.http.release.set()
            worker.join(2)
        self.http.mode = "ok"
        self.stop = threading.Event()
        self.output.clear()
        self.assertEqual(self.stream(), 32)

    def test_cancel_waiting_for_headers(self):
        self.cancel_blocked("headers")

    def test_cancel_mid_frame_preserves_only_complete_pcm(self):
        self.cancel_blocked("body")


if __name__ == "__main__":
    unittest.main()
