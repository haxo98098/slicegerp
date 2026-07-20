#!/usr/bin/env python
"""Train the learned query router from burned benchmark outcomes.

Builds a labeled set of (query features -> which mode won):
  - v3 sessions (real commits): re-mines sessions for every burned seed to
    recover sha -> query, joins with recorded per-strategy hits. Label
    prefer-lex when lexical slicegrep hit and dense missed; prefer-dense for
    the reverse. Ties are dropped.
  - v2 tasks: regenerates task queries for burned seeds; family labels map to
    the measured family winners (comprehend -> vague; the rest -> precise).

Fits logistic regression (pure python gradient descent) on interpretable
query-shape features and prints weights ready to embed in core.py as the
SLICEGREP_ROUTER=learned mode. Training data is exclusively burned
tuning/dev material — the confirmation sets are never touched.

Usage:
    python benchmarks/train_router.py --corpora-dir PATH
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench3 import REPOS, mine_sessions  # noqa: E402

V3_SEED_SETS = [20260719, 777, 991, 992, 993]
V3_RESULTS = ["results_v3.json", "results_v3_heldout.json",
              "results_final_a.json", "results_final_b.json",
              "results_final_c.json"]


def qfeatures(patterns):
    n = len(patterns)
    plain = sum(1 for p in patterns if re.fullmatch(r"[a-z][a-z0-9]{3,}", p))
    return [
        1.0,                                    # bias
        float(n),
        plain / n if n else 0.0,               # frac plain lowercase words
        1.0 if any("_" in p for p in patterns) else 0.0,
        1.0 if any(re.search(r"[A-Z]", p) for p in patterns) else 0.0,
        1.0 if any("\\" in p for p in patterns) else 0.0,
        sum(len(p) for p in patterns) / (4.0 * n) if n else 0.0,  # avg len/4
    ]


FEATURE_NAMES = ["bias", "n_patterns", "frac_plain", "has_underscore",
                 "has_caps", "has_escape", "avg_len_q"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpora-dir", required=True)
    args = ap.parse_args()
    base = Path(args.corpora_dir)
    bdir = Path(__file__).parent

    # sha -> query (re-mine deterministically for every burned seed)
    sha_query = {}
    for seed in V3_SEED_SETS:
        rng = random.Random(seed)
        for name in REPOS:
            repo = base / name
            if not repo.is_dir():
                continue
            for s in mine_sessions(repo, name, 100, rng):
                sha_query[s["sha"][:10]] = s["query"]

    # sha -> (lex_hit, dense_hit) from burned result files
    lex, dense = {}, {}
    for f in V3_RESULTS:
        try:
            d = json.loads((bdir / f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in d.get("cells", []):
            if c.get("strategy") == "slicegrep":
                lex.setdefault(c["sha"], c["hit"])
            elif c.get("strategy") == "dense-emb":
                dense.setdefault(c["sha"], c["hit"])

    X, y = [], []
    for sha, q in sha_query.items():
        if sha not in lex or sha not in dense:
            continue
        if lex[sha] == dense[sha]:
            continue                             # tie: uninformative
        patterns = [p for p in q.split("|") if p]
        X.append(qfeatures(patterns))
        y.append(1.0 if dense[sha] and not lex[sha] else 0.0)  # 1 = vague/dense

    # v2 family-derived labels (precise families -> 0, comprehend -> 1)
    from bench2 import RepoCache, FAMILIES  # noqa: E402
    for seed in (20260719, 777):
        rng = random.Random(seed)
        for cname in ("click", "flask", "requests", "rich"):
            root = base.parent / "corpora" / cname
            if not root.is_dir():
                continue
            cache = RepoCache(root, cname)
            for fam, gen in FAMILIES:
                for _name, q, _gt in gen(cache, rng, 10):
                    patterns = [p for p in q.split("|") if p]
                    X.append(qfeatures(patterns))
                    y.append(1.0 if fam == "comprehend" else 0.0)

    n = len(X)
    print(f"training examples: {n}  (vague={int(sum(y))}, precise={n-int(sum(y))})")
    if n < 40:
        print("insufficient data"); return 1

    # logistic regression, plain gradient descent
    dim = len(X[0])
    w = [0.0] * dim
    lr = 0.5
    for epoch in range(400):
        grad = [0.0] * dim
        for xi, yi in zip(X, y):
            z = sum(wj * xj for wj, xj in zip(w, xi))
            p = 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))
            e = p - yi
            for j in range(dim):
                grad[j] += e * xi[j]
        for j in range(dim):
            w[j] -= lr * grad[j] / n
    correct = 0
    for xi, yi in zip(X, y):
        z = sum(wj * xj for wj, xj in zip(w, xi))
        correct += (z > 0) == (yi > 0.5)
    print(f"train accuracy: {100*correct/n:.1f}%")
    print("\nlearned weights (embed in core.py _ROUTER_W):")
    print("_ROUTER_W = [" + ", ".join(f"{x:.4f}" for x in w) + "]")
    for name, wj in zip(FEATURE_NAMES, w):
        print(f"  {name:14s} {wj:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
