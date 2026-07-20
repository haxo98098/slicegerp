# slicegrep retrieval benchmark v3 — real sessions from git history

80 real changes mined from the corpora's own git history (1-3 .py files, 5-80 changed lines, informative message; merges/bumps/typo commits filtered). For each: the repo is reconstructed at the parent commit, the query is built ONLY from the commit message, and ground truth is the pre-image regions the real fix touched (diff hunks ±2 lines).

**Session hit** = at least half of the regions the real fix changed landed in the retrieved context (coverage ≥ 0.5) under an 8k-token cap. **Coverage** = mean fraction of changed regions retrieved. Retrieval quality, not end-to-end task completion.

| strategy | tokens → model | session hit | mean coverage | tool calls | latency/task |
|---|---|---|---|---|---|
| raw-rg | 5,535 | 0.0% | 0.0% | 1 | 33.3 ms |
| whole-file | 8,000 | 6.2% | 6.8% | 37 | 32.7 ms |
| rg+windows | 8,000 | 7.5% | 8.8% | 47 | 31.5 ms |
| rg+rank | 8,000 | 20.0% | 20.8% | 2 | 32.4 ms |
| lsp(jedi) | 0 | 2.5% | 1.2% | 1 | 108.3 ms |
| tfidf-vec | 2,240 | 22.5% | 20.4% | 1 | 72.1 ms |
| semble | 2,090 | 20.0% | 17.6% | 1 | 956.3 ms |
| slicegrep | 2,104 | 21.2% | 22.6% | 1 | 341.8 ms |

## Notes

- Queries come from commit messages, never from diffs — the message is what a developer/agent knows *before* finding the code. Messages vary in quality; that variance is part of the task, all strategies face the same messages.
- Coverage rewards finding the sites the real author changed. A strategy could retrieve genuinely useful context that the fix didn't touch and get no credit; this is a floor on usefulness, not a ceiling.
- Sessions are seeded and the mining filter is fixed; reproduce with full clones and `python benchmarks/bench3.py --corpora-dir <dir>`.
