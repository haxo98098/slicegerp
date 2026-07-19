# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-07-19

### Added
- **Retrieval objectives** (`objective=` / `--objective`): `auto` (default)
  reserves guaranteed budget slots for the best definition chunk, the best
  cross-file usage chunk, and the best test-file chunk when candidates exist;
  `def+caller` / `def+test` reserve just that pair; `single` restores v0.1
  score-order packing.
- **Diversity-aware budget packing**: chunks from already-represented files
  take a score discount during greedy fill, so one hot file cannot consume
  the whole budget and related code from different files is selected together.
- **Semantic rerank**: a TF-IDF cosine stage (computed over the candidate
  set, zero dependencies) blends into lexical scores, improving ranking for
  concept-style queries.

- Benchmark v2 (`benchmarks/bench2.py`): six task families (symbol,
  docstring-concept comprehension, cross-file call-chain, bug localization,
  config/data-flow, test+impl) × seven strategies (raw rg, whole-file,
  rg+windows, rg+file-ranking, jedi/LSP symbol search, TF-IDF vector
  retriever, slicegrep). 240 seeded tasks; multi-span ground truth. slicegrep
  leads overall (60.8% hit rate at 1,995 median tokens, 1 call) but
  rg+windows wins multi-span families and TF-IDF wins concept queries —
  documented as the v0.2 roadmap. `bench` extra installs jedi.
- Scaled benchmark mode (`--scale N`): generates up to N seeded, reproducible
  lookup tasks across four pinned corpora (click, flask, requests, rich) and
  measures tokens, success, irrelevance, tool calls, and latency per strategy.

### Fixed
- **Ranking bug found by the scaled benchmark:** the `definition` scoring
  signal only fired in `--boundary fn` mode (it depended on `chunk.symbol`,
  which the default mode never sets), so usage-heavy chunks could crowd the
  actual definition out of the token budget. Definition lines are now detected
  directly (pattern match on a `def`/`class`/`fn`/... line, +25), and the best
  definition chunk is guaranteed a slot when packing the budget.
  300-task success rate: 71.7% → 84.7%.

### Measured (benchmark v2, 240 tasks)
- Overall ground-truth hit rate 60.8% -> 63.9% (best baseline: 57.3%).
  test+impl 30.0% -> 47.5%; symbol 80.0% -> 85.0%; call-chain 37.5% -> 40.0%
  (rg+windows still leads that family at 57.5%); comprehend 65.0% -> 60.0%
  (small regression, TF-IDF retriever still leads at 75.0%). Recorded
  honestly; call-chain and concept ranking remain the open gaps.


## [0.1.0] - 2026-07-19

### Added
- `focused_read()` core engine: grep → slice → rank → dedupe → token-budget →
  negative evidence, standard-library only.
- Ranking signals: co-occurrence, all-patterns, rare terms, definition-vs-usage,
  with test/vendor/comment demotion.
- Near-duplicate de-duplication (Jaccard over line sets) and a token-budget cap
  that truncates rather than returning zero chunks.
- Negative evidence: reports patterns/symbols not found, and distinguishes
  "absent from the file" from "fell outside the budgeted chunks".
- Language-aware `--boundary fn` mode (Python indentation + brace languages).
- CLI: `slicegrep` and `fr`, with `--budget`, `--boundary`, `--recursive`,
  `--no-dedupe`, and `--json`.
- MCP server (`slicegrep-mcp`) exposing `focused_read` to any MCP client.
