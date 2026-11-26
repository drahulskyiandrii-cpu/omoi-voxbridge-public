"""Borrowing local services never implies ownership or arbitrary trust."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from voxbridge.local_services import LocalPeer, LocalServiceError, normalized, trusted_peers
from voxbridge.qwen_local_runtime import QwenRuntime, MODELS


class ServiceTests(unittest.TestCase):
    def test_only_exact_configured_loopback_executable(self):
        with tempfile.TemporaryDirectory() as root:
            exe = Path(root) / "llama-server.exe"
            exe.touch()
            endpoints = (("127.0.0.1", 18189, 10), ("127.0.0.1", 1234, 11),
                         ("0.0.0.0", 18190, 10), ("127.0.0.1", 18191, 10),
                         ("0.0.0.0", 18191, 12))
            ids = {10: (normalized(exe), 9), 11: ("untrusted.exe", 8)}
            with patch("voxbridge.local_services.listening_endpoints", return_value=endpoints), \
                 patch("voxbridge.local_services.process_identity", side_effect=ids.get):
                self.assertEqual(trusted_peers((exe,)), (LocalPeer(18189, 10, ids[10]),))

    def test_changed_pid_or_creation_time_rejected(self):
        peer = LocalPeer(18189, 10, ("exe", 1))
        with patch("voxbridge.local_services.listener_owners", return_value=(("127.0.0.1", 10),)), \
             patch("voxbridge.local_services.process_identity", return_value=("exe", 2)):
            with self.assertRaisesRegex(LocalServiceError, "IDENTITY_CHANGED"):
                peer.verify()

    def test_qwen_borrows_matching_voice_without_launch_or_gpu_guard(self):
        import hashlib
        from voxbridge.qwen_local_runtime import runtime_files
        _, _, base = runtime_files()
        voice = hashlib.sha256(str((base/"private-voice-reference"/
                      "own-voice-reference-part-a.wav").resolve()).encode("utf-8")).hexdigest()[:12]
        peer = Mock(port=18883)
        peer.get_json.return_value = {"service": "omoi-mini-local-tts", "offline": True,
            "model": MODELS["qwen_cf050"], "voice_profile": voice, "loaded": True, "state": "ready"}
        guard, verify, popen, job = Mock(), Mock(), Mock(), Mock()
        r = QwenRuntime(reuse=True, discover_shared=lambda: (peer,), guard=guard,
                        verify=verify, popen=popen, job_factory=job)
        r.activate("qwen_cf050", threading.Event(), approved=True)
        self.assertTrue(r.ready("qwen_cf050"))
        self.assertIs(r._peer, peer)
        guard.assert_not_called()
        verify.assert_not_called()
        popen.assert_not_called()
        job.assert_not_called()
        with patch.object(r, "_call", return_value=peer.get_json.return_value), \
             patch("voxbridge.qwen_local_runtime.SpeechCoreTTS") as adapter:
            r.stream("qwen_cf050", "synthetic", "de", Mock(), threading.Event())
            self.assertEqual(adapter.call_args.kwargs["port"], 18883)
            self.assertIs(adapter.call_args.kwargs["peer"], peer)
            self.assertIsNone(adapter.call_args.kwargs["token"])
        r.close()
        self.assertFalse(r.ready("qwen_cf050"))
        job.assert_not_called()
        popen.assert_not_called()

    def test_qwen_does_not_borrow_wrong_model_or_voice(self):
        peer = Mock(port=18883)
        peer.get_json.return_value = {"service": "omoi-mini-local-tts", "offline": True,
            "model": MODELS["qwen_cf050"], "voice_profile": "wrong", "loaded": True, "state": "ready"}
        verify = Mock(side_effect=RuntimeError("owned assets absent"))
        r = QwenRuntime(reuse=True, discover_shared=lambda: (peer,), verify=verify)
        from voxbridge.speech_core_tts import LocalTTSFailure
        with self.assertRaisesRegex(LocalTTSFailure, "ACTIVATION_FAILED"):
            r.activate("qwen_cf050", threading.Event(), approved=True)
        verify.assert_called_once()
        self.assertIsNone(r._peer)
