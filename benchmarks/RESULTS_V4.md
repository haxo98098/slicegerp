# benchmark v4 — multi-turn retrieval (2 rounds) on real sessions

60 sessions from bench3's miner. Every strategy gets the same mechanical refinement: round-1 retrieve, extract the 4 most frequent unseen identifiers from the capped round-1 context, re-retrieve with them appended, union capped at 8k tokens.

| strategy | tokens | session hit | mean coverage | tool calls | latency |
|---|---|---|---|---|---|
| raw-rg | 8,000 | 0.0% | 0.0% | 2 | 108.7 ms |
| whole-file | 8,000 | 10.0% | 8.9% | 87 | 118.8 ms |
| rg+windows | 8,000 | 10.0% | 10.3% | 103 | 125.5 ms |
| rg+rank | 8,000 | 23.3% | 22.4% | 5 | 125.9 ms |
| lsp(jedi) | 0 | 3.3% | 1.7% | 1 | 126.7 ms |
| tfidf-vec | 4,521 | 20.0% | 22.1% | 2 | 111.2 ms |
| bm25 | 4,492 | 26.7% | 24.3% | 2 | 151.8 ms |
| dense-emb | 4,475 | 26.7% | 23.8% | 2 | 481.0 ms |
| ast-tfidf | 4,581 | 28.3% | 23.9% | 2 | 382.0 ms |
| repomap | 4,192 | 5.0% | 4.7% | 2 | 230.2 ms |
| semble | 4,189 | 20.0% | 20.3% | 2 | 1378.5 ms |
| slicegrep | 4,214 | 28.3% | 27.3% | 2 | 1953.2 ms |
