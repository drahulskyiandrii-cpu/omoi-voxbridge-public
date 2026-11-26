"""Portable STT launch specs, no inference or native GPU checks."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from voxbridge.stt import LocalSTTBackend, SpeechFailure


class PortableSTTTests(unittest.TestCase):
    def test_cpu_explicit_assets_override_legacy_without_sibling_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk = root / "sdk"
            (sdk / "node_modules" / "openvino-genai-node").mkdir(parents=True)
            model = root / "model"
            model.mkdir()
            with patch.dict(os.environ, {"VOXBRIDGE_OPENVINO_SDK_DIR": str(sdk),
                    "VOXBRIDGE_OPENVINO_MODEL_DIR": str(model),
                    "OMOI_MINI_STT_MODEL": "missing-legacy"}, clear=True), \
                    patch("voxbridge.stt.shutil.which", return_value="node.exe"):
                command, _, env = LocalSTTBackend(engine="openvino")._launch_spec()
            self.assertEqual(command[0], "node.exe")
            self.assertEqual(Path(env["OMOI_MINI_STT_MODEL"]), model)
            self.assertEqual(Path(env["VOXBRIDGE_OPENVINO_SDK_DIR"]), sdk)

    def test_cuda_explicit_assets_override_legacy_and_are_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "python.exe"
            python.touch()
            model = root / "model"
            model.mkdir()
            (model / "model.bin").touch()
            overlay = root / "overlay"
            overlay.mkdir()
            with patch.dict(os.environ, {"VOXBRIDGE_CUDA_PYTHON": str(python),
                    "VOXBRIDGE_CUDA_MODEL_DIR": str(model), "VOXBRIDGE_CUDA_OVERLAY": str(overlay),
                    "OMOI_MINI_CUDA_STT_MODEL": "missing", "OPENAI_API_KEY": "synthetic-secret"}, clear=True), \
                    patch("voxbridge.stt.require_free_memory") as guard:
                command, _, env = LocalSTTBackend()._launch_spec()
                guard.assert_called_once_with(8000)
            self.assertEqual(Path(command[0]), python)
            self.assertEqual(Path(env["OMOI_MINI_CUDA_STT_MODEL"]), model)
            self.assertEqual(Path(env["OMOI_MINI_CUDA_STT_OVERLAY"]), overlay)
            self.assertNotIn("OPENAI_API_KEY", env)

    def test_explicit_relative_or_empty_path_never_falls_back(self):
        for invalid in ("", "relative/model"):
            with self.subTest(invalid=invalid), patch.dict(os.environ,
                    {"VOXBRIDGE_OPENVINO_MODEL_DIR": invalid,
                     "VOXBRIDGE_OPENVINO_SDK_DIR": str(Path(__file__).resolve().parent)}, clear=True):
                with self.assertRaisesRegex(SpeechFailure, "^STT_ASSET_PATH_INVALID$"):
                    LocalSTTBackend(engine="openvino")._launch_spec()
