# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project adheres to
[Semantic Versioning](https://semver.org/).

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
