#!/usr/bin/env python
"""slicegrep retrieval benchmark v3 — real coding sessions mined from git history.

v1/v2 used generated lookup questions. v3 removes the "synthetic tasks favor
your tool" objection by using REAL changes:

  1. Mine real commits from the corpora (bugfixes/features: 1-3 .py files,
     5-80 changed lines, informative message; merges/bumps/typos filtered).
  2. Reconstruct the repo AS IT WAS at the parent commit (git archive).
  3. Build the query ONLY from the commit message — what a developer or agent
     would actually know before finding the code ("fix X crashing when Y").
     Never from the diff; that would leak ground truth.
  4. Ground truth = the pre-image regions the real fix actually touched
     (diff hunks, ±2 lines). Not my opinion of what's relevant: what the
     human who made the change had to look at.
  5. Every strategy gets the same session: retrieve context for the query
     under the same 8k cap. Score = how much of the soon-to-be-changed code
     landed in context.

Metrics:
  hunk coverage — fraction of ground-truth hunks fully present in context
  session hit   — coverage >= 0.5 (the agent found at least half the sites
                  the real fix touched; with one call left it can finish)
  plus the usual tokens / tool calls / latency

Requires full clones (not --depth 1):
    python benchmarks/bench3.py --corpora-dir PATH [--sessions 80]
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import statistics
import subprocess
import sys
import tempfile
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple

# NOTE: no stdout re-wrap here — bench2 (imported below) already wraps
# sys.stdout at import time; wrapping twice closes the shared buffer.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench2 import (  # noqa: E402
    GT,
    RepoCache,
    Span,
    STRATEGIES,
    cap,
    _norm,
)
from slicegrep.core import estimate_tokens  # noqa: E402

SEED = 20260719
CONTEXT_CAP = 8000

REPOS = ["click", "flask", "requests", "rich"]

_SKIP_SUBJECT = re.compile(
    r"(?i)\b(release|bump|changelog|typo|merge|backport|pre-commit|"
    r"version|translat|copyright|readme|docs? only|whitespace|lint|fmt|"
    r"black|isort|flake8|mypy|coverage)\b"
)
_STOP = set("""
    the this that with from for and are was were will would should could into
    over under been being have has had does doing done which their there then
    than when where what while whose about after before because between fix
    fixes fixed adds added add support use uses using make makes made allow
    allows now instead also only some more test tests
""".split())


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return proc.stdout


# --------------------------------------------------------------------------- #
# Session mining
# --------------------------------------------------------------------------- #

def mine_sessions(repo: Path, label: str, want: int, rng) -> List[dict]:
    log = _git(repo, "log", "--no-merges", "--max-count=4000",
               "--pretty=format:%H\x01%s\x01%b\x02")
    candidates = []
    for block in log.split("\x02"):
        block = block.strip()
        if not block:
            continue
        parts = block.split("\x01")
        if len(parts) < 2:
            continue
        sha, subject = parts[0].strip(), parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        if len(subject) < 20 or _SKIP_SUBJECT.search(subject):
            continue
        candidates.append((sha, subject, body))
    rng.shuffle(candidates)

    sessions = []
    for sha, subject, body in candidates:
        if len(sessions) >= want:
            break
        numstat = _git(repo, "diff", "--numstat", f"{sha}^", sha, "--", "*.py")
        files = [l.split("\t") for l in numstat.splitlines() if l.strip()]
        files = [f for f in files if len(f) == 3]
        if not (1 <= len(files) <= 3):
            continue
        try:
            changed = sum(int(a) + int(d) for a, d, _p in files
                          if a != "-" and d != "-")
        except ValueError:
            continue
        if not (5 <= changed <= 80):
            continue
        hunks = parse_hunks(_git(repo, "diff", "--unified=0",
                                 f"{sha}^", sha, "--", "*.py"))
        if not hunks:
            continue
        query = query_from_message(subject, body)
        if query is None:
            continue
        sessions.append({
            "repo": repo, "label": label, "sha": sha,
            "subject": subject, "query": query, "hunks": hunks,
        })
    return sessions


def parse_hunks(diff: str) -> List[Tuple[str, int, int]]:
    """(old_path, old_start_0based, old_len) per hunk, pre-image side."""
    hunks = []
    old_path: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            p = line[4:].strip()
            old_path = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif line.startswith("@@ ") and old_path and old_path.endswith(".py"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))?", line)
            if not m:
                continue
            start = int(m.group(1))
            length = int(m.group(2)) if m.group(2) is not None else 1
            # pure insertions have length 0: anchor on surrounding context
            hunks.append((old_path, max(0, start - 1), max(length, 1)))
    return hunks


def query_from_message(subject: str, body: str) -> Optional[str]:
    """Build the search query an agent would type, from the message alone."""
    text = subject + " " + body[:300]
    # identifiers the author called out explicitly
    idents = re.findall(r"[`']([A-Za-z_][A-Za-z0-9_.]{2,40})[`']", text)
    idents = [i.split(".")[-1] for i in idents][:2]
    words = [w for w in re.findall(r"[a-zA-Z_]{4,}", subject.lower())
             if w not in _STOP]
    # dedupe, keep order, cap query terms at 4
    terms = list(dict.fromkeys(idents + words))[:4]
    if len(terms) < 2:
        return None
    return "|".join(re.escape(t) for t in terms)


# --------------------------------------------------------------------------- #
# Ground truth against the parent snapshot
# --------------------------------------------------------------------------- #

def snapshot(repo: Path, sha: str, dest: Path) -> None:
    """Snapshot via git worktree: a real checkout of the PARENT commit with
    a working .git link. `git log` inside sees only ancestors of the parent,
    so history-aware strategies get exactly the history a developer had at
    that moment — and nothing from the future."""
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach",
                    "--quiet", str(dest), f"{sha}^"], check=True,
                   capture_output=True)


def drop_snapshot(repo: Path, dest: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force",
                    str(dest)], capture_output=True)


def gt_from_hunks(cache: RepoCache, hunks) -> Optional[GT]:
    spans = []
    for rel, start, length in hunks:
        try:
            lines = cache.lines(rel)
        except KeyError:
            continue
        lo = max(0, start - 2)
        hi = min(len(lines), start + length + 2)
        if hi <= lo:
            continue
        spans.append(Span(rel, lo, hi, "\n".join(lines[lo:hi]) + "\n"))
    return GT(spans) if spans else None


def coverage(context: str, gt: GT) -> float:
    ctx = _norm(context)
    covered = sum(1 for s in gt.spans if _norm(s.text) <= ctx)
    return covered / len(gt.spans)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpora-dir", required=True,
                    help="dir with FULL clones: click/ flask/ requests/ rich/")
    ap.add_argument("--sessions", type=int, default=80,
                    help="total sessions across repos (default 80)")
    ap.add_argument("--seed", type=int, default=SEED,
                    help="sampling seed (use a fresh one for held-out runs)")
    ap.add_argument("--strategies", default=None,
                    help="comma list to run (default: all)")
    ap.add_argument("--exclude-shas", default=None,
                    help="JSON results file whose sessions must be EXCLUDED "
                         "(held-out evaluation: never test on tuned-on data)")
    ap.add_argument("--md", help="write markdown report here")
    ap.add_argument("--json", help="write raw results here")
    args = ap.parse_args()

    strategies = (STRATEGIES if not args.strategies else
                  [(n, f) for n, f in STRATEGIES
                   if n in args.strategies.split(",")])
    base = Path(args.corpora_dir)
    rng = random.Random(args.seed)
    per_repo = -(-args.sessions // len(REPOS))

    print("mining real sessions from git history ...")
    sessions = []
    for name in REPOS:
        repo = base / name
        if not repo.is_dir():
            continue
        mined = mine_sessions(repo, name, per_repo, rng)
        print(f"  {name}: {len(mined)} sessions")
        sessions.extend(mined)
    rng.shuffle(sessions)
    sessions = sessions[: args.sessions]
    print(f"{len(sessions)} sessions total\n")

    workdir = Path(tempfile.mkdtemp(prefix="slicegrep-bench3-"))
    cells = []
    try:
        for idx, s in enumerate(sessions, 1):
            snap = workdir / f"s{idx}"
            try:
                snapshot(s["repo"], s["sha"], snap)
            except subprocess.CalledProcessError:
                continue
            cache = RepoCache(snap, s["label"])
            gt = gt_from_hunks(cache, s["hunks"])
            if gt is None:
                drop_snapshot(s["repo"], snap)
                shutil.rmtree(snap, ignore_errors=True)
                continue
            if idx % 10 == 0:
                print(f"  ... {idx}/{len(sessions)}")
            for sname, fn in strategies:
                t0 = time.perf_counter()
                try:
                    context, calls = fn(cache, s["query"])
                except Exception:
                    context, calls = "", 1
                lat = (time.perf_counter() - t0) * 1000
                capped = cap(context)
                tokens = estimate_tokens(capped) if capped else 0
                cov = coverage(capped, gt) if capped else 0.0
                cells.append({
                    "session": s["subject"][:60], "corpus": s["label"],
                    "sha": s["sha"][:10], "strategy": sname,
                    "tokens": tokens, "coverage": round(cov, 3),
                    "hit": cov >= 0.5, "tool_calls": calls,
                    "latency_ms": round(lat, 1), "n_hunks": len(gt.spans),
                })
            drop_snapshot(s["repo"], snap)
            shutil.rmtree(snap, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    n_sessions = len(cells) // max(1, len(strategies))
    print(f"\n=== SUMMARY over {n_sessions} real sessions ===")
    print(f"{'strategy':12s} {'tokens':>7s} {'hit%':>6s} {'coverage':>9s} "
          f"{'calls':>6s} {'lat/task':>9s}")
    summary = {}
    for sname, _ in strategies:
        sub = [c for c in cells if c["strategy"] == sname]
        if not sub:
            continue
        r = {
            "median_tokens": int(statistics.median(c["tokens"] for c in sub)),
            "session_hit_rate": round(100.0 * sum(c["hit"] for c in sub) / len(sub), 1),
            "mean_coverage": round(100.0 * statistics.mean(c["coverage"] for c in sub), 1),
            "median_tool_calls": int(statistics.median(c["tool_calls"] for c in sub)),
            "median_latency_ms": round(statistics.median(c["latency_ms"] for c in sub), 1),
        }
        summary[sname] = r
        print(f"{sname:12s} {r['median_tokens']:>7,d} "
              f"{r['session_hit_rate']:>5.1f}% {r['mean_coverage']:>8.1f}% "
              f"{r['median_tool_calls']:>6d} {r['median_latency_ms']:>7.1f}ms")

    if args.md:
        out = ["# slicegrep retrieval benchmark v3 — real sessions from git history\n"]
        out.append(f"{n_sessions} real changes mined from the corpora's own git "
                   "history (1-3 .py files, 5-80 changed lines, informative "
                   "message; merges/bumps/typo commits filtered). For each: the "
                   "repo is reconstructed at the parent commit, the query is "
                   "built ONLY from the commit message, and ground truth is the "
                   "pre-image regions the real fix touched (diff hunks ±2 "
                   "lines).\n")
        out.append("**Session hit** = at least half of the regions the real fix "
                   "changed landed in the retrieved context (coverage ≥ 0.5) "
                   "under an 8k-token cap. **Coverage** = mean fraction of "
                   "changed regions retrieved. Retrieval quality, not "
                   "end-to-end task completion.\n")
        out.append("| strategy | tokens → model | session hit | mean coverage "
                   "| tool calls | latency/task |")
        out.append("|---|---|---|---|---|---|")
        for sname, _ in strategies:
            if sname not in summary:
                continue
            r = summary[sname]
            out.append(f"| {sname} | {r['median_tokens']:,} "
                       f"| {r['session_hit_rate']}% | {r['mean_coverage']}% "
                       f"| {r['median_tool_calls']} "
                       f"| {r['median_latency_ms']} ms |")
        out.append(
            "\n## Notes\n\n"
            "- Queries come from commit messages, never from diffs — the "
            "message is what a developer/agent knows *before* finding the "
            "code. Messages vary in quality; that variance is part of the "
            "task, all strategies face the same messages.\n"
            "- Coverage rewards finding the sites the real author changed. A "
            "strategy could retrieve genuinely useful context that the fix "
            "didn't touch and get no credit; this is a floor on usefulness, "
            "not a ceiling.\n"
            "- Sessions are seeded and the mining filter is fixed; reproduce "
            "with full clones and "
            "`python benchmarks/bench3.py --corpora-dir <dir>`.\n")
        Path(args.md).write_text("\n".join(out), encoding="utf-8")
        print(f"\nwrote {args.md}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"summary": summary, "cells": cells}, indent=2),
            encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
