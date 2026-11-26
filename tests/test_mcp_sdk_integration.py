"""MCP SDK v2 roundtrip, no model, user data or network service.

Run with the optional SDK extra installed. For isolated --target installations,
invoke the runner with that directory available on sys.path / PYTHONPATH.
"""
import json
import os
from pathlib import Path
import sys
import unittest

try:
    from mcp import Client, StdioServerParameters
except ImportError:
    Client = None

from voxbridge.mcp_stdio import create_server


@unittest.skipUnless(Client is not None, "optional MCP SDK v2 not installed")
class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def contract(self, target):
        async with Client(target, read_timeout_seconds=5) as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({t.name for t in tools},
                             {"voxbridge_capabilities", "voxbridge_prepare_analysis",
                              "voxbridge_prepare_caption_translation"})
            for tool in tools:
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.annotations.open_world_hint)
                self.assertIsNotNone(tool.output_schema)
            result = await client.call_tool("voxbridge_capabilities", {})
            self.assertFalse(result.is_error)
            cap = result.structured_content
            self.assertFalse(cap["can_read_screen"])
            self.assertFalse(cap["can_read_clipboard"])
            self.assertFalse(cap["can_send_cloud_requests"])
            self.assertFalse(cap["can_read_live_selection"])
            result = await client.call_tool("voxbridge_prepare_analysis", {
                "text":"Synthetic VLAN question", "operation":"answer",
                "response_language":"ru",
            })
            self.assertFalse(result.is_error)
            content = result.structured_content
            self.assertIn("UNTRUSTED DATA", content["system"])
            self.assertEqual(json.loads(content["user"])["selected_material"],
                             "Synthetic VLAN question")
            invalid = await client.call_tool("voxbridge_prepare_analysis", {
                "text":"private mock", "operation":"execute",
            })
            self.assertTrue(invalid.is_error)
            self.assertNotIn("private mock", repr(invalid.content))
            translated = await client.call_tool("voxbridge_prepare_caption_translation", {
                "text": "Der Server startet.", "source_language": "de", "target_language": "ru",
            })
            self.assertFalse(translated.is_error)
            self.assertIn("German", translated.structured_content["system"])
            self.assertIn("Der Server startet.", translated.structured_content["user"])
            self.assertIn("No local or cloud translation was run", translated.structured_content["note"])
            invalid_caption = await client.call_tool("voxbridge_prepare_caption_translation", {
                "text": "private mock", "source_language": "de", "target_language": "de",
            })
            self.assertTrue(invalid_caption.is_error)
            self.assertNotIn("private mock", repr(invalid_caption.content))
            unknown = await client.call_tool("read_clipboard", {})
            self.assertTrue(unknown.is_error)

    async def test_in_memory_protocol(self):
        await self.contract(create_server())

    async def test_repository_launcher(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "start_mcp_stdio.py"
        await self.contract(StdioServerParameters(command=sys.executable,
                            args=[str(script)], env={}))

    async def test_stdio_owned_subprocess(self):
        # Explicit project paths only; no credentials passed to the server.
        src = str(Path(__file__).resolve().parents[1] / "src")
        optional = [p for p in sys.path if p.endswith("mcp-contract")]
        env = {"PYTHONPATH": os.pathsep.join([src, *optional])}
        # site.addsitedir is needed for pywin32's .pth in an isolated target.
        code = "import site,sys; " + "; ".join(
            "site.addsitedir(" + repr(p) + ")" for p in optional
        ) + ("; " if optional else "") + "from voxbridge.mcp_stdio import main; main()"
        await self.contract(StdioServerParameters(command=sys.executable,
                                args=["-c",code], env=env))
