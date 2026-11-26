"""Optional local-only STDIO MCP server. Explicit text; no OS capture or model calls."""
from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import AnalysisError
from .mcp_tools import capability_manifest, prepare_study_prompt, prepare_caption_prompt
from .captions_core import CaptionError


@dataclass(frozen=True)
class Capabilities:
    service: str
    mode: str
    operations: dict[str, str]
    can_read_screen: bool
    can_read_clipboard: bool
    can_execute_commands: bool
    can_read_files: bool
    can_capture_audio: bool
    can_send_cloud_requests: bool
    can_read_live_selection: bool
    cloud_default: bool
    note: str


@dataclass(frozen=True)
class PreparedAnalysis:
    system: str
    user: str = field(repr=False)
    note: str


def create_server():
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError:
        raise RuntimeError("MCP_SDK_V2_NOT_INSTALLED") from None

    server = MCPServer(
        "OMOI VoxBridge - Explicit Text",
        instructions="Use only text explicitly supplied by the user. No device or model access.",
    )
    readonly = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                               idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=readonly)
    def voxbridge_capabilities() -> Capabilities:
        """List supported educational tasks and privacy boundaries."""
        return Capabilities(**capability_manifest())

    @server.tool(annotations=readonly)
    def voxbridge_prepare_analysis(
        text: str, operation: str = "explain", question: str = "",
        response_language: str = "ru", target_language: str = "ru",
    ) -> PreparedAnalysis:
        """Prepare a study request from user-supplied text only; the host model answers."""
        try:
            return PreparedAnalysis(**prepare_study_prompt(text, operation, question,
                                                            response_language, target_language))
        except AnalysisError:
            raise ToolError("INVALID_ANALYSIS_REQUEST") from None

    @server.tool(annotations=readonly)
    def voxbridge_prepare_caption_translation(
        text: str, source_language: str = "de", target_language: str = "ru",
    ) -> PreparedAnalysis:
        """Prepare translation for one user-supplied finalized segment; no live captions or inference."""
        try:
            return PreparedAnalysis(**prepare_caption_prompt(text, source_language, target_language))
        except CaptionError:
            raise ToolError("INVALID_CAPTION_REQUEST") from None

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
