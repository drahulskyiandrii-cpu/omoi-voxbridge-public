"""Source and distribution presentation contract; no device or model I/O."""
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "distribution" if (ROOT / "distribution/README.md").is_file() else ROOT


class PresentationAssetsTests(unittest.TestCase):
    def test_markdown_navigation_and_architecture_modules(self):
        files = [DOCS / "README.md", *sorted((DOCS / "docs").glob("*.md"))]
        self.assertGreaterEqual(len(files), 5)
        for path in files:
            text = path.read_text(encoding="utf-8")
            for link in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text):
                if "://" not in link:
                    target = (path.parent / link).resolve()
                    self.assertTrue(target.is_relative_to(DOCS.resolve()), link)
                    self.assertTrue(target.is_file(), f"{path.name}: {link}")
        architecture = (DOCS / "docs/ARCHITEKTUR.md").read_text(encoding="utf-8")
        modules = set(re.findall(r"\x60([a-z_]+\.py)\x60", architecture))
        self.assertGreater(len(modules), 15)
        for module in modules:
            self.assertTrue((ROOT / "src/voxbridge" / module).is_file(), module)

    def test_file_map_covers_runtime_and_helper_scripts(self):
        text = (DOCS / "docs/DATEIEN.md").read_text(encoding="utf-8")
        modules = re.findall(r"^\| `([a-zA-Z0-9_]+\.(?:py|mjs|ps1))` \|", text, re.M)
        actual = {p.name for p in (ROOT / "src/voxbridge").iterdir()
                  if p.is_file() and p.suffix in {".py", ".mjs", ".ps1"}}
        self.assertEqual(set(modules), actual)
        self.assertEqual(len(modules), len(actual))
        for script in re.findall(r"^\| `(scripts/[^\x60]+)` \|", text, re.M):
            self.assertTrue((ROOT / script).is_file(), script)

    def test_offline_demo_from_source(self):
        keys = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TEMP", "TMP",
                "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME"}
        env = {k: v for k, v in os.environ.items() if k.upper() in keys}
        env.update(PYTHONPATH=str(ROOT / "src"), QT_QPA_PLATFORM="offscreen")
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/demo_captions.py"), "--self-test"],
            env=env, cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OFFLINE_DEMO_PASS", result.stdout)
