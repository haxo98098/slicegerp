# slicegrep retrieval benchmark v3 — real sessions from git history

80 real changes mined from the corpora's own git history (1-3 .py files, 5-80 changed lines, informative message; merges/bumps/typo commits filtered). For each: the repo is reconstructed at the parent commit, the query is built ONLY from the commit message, and ground truth is the pre-image regions the real fix touched (diff hunks ±2 lines).

**Session hit** = at least half of the regions the real fix changed landed in the retrieved context (coverage ≥ 0.5) under an 8k-token cap. **Coverage** = mean fraction of changed regions retrieved. Retrieval quality, not end-to-end task completion.

| strategy | tokens → model | session hit | mean coverage | tool calls | latency/task |
|---|---|---|---|---|---|
| raw-rg | 6,301 | 0.0% | 0.0% | 1 | 33.1 ms |
| whole-file | 8,000 | 5.0% | 4.5% | 34 | 32.2 ms |
| rg+windows | 8,000 | 8.8% | 7.2% | 45 | 32.4 ms |
| rg+rank | 8,000 | 30.0% | 27.1% | 3 | 30.0 ms |
| lsp(jedi) | 0 | 0.0% | 0.0% | 1 | 147.1 ms |
| tfidf-vec | 2,253 | 22.5% | 21.9% | 1 | 73.8 ms |
| bm25 | 2,231 | 27.5% | 25.5% | 1 | 116.2 ms |
| dense-emb | 2,252 | 25.0% | 23.3% | 1 | 384.3 ms |
| ast-tfidf | 2,291 | 22.5% | 19.9% | 1 | 250.8 ms |
| repomap | 2,106 | 3.8% | 3.8% | 1 | 149.9 ms |
| semble | 2,077 | 13.8% | 15.5% | 1 | 1183.8 ms |
| slicegrep | 2,239 | 22.5% | 20.9% | 1 | 492.1 ms |

## Notes

- Queries come from commit messages, never from diffs — the message is what a developer/agent knows *before* finding the code. Messages vary in quality; that variance is part of the task, all strategies face the same messages.
- Coverage rewards finding the sites the real author changed. A strategy could retrieve genuinely useful context that the fix didn't touch and get no credit; this is a floor on usefulness, not a ceiling.
- Sessions are seeded and the mining filter is fixed; reproduce with full clones and `python benchmarks/bench3.py --corpora-dir <dir>`.
