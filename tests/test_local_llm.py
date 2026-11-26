"""Local Gemma model discovery, borrowing, streaming and UI routing."""
import contextlib
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from voxbridge.analysis_providers import ProviderFailure
from voxbridge.config import Settings
from voxbridge.gpu import GpuBusy
from voxbridge.local_llm import LocalModel, LocalModelRuntime, discover_models, select_model
from voxbridge.local_services import normalized


class CatalogTests(unittest.TestCase):
    def test_all_present_gemma_variants_and_unique_vision_projector(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for variant in ("E2B", "E4B", "12B", "16B", "26B"):
                (root/f"gemma-4-{variant}-it.gguf").write_bytes(b"GGUF"+bytes(1024))
            (root/"gemma-4-E2B-mmproj.gguf").write_bytes(b"GGUF"+bytes(1024))
            (root/"gemma-4-16B-invalid.gguf").write_bytes(b"bad!"+bytes(1024))
            models = discover_models([root])
            self.assertEqual(len(models), 5)
            self.assertEqual(select_model(models, vision=True).label, "gemma-4-E2B-it")
            self.assertEqual(select_model(models, models[-1].key), models[-1])
            self.assertIsNone(select_model(models, models[-1].key, vision=True))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.model = LocalModel(Path(self.temp.name)/"gemma-4-E2B.gguf")
        self.model.path.write_bytes(b"GGUF"+bytes(1024))
        self.peer = Mock(port=18189)
        self.peer.get_json.return_value = {"model_path": str(self.model.path),
                    "model_alias": "omoi-managed-chat", "modalities": {"vision": False}}
        self.guard = Mock()
        self.runtime = LocalModelRuntime(reuse=True, peers=Mock(return_value=(self.peer,)), guard=self.guard)
        self.addCleanup(self.runtime.close)

    def test_borrowed_model_does_not_use_gpu_guard_or_spawn(self):
        with patch("voxbridge.local_llm.subprocess.Popen") as spawn:
            self.assertEqual(self.runtime._ensure(self.model, False, threading.Event()),
                             (self.peer, "omoi-managed-chat", None))
        self.guard.assert_not_called()
        spawn.assert_not_called()
        self.assertIsNone(self.runtime._child)

    def test_auto_reuses_a_larger_resident_model(self):
        larger = LocalModel(Path(self.temp.name)/"gemma-4-12B.gguf")
        self.peer.get_json.return_value["model_path"] = str(larger.path)
        self.assertEqual(self.runtime._ensure((self.model, larger), False, threading.Event()),
                         (self.peer, "omoi-managed-chat", None))
        self.guard.assert_not_called()
        self.peer.get_json.assert_called_once()

    def test_ocr_read_needs_no_model_and_analysis_passes_only_text(self):
        from voxbridge.local_llm import LocalGemmaOCR
        runtime = Mock()
        runtime.stream.return_value = "answer"
        provider = LocalGemmaOCR(runtime, self.model, "de")
        with patch("voxbridge.local_llm.recognize_png", return_value="VLAN 20"):
            self.assertEqual(provider.read_png(b"synthetic"), "VLAN 20")
            runtime.stream.assert_not_called()
            self.assertEqual(provider.analyze_task_png(b"synthetic", "Explain"), "answer")
        self.assertIn("VLAN 20", runtime.stream.call_args.args[1][1]["content"])
        self.assertIn("not an image", runtime.stream.call_args.args[1][0]["content"])
        self.assertNotIn("vision", runtime.stream.call_args.kwargs)

    def test_text_only_peer_does_not_receive_image(self):
        self.guard.side_effect = GpuBusy("GPU_BUSY_SKIP_MODEL_LOAD")
        projector = Path(self.temp.name)/"mmproj.gguf"
        projector.write_bytes(b"GGUF"+bytes(1024))
        model = LocalModel(self.model.path, projector)
        exe = Path(self.temp.name)/"llama-server.exe"
        exe.touch()
        self.runtime.server = str(exe)
        with patch("voxbridge.local_llm.listener_owners", return_value=()), \
             patch("voxbridge.local_llm.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(ProviderFailure, "TEXT_ONLY_GPU_BUSY"):
                self.runtime._ensure(model, True, threading.Event())
        spawn.assert_not_called()

    def test_stream_and_completion_without_foreign_termination(self):
        data = (b'data: {"choices":[{"delta":{"content":"VLAN"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":" 20"},"finish_reason":"stop"}]}\n\n'
                b'data: [DONE]\n\n')
        response = Mock(status=200)
        response.getheader.return_value = "text/event-stream"
        response.readline.side_effect = io.BytesIO(data).readline
        @contextlib.contextmanager
        def socket(*args, **kwargs):
            kwargs["verify_connected"]()
            yield response
        delta = Mock()
        with patch("voxbridge.local_llm.loopback_response", side_effect=socket), \
             patch("voxbridge.local_llm.subprocess.Popen") as spawn:
            result = self.runtime.stream(self.model, [{"role":"user","content":"synthetic"}],
                                         delta, threading.Event())
        self.assertEqual(result, "VLAN 20")
        self.assertEqual([c.args[0] for c in delta.call_args_list], ["VLAN", " 20"])
        self.peer.verify.assert_called_once()
        spawn.assert_not_called()
        self.assertIsNone(self.runtime._child)

    def test_incomplete_response_does_not_return_partial_as_final(self):
        response = Mock(status=200)
        response.getheader.return_value = "text/event-stream"
        response.readline.side_effect = io.BytesIO(b'data: {"choices":[{"delta":{"content":"part"}}]}\n').readline
        with patch("voxbridge.local_llm.loopback_response", return_value=contextlib.nullcontext(response)):
            with self.assertRaisesRegex(ProviderFailure, "STREAM_INCOMPLETE"):
                self.runtime.stream(self.model, [], Mock(), threading.Event())
        self.assertIsNone(self.runtime._child)

    def test_pre_cancelled_request_does_not_discover_or_start(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(ProviderFailure, "CANCELLED"):
            self.runtime.stream(self.model, [], Mock(), stop)
        self.runtime._peers.assert_not_called()
