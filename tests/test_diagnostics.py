import json
import os
import tempfile
import unittest
from pathlib import Path
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch
from voxbridge.diagnostics import collect, main
from voxbridge.config import Settings, save_settings
from voxbridge.cloud_permissions import CloudPermissions, save_permissions


class DiagnosticsTests(unittest.TestCase):
    def test_empty_directory_no_writes_no_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            checks = collect(config_dir=path, environ={"OPENAI_API_KEY":"secret-value"},
                             package_version=lambda _: "1.2.3")
            self.assertEqual(list(path.iterdir()), [])
            self.assertNotIn("secret-value", repr(checks))
            self.assertEqual(next(c.status for c in checks if c.name == "OpenAI credential"), "configured")
            self.assertEqual(next(c.status for c in checks if c.name == "cloud model access"), "untested")

    def test_invalid_config_is_reported_not_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            invalid = path / "settings.json"
            invalid.write_text('{"private":"secret-value"}')
            checks = collect(config_dir=path, environ={}, package_version=lambda _: "1")
            self.assertEqual(next(c.status for c in checks if c.name == "settings"), "blocked")
            self.assertEqual(invalid.read_text(), '{"private":"secret-value"}')
            self.assertNotIn("secret-value", repr(checks))

    def test_local_and_permission_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            save_settings(Settings(stt_backend="shared"), path / "settings.json")
            save_permissions(CloudPermissions(tts_cartesia=True), path / "cloud_permissions.json")
            checks = collect(config_dir=path, environ={}, package_version=lambda _: "1")
            self.assertEqual(next(c.status for c in checks if c.name == "STT mode"), "blocked")
            self.assertEqual(next(c.detail for c in checks if c.name == "standing permissions"), "1 of 8 enabled")

    def test_missing_dependencies(self):
        def metadata(name):
            raise PackageNotFoundError(name)
        with tempfile.TemporaryDirectory() as tmp:
            checks = collect(config_dir=Path(tmp), environ={}, package_version=metadata)
        self.assertEqual(next(c.status for c in checks if c.name == "openai"), "blocked")
        self.assertEqual(next(c.status for c in checks if c.name == "comtypes"), "blocked" if os.name == "nt" else "optional_missing")

    def test_qt_parts_without_metapackage(self):
        def metadata(name):
            if name == "PySide6":
                raise AssertionError("meta-package must not be required")
            return "6.12.0"
        with tempfile.TemporaryDirectory() as tmp:
            checks = collect(config_dir=Path(tmp), environ={}, package_version=metadata)
        self.assertEqual(next(c.status for c in checks if c.name == "PySide6"), "ready")

    def test_qt_missing_or_mismatched_parts_block(self):
        for missing in (True, False):
            def metadata(name):
                if name == "PySide6-Essentials":
                    if missing:
                        raise PackageNotFoundError(name)
                    return "6.11.0"
                return "6.12.0"
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                checks = collect(config_dir=Path(tmp), environ={}, package_version=metadata)
                self.assertEqual(next(c.status for c in checks if c.name == "PySide6"), "blocked")

    def test_runtime_profile_invalid_root_is_safe_and_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = collect(config_dir=Path(tmp), environ={"VOXBRIDGE_RUNTIME_ROOT": "private-relative"},
                             package_version=lambda _: "6.12.0")
        result = next(c for c in checks if c.name == "runtime profile")
        self.assertEqual(result.status, "blocked")
        self.assertNotIn("private-relative", result.detail)

    def test_cli_json_exit(self):
        from io import StringIO
        from voxbridge.diagnostics import Check
        output=StringIO()
        with patch("voxbridge.diagnostics.collect",return_value=[Check("x","blocked","synthetic")]), patch("sys.stdout",output):
            self.assertEqual(main(["--json"]),2)
        self.assertEqual(json.loads(output.getvalue())["mode"],"offline")
