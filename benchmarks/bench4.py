#!/usr/bin/env python
"""slicegrep benchmark v4 — MULTI-TURN retrieval on real sessions.

The single-shot v3 benchmark denies every strategy the thing real agents do:
look at round-1 results and refine. v4 allows exactly one refinement, with the
SAME mechanical policy for every strategy (no strategy-specific tuning):

  round 1: retrieve with the session query (from the commit message)
  refine : extract the most frequent rare identifiers from round-1 context
           that are not already in the query (top 4)
  round 2: retrieve with query + those identifiers
  context: round1 + round2 concatenated, capped at 8k tokens
  calls  : sum of both rounds' tool calls

Ground truth, sessions, and metrics are identical to bench3 (session hit =
coverage >= 0.5 of the regions the real fix touched).

Usage:
    python benchmarks/bench4.py --corpora-dir PATH [--sessions 80]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench2 import RepoCache, STRATEGIES, cap  # noqa: E402
from bench3 import (  # noqa: E402
    REPOS,
    SEED,
    coverage,
    gt_from_hunks,
    mine_sessions,
    snapshot,
)
from slicegrep.core import estimate_tokens  # noqa: E402

_REFINE_STOP = set("""
    self return import from class def none true false print raise assert
    except finally lambda yield async await global nonlocal
""".split())


def refine_terms(context: str, query: str, k: int = 4) -> list:
    """Identifiers an agent would pull from round-1 output to search next:
    frequent, identifier-shaped, length>=5, not already queried."""
    qlow = query.lower()
    counts = Counter(
        t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", context)
        if t.lower() not in _REFINE_STOP and t.lower() not in qlow
    )
    # prefer snake_case/camelCase (real identifiers) over plain words
    scored = sorted(counts.items(),
                    key=lambda kv: (("_" in kv[0] or kv[0][:1].islower() and
                                     any(c.isupper() for c in kv[0])) * 100
                                    + kv[1]),
                    reverse=True)
    return [t for t, _c in scored[:k]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpora-dir", required=True)
    ap.add_argument("--sessions", type=int, default=80)
    ap.add_argument("--md", help="write markdown report here")
    ap.add_argument("--json", help="write raw results here")
    args = ap.parse_args()

    base = Path(args.corpora_dir)
    rng = random.Random(SEED)
    per_repo = -(-args.sessions // len(REPOS))
    sessions = []
    for name in REPOS:
        repo = base / name
        if repo.is_dir():
            sessions.extend(mine_sessions(repo, name, per_repo, rng))
    rng.shuffle(sessions)
    sessions = sessions[: args.sessions]
    print(f"{len(sessions)} sessions, 2-round retrieval\n")

    workdir = Path(tempfile.mkdtemp(prefix="slicegrep-bench4-"))
    cells = []
    try:
        for idx, s in enumerate(sessions, 1):
            snap = workdir / f"s{idx}"
            try:
                snapshot(s["repo"], s["sha"], snap)
            except Exception:
                continue
            cache = RepoCache(snap, s["label"])
            gt = gt_from_hunks(cache, s["hunks"])
            if gt is None:
                shutil.rmtree(snap, ignore_errors=True)
                continue
            if idx % 10 == 0:
                print(f"  ... {idx}/{len(sessions)}")
            for sname, fn in STRATEGIES:
                t0 = time.perf_counter()
                try:
                    ctx1, calls1 = fn(cache, s["query"])
                except Exception:
                    ctx1, calls1 = "", 1
                terms = refine_terms(cap(ctx1), s["query"])
                if terms:
                    q2 = s["query"] + "|" + "|".join(re.escape(t) for t in terms)
                    try:
                        ctx2, calls2 = fn(cache, q2)
                    except Exception:
                        ctx2, calls2 = "", 1
                else:
                    ctx2, calls2 = "", 0
                lat = (time.perf_counter() - t0) * 1000
                combined = cap(ctx1 + "\n" + ctx2)
                tokens = estimate_tokens(combined) if combined.strip() else 0
                cov = coverage(combined, gt) if combined.strip() else 0.0
                cells.append({
                    "session": s["subject"][:60], "strategy": sname,
                    "tokens": tokens, "coverage": round(cov, 3),
                    "hit": cov >= 0.5, "tool_calls": calls1 + calls2,
                    "latency_ms": round(lat, 1),
                })
            shutil.rmtree(snap, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    n = len(cells) // len(STRATEGIES)
    print(f"\n=== MULTI-TURN SUMMARY over {n} sessions ===")
    print(f"{'strategy':12s} {'tokens':>7s} {'hit%':>6s} {'coverage':>9s} "
          f"{'calls':>6s} {'lat/task':>9s}")
    summary = {}
    for sname, _ in STRATEGIES:
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
        print(f"{sname:12s} {r['median_tokens']:>7,d} {r['session_hit_rate']:>5.1f}% "
              f"{r['mean_coverage']:>8.1f}% {r['median_tool_calls']:>6d} "
              f"{r['median_latency_ms']:>7.1f}ms")

    if args.md:
        out = ["# benchmark v4 — multi-turn retrieval (2 rounds) on real sessions\n"]
        out.append(f"{n} sessions from bench3's miner. Every strategy gets the "
                   "same mechanical refinement: round-1 retrieve, extract the 4 "
                   "most frequent unseen identifiers from the capped round-1 "
                   "context, re-retrieve with them appended, union capped at 8k "
                   "tokens.\n")
        out.append("| strategy | tokens | session hit | mean coverage | tool calls | latency |")
        out.append("|---|---|---|---|---|---|")
        for sname, _ in STRATEGIES:
            if sname not in summary:
                continue
            r = summary[sname]
            out.append(f"| {sname} | {r['median_tokens']:,} | {r['session_hit_rate']}% "
                       f"| {r['mean_coverage']}% | {r['median_tool_calls']} "
                       f"| {r['median_latency_ms']} ms |")
        Path(args.md).write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nwrote {args.md}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"summary": summary, "cells": cells}, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
