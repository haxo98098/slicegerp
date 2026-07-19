# slicegrep retrieval benchmark

Corpus: **pallets/click@8.1.7, pallets/flask@3.0.0, psf/requests@v2.31.0, Textualize/rich@v13.7.0** — 300 real code-lookup tasks. Context cap 8000 tokens/lookup; slicegrep budget 2000; window strategy ±60 lines. Search engine for baselines: **python-scan (rg not found)**.

**Definition hit rate** = the required definition site landed inside the capped context. This measures *retrieval* quality — the necessary condition for a model to answer — not end-to-end agent task completion. Reproduce with `python benchmarks/bench.py --clone --scale 300`.

## Summary (median over 300 tasks)

| strategy | tokens → model | definition hit rate | irrelevant code | tool calls (med/mean) | latency/task | total time |
|---|---|---|---|---|---|---|
| whole-file | 8,000 | 54.3% | 99.7% | 3 / 18.3 | 163.7 ms | 55.0 s |
| rg+windows | 2,384 | 76.0% | 97.9% | 3 / 22.2 | 164.0 ms | 52.6 s |
| slicegrep | 1,586 | 84.7% | 95.9% | 1 / 1 | 186.1 ms | 68.8 s |

## Tradeoff and limitations

slicegrep trades roughly 20 ms of extra local retrieval time per lookup for a higher context hit rate, fewer tool calls, and substantially fewer input tokens — trivial beside an LLM request that takes seconds, but stated here rather than hidden.

Known limitations of this benchmark:

- Generated tasks are symbol-definition lookups, which plays to a tool with explicit definition-ranking logic. Harder task families (bug localization, cross-file call chains, config/data-flow tracing, test+implementation retrieval) are planned.
- The whole-file and window baselines concatenate results in search order and truncate at the cap, so ordering luck affects them. This models a naive agent; a smarter baseline would rank matching files first. Stronger baselines (ripgrep + heuristic ranking, LSP definition/references, a lightweight embedding retriever) are planned.

