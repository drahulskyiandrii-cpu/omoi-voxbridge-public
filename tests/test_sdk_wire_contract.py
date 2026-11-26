"""Actual installed SDK serialization, parsing and error paths, zero network.

Every real SDK client is built with HTTPX MockTransport and fake credentials.
No provider service or localhost listener is contacted.
"""
import base64
import io
import json
import unittest
import wave
from unittest.mock import patch

try:
    import httpx
    from openai import OpenAI
    from cartesia import Cartesia
except ImportError:
    httpx = None

from voxbridge.audio import pcm_to_wav
from voxbridge.analysis_providers import ProviderFailure
from voxbridge.cloud_providers import OpenAIResponses, OpenAIFileSTT, CartesiaTTSProvider
from voxbridge.tts_stream import stream_to_callback
from voxbridge.vision import OpenAIVision


def response_body(status="completed"):
    return {"id":"resp_test","object":"response","created_at":0,"status":status,
            "model":"gpt-6-luna","output":[{"id":"msg_test","type":"message",
            "role":"assistant","status":"completed","content":[{"type":"output_text",
            "text":"synthetic","annotations":[]}]}],
            "usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3,
            "input_tokens_details":{"cached_tokens":0},
            "output_tokens_details":{"reasoning_tokens":0}}}


def wav24():
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(bytes(6400))
    return out.getvalue()


@unittest.skipUnless(httpx is not None, "cloud SDK extra not installed")
class ActualSDKTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responder = None

    def transport(self):
        def handle(req):
            self.requests.append(req)
            return self.responder(req)
        return httpx.Client(transport=httpx.MockTransport(handle), trust_env=False,
                            follow_redirects=False)

    def openai(self):
        client = OpenAI(api_key="offline-only", base_url="https://api.openai.com/v1",
                        http_client=self.transport(), max_retries=0)
        self.addCleanup(client.close)
        return client

    def cartesia(self):
        client = Cartesia(api_key="offline-only", base_url="https://api.cartesia.ai",
                          http_client=self.transport(), max_retries=0)
        self.addCleanup(client.close)
        return client

    def test_responses_json_and_completed_status(self):
        self.responder = lambda _: httpx.Response(200,json=response_body())
        provider = OpenAIResponses(client=self.openai(), effort="high", max_output_tokens=256)
        self.assertEqual(provider.generate("system", "synthetic input"), "synthetic")
        req = self.requests[0]
        self.assertEqual((req.method,req.url.host,req.url.path),("POST","api.openai.com","/v1/responses"))
        body = json.loads(req.content)
        self.assertEqual(body["reasoning"], {"effort":"high"})
        self.assertEqual(body["max_output_tokens"], 256)
        self.assertIs(body["store"], False)
        self.assertEqual(provider.last_usage.input_tokens,2)
        self.responder = lambda _: httpx.Response(200,json=response_body("incomplete"))
        with self.assertRaisesRegex(ProviderFailure,"RESPONSE_INCOMPLETE"):
            provider.generate("s","u")

    def test_responses_sse_actual_sdk(self):
        final = response_body()
        message = final["output"][0]
        part = message["content"][0]
        start = {**final, "status":"in_progress", "output":[], "usage":None}
        events = [
            {"type":"response.created","response":start},
            {"type":"response.output_item.added","output_index":0,
             "item":{**message,"status":"in_progress","content":[]}},
            {"type":"response.content_part.added","item_id":"msg_test",
             "output_index":0,"content_index":0,"part":{**part,"text":""}},
            {"type":"response.output_text.delta","item_id":"msg_test",
             "output_index":0,"content_index":0,"delta":"syn","logprobs":[]},
            {"type":"response.output_text.delta","item_id":"msg_test",
             "output_index":0,"content_index":0,"delta":"thetic","logprobs":[]},
            {"type":"response.output_text.done","item_id":"msg_test",
             "output_index":0,"content_index":0,"text":"synthetic","logprobs":[]},
            {"type":"response.content_part.done","item_id":"msg_test",
             "output_index":0,"content_index":0,"part":part},
            {"type":"response.output_item.done","output_index":0,"item":message},
            {"type":"response.completed","response":final},
        ]
        self.responder = lambda _: httpx.Response(200,headers={"content-type":"text/event-stream"},
            text="".join("data: "+json.dumps({**event,"sequence_number":i})+"\n\n"
                         for i,event in enumerate(events)))
        provider=OpenAIResponses(client=self.openai())
        chunks=[]
        self.assertEqual(provider.generate_stream("s","u",chunks.append),"synthetic")
        self.assertEqual(chunks,["syn","thetic"])
        self.assertEqual(provider.last_usage.output_tokens,1)
        self.assertIs(json.loads(self.requests[0].content)["stream"],True)

    def test_stt_multipart_language_and_audio(self):
        self.responder = lambda _: httpx.Response(200,json={"text":"synthetic"})
        provider = OpenAIFileSTT(client=self.openai())
        self.assertEqual(provider.transcribe(pcm_to_wav(bytes(6400)), "de"), "synthetic")
        req = self.requests[0]
        self.assertEqual(req.url.path,"/v1/audio/transcriptions")
        self.assertIn("multipart/form-data", req.headers["content-type"])
        self.assertIn(b'name="languages[]"' ,req.content)
        self.assertIn(b"gpt-transcribe",req.content)
        self.assertIn(b"recording.wav",req.content)
        self.assertIn(b"RIFF",req.content)

    def test_vision_image_only_and_store_false(self):
        import struct
        from voxbridge.vision import PNG_SIGNATURE
        image=PNG_SIGNATURE+struct.pack(">I",13)+b"IHDR"+struct.pack(">II",80,80)+bytes(9)
        self.responder=lambda _:httpx.Response(200,json=response_body())
        self.assertEqual(OpenAIVision(client=self.openai()).analyze_png(image,"synthetic question"),"synthetic")
        body=json.loads(self.requests[0].content)
        self.assertIs(body["store"],False)
        content=body["input"][0]["content"]
        self.assertEqual(content[0]["text"],"synthetic question")
        self.assertEqual(base64.b64decode(content[1]["image_url"].split(",")[1]),image)

    def test_cartesia_sse_parsed_by_sdk(self):
        pcm=bytes((1,0,2,0))
        events=[{"type":"chunk","data":base64.b64encode(pcm).decode(),"status_code":200,
                 "done":False,"step_time":1,"context_id":"test"},
                {"type":"done","status_code":200,"done":True,"context_id":"test"}]
        self.responder=lambda _:httpx.Response(200,headers={"content-type":"text/event-stream"},
            text="".join("data: "+json.dumps(e)+"\n\n" for e in events))
        chunks=[]
        provider=CartesiaTTSProvider(voice_id="synthetic",client=self.cartesia())
        self.assertEqual(stream_to_callback(provider,"synthetic","ru",chunks.append),4)
        self.assertEqual(chunks,[pcm])
        req=self.requests[0]
        self.assertEqual(req.url.path,"/tts/sse")
        self.assertEqual(json.loads(req.content)["output_format"],
            {"container":"raw","encoding":"pcm_s16le","sample_rate":24000})

    def test_cartesia_raw_http_pcm_stream_validated_by_actual_sdk(self):
        from voxbridge.tts_stream import stream_bytes_to_callback
        pcm=bytes((1,0,2,0))
        self.responder=lambda _:httpx.Response(200,content=pcm,headers={"content-type":"application/octet-stream"})
        provider=CartesiaTTSProvider(voice_id="synthetic",client=self.cartesia())
        chunks=[]
        self.assertEqual(stream_bytes_to_callback(provider,"synthetic","ru",chunks.append),4)
        self.assertEqual(b"".join(chunks),pcm)
        self.assertEqual(len(self.requests),1)
        req=self.requests[0]
        self.assertEqual(req.url.path,"/tts/bytes")
        body=json.loads(req.content)
        self.assertEqual(body["output_format"],{"container":"raw","encoding":"pcm_s16le","sample_rate":24000})
        self.assertIs(body["save"],False)

    def test_cartesia_wav_stream_validated(self):
        self.responder=lambda _:httpx.Response(200,content=wav24())
        provider=CartesiaTTSProvider(voice_id="synthetic",client=self.cartesia())
        self.assertEqual(provider.synthesize("synthetic","ru"),wav24())
        self.assertEqual(self.requests[0].url.path,"/tts/bytes")

    def test_rate_limit_no_retry(self):
        self.responder=lambda _:httpx.Response(429,json={"error":{"message":"private"}})
        with self.assertRaisesRegex(ProviderFailure,"OPENAI_RATE_LIMIT"):
            OpenAIResponses(client=self.openai()).generate("s","u")
        self.assertEqual(len(self.requests),1)

    def test_production_clients_ignore_env_endpoint_override(self):
        from voxbridge.cloud_providers import _openai_client,_cartesia_client
        with patch.dict("os.environ",{"OPENAI_BASE_URL":"https://untrusted.invalid",
                        "CARTESIA_BASE_URL":"https://untrusted.invalid",
                        "HTTPS_PROXY":"http://untrusted.invalid:1234"}):
            with _openai_client("offline-only",5) as o, _cartesia_client("offline-only") as c:
                self.assertEqual(o.base_url.host,"api.openai.com")
                self.assertEqual(c.base_url.host,"api.cartesia.ai")
                self.assertFalse(o._client.follow_redirects)
                self.assertFalse(c._client.follow_redirects)
                self.assertFalse(o._client.trust_env)
                self.assertFalse(c._client.trust_env)
