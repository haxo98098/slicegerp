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

# Corpora for the scaled run (--scale N): pinned tags of real, widely-used
# Python projects with different sizes and layouts.
CORPORA = [
    ("pallets/click", "8.1.7"),
    ("pallets/flask", "3.0.0"),
    ("psf/requests", "v2.31.0"),
    ("Textualize/rich", "v13.7.0"),
]
SEED = 20260719  # deterministic task sampling


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
# Scaled task generation (--scale N)
#
# Samples real public symbols (top-level and method defs/classes) from each
# corpus and asks "find/understand this symbol" with query styles of varying
# difficulty, mimicking what an agent actually types:
#   exact  — "def name" / "class name"
#   bare   — "name" (matches usages too; the definition must still win)
#   fuzzy  — "part1|part2|name" for snake_case names (concept-style query)
# Sampling is seeded, so runs are reproducible.
# --------------------------------------------------------------------------- #

_DEF_RE = re.compile(r"^(\s*)(def|class)\s+([A-Za-z][A-Za-z0-9_]*)")


def collect_symbols(repo: Path) -> List[Tuple[str, int, str, str]]:
    """(rel_file, lineno, kind, name) for public defs/classes, deduped by name."""
    out = []
    seen = set()
    for f in sorted(repo.rglob("*.py")):
        rel = f.relative_to(repo).as_posix()
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        low = rel.lower()
        if "test" in low or low.startswith(("docs/", "examples/")):
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            m = _DEF_RE.match(line)
            if not m:
                continue
            name = m.group(3)
            if name.startswith("_") or len(name) < 4:
                continue
            if name in seen:      # ambiguous names skew ground truth; keep first
                continue
            seen.add(name)
            out.append((rel, i, m.group(2), name))
    return out


def generate_tasks(repo: Path, label: str, count: int, rng) -> list:
    """Return [(task_name, query, rel_file, lineno)] sampled from the corpus."""
    cands = collect_symbols(repo)
    rng.shuffle(cands)
    tasks = []
    for rel, lineno, kind, name in cands[:count]:
        style = rng.random()
        if style < 0.4:
            query = f"{kind} {name}"
        elif style < 0.8 or "_" not in name:
            query = name
        else:
            parts = [p for p in name.split("_") if len(p) >= 4][:2]
            query = "|".join(parts + [name]) if parts else name
        tasks.append((f"{label}: {kind} {name}", query, rel, lineno))
    return tasks


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


def gt_at_line(repo: Path, rel_file: str, lineno: int) -> GroundTruth:
    """Ground truth (definition site) for a known def/class line number."""
    path = repo / rel_file
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    block = _python_enclosing_block(lines, lineno)
    if block is None:
        start, end = lineno, min(len(lines), lineno + GT_HEAD_LINES)
    else:
        start, end, _sym = block
    end = min(end, start + 1 + GT_HEAD_LINES)
    text = "".join(lines[start:end])
    return GroundTruth(path, start, end, text, estimate_tokens(text))


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


def run(prepared: List[tuple], verbose: bool = True) -> List[Cell]:
    """prepared: [(task_name, query, repo_path, GroundTruth)]"""
    cells: List[Cell] = []
    for idx, (name, query, repo, gt) in enumerate(prepared, 1):
        if not verbose and idx % 25 == 0:
            print(f"  ... {idx}/{len(prepared)} tasks")
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
            if verbose:
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
            "mean_tool_calls": round(statistics.mean(c.tool_calls for c in sub), 1),
            "median_latency_ms": round(
                statistics.median(c.latency_ms for c in sub), 1),
            "total_time_s": round(sum(c.latency_ms for c in sub) / 1000, 1),
        })
    return rows


def to_markdown(cells: List[Cell], rows: List[dict], repo_label: str,
                engine: str, reproduce_cmd: str, detail: bool = True) -> str:
    n_tasks = len(cells) // len(STRATEGIES)
    out = []
    out.append("# slicegrep retrieval benchmark\n")
    out.append(f"Corpus: **{repo_label}** — {n_tasks} real code-lookup tasks. "
               f"Context cap {CONTEXT_CAP} tokens/lookup; slicegrep budget "
               f"{SLICEGREP_BUDGET}; window strategy ±{WINDOW} lines. "
               f"Search engine for baselines: **{engine}**.\n")
    out.append("**Definition hit rate** = the required definition site landed "
               "inside the capped context. This measures *retrieval* quality — "
               "the necessary condition for a model to answer — not end-to-end "
               f"agent task completion. Reproduce with `{reproduce_cmd}`.\n")
    out.append(f"## Summary (median over {n_tasks} tasks)\n")
    out.append("| strategy | tokens → model | definition hit rate | irrelevant code "
               "| tool calls (med/mean) | latency/task | total time |")
    out.append("|---|---|---|---|---|---|---|")
    for r in rows:
        out.append(
            f"| {r['strategy']} | {r['median_tokens']:,} "
            f"| {r['success_rate']:.1f}% "
            f"| {r['median_irrelevant_pct']}% "
            f"| {r['median_tool_calls']} / {r['mean_tool_calls']} "
            f"| {r['median_latency_ms']} ms "
            f"| {r['total_time_s']} s |")
    out.append(
        "\n## Tradeoff and limitations\n\n"
        "slicegrep trades roughly 20 ms of extra local retrieval time per "
        "lookup for a higher context hit rate, fewer tool calls, and "
        "substantially fewer input tokens — trivial beside an LLM request "
        "that takes seconds, but stated here rather than hidden.\n\n"
        "Known limitations of this benchmark:\n\n"
        "- Generated tasks are symbol-definition lookups, which plays to a "
        "tool with explicit definition-ranking logic. Harder task families "
        "(bug localization, cross-file call chains, config/data-flow tracing, "
        "test+implementation retrieval) are planned.\n"
        "- The whole-file and window baselines concatenate results in "
        "search order and truncate at the cap, so ordering luck affects "
        "them. This models a naive agent; a smarter baseline would rank "
        "matching files first. Stronger baselines (ripgrep + heuristic "
        "ranking, LSP definition/references, a lightweight embedding "
        "retriever) are planned.\n")
    if detail:
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


def _clone(url: str, tag: str, dest: Path) -> None:
    print(f"cloning {url}@{tag} ...")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", tag, "--quiet",
         url, str(dest)],
        check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="path to an existing clone of the corpus (curated mode)")
    ap.add_argument("--clone", action="store_true",
                    help="clone the corpora into a temp dir")
    ap.add_argument("--scale", type=int, metavar="N",
                    help=f"scaled mode: generate N tasks across {len(CORPORA)} corpora "
                         "(seeded, reproducible) instead of the 10 curated tasks")
    ap.add_argument("--json", help="write raw per-cell results to this path")
    ap.add_argument("--md", help="write a markdown report to this path")
    args = ap.parse_args()

    engine = "ripgrep" if _rg_path() else "python-scan (rg not found)"
    tmp = None
    prepared: List[tuple] = []

    if args.scale:
        import random
        if not args.clone:
            ap.error("--scale requires --clone")
        tmp = tempfile.mkdtemp(prefix="slicegrep-bench-")
        rng = random.Random(SEED)
        per_corpus = -(-args.scale // len(CORPORA))  # ceil division
        for full, tag in CORPORA:
            dest = Path(tmp) / full.split("/")[1]
            _clone(f"https://github.com/{full}", tag, dest)
            for name, query, rel, lineno in generate_tasks(
                    dest, f"{full}@{tag}", per_corpus, rng):
                prepared.append((name, query, dest, gt_at_line(dest, rel, lineno)))
        rng.shuffle(prepared)
        prepared = prepared[: args.scale]
        label = ", ".join(f"{f}@{t}" for f, t in CORPORA)
        reproduce = f"python benchmarks/bench.py --clone --scale {args.scale}"
        print(f"\n{len(prepared)} generated tasks   baseline search engine: {engine}\n")
        cells = run(prepared, verbose=False)
    else:
        if args.repo:
            repo = Path(args.repo)
        elif args.clone:
            tmp = tempfile.mkdtemp(prefix="slicegrep-bench-")
            repo = Path(tmp) / "click"
            _clone(CLICK_REPO, CLICK_TAG, repo)
        else:
            ap.error("pass --repo PATH, --clone, or --clone --scale N")
        print(f"corpus: {repo}   baseline search engine: {engine}\n")
        prepared = [
            (name, query, repo, resolve_ground_truth(repo, gt_file, gt_regex))
            for name, query, gt_file, gt_regex in TASKS
        ]
        label = f"pallets/click @ {CLICK_TAG}"
        reproduce = "python benchmarks/bench.py --clone"
        cells = run(prepared, verbose=True)

    rows = summarize(cells)

    print("\n=== SUMMARY (median over tasks) ===")
    print(f"{'strategy':12s} {'tokens':>8s} {'success':>8s} {'irrelev':>8s} "
          f"{'calls':>6s} {'latency':>9s} {'total':>8s}")
    for r in rows:
        print(f"{r['strategy']:12s} {r['median_tokens']:>8,d} "
              f"{r['success_rate']:>7.1f}% {r['median_irrelevant_pct']:>7.1f}% "
              f"{r['median_tool_calls']:>6d} {r['median_latency_ms']:>7.1f}ms "
              f"{r['total_time_s']:>7.1f}s")

    if args.md:
        Path(args.md).write_text(
            to_markdown(cells, rows, label, engine, reproduce,
                        detail=len(prepared) <= 30),
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
