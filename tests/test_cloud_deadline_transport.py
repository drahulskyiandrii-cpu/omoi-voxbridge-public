"""Actual local HTTP and SDK tests. No public API traffic or credentials."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

try:
    import httpx
    from openai import OpenAI
    from cartesia import Cartesia
except ImportError:
    httpx = None

from voxbridge.cloud_budget import CloudAbort, cloud_budget, current_budget
from voxbridge.cloud_providers import OpenAIFileSTT, CartesiaTTSProvider, OpenAIResponses
from voxbridge.stt import SpeechFailure
from voxbridge.audio import pcm_to_wav
from voxbridge.analysis_providers import ProviderFailure
from voxbridge.tts_stream import stream_to_callback


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # client deliberately cancelled/closed the connection

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
        self.do_GET(record=False)

    def do_GET(self, record=True):
        if record:
            self.server.requests.append((self.path,b""))
        mode = self.server.mode
        try:
            if mode == "headers":
                self.server.entered.set()
                self.server.release.wait(4)
                self.close_connection = True
                return
            if mode == "drip_headers":
                for value in b"HTTP/1.1 200 OK\r\n":
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    self.server.entered.set()
                    if self.server.release.wait(.04):
                        break
                self.close_connection = True
                return
            content_type = "application/json"
            data = b'{"text":"synthetic"}'
            if mode == "voice_pages":
                time.sleep(1.0)
                data = (b'{"data":[],"has_more":false}' if "starting_after=" in self.path
                        else b'{"data":[],"has_more":true,"next_page":"11111111-1111-4111-8111-111111111111"}')
            elif mode in ("sse","sse_wait"):
                content_type = "text/event-stream"
                pcm=base64.b64encode(bytes((1,0,2,0))).decode("ascii")
                data=("data: "+json.dumps({"type":"chunk","data":pcm,"status_code":200,
                    "done":False,"step_time":1,"context_id":"test"})+"\n\n").encode()
                if mode=="sse":
                    data+=("data: "+json.dumps({"type":"done","status_code":200,
                        "done":True,"context_id":"test"})+"\n\n").encode()
            elif mode == "oversize":
                data=bytes(4096)
            elif mode == "chunked_oversize":
                self.send_response(200)
                self.send_header("Transfer-Encoding","chunked")
                self.end_headers()
                for _ in range(3):
                    self.wfile.write(b"40\r\n"+bytes(64)+b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            self.send_response(200)
            self.send_header("Content-Type",content_type)
            self.send_header("Content-Length",str(len(data)+(100 if mode=="sse_wait" else 0)))
            self.end_headers()
            if mode=="body":
                self.wfile.write(data[:1])
                self.wfile.flush()
                self.server.entered.set()
                self.server.release.wait(4)
                self.close_connection=True
            elif mode=="drip_body":
                for value in data:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    self.server.entered.set()
                    if self.server.release.wait(.04):
                        self.close_connection=True
                        break
            elif mode=="sse_wait":
                self.wfile.write(data)
                self.wfile.flush()
                self.server.entered.set()
                self.server.release.wait(4)
                self.close_connection=True
            else:
                self.wfile.write(data[:2])
                self.wfile.flush()
                if mode=="delayed":
                    time.sleep(.15)
                self.wfile.write(data[2:])
                self.wfile.flush()
        except OSError:
            pass


@unittest.skipUnless(httpx is not None, "cloud SDK extras required")
class DeadlineTransportTests(unittest.TestCase):
    def setUp(self):
        from voxbridge.cloud_http import DeadlineTransport
        self.transport_type=DeadlineTransport
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.server.requests=[]
        self.server.mode="delayed"
        self.server.entered=threading.Event()
        self.server.release=threading.Event()
        self.thread=threading.Thread(target=self.server.serve_forever,
                                     kwargs={"poll_interval":.02},daemon=True)
        self.thread.start()
        self.url="http://127.0.0.1:"+str(self.server.server_port)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def client(self, seconds=5, max_bytes=24_000_000):
        return httpx.Client(transport=self.transport_type(seconds,max_bytes=max_bytes),
                            timeout=10,trust_env=False,follow_redirects=False)

    def test_failed_transport_close_is_sticky_and_sanitized(self):
        class FailingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, content=b"synthetic")
            async def aclose(self):
                raise OSError("private")
        transport = self.transport_type(5, transport=FailingTransport())
        client = httpx.Client(transport=transport, trust_env=False)
        self.assertEqual(client.get(self.url).content, b"synthetic")
        loop = transport.loop
        with self.assertRaisesRegex(CloudAbort, "^CLEANUP_FAILED$"):
            client.close()
        self.assertTrue(loop.is_closed())
        with self.assertRaisesRegex(CloudAbort, "^CLEANUP_FAILED$"):
            transport.close()
        self.assertEqual(self.server.requests, [])

    def test_failed_response_cleanup_survives_successful_pool_close(self):
        class BrokenBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"synthetic"
            async def aclose(self):
                raise OSError("private")
        class MockTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return httpx.Response(200, stream=BrokenBody())
        transport = self.transport_type(5, transport=MockTransport())
        client = httpx.Client(transport=transport, trust_env=False)
        with self.assertRaisesRegex(CloudAbort, "^CLEANUP_FAILED$"):
            client.get(self.url)
        with self.assertRaisesRegex(CloudAbort, "^CLEANUP_FAILED$"):
            client.close()
        self.assertTrue(transport.closed)
        self.assertTrue(transport.failed)

    def test_voice_metadata_uses_one_deadline_for_all_pages(self):
        from voxbridge.cartesia_voices import list_owned_voices
        self.server.mode = "voice_pages"
        with patch("voxbridge.cartesia_voices._URL", self.url), patch(
                "voxbridge.cartesia_voices._REQUEST_SECONDS", 1.65):
            with self.assertRaisesRegex(ProviderFailure, "CARTESIA_VOICES_TIMEOUT"):
                list_owned_voices(api_key="synthetic-only")
        self.assertEqual(len(self.server.requests), 2)

    def test_voice_metadata_cancel_during_real_body_read(self):
        from voxbridge.cartesia_voices import list_owned_voices
        self.server.mode = "body"
        cancel = threading.Event()
        def stop_after_body():
            if self.server.entered.wait(2):
                cancel.set()
        worker = threading.Thread(target=stop_after_body, daemon=True)
        worker.start()
        started = time.monotonic()
        try:
            with patch("voxbridge.cartesia_voices._URL", self.url):
                with self.assertRaisesRegex(ProviderFailure, "CARTESIA_VOICES_CANCELLED"):
                    list_owned_voices(api_key="synthetic-only", cancel_event=cancel)
            self.assertLess(time.monotonic()-started, 2)
            self.assertTrue(cancel.is_set())
        finally:
            worker.join(2)

    def test_delayed_body_exact_and_connection_reusable(self):
        with self.client() as client:
            for _ in range(2):
                self.assertEqual(client.get(self.url).json(),{"text":"synthetic"})
        self.assertEqual(len(self.server.requests),2)

    def test_precancel_never_connects(self):
        cancel=threading.Event()
        cancel.set()
        with self.client() as client:
            with self.assertRaisesRegex(CloudAbort,"^CANCELLED$"):
                with cloud_budget(5,cancel):
                    client.get(self.url)
        self.assertEqual(self.server.requests,[])

    def test_trickled_headers_and_body_cannot_extend_deadline(self):
        for mode in ("drip_headers","drip_body"):
            self.server.mode=mode
            start=time.monotonic()
            with self.client(.25) as client:
                with self.assertRaisesRegex(CloudAbort,"^TIMEOUT$"):
                    client.get(self.url)
            self.assertLess(time.monotonic()-start,2)
            self.assertTrue(self.server.entered.is_set())
            self.server.entered.clear()
        self.assertEqual(len(self.server.requests),2)

    def test_oversized_response_rejected_before_parsing(self):
        self.server.mode="oversize"
        with self.client(max_bytes=100) as client:
            with self.assertRaisesRegex(CloudAbort,"^RESPONSE_TOO_LARGE$"):
                client.get(self.url)

    def test_wire_limit_applies_without_content_length(self):
        self.server.mode="chunked_oversize"
        with self.client(max_bytes=100) as client:
            with self.assertRaisesRegex(CloudAbort,"^RESPONSE_TOO_LARGE$"):
                client.get(self.url)

    def test_client_usable_after_cancelled_body(self):
        self.server.mode="body"
        cancel=threading.Event()
        # Cancel from another thread; client remains on the same owner.
        def cancel_after_enter():
            if self.server.entered.wait(2):
                cancel.set()
        signal=threading.Thread(target=cancel_after_enter,daemon=True)
        signal.start()
        try:
            with self.client() as client:
                with self.assertRaisesRegex(CloudAbort,"^CANCELLED$"):
                    with cloud_budget(5,cancel):
                        client.get(self.url)
                self.server.release.set()
                self.server.mode="delayed"
                self.assertEqual(client.get(self.url).json(),{"text":"synthetic"})
        finally:
            self.server.release.set()
            signal.join(2)
        self.assertEqual(len(self.server.requests),2)

    def test_cancel_blocked_headers_and_body(self):
        for mode in ("headers","body"):
            with self.subTest(mode=mode):
                self.server.mode=mode
                self.server.entered.clear()
                self.server.release.clear()
                cancel=threading.Event()
                errors=[]
                def run():
                    try:
                        with self.client() as client, cloud_budget(8,cancel):
                            client.get(self.url)
                    except Exception as exc:
                        errors.append((type(exc),str(exc)))
                worker=threading.Thread(target=run,daemon=True)
                worker.start()
                try:
                    self.assertTrue(self.server.entered.wait(2))
                    start=time.monotonic()
                    cancel.set()
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    self.assertLess(time.monotonic()-start,1)
                    self.assertEqual(errors,[(CloudAbort,"CANCELLED")])
                finally:
                    self.server.release.set()
                    worker.join(3)
        self.assertEqual(len(self.server.requests),2)

    def test_real_openai_sdk_stt_cancel_maps_typed_error(self):
        self.server.mode="headers"
        result=[]
        provider=OpenAIFileSTT(client=Mock())
        def run():
            try:
                with OpenAI(api_key="synthetic-only",base_url=self.url+"/v1",
                            http_client=self.client(),max_retries=0) as client:
                    provider.client=client
                    provider.transcribe(pcm_to_wav(bytes(6400)),"de")
            except Exception as exc:
                result.append((type(exc),str(exc)))
        worker=threading.Thread(target=run,daemon=True)
        worker.start()
        try:
            self.assertTrue(self.server.entered.wait(15))
            provider.cancel()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result,[(SpeechFailure,"OPENAI_STT_CANCELLED")])
            self.assertEqual(len(self.server.requests),1)
            self.assertIn(b"RIFF",self.server.requests[0][1])
        finally:
            provider.cancel()
            self.server.release.set()
            worker.join(3)

    def test_real_openai_sdk_analysis_cancel_maps_typed_error(self):
        self.server.mode="headers"
        cancel=threading.Event()
        errors=[]
        def run():
            try:
                with OpenAI(api_key="synthetic-only",base_url=self.url+"/v1",
                            http_client=self.client(),max_retries=0) as client:
                    OpenAIResponses(client=client).generate_stream("system","test",lambda _:None,cancel)
            except Exception as exc:
                errors.append((type(exc),str(exc)))
        worker=threading.Thread(target=run,daemon=True)
        worker.start()
        try:
            # Cold SDK schema initialization can exceed eight seconds on a
            # loaded Windows host. Setup only: cancellation still has 1s.
            self.assertTrue(self.server.entered.wait(15))
            cancel.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors,[(ProviderFailure,"OPENAI_CANCELLED")])
            self.assertEqual(len(self.server.requests),1)
            self.assertTrue(json.loads(self.server.requests[0][1])["stream"])
        finally:
            cancel.set()
            self.server.release.set()
            worker.join(3)

    def test_real_openai_sdk_multipart_success(self):
        with OpenAI(api_key="synthetic-only",base_url=self.url+"/v1",
                    http_client=self.client(),max_retries=0) as client:
            stt=OpenAIFileSTT(client=client)
            self.assertEqual(stt.transcribe(pcm_to_wav(bytes(6400)),"de"),"synthetic")
        self.assertIn(b"recording.wav",self.server.requests[0][1])
        self.assertIn(b"RIFF",self.server.requests[0][1])

    def test_real_cartesia_sdk_sse_exact_pcm(self):
        self.server.mode="sse"
        with Cartesia(api_key="synthetic-only",base_url=self.url,
                      http_client=self.client(),max_retries=0) as client:
            provider=CartesiaTTSProvider(voice_id="synthetic",client=client)
            blocks=[]
            self.assertEqual(stream_to_callback(provider,"test","de",blocks.append),4)
            self.assertEqual(blocks,[bytes((1,0,2,0))])

    def test_real_cartesia_sdk_sse_cancel_after_first_pcm(self):
        self.server.mode="sse_wait"
        cancel=threading.Event()
        received=threading.Event()
        blocks, errors=[],[]
        def emit(block):
            blocks.append(block)
            received.set()
        def run():
            try:
                with Cartesia(api_key="synthetic-only",base_url=self.url,
                              http_client=self.client(),max_retries=0) as client:
                    provider=CartesiaTTSProvider(voice_id="synthetic",client=client)
                    stream_to_callback(provider,"test","de",emit,cancel)
            except Exception as exc:
                errors.append((type(exc),str(exc)))
        worker=threading.Thread(target=run,daemon=True)
        worker.start()
        try:
            self.assertTrue(received.wait(15))
            cancel.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors,[(ProviderFailure,"CARTESIA_STREAM_CANCELLED")])
            self.assertEqual(blocks,[bytes((1,0,2,0))])
            self.assertEqual(len(self.server.requests),1)
        finally:
            cancel.set()
            self.server.release.set()
            worker.join(3)

    def test_anthropic_analysis_cancel_during_headers_and_body(self):
        from voxbridge.analysis import AnalysisRequest
        from voxbridge.analysis_providers import AnthropicAPI, execute_analysis
        for mode in ("headers", "body"):
            with self.subTest(mode=mode):
                self.server.mode = mode
                self.server.entered.clear()
                self.server.release.clear()
                stop, errors = threading.Event(), []
                provider = AnthropicAPI("synthetic-model", api_key="synthetic-only")
                def run():
                    try:
                        with patch.object(provider, "URL", self.url+"/v1/messages"), \
                                patch.object(provider, "_client", side_effect=self.client):
                            execute_analysis(AnalysisRequest("synthetic"), provider,
                                             cloud_approved=True, cancel_event=stop)
                    except Exception as exc:
                        errors.append((type(exc), str(exc)))
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                try:
                    self.assertTrue(self.server.entered.wait(15))
                    stop.set()
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [(ProviderFailure, "ANTHROPIC_CANCELLED")])
                finally:
                    stop.set()
                    self.server.release.set()
                    worker.join(3)

    def test_budget_context_restored_after_exception(self):
        with self.assertRaisesRegex(CloudAbort,"TIMEOUT"):
            with cloud_budget(.01):
                self.assertIsNotNone(current_budget())
                time.sleep(.03)
        self.assertIsNone(current_budget())
