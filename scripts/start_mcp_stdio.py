"""Launch repository MCP STDIO with optional isolated SDK; no model or device."""
from pathlib import Path
import site
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    # Optional SDK belongs to the selected interpreter, not the source tree.
    # A clean source copy may be tested using a separately provisioned venv.
    target = Path(sys.prefix) / "mcp-contract"
    if target.is_dir():
        site.addsitedir(str(target))
        sys.path.insert(0, str(target))
    sys.path.insert(0, str(root / "src"))
    from voxbridge.mcp_stdio import main as serve
    serve()


if __name__ == "__main__":
    main()
