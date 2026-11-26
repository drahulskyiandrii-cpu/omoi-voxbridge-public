import base64
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from voxbridge.audio import pcm_to_wav
from voxbridge.stt import SpeechCoreHTTP, SpeechFailure, MiniOneShotSTT

TOKEN = "x" * 40
AUDIO = pcm_to_wav(b"\x01\x00" * 4800)

class Handler(BaseHTTPRequestHandler):
    response_mode = "ok"
    calls = 0
    def log_message(self, fmt, *args):
        pass

    def handle_call(self):
        self.__class__.calls += 1
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/health":
            reply = {"service": "omoi-speech-core-local-stt",
                     "model": "faster-whisper-small-ct2", "ready": True}
        else:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert base64.b64decode(body["audio_b64"]) == AUDIO
            reply = {"model": "faster-whisper-small-ct2",
                     "language": body["language"], "text": "Guten Tag"}
            if self.__class__.response_mode == "bad-model":
                reply["model"] = "untrusted-model"
        blob = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)
    def do_GET(self):
        self.handle_call()
    def do_POST(self):
        self.handle_call()

class TestSharedSTT(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.response_mode = "ok"

    def test_oneshot_rejects_nonboolean_success(self):
        from pathlib import Path
        from unittest.mock import patch
        import os
        import sys
        import tempfile
        backend = MiniOneShotSTT()
        script = ("import sys,json; print(json.dumps({'event':'ready'}),flush=True); "
                  "sys.stdin.readline(); print(json.dumps({'id':1,'ok':'false','text':'synthetic'}),flush=True)")
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                backend, "_launch_spec",
                return_value=([sys.executable, "-u", "-c", script], Path(tmp)/"worker.py", os.environ.copy())):
            with self.assertRaisesRegex(SpeechFailure, "LOCAL_PARTIAL_BAD_REPLY"):
                backend.transcribe(AUDIO, "de")

    def test_cancel_only_signals_no_process_operations(self):
        from unittest.mock import patch
        backend = MiniOneShotSTT()
        with patch("voxbridge.stt_process.subprocess.Popen") as launch:
            backend.cancel()
        self.assertTrue(backend._cancel.is_set())
        launch.assert_not_called()

    def test_real_local_socket_no_gpu(self):
        backend = SpeechCoreHTTP(port=self.http.server_port, token=TOKEN)
        self.assertEqual(backend.transcribe(AUDIO, "de"), "Guten Tag")

    def test_reject_server_mismatch(self):
        Handler.response_mode = "bad-model"
        backend = SpeechCoreHTTP(port=self.http.server_port, token=TOKEN)
        with self.assertRaises(SpeechFailure):
            backend.transcribe(AUDIO, "ru")

    def test_no_token(self):
        with self.assertRaises(SpeechFailure):
            SpeechCoreHTTP(port=self.http.server_port, token="")

    def test_language_and_wav_validation(self):
        backend = SpeechCoreHTTP(port=self.http.server_port, token=TOKEN)
        for lang, audio in (("xx", AUDIO), ("de", b"bad")):
            with self.subTest(lang=lang), self.assertRaises(SpeechFailure):
                backend.transcribe(audio, lang)

    def test_mini_english_rejected_before_gpu(self):
        with self.assertRaises(SpeechFailure):
            MiniOneShotSTT().transcribe(AUDIO, "en")

class SlowHandler(BaseHTTPRequestHandler):
    """A loopback-only fake service, including malformed and slow HTTP peers."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        if self.server.mode == "slow-health":
            self.server.entered.set()
            self.server.release.wait(3)
        reply = {"service": "omoi-speech-core-local-stt",
                 "model": "faster-whisper-small-ct2", "ready": True}
        self.reply(reply)

    def do_POST(self):
        self.server.paths.append(self.path)
        self.rfile.read(int(self.headers["Content-Length"]))
        reply = {"model": "faster-whisper-small-ct2",
                 "language": "de", "text": "Guten Tag"}
        if self.server.mode == "nul":
            reply["text"] = "bad\x00text"
        if self.server.mode == "surrogate":
            reply["text"] = "\ud800"
        self.reply(reply, mode=self.server.mode)

    def reply(self, reply, mode="ok"):
        body = json.dumps(reply).encode("ascii")
        try:
            if mode == "slow-headers":
                # Each new byte would reset an ordinary inactivity timeout.
                for byte in b"HTTP/1.1 200 OK\r\n":
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    self.server.entered.set()
                    if self.server.release.wait(.04):
                        return
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            length = len(body)
            if mode == "truncated":
                length += 10  # valid JSON, invalid message framing
            if mode == "oversize":
                length = 9000
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if mode == "slow-body":
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    self.server.entered.set()
                    if self.server.release.wait(.04):
                        return
            elif mode == "blocked-body":
                self.wfile.write(body[:1])
                self.wfile.flush()
                self.server.entered.set()
                self.server.release.wait(3)
            elif mode == "delayed-success":
                self.wfile.write(body[:5])
                self.wfile.flush()
                time.sleep(.15)  # exceeds several polling timeouts
                self.wfile.write(body[5:])
            elif mode != "oversize":
                self.wfile.write(body)
        except OSError:
            pass  # Expected after the client's own socket shutdown.


class HTTPDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        self.http.mode = "ok"
        self.http.paths = []
        self.http.entered = threading.Event()
        self.http.release = threading.Event()
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.backend = SpeechCoreHTTP(port=self.http.server_port, token=TOKEN)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.http.release.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(1)

    def test_precancel_prevents_any_connection(self):
        self.backend.cancel()
        with self.assertRaisesRegex(SpeechFailure, "^STT_CANCELLED$"):
            self.backend.transcribe(AUDIO, "de")
        self.assertEqual(self.http.paths, [])

    def test_slow_headers_cannot_extend_absolute_deadline(self):
        self.http.mode = "slow-headers"
        self.backend.DECODE_SECONDS = .3
        start = time.monotonic()
        with self.assertRaisesRegex(SpeechFailure, "^STT_REQUEST_TIMEOUT$"):
            self.backend.transcribe(AUDIO, "de")
        self.assertLess(time.monotonic() - start, 2)
        self.assertTrue(self.http.entered.is_set())
        self.assertEqual(self.http.paths, ["/health", "/v1/audio/transcriptions"])

    def test_slow_body_cannot_extend_absolute_deadline(self):
        self.http.mode = "slow-body"
        self.backend.DECODE_SECONDS = .3
        start = time.monotonic()
        with self.assertRaisesRegex(SpeechFailure, "^STT_REQUEST_TIMEOUT$"):
            self.backend.transcribe(AUDIO, "de")
        self.assertLess(time.monotonic() - start, 2)
        self.assertTrue(self.http.entered.is_set())

    def test_health_timeout_prevents_audio_post(self):
        self.http.mode = "slow-health"
        self.backend.HEALTH_SECONDS = .25
        with self.assertRaisesRegex(SpeechFailure, "^STT_REQUEST_TIMEOUT$"):
            self.backend.transcribe(AUDIO, "de")
        self.assertEqual(self.http.paths, ["/health"])

    def cancel_while_blocked(self, mode, expected_paths):
        self.http.mode = mode
        errors = []
        def run():
            try:
                self.backend.transcribe(AUDIO, "de")
            except SpeechFailure as exc:
                errors.append(str(exc))
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(self.http.entered.wait(2))
            start = time.monotonic()
            self.backend.cancel()
            self.assertLess(time.monotonic() - start, .1)
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ["STT_CANCELLED"])
            self.assertEqual(self.http.paths, expected_paths)
        finally:
            self.http.release.set()
            worker.join(2)
        # Only the request is aborted; a fresh client can still use the server.
        self.http.mode = "ok"
        fresh = SpeechCoreHTTP(port=self.http.server_port, token=TOKEN)
        self.assertEqual(fresh.transcribe(AUDIO, "de"), "Guten Tag")

    def test_cancel_during_health_prevents_audio_post(self):
        self.cancel_while_blocked("slow-health", ["/health"])

    def test_cancel_during_body_interrupts_detached_connection(self):
        self.cancel_while_blocked("blocked-body", ["/health", "/v1/audio/transcriptions"])

    def test_read_polling_preserves_bytes_across_multiple_timeouts(self):
        self.http.mode = "delayed-success"
        self.backend.DECODE_SECONDS = 2
        self.assertEqual(self.backend.transcribe(AUDIO, "de"), "Guten Tag")

    def test_partial_socket_send_preserves_exact_payload(self):
        import socket
        from unittest.mock import Mock
        from voxbridge.loopback_http import _PollingHTTPSocket
        sock = Mock()
        sent = bytearray()
        outcomes = iter([2, socket.timeout(), 3])
        def send(pending):
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            sent.extend(pending[:result])
            return result
        sock.send.side_effect = send
        wrapped = _PollingHTTPSocket(sock, lambda: None, .05)
        wrapped.sendall(b"hello")
        self.assertEqual(sent, b"hello")
        self.assertEqual(sock.send.call_count, 3)

    def test_truncated_valid_json_is_not_success(self):
        self.http.mode = "truncated"
        with self.assertRaisesRegex(SpeechFailure, "^STT_INCOMPLETE_RESPONSE$"):
            self.backend.transcribe(AUDIO, "de")

    def test_oversized_content_length_rejected_without_waiting_for_body(self):
        self.http.mode = "oversize"
        with self.assertRaisesRegex(SpeechFailure, "^STT_RESPONSE_TOO_LARGE$"):
            self.backend.transcribe(AUDIO, "de")

    def test_invalid_text_rejected_without_leaking_content(self):
        for mode in ("nul", "surrogate"):
            self.http.mode = mode
            with self.assertRaisesRegex(SpeechFailure, "^STT_BAD_RESPONSE$"):
                self.backend.transcribe(AUDIO, "de")

    def test_invalid_token_rejected_before_network(self):
        for token in (123, "x"*513, "x"*40+"\r\n", "x"*40+" "):
            with self.assertRaisesRegex(SpeechFailure, "TOKEN_NOT_CONFIGURED"):
                SpeechCoreHTTP(port=self.http.server_port, token=token)
        self.assertEqual(self.http.paths, [])

    def test_busy_client_does_not_start_second_request(self):
        self.backend._lock.acquire()
        try:
            with self.assertRaisesRegex(SpeechFailure, "^STT_BUSY$"):
                self.backend.transcribe(AUDIO, "de")
        finally:
            self.backend._lock.release()
        self.assertEqual(self.http.paths, [])
        self.assertEqual(self.backend.transcribe(AUDIO, "de"), "Guten Tag")
        self.assertEqual(self.backend.transcribe(AUDIO, "de"), "Guten Tag")
