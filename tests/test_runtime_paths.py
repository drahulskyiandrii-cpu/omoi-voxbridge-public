"""Standalone path contracts. Synthetic files only, no GPU or model startup."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voxbridge.runtime_paths import standalone_root
from voxbridge.stt import LocalSTTBackend, SpeechFailure
from voxbridge.qwen_local_runtime import runtime_files, voice_assets, verify_files, child_env
from voxbridge.speech_core_tts import LocalTTSFailure
from voxbridge.local_llm import discover_models, runtime_paths
from voxbridge.analysis_providers import ProviderFailure


class StandalonePathsTests(unittest.TestCase):
    def test_legacy_is_explicitly_distinct_and_invalid_root_fails(self):
        self.assertIsNone(standalone_root({}))
        for invalid in ("", "relative"):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {"VOXBRIDGE_RUNTIME_ROOT": invalid}):
                with self.assertRaisesRegex(SpeechFailure, "RUNTIME_ROOT_INVALID"):
                    LocalSTTBackend(engine="openvino")._launch_spec()
                with self.assertRaisesRegex(LocalTTSFailure, "RUNTIME_ROOT_INVALID"):
                    runtime_files()
                with self.assertRaisesRegex(ProviderFailure, "RUNTIME_ROOT_INVALID"):
                    discover_models()

    def test_cpu_standalone_never_uses_legacy_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk = root / "openvino-sdk"
            (sdk / "node_modules" / "openvino-genai-node").mkdir(parents=True)
            model = root / "openvino-whisper-medium-int8-ov"
            model.mkdir()
            with patch.dict(os.environ, {"VOXBRIDGE_RUNTIME_ROOT": tmp,
                            "OMOI_MINI_STT_MODEL": "legacy"}, clear=True), \
                    patch("voxbridge.stt.shutil.which", return_value="node.exe"), \
                    patch("voxbridge.stt.require_free_memory") as gpu:
                _, _, env = LocalSTTBackend(engine="openvino")._launch_spec()
                gpu.assert_not_called()
            self.assertEqual(Path(env["OMOI_MINI_STT_MODEL"]), model)
            self.assertEqual(Path(env["VOXBRIDGE_OPENVINO_SDK_DIR"]), sdk)

    def test_cuda_standalone_requires_overlay_and_ignores_legacy_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "cuda-venv" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            model = root / "faster-whisper-small-ct2"
            model.mkdir()
            (model / "model.bin").touch()
            overlay = root / "faster-whisper-overlay"
            with patch.dict(os.environ, {"VOXBRIDGE_RUNTIME_ROOT": tmp,
                            "OMOI_MINI_CUDA_STT_PYTHON": "legacy",
                            "OMOI_MINI_CUDA_STT_OVERLAY": "legacy"}, clear=True), \
                    patch("voxbridge.stt.require_free_memory"):
                with self.assertRaisesRegex(SpeechFailure, "MINI_WORKER_NOT_INSTALLED"):
                    LocalSTTBackend()._launch_spec()
                overlay.mkdir()
                command, _, env = LocalSTTBackend()._launch_spec()
            self.assertEqual(Path(command[0]), python)
            self.assertEqual(Path(env["OMOI_MINI_CUDA_STT_OVERLAY"]), overlay)
            self.assertEqual(Path(env["OMOI_MINI_CUDA_STT_MODEL"]), model)

    def test_qwen_standalone_paths_are_forwarded_and_git_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            with patch.dict(os.environ, {"VOXBRIDGE_RUNTIME_ROOT": tmp,
                            "LOCALAPPDATA": str(profile)}, clear=True), \
                    patch("voxbridge.qwen_local_runtime.subprocess.run") as git:
                python, worker, base = runtime_files()
                reference, adapter = voice_assets(base)
                self.assertEqual(base, root)
                self.assertEqual(worker, root / "qwen_server.py")
                python.parent.mkdir(parents=True)
                python.touch()
                reference.parent.mkdir(parents=True)
                reference.touch()
                worker.write_bytes(b"unknown")
                with self.assertRaisesRegex(LocalTTSFailure, "QWEN_WORKER_CHANGED"):
                    verify_files("qwen_base")
                digest = hashlib.sha256(worker.read_bytes()).hexdigest()
                with patch("voxbridge.qwen_local_runtime.WORKER_SHAS", (digest,)):
                    _, cached = verify_files("qwen_base")
                self.assertEqual(cached.read_bytes(), b"unknown")
                env = child_env("qwen_base", "x"*32, "job")
                self.assertEqual(env["LOCALAPPDATA"], str(root / "qwen-profile"))
                self.assertEqual(reference, root / "qwen-profile" / "OMOI-Mini" /
                                 "tts-eval" / "private-voice-reference" / "voice.wav")
                self.assertEqual(env["OMOI_MINI_QWEN_REFERENCE"], str(reference))
                self.assertEqual(env["OMOI_MINI_QWEN_CF50_ADAPTER"], str(adapter))
                outside = root / "outside.wav"
                outside.touch()
                with patch.dict(os.environ, {"VOXBRIDGE_QWEN_REFERENCE": str(outside)}):
                    with self.assertRaisesRegex(LocalTTSFailure, "OUTSIDE_STANDALONE_PROFILE"):
                        verify_files("qwen_base")
                git.assert_not_called()

    def test_llm_discovery_avoids_other_project_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gguf = root / "gguf"
            gguf.mkdir()
            model = gguf / "gemma-4-e2b.gguf"
            model.write_bytes(b"GGUF" + bytes(1024))
            with patch.dict(os.environ, {"VOXBRIDGE_RUNTIME_ROOT": tmp}, clear=True), \
                    patch("voxbridge.local_llm._model_root", side_effect=AssertionError("legacy path")):
                self.assertEqual([m.path for m in discover_models()], [model])
                self.assertTrue(all(p.is_relative_to(root / "llama") for p in runtime_paths()))

    def test_standalone_launcher_sets_default_without_starting_model_in_test(self):
        from voxbridge.app import main_standalone
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=True), \
                    patch("voxbridge.app.main", return_value=0) as main:
                self.assertEqual(main_standalone(), 0)
                self.assertEqual(standalone_root(), Path(tmp) / "OMOI-VoxBridge" / "local-runtime")
                main.assert_called_once_with()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_explicit_override_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / "custom.py"
            with patch.dict(os.environ, {"VOXBRIDGE_RUNTIME_ROOT": str(root / "default"),
                            "VOXBRIDGE_QWEN_WORKER": str(worker)}, clear=True):
                self.assertEqual(runtime_files()[1], worker)
