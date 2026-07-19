# slicegrep retrieval benchmark

Corpus: **pallets/click @ 8.1.7** — 10 real code-lookup tasks. Context cap 8000 tokens/lookup; slicegrep budget 2000; window strategy ±60 lines. Search engine for baselines: **python-scan (rg not found)**.

**Definition hit rate** = the required definition site landed inside the capped context. This measures *retrieval* quality — the necessary condition for a model to answer — not end-to-end agent task completion. Reproduce with `python benchmarks/bench.py --clone`.

## Summary (median over 10 tasks)

| strategy | tokens → model | definition hit rate | irrelevant code | tool calls (med/mean) | latency/task | total time |
|---|---|---|---|---|---|---|
| whole-file | 8,000 | 20.0% | 100.0% | 5 / 9.3 | 124.0 ms | 1.6 s |
| rg+windows | 6,047 | 60.0% | 99.2% | 7 / 13.2 | 120.4 ms | 1.2 s |
| slicegrep | 1,983 | 90.0% | 93.6% | 1 / 1 | 113.6 ms | 1.3 s |

## Tradeoff and limitations

slicegrep trades roughly 20 ms of extra local retrieval time per lookup for a higher context hit rate, fewer tool calls, and substantially fewer input tokens — trivial beside an LLM request that takes seconds, but stated here rather than hidden.

Known limitations of this benchmark:

- Generated tasks are symbol-definition lookups, which plays to a tool with explicit definition-ranking logic. Harder task families (bug localization, cross-file call chains, config/data-flow tracing, test+implementation retrieval) are planned.
- The whole-file and window baselines concatenate results in search order and truncate at the cap, so ordering luck affects them. This models a naive agent; a smarter baseline would rank matching files first. Stronger baselines (ripgrep + heuristic ranking, LSP definition/references, a lightweight embedding retriever) are planned.


## Per-task detail

| task | strategy | tokens | def included | irrelevant | calls | latency |
|---|---|---|---|---|---|---|
| find Context class | whole-file | 8,000 | ✅ | 96.5% | 2 | 503 ms |
| find Context class | rg+windows | 1,400 | ✅ | 80.1% | 2 | 112 ms |
| find Context class | slicegrep | 956 | ✅ | 70.9% | 1 | 105 ms |
| find Option class | whole-file | 8,000 | ❌ | 100.0% | 4 | 113 ms |
| find Option class | rg+windows | 4,094 | ✅ | 92.4% | 4 | 119 ms |
| find Option class | slicegrep | 1,631 | ✅ | 80.9% | 1 | 107 ms |
| find ParamType base | whole-file | 8,000 | ✅ | 97.2% | 2 | 112 ms |
| find ParamType base | rg+windows | 694 | ✅ | 68.0% | 2 | 111 ms |
| find ParamType base | slicegrep | 581 | ✅ | 61.8% | 1 | 105 ms |
| find echo implementation | whole-file | 8,000 | ❌ | 100.0% | 5 | 114 ms |
| find echo implementation | rg+windows | 3,912 | ✅ | 99.1% | 5 | 118 ms |
| find echo implementation | slicegrep | 2,068 | ✅ | 98.3% | 1 | 110 ms |
| find prompt implementation | whole-file | 8,000 | ❌ | 100.0% | 10 | 126 ms |
| find prompt implementation | rg+windows | 8,000 | ❌ | 99.7% | 12 | 127 ms |
| find prompt implementation | slicegrep | 1,741 | ✅ | 95.3% | 1 | 120 ms |
| find UsageError | whole-file | 8,000 | ❌ | 100.0% | 35 | 134 ms |
| find UsageError | rg+windows | 8,000 | ❌ | 100.0% | 57 | 133 ms |
| find UsageError | slicegrep | 2,029 | ✅ | 90.6% | 1 | 213 ms |
| find HelpFormatter | whole-file | 8,000 | ❌ | 99.9% | 3 | 122 ms |
| find HelpFormatter | rg+windows | 2,457 | ✅ | 93.1% | 3 | 121 ms |
| find HelpFormatter | slicegrep | 2,101 | ✅ | 91.9% | 1 | 116 ms |
| how does command decorator work | whole-file | 8,000 | ❌ | 100.0% | 6 | 115 ms |
| how does command decorator work | rg+windows | 8,000 | ✅ | 99.4% | 9 | 115 ms |
| how does command decorator work | slicegrep | 2,150 | ✅ | 97.7% | 1 | 111 ms |
| how are envvars resolved | whole-file | 8,000 | ❌ | 100.0% | 17 | 128 ms |
| how are envvars resolved | rg+windows | 8,000 | ❌ | 100.0% | 26 | 133 ms |
| how are envvars resolved | slicegrep | 2,074 | ✅ | 96.9% | 1 | 150 ms |
| how does progress bar render | whole-file | 8,000 | ❌ | 100.0% | 9 | 128 ms |
| how does progress bar render | rg+windows | 8,000 | ❌ | 100.0% | 12 | 125 ms |
| how does progress bar render | slicegrep | 1,937 | ❌ | 100.0% | 1 | 127 ms |
