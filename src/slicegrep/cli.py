"""slicegrep command-line interface.

    slicegrep <path> <pattern> [before] [after] [options]

Installed as both ``slicegrep`` and ``fr`` (focused read).
"""
from __future__ import annotations

import argparse
import io
import sys

from . import __version__
from .core import focused_read

_EPILOG = """\
examples:
  slicegrep src/app.py "def handle_request"          find a function
  slicegrep src/ "Scorer|def score" --boundary fn    whole enclosing blocks, recursively
  slicegrep . "retry|timeout|backoff" --budget 1500  co-occurring concepts under a token cap
  slicegrep src/ "TODO" 2 2 --json                   raw JSON for tooling

The pattern is a case-insensitive regex. Join alternatives with '|'; a chunk
matching MORE of them ranks higher (co-occurrence scoring).
"""


def _force_utf8_stdout() -> None:
    # Windows consoles default to cp1252 and choke on the em-dash / box chars
    # the report emits. Force UTF-8 so output is never mojibake.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
    except (AttributeError, ValueError):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="slicegrep",
        description="grep that returns ranked, token-budgeted code slices for LLMs.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("path", help="file OR directory (a directory implies a recursive walk)")
    p.add_argument("pattern", help="regex; join alternatives with '|'")
    p.add_argument("before", nargs="?", type=int, default=40,
                   help="context lines before each match (default 40)")
    p.add_argument("after", nargs="?", type=int, default=40,
                   help="context lines after each match (default 40)")
    p.add_argument("--budget", type=int, default=0, metavar="N",
                   help="keep only the highest-ranked chunks fitting ~N tokens")
    p.add_argument("--boundary", choices=("auto", "fn", "none"), default="auto",
                   help="'fn' snaps each chunk to its enclosing function/class")
    p.add_argument("--recursive", "-r", action="store_true",
                   help="force a directory walk even for a file path")
    p.add_argument("--objective", choices=("auto", "single", "def+caller", "def+test"),
                   default="auto",
                   help="what the budget must cover: auto guarantees definition + "
                        "cross-file usage + test chunks when present; single is "
                        "pure score order (default: auto)")
    p.add_argument("--no-dedupe", action="store_true",
                   help="keep near-duplicate chunks (exact dups still collapse)")
    p.add_argument("--json", action="store_true",
                   help="print raw JSON instead of the rendered report")
    p.add_argument("--version", action="version", version=f"slicegrep {__version__}")
    return p


def main(argv=None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)

    try:
        result = focused_read(
            args.path,
            args.pattern,
            before=args.before,
            after=args.after,
            budget=args.budget,
            boundary=args.boundary,
            objective=args.objective,
            recursive=args.recursive,
            dedupe=not args.no_dedupe,
        )
    except FileNotFoundError:
        print(f"slicegrep: path not found: {args.path}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"slicegrep: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(result.to_json(indent=2))
    else:
        print(result.render())

    # Exit 1 when nothing matched, so scripts and CI can branch on it.
    return 0 if result.chunks else 1


if __name__ == "__main__":
    raise SystemExit(main())
