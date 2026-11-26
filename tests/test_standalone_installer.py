"""No installation or network in these tests."""
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location("install_standalone",
    Path(__file__).resolve().parents[1]/"scripts/install_standalone.py")
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@unittest.skipUnless(os.name=="nt","Windows installer")
class StandaloneInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.wheel=self.root/installer.WHEEL_NAME
        self.wheel.write_bytes(b"synthetic test only")
        self.sha=hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        self.target=self.root/"install"
        self.args=["--wheel",str(self.wheel),"--sha256",self.sha,"--target",str(self.target)]

    def test_plan_no_writes_no_process(self):
        with patch.object(installer.subprocess,"run") as child, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.run(self.args+["--plan"]),0)
        child.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_existing_target_never_overwritten(self):
        self.target.mkdir()
        sentinel=self.target/"keep"
        sentinel.write_text("existing")
        with patch.object(installer.subprocess,"run") as child, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):installer.run(self.args)
        child.assert_not_called()
        self.assertEqual(sentinel.read_text(),"existing")

    def test_invalid_hash_fails_before_creation(self):
        with patch.object(installer.subprocess,"run") as child, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):installer.run(self.args+["--sha256","0"*64])
        child.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_failed_child_cleans_only_new_directory(self):
        sentinel=self.root/"keep"
        sentinel.write_text("existing")
        with patch.object(installer.subprocess,"run",side_effect=RuntimeError("synthetic failure")), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,"synthetic failure"):installer.run(self.args)
        self.assertFalse(self.target.exists())
        self.assertEqual(sentinel.read_text(),"existing")

    def test_offline_index_and_credential_isolation(self):
        with patch.dict(os.environ,{"OPENAI_API_KEY":"synthetic","PIP_EXTRA_INDEX_URL":"https://example.invalid"}), \
                patch.object(installer.subprocess,"run") as child,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.run(self.args+["--wheelhouse",str(self.root)]),0)
        commands=[call.args[0] for call in child.call_args_list]
        install=next(c for c in commands if "install" in c)
        self.assertIn("--no-index",install)
        self.assertNotIn("--index-url",install)
        for call in child.call_args_list:
            self.assertNotIn("OPENAI_API_KEY",call.kwargs["env"])
            self.assertNotIn("PIP_EXTRA_INDEX_URL",call.kwargs["env"])
        self.assertTrue((self.target/"Start-VoxBridge.cmd").is_file())
        self.assertIn("main_standalone",(self.target/"Start-VoxBridge.cmd").read_text())
        for name in ("Start-VoxBridge.cmd","Doctor.cmd"):
            self.assertIn("OMOI-VoxBridge-Standalone",(self.target/name).read_text())


    def test_sandbox_configuration_isolated_mappings(self):
        import sys
        import xml.etree.ElementTree as ET
        module_spec=importlib.util.spec_from_file_location("prepare_sandbox",
            Path(__file__).resolve().parents[1]/"scripts/prepare_sandbox.py")
        module=importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        house=self.root/"wheelhouse";house.mkdir()
        (house/installer.WHEEL_NAME).write_bytes(self.wheel.read_bytes())
        python=self.root/"python.exe";python.write_bytes(b"test only")
        target=self.root/"sandbox"
        args=["prepare_sandbox.py","--python-installer",str(python),
              "--python-sha256",hashlib.sha256(python.read_bytes()).hexdigest(),
              "--wheelhouse",str(house),"--wheel-sha256",self.sha,"--target",str(target)]
        with patch.object(sys,"argv",args),contextlib.redirect_stdout(io.StringIO()):
            module.main()
        config=ET.parse(target/"VoxBridge-test.wsb").getroot()
        for tag in ("VGpu","Networking","AudioInput","VideoInput","ClipboardRedirection","PrinterRedirection"):
            self.assertEqual(config.findtext(tag),"Disable")
        mapped=config.findall("MappedFolders/MappedFolder")
        self.assertEqual(len(mapped),2)
        self.assertEqual(mapped[0].findtext("HostFolder"),str(target/"input"))
        self.assertEqual(mapped[0].findtext("ReadOnly"),"true")
        self.assertEqual(mapped[1].findtext("HostFolder"),str(target/"output"))
        self.assertEqual(mapped[1].findtext("ReadOnly"),"false")
        self.assertEqual(set(f.name for f in (target/"input").iterdir()),
                         set(module.SCRIPTS)|{"wheelhouse",module.PYTHON_INSTALLER,"python.sha256","wheel.sha256"})
