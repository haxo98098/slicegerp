# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
