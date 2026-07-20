#!/usr/bin/env python
"""slicegrep benchmark v6 — END-TO-END: real LLM answers on retrieved context.

The retrieval suites measure whether the needed code lands in context. v6
measures what that buys: a real model (claude CLI, haiku) receives ONLY the
retrieved context plus the task, and must name the files that need modifying.
Graded against the files the real fix actually touched.

Per (session, strategy):
  1. retrieve context with the strategy (8k cap) for the bench3 session query
  2. prompt = task (commit subject) + context; model must output JSON
     {"files": [...]} — the repo-relative files it believes must change
  3. score: file recall = |named ∩ ground-truth files| / |ground-truth files|
     (basename match); answer correct = recall >= 0.5

Results append to a .jsonl so interrupted runs resume (already-done cells are
skipped). Model calls are real and non-deterministic; treat small deltas as
noise and the ordering as the signal.

Usage:
    python benchmarks/bench6.py --corpora-dir PATH --sessions 15 \\
        --strategies slicegrep,tfidf-vec,bm25 --out results_v6.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench2 import RepoCache, STRATEGIES, cap  # noqa: E402
from bench3 import REPOS, SEED, gt_from_hunks, mine_sessions, snapshot  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"

PROMPT = """You are helping fix an issue in the {repo} codebase.

Task (from the issue/commit): {subject}

Below is the ONLY context you have from the codebase. Based on it, name the
repo-relative file paths that most likely must be MODIFIED to complete the
task. Reply with ONLY a JSON object, no prose: {{"files": ["path1", ...]}}
List at most 4 files.

CONTEXT:
{context}
"""


_CLAUDE = shutil.which("claude") or "claude"


def ask_model(prompt: str) -> list:
    try:
        proc = subprocess.run(
            [_CLAUDE, "-p", "--model", MODEL],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
        out = proc.stdout.strip()
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return []
        return [str(f) for f in json.loads(m.group(0)).get("files", [])][:4]
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpora-dir", required=True)
    ap.add_argument("--sessions", type=int, default=15)
    ap.add_argument("--strategies", default="slicegrep,tfidf-vec,bm25")
    ap.add_argument("--out", default="benchmarks/results_v6.jsonl")
    args = ap.parse_args()

    wanted = args.strategies.split(",")
    strats = [(n, f) for n, f in STRATEGIES if n in wanted]

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

    out_path = Path(args.out)
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                done.add((d["sha"], d["strategy"]))
            except Exception:
                pass

    print(f"{len(sessions)} sessions x {len(strats)} strategies "
          f"({len(done)} cells already done)\n")

    workdir = Path(tempfile.mkdtemp(prefix="slicegrep-bench6-"))
    try:
        for idx, s in enumerate(sessions, 1):
            todo = [(n, f) for n, f in strats if (s["sha"][:10], n) not in done]
            if not todo:
                continue
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
            gt_files = {Path(sp.rel).name for sp in gt.spans}
            for sname, fn in todo:
                try:
                    ctx, _calls = fn(cache, s["query"])
                except Exception:
                    ctx = ""
                ctx = cap(ctx)
                t0 = time.perf_counter()
                named = ask_model(PROMPT.format(
                    repo=s["label"], subject=s["subject"], context=ctx))
                llm_s = time.perf_counter() - t0
                named_base = {Path(f.replace("\\", "/")).name for f in named}
                recall = (len(named_base & gt_files) / len(gt_files)) if gt_files else 0.0
                row = {
                    "sha": s["sha"][:10], "subject": s["subject"][:70],
                    "strategy": sname, "named": sorted(named_base),
                    "gt_files": sorted(gt_files),
                    "file_recall": round(recall, 3),
                    "correct": recall >= 0.5, "llm_seconds": round(llm_s, 1),
                }
                with open(out_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
                print(f"  [{idx}/{len(sessions)}] {sname:10s} "
                      f"recall={recall:.2f} {'OK' if row['correct'] else '--'} "
                      f"({llm_s:.0f}s)")
            shutil.rmtree(snap, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # summary
    rows = [json.loads(l) for l in out_path.read_text(encoding="utf-8").splitlines()]
    print("\n=== END-TO-END SUMMARY (real claude calls) ===")
    print(f"{'strategy':12s} {'n':>4s} {'correct%':>9s} {'mean file recall':>17s}")
    for sname, _ in strats:
        sub = [r for r in rows if r["strategy"] == sname]
        if not sub:
            continue
        print(f"{sname:12s} {len(sub):>4d} "
              f"{100.0 * sum(r['correct'] for r in sub) / len(sub):>8.1f}% "
              f"{100.0 * statistics.mean(r['file_recall'] for r in sub):>16.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
