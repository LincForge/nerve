"""[contract] No mcp 1.x-only SDK surface survives the mcp 2.x port (molecule mcp-2x, mcp2-port).

``mcp.server.fastmcp`` is gone in mcp 2.x (ModuleNotFoundError at import) and
``mcp.server.session.ServerRequestResponder`` no longer exists (the old session-layer
monkeypatch in ``nerve bellows`` crashed at start). Walks the AST of every .py under src/ and
tests/ (tests/manual included) so a stray import cannot hide in a lazily imported function.
"""

from __future__ import annotations

import ast
from pathlib import Path

import linc_nerve.core.ios  # conftest imports ios_discovery; break the import cycle

REPO = Path(__file__).resolve().parents[1]
_REPO_SRC = REPO / "src"
SRC = _REPO_SRC if _REPO_SRC.is_dir() else Path(linc_nerve.__file__).resolve().parent
_GONE_MODULE = "mcp.server.fastmcp"
_GONE_NAME = "ServerRequestResponder"


def _offenders(path: Path, root: Path = REPO) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _GONE_MODULE or alias.name.startswith(_GONE_MODULE + "."):
                    found.append(f"{path.relative_to(root)}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _GONE_MODULE or module.startswith(_GONE_MODULE + "."):
                found.append(f"{path.relative_to(root)}:{node.lineno} from {module}")
            for alias in node.names:
                if alias.name == _GONE_NAME:
                    found.append(f"{path.relative_to(root)}:{node.lineno} import {_GONE_NAME}")
    return found


def test_contract_no_fastmcp_or_server_request_responder_imports() -> None:
    src_files = sorted(SRC.rglob("*.py"))
    test_files = sorted((REPO / "tests").rglob("*.py"))
    assert len(src_files) + len(test_files) > 100
    offenders = [
        *(hit for f in src_files for hit in _offenders(f, SRC)),
        *(hit for f in test_files for hit in _offenders(f, REPO)),
    ]
    assert offenders == []


def test_contract_detector_flags_each_import_form(tmp_path: Path) -> None:
    """The walker itself catches every spelling (guards against a vacuous pass)."""
    probe = tmp_path / "probe.py"
    snippets = [
        "from mcp.server.fastmcp import FastMCP\n",
        "from mcp.server.fastmcp.exceptions import ToolError\n",
        "import mcp.server.fastmcp\n",
        "def f():\n    from mcp.server.session import ServerRequestResponder\n",
    ]
    for snippet in snippets:
        probe.write_text(snippet, encoding="utf-8")
        assert len(_offenders(probe, tmp_path)) == 1, snippet
    probe.write_text("from mcp.server.mcpserver import MCPServer\n", encoding="utf-8")
    assert _offenders(probe, tmp_path) == []
