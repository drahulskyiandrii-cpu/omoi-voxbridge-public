"""Native localhost WebSocket tests. No cloud, capture, models or GPU."""
import asyncio
import json
import socketserver
import threading
import time
import unittest
from unittest.mock import patch

from voxbridge.cloud_budget import CloudAbort
from voxbridge.realtime_socket import RealtimeSocket

try:
    from websockets.sync.server import serve
except ImportError:
    serve = None


class RawHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            part = self.request.recv(1024)
            if not part:
                return
            data += part
        self.server.requests.append(data)
        self.server.entered.set()
        if self.server.response:
            self.request.sendall(self.server.response)
        try:
            # Observe the peer disconnect on cancel, not merely a returned error.
            if self.request.recv(1) == b"":
                self.server.disconnected.set()
        except (ConnectionResetError, ConnectionAbortedError):
            self.server.disconnected.set()
        except OSError:
            pass


@unittest.skipUnless(serve is not None, "websockets caption extra required")
class SocketTests(unittest.TestCase):
    def raw_server(self, response=b""):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), RawHandler)
        server.daemon_threads = True
        server.requests = []
        server.response = response
        server.entered = threading.Event()
        server.disconnected = threading.Event()
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(cleanup)
        return server, f"ws://127.0.0.1:{server.server_address[1]}"

    def new_socket(self, stop=None, seconds=5):
        client = RealtimeSocket(stop or threading.Event(), time.monotonic()+seconds)
        self.addCleanup(client.close)
        return client

    def test_cancel_stalled_opening_handshake_disconnects(self):
        server, url = self.raw_server()
        stop = threading.Event()
        client = self.new_socket(stop)
        def cancel():
            if server.entered.wait(2):
                stop.set()
        thread = threading.Thread(target=cancel, daemon=True)
        thread.start()
        start = time.monotonic()
        with self.assertRaisesRegex(CloudAbort, "CANCELLED"):
            client.connect(url, open_timeout=4, proxy=None)
        self.assertLess(time.monotonic()-start, 2)
        self.assertTrue(server.disconnected.wait(1))
        client.close()
        thread.join(2)

    def test_redirect_never_contacts_second_server(self):
        target, target_url = self.raw_server()
        reply = ("HTTP/1.1 302 Found\r\nLocation: "+target_url+
                 "/redirect\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").encode()
        server, url = self.raw_server(reply)
        client = self.new_socket()
        with self.assertRaises(Exception):
            client.connect(url, open_timeout=2, proxy=None,
                           additional_headers={"Authorization": "Bearer synthetic-only"})
        self.assertEqual(len(server.requests), 1)
        self.assertFalse(target.entered.wait(.2))
        client.close()

    def test_real_exchange_receive_poll_and_clean_close(self):
        messages = []
        finished = threading.Event()
        def handler(ws):
            try:
                messages.append(ws.recv(timeout=3))
                ws.send("synthetic")
                ws.recv(timeout=3)
            except Exception:
                pass
            finally:
                finished.set()
        with serve(handler, "127.0.0.1", 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = self.new_socket()
            try:
                client.connect(f"ws://127.0.0.1:{server.socket.getsockname()[1]}",
                               open_timeout=2, proxy=None, close_timeout=1)
                with self.assertRaises(TimeoutError):
                    client.recv(.01)
                client.send("hello")
                self.assertEqual(client.recv(1), "synthetic")
                client.close()
                self.assertTrue(finished.wait(1))
                self.assertEqual(messages, ["hello"])
                self.assertTrue(client.loop.is_closed())
            finally:
                client.close()
                server.shutdown()
                thread.join(2)

    def test_production_caption_pipeline_with_local_peer(self):
        from voxbridge.captions_core import CaptionCoordinator
        from voxbridge.realtime_transport import run_realtime_transcription
        received = []
        finished = threading.Event()
        def handler(ws):
            try:
                for raw in ws:
                    event = json.loads(raw)
                    received.append(event["type"])
                    if event["type"] == "session.update":
                        ws.send(json.dumps({"type": "session.updated",
                                            "session": {"type": "transcription"}}))
                    elif event["type"] == "input_audio_buffer.commit":
                        ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": "one"}))
                        ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                                            "item_id": "one", "transcript": "Synthetic"}))
            finally:
                finished.set()
        with serve(handler, "127.0.0.1", 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            core = CaptionCoordinator()
            generation = core.begin(source_language="de", target_language=None)
            try:
                with patch("voxbridge.realtime_transport.REALTIME_TRANSCRIPTION_URL",
                           f"ws://127.0.0.1:{server.socket.getsockname()[1]}"):
                    turns = run_realtime_transcription(
                        core, generation, [bytes((0, 32))*4800], approved=True,
                        stop=threading.Event(), api_key="synthetic-only")
                self.assertEqual(turns, 1)
                self.assertTrue(finished.wait(1))
                self.assertEqual(received, ["session.update", "input_audio_buffer.append",
                                            "input_audio_buffer.commit"])
                self.assertEqual(core.snapshot().lines[0].original, "Synthetic")
            finally:
                server.shutdown()
                thread.join(2)

    def test_absolute_budget_limits_opening(self):
        server, url = self.raw_server()
        client = self.new_socket(seconds=.8)
        with self.assertRaisesRegex(CloudAbort, "TIMEOUT"):
            client.connect(url, open_timeout=4, proxy=None)
        self.assertTrue(server.entered.is_set())
        self.assertTrue(server.disconnected.wait(1))

    def test_cancel_and_deadline_during_blocked_send_abort(self):
        class Transport:
            aborted = False
            def abort(self):
                self.aborted = True
        class Connection:
            def __init__(self):
                self.transport = Transport()
            async def send(self, _):
                await asyncio.Event().wait()
            async def wait_closed(self):
                self.closed = True
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                stop = threading.Event()
                client = self.new_socket(stop)
                conn = client.connection = Connection()
                timer = threading.Timer(.1, stop.set)
                if cancel:
                    timer.start()
                try:
                    with patch("voxbridge.realtime_socket.SEND_SECONDS", .25):
                        with self.assertRaisesRegex(CloudAbort, "CANCELLED" if cancel else "TIMEOUT"):
                            client.send("synthetic")
                    client.close()
                    self.assertTrue(conn.transport.aborted)
                    self.assertTrue(conn.closed)
                finally:
                    timer.cancel()

    def test_close_failure_is_typed_and_loop_closed(self):
        class Transport:
            def abort(self):
                raise OSError("private")
        class Connection:
            transport = Transport()
            async def close(self):
                raise OSError("private")
        client = self.new_socket()
        client.connection = Connection()
        with self.assertRaisesRegex(CloudAbort, "^CLEANUP_FAILED$"):
            client.close()
        self.assertTrue(client.loop.is_closed())
