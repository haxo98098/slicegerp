# slicegrep retrieval benchmark v2

227 generated tasks across pallets/click@8.1.7, pallets/flask@3.0.0, psf/requests@v2.31.0, Textualize/rich@v13.7.0 — families: symbol (40), comprehend (40), call-chain (40), bug-local (33), config-flow (34), test+impl (40). Context cap 8000 tokens; retriever budgets 2000. Seeded (SEED=20260719); reproduce with `python benchmarks/bench2.py --clone --scale 240`.

**Ground-truth context hit rate** = ALL required spans for the task landed in the capped context (multi-span families require e.g. the definition AND a cross-file call site). This measures retrieval, not end-to-end task completion.

## Summary (median over tasks)

| strategy | tokens → model | hit rate | irrelevant | tool calls | latency/task | total |
|---|---|---|---|---|---|---|
| raw-rg | 222 | 0.0% | 100.0% | 1 | 17.4 ms | 6.5 s |
| whole-file | 8,000 | 37.0% | 100.0% | 5 | 18.4 ms | 6.8 s |
| rg+windows | 5,680 | 59.9% | 98.9% | 6 | 18.7 ms | 6.6 s |
| rg+rank | 8,000 | 43.2% | 99.6% | 2 | 17.8 ms | 6.8 s |
| lsp(jedi) | 0 | 6.2% | 100.0% | 1 | 99.3 ms | 29.8 s |
| tfidf-vec | 2,192 | 57.7% | 97.8% | 1 | 20.3 ms | 6.9 s |
| bm25 | 2,195 | 65.6% | 97.3% | 1 | 0.6 ms | 1.0 s |
| dense-emb | 2,240 | 27.3% | 99.7% | 1 | 1.6 ms | 5.1 s |
| ast-tfidf | 2,289 | 63.4% | 97.9% | 1 | 68.4 ms | 22.9 s |
| repomap | 2,094 | 4.0% | 100.0% | 1 | 6.8 ms | 4.1 s |
| semble | 2,087 | 46.7% | 97.7% | 1 | 22.0 ms | 13.1 s |
| slicegrep | 2,263 | 70.0% | 97.3% | 1 | 45.5 ms | 18.5 s |

## Hit rate by task family

| strategy | symbol | comprehend | call-chain | bug-local | config-flow | test+impl |
|---|---|---|---|---|---|---|
| raw-rg | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| whole-file | 55.0% | 0.0% | 22.5% | 90.9% | 64.7% | 2.5% |
| rg+windows | 85.0% | 0.0% | 62.5% | 93.9% | 73.5% | 52.5% |
| rg+rank | 70.0% | 22.5% | 17.5% | 90.9% | 67.6% | 2.5% |
| lsp(jedi) | 32.5% | 0.0% | 0.0% | 0.0% | 2.9% | 0.0% |
| tfidf-vec | 70.0% | 52.5% | 45.0% | 66.7% | 94.1% | 25.0% |
| bm25 | 72.5% | 87.5% | 37.5% | 75.8% | 94.1% | 32.5% |
| dense-emb | 55.0% | 20.0% | 17.5% | 48.5% | 26.5% | 0.0% |
| ast-tfidf | 95.0% | 80.0% | 62.5% | 81.8% | 23.5% | 35.0% |
| repomap | 17.5% | 0.0% | 0.0% | 3.0% | 2.9% | 0.0% |
| semble | 75.0% | 37.5% | 50.0% | 42.4% | 67.6% | 10.0% |
| slicegrep | 90.0% | 70.0% | 47.5% | 93.9% | 82.4% | 42.5% |

## Notes and limitations

- **lsp(jedi)** drives jedi (the engine inside jedi-language-server) via project-wide symbol search. It only receives the first identifier-shaped token of each query — like an LSP client, it cannot search error strings or concept words.
- **tfidf-vec** is a lexical vector retriever (TF-IDF cosine over 60-line chunks): the standard lightweight stand-in for an embedding retriever. A neural embedding baseline would add a heavy model dependency; treat tfidf-vec as its floor, not its ceiling.
- Baselines share one in-memory file cache, so latency reflects matching cost, not disk IO. slicegrep walks the tree itself; its latency includes that overhead.
- Ground truth is auto-generated and span-based; families were designed before results were seen and none were removed after.
