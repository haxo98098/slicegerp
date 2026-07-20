#!/usr/bin/env python
"""slicegrep benchmark v5 — cross-language and monorepo-scale symbol lookup.

Answers two objections to the Python-only, small-repo suites:

  cross-language: symbol-lookup tasks on a TypeScript corpus (zod) and a Rust
      corpus (serde), with language-appropriate definition regexes. Note which
      baselines are structurally Python-only (jedi, ast-tfidf's ast pass,
      repomap's def scan) — they are expected to collapse here and that is
      part of the result.
  scale: the same task family on django (~2,800 .py files) to measure whether
      hit rate and latency degrade at ~10x corpus size.

Ground truth = definition line + 25 lines. Hit = all non-blank ground-truth
lines inside the capped (8k) context. Seeded and reproducible.

Usage:
    python benchmarks/bench5.py --corpora-dir PATH [--tasks-per-corpus 40]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench2 import GT, RepoCache, Span, STRATEGIES, cap, hit  # noqa: E402
from slicegrep.core import estimate_tokens  # noqa: E402

SEED = 20260719
GT_HEAD = 25

CORPORA = [
    ("zod", "typescript",
     re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?"
                r"(?:function|class|interface|type|enum)\s+([A-Za-z_]\w{3,})")),
    ("serde", "rust",
     re.compile(r"^\s*(?:pub(?:\(\w+\))?\s+)?"
                r"(?:fn|struct|enum|trait|macro_rules!)\s+([A-Za-z_]\w{3,})")),
    ("django", "python-at-scale",
     re.compile(r"^\s*(?:def|class)\s+([A-Za-z_]\w{3,})")),
]

_EXT = {"typescript": (".ts", ".tsx"), "rust": (".rs",),
        "python-at-scale": (".py",)}


def gen_tasks(cache: RepoCache, lang: str, rx, count: int, rng) -> list:
    cands, seen = [], set()
    for rel, lines in cache.files:
        if not rel.endswith(_EXT[lang]):
            continue
        low = rel.lower()
        if "test" in low or "/bench" in low or low.startswith(("docs/", "examples/")):
            continue
        for i, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if name.startswith("_") or name in seen:
                continue
            seen.add(name)
            cands.append((rel, i, name))
    rng.shuffle(cands)
    tasks = []
    for rel, i, name in cands[:count]:
        lines = cache.lines(rel)
        end = min(len(lines), i + 1 + GT_HEAD)
        text = "\n".join(lines[i:end]) + "\n"
        query = name if rng.random() < 0.6 else re.sub(
            r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ")
        tasks.append((name, query, GT([Span(rel, i, end, text)])))
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpora-dir", required=True)
    ap.add_argument("--tasks-per-corpus", type=int, default=40)
    ap.add_argument("--md")
    ap.add_argument("--json")
    args = ap.parse_args()

    base = Path(args.corpora_dir)
    rng = random.Random(SEED)
    all_cells = []
    per_corpus = {}
    for cname, lang, rx in CORPORA:
        root = base / cname
        if not root.is_dir():
            print(f"skip {cname} (not cloned)")
            continue
        t0 = time.perf_counter()
        cache = RepoCache(root, cname)
        load_s = time.perf_counter() - t0
        tasks = gen_tasks(cache, lang, rx, args.tasks_per_corpus, rng)
        print(f"{cname} ({lang}): {len(cache.files)} files loaded in "
              f"{load_s:.1f}s, {len(tasks)} tasks")
        for name, query, gt in tasks:
            for sname, fn in STRATEGIES:
                t0 = time.perf_counter()
                try:
                    ctx, calls = fn(cache, query)
                except Exception:
                    ctx, calls = "", 1
                lat = (time.perf_counter() - t0) * 1000
                capped = cap(ctx)
                cell = {
                    "corpus": cname, "lang": lang, "task": name,
                    "strategy": sname,
                    "tokens": estimate_tokens(capped) if capped else 0,
                    "included": bool(capped) and hit(capped, gt),
                    "tool_calls": calls, "latency_ms": round(lat, 1),
                }
                all_cells.append(cell)
        per_corpus[cname] = lang

    for cname, lang in per_corpus.items():
        sub_all = [c for c in all_cells if c["corpus"] == cname]
        n = len(sub_all) // len(STRATEGIES)
        print(f"\n=== {cname} ({lang}) — {n} tasks ===")
        print(f"{'strategy':12s} {'tokens':>7s} {'hit%':>6s} {'calls':>6s} {'lat/task':>9s}")
        for sname, _ in STRATEGIES:
            sub = [c for c in sub_all if c["strategy"] == sname]
            if not sub:
                continue
            print(f"{sname:12s} "
                  f"{int(statistics.median(c['tokens'] for c in sub)):>7,d} "
                  f"{100.0 * sum(c['included'] for c in sub) / len(sub):>5.1f}% "
                  f"{int(statistics.median(c['tool_calls'] for c in sub)):>6d} "
                  f"{statistics.median(c['latency_ms'] for c in sub):>7.1f}ms")

    if args.md:
        out = ["# benchmark v5 — cross-language and monorepo-scale symbol lookup\n"]
        out.append("Symbol-definition retrieval with language-appropriate def "
                   "regexes; ground truth = definition line + 25 lines; 8k cap. "
                   "Python-only baselines (jedi, ast-tfidf, repomap) are "
                   "expected to collapse on TS/Rust — that is part of the "
                   "result, not an error.\n")
        for cname, lang in per_corpus.items():
            sub_all = [c for c in all_cells if c["corpus"] == cname]
            n = len(sub_all) // len(STRATEGIES)
            out.append(f"\n## {cname} ({lang}) — {n} tasks\n")
            out.append("| strategy | tokens | hit rate | tool calls | latency/task |")
            out.append("|---|---|---|---|---|")
            for sname, _ in STRATEGIES:
                sub = [c for c in sub_all if c["strategy"] == sname]
                if not sub:
                    continue
                out.append(
                    f"| {sname} "
                    f"| {int(statistics.median(c['tokens'] for c in sub)):,} "
                    f"| {100.0 * sum(c['included'] for c in sub) / len(sub):.1f}% "
                    f"| {int(statistics.median(c['tool_calls'] for c in sub))} "
                    f"| {statistics.median(c['latency_ms'] for c in sub):.0f} ms |")
        Path(args.md).write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nwrote {args.md}")
    if args.json:
        Path(args.json).write_text(json.dumps(all_cells, indent=2),
                                   encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
