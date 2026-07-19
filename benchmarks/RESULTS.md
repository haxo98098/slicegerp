# slicegrep retrieval benchmark

Corpus: **pallets/click @ 8.1.7** — 10 real code-lookup tasks. Context cap 8000 tokens/lookup; slicegrep budget 2000; window strategy ±60 lines. Search engine for baselines: **python-scan (rg not found)**.

**Task success** = the full ground-truth definition block landed inside the capped context (the necessary condition for the model to answer). Reproduce with `python benchmarks/bench.py --clone`.

## Summary (median over 10 tasks)

| strategy | tokens → model | task success | irrelevant code | tool calls | latency |
|---|---|---|---|---|---|
| whole-file | 8,000 | 20% | 100.0% | 5 | 124.7 ms |
| rg+windows | 6,047 | 60% | 99.2% | 7 | 122.9 ms |
| slicegrep | 1,990 | 90% | 93.6% | 1 | 117.0 ms |

## Per-task detail

| task | strategy | tokens | def included | irrelevant | calls | latency |
|---|---|---|---|---|---|---|
| find Context class | whole-file | 8,000 | ✅ | 96.5% | 2 | 523 ms |
| find Context class | rg+windows | 1,400 | ✅ | 80.1% | 2 | 116 ms |
| find Context class | slicegrep | 956 | ✅ | 70.9% | 1 | 108 ms |
| find Option class | whole-file | 8,000 | ❌ | 100.0% | 4 | 119 ms |
| find Option class | rg+windows | 4,094 | ✅ | 92.4% | 4 | 119 ms |
| find Option class | slicegrep | 1,631 | ✅ | 80.9% | 1 | 115 ms |
| find ParamType base | whole-file | 8,000 | ✅ | 97.2% | 2 | 118 ms |
| find ParamType base | rg+windows | 694 | ✅ | 68.0% | 2 | 120 ms |
| find ParamType base | slicegrep | 581 | ✅ | 61.8% | 1 | 111 ms |
| find echo implementation | whole-file | 8,000 | ❌ | 100.0% | 5 | 120 ms |
| find echo implementation | rg+windows | 3,912 | ✅ | 99.1% | 5 | 118 ms |
| find echo implementation | slicegrep | 2,062 | ✅ | 98.3% | 1 | 110 ms |
| find prompt implementation | whole-file | 8,000 | ❌ | 100.0% | 10 | 126 ms |
| find prompt implementation | rg+windows | 8,000 | ❌ | 99.7% | 12 | 135 ms |
| find prompt implementation | slicegrep | 1,735 | ✅ | 95.3% | 1 | 121 ms |
| find UsageError | whole-file | 8,000 | ❌ | 100.0% | 35 | 137 ms |
| find UsageError | rg+windows | 8,000 | ❌ | 100.0% | 57 | 139 ms |
| find UsageError | slicegrep | 2,020 | ✅ | 90.6% | 1 | 194 ms |
| find HelpFormatter | whole-file | 8,000 | ❌ | 99.9% | 3 | 124 ms |
| find HelpFormatter | rg+windows | 2,457 | ✅ | 93.1% | 3 | 126 ms |
| find HelpFormatter | slicegrep | 2,098 | ✅ | 91.9% | 1 | 119 ms |
| how does command decorator work | whole-file | 8,000 | ❌ | 100.0% | 6 | 117 ms |
| how does command decorator work | rg+windows | 8,000 | ✅ | 99.4% | 9 | 115 ms |
| how does command decorator work | slicegrep | 2,141 | ✅ | 97.7% | 1 | 114 ms |
| how are envvars resolved | whole-file | 8,000 | ❌ | 100.0% | 17 | 133 ms |
| how are envvars resolved | rg+windows | 8,000 | ❌ | 100.0% | 26 | 133 ms |
| how are envvars resolved | slicegrep | 2,065 | ✅ | 96.9% | 1 | 144 ms |
| how does progress bar render | whole-file | 8,000 | ❌ | 100.0% | 9 | 125 ms |
| how does progress bar render | rg+windows | 8,000 | ❌ | 100.0% | 12 | 130 ms |
| how does progress bar render | slicegrep | 1,961 | ❌ | 100.0% | 1 | 167 ms |
