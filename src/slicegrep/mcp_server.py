"""slicegrep MCP server.

Exposes ``focused_read`` as a Model Context Protocol tool so any MCP client
(Claude Desktop, Claude Code, Cursor, Windsurf, ...) can read a codebase in
ranked, token-budgeted slices instead of dumping whole files into context.

Run it::

    slicegrep-mcp                 # stdio transport (what MCP clients spawn)
    python -m slicegrep.mcp_server

Requires the optional ``mcp`` dependency::

    pip install "slicegrep[mcp]"
"""
from __future__ import annotations

import sys
from typing import Optional

from .core import focused_read

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - exercised only without the extra
    FastMCP = None  # type: ignore


_INSTRUCTIONS = (
    "slicegrep reads code the way an LLM should: it greps a file or directory, "
    "extracts only the relevant slices, ranks them, dedupes near-duplicates, "
    "caps the total to a token budget, and reports what it did NOT find. "
    "Prefer it over reading whole files when you need to locate or understand code."
)


def build_server() -> "FastMCP":
    if FastMCP is None:
        raise RuntimeError(
            "The 'mcp' package is required for the slicegrep MCP server. "
            'Install it with:  pip install "slicegrep[mcp]"'
        )

    mcp = FastMCP("slicegrep", instructions=_INSTRUCTIONS)

    @mcp.tool()
    def focused_read_tool(
        path: str,
        pattern: str,
        before: int = 40,
        after: int = 40,
        budget: int = 800,
        boundary: str = "auto",
        recursive: bool = False,
        dedupe: bool = True,
    ) -> str:
        """Read code sections matching a regex, ranked and capped to a token budget.

        Args:
            path: A file or directory. A directory triggers a recursive walk of
                source files (vendored/build dirs and files >1 MB are skipped).
            pattern: Case-insensitive regex. Join alternatives with '|'; a chunk
                matching more of them ranks higher (co-occurrence scoring).
            before: Context lines before each match (ignored when boundary='fn').
            after: Context lines after each match (ignored when boundary='fn').
            budget: Keep only the top-ranked chunks fitting ~this many tokens.
                Defaults to 800; raise for a broader survey, 0 for no cap.
            boundary: 'auto' (fixed window) or 'fn' (snap to the enclosing
                function/class).
            recursive: Force a directory walk even for a file path.
            dedupe: Collapse near-duplicate chunks (exact dups always collapse).

        Returns:
            A ranked, budget-capped text report with a NEGATIVE EVIDENCE section
            listing patterns/symbols that were not found.
        """
        result = focused_read(
            path,
            pattern,
            before=before,
            after=after,
            budget=budget,
            boundary=boundary,
            recursive=recursive,
            dedupe=dedupe,
        )
        return result.render()

    return mcp


def main(argv: Optional[list] = None) -> int:
    if FastMCP is None:
        print(
            'slicegrep MCP server needs the "mcp" package. '
            'Install with:  pip install "slicegrep[mcp]"',
            file=sys.stderr,
        )
        return 1
    build_server().run()  # stdio transport
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
