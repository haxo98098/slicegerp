# slicegrep retrieval benchmark v2

227 generated tasks across pallets/click@8.1.7, pallets/flask@3.0.0, psf/requests@v2.31.0, Textualize/rich@v13.7.0 — families: symbol (40), comprehend (40), call-chain (40), bug-local (33), config-flow (34), test+impl (40). Context cap 8000 tokens; retriever budgets 2000. Seeded (SEED=20260719); reproduce with `python benchmarks/bench2.py --clone --scale 240`.

**Ground-truth context hit rate** = ALL required spans for the task landed in the capped context (multi-span families require e.g. the definition AND a cross-file call site). This measures retrieval, not end-to-end task completion.

## Summary (median over tasks)

| strategy | tokens → model | hit rate | irrelevant | tool calls | latency/task | total |
|---|---|---|---|---|---|---|
| raw-rg | 271 | 0.0% | 100.0% | 1 | 17.0 ms | 6.5 s |
| whole-file | 8,000 | 36.6% | 100.0% | 6 | 16.2 ms | 6.4 s |
| rg+windows | 6,345 | 57.3% | 99.0% | 7 | 16.3 ms | 6.5 s |
| rg+rank | 8,000 | 43.2% | 99.7% | 2 | 15.4 ms | 6.4 s |
| lsp(jedi) | 0 | 6.6% | 100.0% | 1 | 94.3 ms | 27.1 s |
| tfidf-vec | 2,209 | 56.8% | 98.0% | 1 | 19.5 ms | 6.6 s |
| slicegrep | 2,024 | 63.9% | 97.2% | 1 | 312.4 ms | 109.4 s |

## Hit rate by task family

| strategy | symbol | comprehend | call-chain | bug-local | config-flow | test+impl |
|---|---|---|---|---|---|---|
| raw-rg | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| whole-file | 57.5% | 0.0% | 15.0% | 90.9% | 55.9% | 12.5% |
| rg+windows | 77.5% | 2.5% | 57.5% | 93.9% | 73.5% | 47.5% |
| rg+rank | 65.0% | 15.0% | 22.5% | 90.9% | 58.8% | 17.5% |
| lsp(jedi) | 32.5% | 2.5% | 0.0% | 0.0% | 2.9% | 0.0% |
| tfidf-vec | 57.5% | 75.0% | 37.5% | 66.7% | 79.4% | 30.0% |
| slicegrep | 82.5% | 67.5% | 35.0% | 84.8% | 76.5% | 42.5% |

## Notes and limitations

- **lsp(jedi)** drives jedi (the engine inside jedi-language-server) via project-wide symbol search. It only receives the first identifier-shaped token of each query — like an LSP client, it cannot search error strings or concept words.
- **tfidf-vec** is a lexical vector retriever (TF-IDF cosine over 60-line chunks): the standard lightweight stand-in for an embedding retriever. A neural embedding baseline would add a heavy model dependency; treat tfidf-vec as its floor, not its ceiling.
- Baselines share one in-memory file cache, so latency reflects matching cost, not disk IO. slicegrep walks the tree itself; its latency includes that overhead.
- Ground truth is auto-generated and span-based; families were designed before results were seen and none were removed after.
