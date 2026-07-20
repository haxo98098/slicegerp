# benchmark v5 — cross-language and monorepo-scale symbol lookup

Symbol-definition retrieval with language-appropriate def regexes; ground truth = definition line + 25 lines; 8k cap. Python-only baselines (jedi, ast-tfidf, repomap) are expected to collapse on TS/Rust — that is part of the result, not an error.


## zod (typescript) — 40 tasks

| strategy | tokens | hit rate | tool calls | latency/task |
|---|---|---|---|---|
| raw-rg | 67 | 0.0% | 1 | 27 ms |
| whole-file | 5,218 | 32.5% | 3 | 29 ms |
| rg+windows | 1,770 | 60.0% | 3 | 26 ms |
| rg+rank | 5,218 | 30.0% | 2 | 27 ms |
| lsp(jedi) | 0 | 0.0% | 1 | 27 ms |
| tfidf-vec | 2,129 | 52.5% | 1 | 17 ms |
| bm25 | 2,117 | 50.0% | 1 | 1 ms |
| dense-emb | 2,219 | 27.5% | 1 | 2 ms |
| ast-tfidf | 1,510 | 20.0% | 1 | 28 ms |
| repomap | 0 | 0.0% | 1 | 0 ms |
| semble | 2,082 | 32.5% | 1 | 39 ms |
| slicegrep | 2,037 | 77.5% | 1 | 1006 ms |

## serde (rust) — 40 tasks

| strategy | tokens | hit rate | tool calls | latency/task |
|---|---|---|---|---|
| raw-rg | 87 | 0.0% | 1 | 22 ms |
| whole-file | 8,000 | 12.5% | 2 | 22 ms |
| rg+windows | 2,197 | 50.0% | 3 | 20 ms |
| rg+rank | 8,000 | 17.5% | 2 | 22 ms |
| lsp(jedi) | 0 | 0.0% | 1 | 37 ms |
| tfidf-vec | 2,127 | 47.5% | 1 | 17 ms |
| bm25 | 2,198 | 45.0% | 1 | 1 ms |
| dense-emb | 2,257 | 35.0% | 1 | 2 ms |
| ast-tfidf | 2,041 | 10.0% | 1 | 12 ms |
| repomap | 0 | 0.0% | 1 | 0 ms |
| semble | 2,090 | 15.0% | 1 | 41 ms |
| slicegrep | 2,116 | 67.5% | 1 | 794 ms |

## django (python-at-scale) — 40 tasks

| strategy | tokens | hit rate | tool calls | latency/task |
|---|---|---|---|---|
| raw-rg | 189 | 0.0% | 1 | 282 ms |
| whole-file | 8,000 | 40.0% | 4 | 283 ms |
| rg+windows | 4,792 | 57.5% | 5 | 285 ms |
| rg+rank | 8,000 | 32.5% | 2 | 273 ms |
| lsp(jedi) | 25 | 17.5% | 1 | 2276 ms |
| tfidf-vec | 2,206 | 37.5% | 1 | 316 ms |
| bm25 | 2,240 | 42.5% | 1 | 1 ms |
| dense-emb | 2,271 | 37.5% | 1 | 26 ms |
| ast-tfidf | 2,240 | 25.0% | 1 | 1547 ms |
| repomap | 2,144 | 0.0% | 1 | 646 ms |
| semble | 2,085 | 22.5% | 1 | 173 ms |
| slicegrep | 2,117 | 60.0% | 1 | 14359 ms |
