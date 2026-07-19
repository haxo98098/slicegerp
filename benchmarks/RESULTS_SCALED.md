# slicegrep retrieval benchmark

Corpus: **pallets/click@8.1.7, pallets/flask@3.0.0, psf/requests@v2.31.0, Textualize/rich@v13.7.0** — 300 real code-lookup tasks. Context cap 8000 tokens/lookup; slicegrep budget 2000; window strategy ±60 lines. Search engine for baselines: **python-scan (rg not found)**.

**Task success** = the required definition site landed inside the capped context (the necessary condition for the model to answer). Reproduce with `python benchmarks/bench.py --clone --scale 300`.

## Summary (median over 300 tasks)

| strategy | tokens → model | task success | irrelevant code | tool calls (med/mean) | latency/task | total time |
|---|---|---|---|---|---|---|
| whole-file | 8,000 | 54.3% | 99.7% | 3 / 18.3 | 163.7 ms | 55.0 s |
| rg+windows | 2,384 | 76.0% | 97.9% | 3 / 22.2 | 164.0 ms | 52.6 s |
| slicegrep | 1,586 | 84.7% | 95.9% | 1 / 1 | 186.1 ms | 68.8 s |
