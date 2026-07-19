#!/usr/bin/env python
"""slicegrep retrieval benchmark.

Compares three context-retrieval strategies an LLM coding agent could use to
answer real code-lookup tasks against a real codebase:

  1. whole-file  — search for the pattern, read every matching file in full
  2. rg+windows  — search for the pattern, read a +/-60-line window around
                   each match (merged when overlapping), like an agent doing
                   offset reads after a grep
  3. slicegrep   — one focused_read() call with a token budget

For every (task, strategy) pair we measure:

  * tokens delivered to the model (post context-cap)
  * whether the required definition was fully included  -> "task success"
  * irrelevant-code percentage (tokens outside the ground-truth block)
  * tool calls required (search + read operations)
  * retrieval latency (wall clock, this machine)

"Task success" is deliberately mechanical: the full ground-truth definition
block must land inside the capped context. That is the *necessary* condition
for a model to answer correctly; using a live LLM per cell would add noise
and cost without changing the ordering. The cap models a realistic amount an
agent is willing to paste into its context per lookup (default 8000 tokens).

Usage:
    python benchmarks/bench.py --repo /path/to/click        # pre-cloned
    python benchmarks/bench.py --clone                      # clone click 8.1.7
    python benchmarks/bench.py --clone --json results.json --md RESULTS.md
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Allow running from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slicegrep import focused_read  # noqa: E402
from slicegrep.core import (  # noqa: E402
    SEARCHABLE_EXTS,
    SKIP_DIRS,
    _python_enclosing_block,
    estimate_tokens,
)

CONTEXT_CAP = 8000          # tokens an agent will paste per lookup
WINDOW = 60                 # +/- lines for the rg+windows strategy
SLICEGREP_BUDGET = 2000     # slicegrep's token budget per call
GT_HEAD_LINES = 25          # definition site = def line + this many body lines

CLICK_REPO = "https://github.com/pallets/click"
CLICK_TAG = "8.1.7"


# --------------------------------------------------------------------------- #
# Tasks: (name, agent query, ground-truth file, ground-truth def-line regex)
#
# The query is what an agent would plausibly search for — a mix of exact
# symbol lookups and fuzzier concept queries. The ground truth is the one
# definition block the agent must see to answer the task.
# --------------------------------------------------------------------------- #

TASKS = [
    ("find Context class",
     "class Context", "src/click/core.py", r"^class Context[\(:]"),
    ("find Option class",
     "class Option", "src/click/core.py", r"^class Option[\(:]"),
    ("find ParamType base",
     "class ParamType", "src/click/types.py", r"^class ParamType[\(:]"),
    ("find echo implementation",
     "def echo", "src/click/utils.py", r"^def echo\("),
    ("find prompt implementation",
     "def prompt|hide_input", "src/click/termui.py", r"^def prompt\("),
    ("find UsageError",
     "class UsageError|show", "src/click/exceptions.py", r"^class UsageError[\(:]"),
    ("find HelpFormatter",
     "class HelpFormatter|write_usage", "src/click/formatting.py",
     r"^class HelpFormatter[\(:]"),
    ("how does command decorator work",
     "def command|def decorator", "src/click/decorators.py", r"^def command\("),
    ("how are envvars resolved",
     "envvar|resolve_envvar_value", "src/click/core.py",
     r"^\s+def resolve_envvar_value\("),
    ("how does progress bar render",
     "progressbar|def render_progress", "src/click/_termui_impl.py",
     r"^\s+def render_progress\("),
]


# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #

@dataclass
class GroundTruth:
    file: Path
    start: int          # 0-based, inclusive
    end: int            # exclusive
    text: str
    tokens: int


def resolve_ground_truth(repo: Path, rel_file: str, def_regex: str) -> GroundTruth:
    path = repo / rel_file
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    rx = re.compile(def_regex)
    for i, line in enumerate(lines):
        if rx.search(line):
            block = _python_enclosing_block(lines, i)
            if block is None:
                start, end = i, min(len(lines), i + GT_HEAD_LINES)
            else:
                start, end, _sym = block
            # Ground truth is the DEFINITION SITE: the def/class line plus the
            # head of its body (signature, docstring, opening logic). Requiring
            # the entire block would demand e.g. all ~600 lines of class
            # Context — something no strategy (or sane agent) would fetch for
            # a lookup task, and more than a model needs to answer it.
            end = min(end, start + 1 + GT_HEAD_LINES)
            text = "".join(lines[start:end])
            return GroundTruth(path, start, end, text, estimate_tokens(text))
    raise SystemExit(f"ground truth not found: {def_regex!r} in {rel_file}")


# --------------------------------------------------------------------------- #
# Search backends (rg when available, pure-python fallback)
# --------------------------------------------------------------------------- #

def _rg_path() -> Optional[str]:
    return shutil.which("rg")


def search_files(repo: Path, pattern: str) -> List[Path]:
    """Files containing the pattern (rg -l equivalent)."""
    rg = _rg_path()
    if rg:
        proc = subprocess.run(
            [rg, "-l", "-i", "--no-messages", pattern, str(repo)],
            capture_output=True, text=True,
        )
        return [Path(p) for p in proc.stdout.splitlines() if p.strip()]
    return [f for f, _ in _py_scan(repo, pattern)]


def search_matches(repo: Path, pattern: str) -> Dict[Path, List[int]]:
    """Match line numbers per file (rg -n equivalent), 0-based."""
    rg = _rg_path()
    out: Dict[Path, List[int]] = {}
    if rg:
        proc = subprocess.run(
            [rg, "-n", "-i", "--no-messages", pattern, str(repo)],
            capture_output=True, text=True,
        )
        for line in proc.stdout.splitlines():
            # windows drive letters contain ':', so split from the right side
            m = re.match(r"^(.*?):(\d+):", line)
            if m and not m.group(1).isdigit():
                out.setdefault(Path(m.group(1)), []).append(int(m.group(2)) - 1)
        return out
    for f, hits in _py_scan(repo, pattern):
        out[f] = hits
    return out


def _py_scan(repo: Path, pattern: str) -> List[Tuple[Path, List[int]]]:
    rx = re.compile(pattern, re.IGNORECASE)
    results = []
    for f in sorted(repo.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in SEARCHABLE_EXTS:
            continue
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        hits = [
            i for i, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines())
            if rx.search(line)
        ]
        if hits:
            results.append((f, hits))
    return results


# --------------------------------------------------------------------------- #
# Strategies — each returns (context_text, tool_calls)
# --------------------------------------------------------------------------- #

def strat_whole_file(repo: Path, query: str) -> Tuple[str, int]:
    files = search_files(repo, query)
    calls = 1  # the search
    parts = []
    for f in files:
        parts.append(f.read_text(encoding="utf-8", errors="replace"))
        calls += 1
    return "\n".join(parts), calls


def strat_rg_windows(repo: Path, query: str) -> Tuple[str, int]:
    matches = search_matches(repo, query)
    calls = 1  # the search
    parts = []
    for f, hits in matches.items():
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        # merge overlapping +/-WINDOW windows; each merged window = one read
        windows: List[Tuple[int, int]] = []
        for h in sorted(hits):
            lo, hi = max(0, h - WINDOW), min(len(lines), h + WINDOW + 1)
            if windows and lo <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
            else:
                windows.append((lo, hi))
        for lo, hi in windows:
            parts.append("".join(lines[lo:hi]))
            calls += 1
    return "\n".join(parts), calls


def strat_slicegrep(repo: Path, query: str) -> Tuple[str, int]:
    result = focused_read(str(repo), query, budget=SLICEGREP_BUDGET)
    return result.render(), 1


STRATEGIES = [
    ("whole-file", strat_whole_file),
    ("rg+windows", strat_rg_windows),
    ("slicegrep", strat_slicegrep),
]


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def _norm_lines(text: str) -> List[str]:
    return [l.strip() for l in text.splitlines() if l.strip()]


def definition_included(context: str, gt: GroundTruth) -> bool:
    """All non-blank ground-truth lines present in the (capped) context."""
    ctx = set(_norm_lines(context))
    gt_lines = _norm_lines(gt.text)
    return all(l in ctx for l in gt_lines)


def relevant_tokens(context: str, gt: GroundTruth) -> int:
    ctx = set(_norm_lines(context))
    hit_chars = sum(len(l) for l in _norm_lines(gt.text) if l in ctx)
    return max(0, hit_chars // 4)


def cap_context(text: str, cap_tokens: int) -> str:
    if estimate_tokens(text) <= cap_tokens:
        return text
    return text[: cap_tokens * 4]


@dataclass
class Cell:
    task: str
    strategy: str
    tokens: int
    raw_tokens: int
    included: bool
    irrelevant_pct: float
    tool_calls: int
    latency_ms: float


def run(repo: Path) -> List[Cell]:
    cells: List[Cell] = []
    for name, query, gt_file, gt_regex in TASKS:
        gt = resolve_ground_truth(repo, gt_file, gt_regex)
        for sname, fn in STRATEGIES:
            t0 = time.perf_counter()
            context, calls = fn(repo, query)
            latency = (time.perf_counter() - t0) * 1000
            raw_tokens = estimate_tokens(context) if context else 0
            capped = cap_context(context, CONTEXT_CAP)
            tokens = estimate_tokens(capped) if capped else 0
            included = bool(capped) and definition_included(capped, gt)
            rel = relevant_tokens(capped, gt) if capped else 0
            irr = 100.0 * (1 - rel / tokens) if tokens else 100.0
            cells.append(Cell(name, sname, tokens, raw_tokens, included,
                              irr, calls, latency))
            print(f"  {name:34s} {sname:11s} tok={tokens:>6d} "
                  f"def={'Y' if included else 'N'} irr={irr:5.1f}% "
                  f"calls={calls:>3d} {latency:7.1f}ms")
    return cells


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def summarize(cells: List[Cell]) -> List[dict]:
    rows = []
    for sname, _ in STRATEGIES:
        sub = [c for c in cells if c.strategy == sname]
        rows.append({
            "strategy": sname,
            "median_tokens": int(statistics.median(c.tokens for c in sub)),
            "mean_tokens": int(statistics.mean(c.tokens for c in sub)),
            "success_rate": 100.0 * sum(c.included for c in sub) / len(sub),
            "median_irrelevant_pct": round(
                statistics.median(c.irrelevant_pct for c in sub), 1),
            "median_tool_calls": int(statistics.median(c.tool_calls for c in sub)),
            "median_latency_ms": round(
                statistics.median(c.latency_ms for c in sub), 1),
        })
    return rows


def to_markdown(cells: List[Cell], rows: List[dict], repo_label: str,
                engine: str) -> str:
    out = []
    out.append("# slicegrep retrieval benchmark\n")
    out.append(f"Corpus: **{repo_label}** — 10 real code-lookup tasks. "
               f"Context cap {CONTEXT_CAP} tokens/lookup; slicegrep budget "
               f"{SLICEGREP_BUDGET}; window strategy ±{WINDOW} lines. "
               f"Search engine for baselines: **{engine}**.\n")
    out.append("**Task success** = the full ground-truth definition block "
               "landed inside the capped context (the necessary condition for "
               "the model to answer). Reproduce with "
               "`python benchmarks/bench.py --clone`.\n")
    out.append("## Summary (median over 10 tasks)\n")
    out.append("| strategy | tokens → model | task success | irrelevant code | tool calls | latency |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        out.append(
            f"| {r['strategy']} | {r['median_tokens']:,} "
            f"| {r['success_rate']:.0f}% "
            f"| {r['median_irrelevant_pct']}% "
            f"| {r['median_tool_calls']} "
            f"| {r['median_latency_ms']} ms |")
    out.append("\n## Per-task detail\n")
    out.append("| task | strategy | tokens | def included | irrelevant | calls | latency |")
    out.append("|---|---|---|---|---|---|---|")
    for c in cells:
        out.append(
            f"| {c.task} | {c.strategy} | {c.tokens:,} "
            f"| {'✅' if c.included else '❌'} | {c.irrelevant_pct:.1f}% "
            f"| {c.tool_calls} | {c.latency_ms:.0f} ms |")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="path to an existing clone of the corpus")
    ap.add_argument("--clone", action="store_true",
                    help=f"clone {CLICK_REPO}@{CLICK_TAG} into a temp dir")
    ap.add_argument("--json", help="write raw per-cell results to this path")
    ap.add_argument("--md", help="write a markdown report to this path")
    args = ap.parse_args()

    tmp = None
    if args.repo:
        repo = Path(args.repo)
    elif args.clone:
        tmp = tempfile.mkdtemp(prefix="slicegrep-bench-")
        repo = Path(tmp) / "click"
        print(f"cloning {CLICK_REPO}@{CLICK_TAG} ...")
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", CLICK_TAG,
             "--quiet", CLICK_REPO, str(repo)],
            check=True,
        )
    else:
        ap.error("pass --repo PATH or --clone")

    engine = "ripgrep" if _rg_path() else "python-scan (rg not found)"
    print(f"corpus: {repo}   baseline search engine: {engine}\n")

    cells = run(repo)
    rows = summarize(cells)

    print("\n=== SUMMARY (median over tasks) ===")
    hdr = f"{'strategy':12s} {'tokens':>8s} {'success':>8s} {'irrelev':>8s} {'calls':>6s} {'latency':>9s}"
    print(hdr)
    for r in rows:
        print(f"{r['strategy']:12s} {r['median_tokens']:>8,d} "
              f"{r['success_rate']:>7.0f}% {r['median_irrelevant_pct']:>7.1f}% "
              f"{r['median_tool_calls']:>6d} {r['median_latency_ms']:>7.1f}ms")

    label = f"pallets/click @ {CLICK_TAG}" if (args.clone or "click" in str(repo).lower()) else str(repo)
    if args.md:
        Path(args.md).write_text(to_markdown(cells, rows, label, engine),
                                 encoding="utf-8")
        print(f"\nwrote {args.md}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"summary": rows, "cells": [c.__dict__ for c in cells]},
            indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}")

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
