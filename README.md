# slicegrep

**grep that returns ranked, token-budgeted code slices — built for LLMs and coding agents.**

Plain `grep` gives you matching lines with no context. "Read the whole file" gives
you context but burns thousands of tokens on code the model doesn't need. `slicegrep`
sits in between: it greps a file or directory, extracts only the **relevant slices**,
**ranks** them, **dedupes** near-duplicates, caps the total to a **token budget**, and
tells you what it did **not** find.

That's the grep-then-read loop an LLM agent runs dozens of times per task — collapsed
into one call that returns a fraction of the tokens.

```bash
pip install git+https://github.com/haxo98098/slicegerp
```

- **Zero dependencies** for the core (standard library only). Python 3.8+ (the
  optional MCP server needs 3.10+).
- **CLI + library + MCP server.** Use it from a shell, import it, or plug it into
  Claude Desktop / Claude Code / Cursor / Windsurf over the Model Context Protocol.
- **Regex or natural language.** `"def score|budget"` works; so does
  `"how does budget packing guarantee definitions"` — phrases with 3+ content
  words expand automatically (subword + stemmed matching closes the
  vocabulary gap on vague queries).

---

## Why

An LLM reading code doesn't want the file — it wants the *five slices that matter*,
ordered by relevance, small enough to fit its context. `slicegrep` is that primitive:

| | plain `grep` | read whole files | **slicegrep** |
|---|---|---|---|
| Context around matches | ✗ (lines only) | ✓ (all of it) | ✓ (just enough) |
| Ranked by relevance | ✗ | ✗ | ✓ |
| Near-duplicates collapsed | ✗ | ✗ | ✓ |
| Fits a token budget | ✗ | ✗ | ✓ |
| Tells you what's absent | ✗ | ✗ | ✓ (negative evidence) |

### The point, in tokens

Reading one 660-line source file to answer "how does scoring and dedup work?":

```
whole file  : ~6600 tokens
slicegrep   :  ~375 tokens   →  94% fewer tokens, only the slices that matter
```

```bash
slicegrep src/core.py "class Scorer|def score|dedupe|rare" --budget 600
```

Multiply that by every file an agent reads per task.

## Benchmarks

Three suites, all seeded and reproducible from this repo. Scores measure
retrieval quality (the required code landing in the delivered context under an
8k-token cap), not end-to-end task completion.

### v3: retrieval for real changes (primary)

80 commits mined from the git history of click, flask, requests, and rich.
Setup per task: repo reconstructed at the parent commit; query built from the
commit message only; ground truth = the pre-image regions the commit modified
(diff hunks ±2 lines). Session hit = ≥50% of those regions retrieved.

| strategy | tokens → model | session hit | mean coverage | tool calls |
|---|---|---|---|---|
| raw ripgrep output | 5,535 | 0.0% | 0.0% | 1 |
| whole-file reads | 8,000 | 6.2% | 6.8% | 37 |
| grep + window reads | 8,000 | 7.5% | 8.8% | 47 |
| grep + file ranking | 8,000 | 20.0% | 20.8% | 2 |
| lsp (jedi) | 0 | 2.5% | 1.2% | 1 |
| tf-idf vector retriever | 2,240 | 22.5% | 20.4% | 1 |
| semble (embeddings+BM25) | 2,090 | 20.0% | 17.6% | 1 |
| **slicegrep 0.4** | 2,110 | **23.8%** | **23.8%** | 1 |

Notes:

- Low absolute scores are inherent to the task: predicting the regions a fix
  will touch from the commit message alone. No tested method exceeds 23.8%.
- Margins over tf-idf are a few points on an 80-session sample.
- Earlier slicegrep versions scored 16.2% (0.2) and 21.2% (0.3) here; the
  changes between versions were driven by these results. See CHANGELOG.
- Reproduce: full clones + `python benchmarks/bench3.py --corpora-dir <dir>`.

### v2: controlled retrieval suite

240 seeded tasks, six families (symbol lookup, docstring-concept queries,
cross-file call-chain, bug localization from error strings, config/data-flow,
test+implementation). Multi-span families require all spans in context.

| strategy | tokens → model | hit rate | tool calls |
|---|---|---|---|
| raw ripgrep output | 271 | 0.0% | 1 |
| whole-file reads | 8,000 | 36.6% | 6 |
| grep + window reads | 6,345 | 57.3% | 7 |
| grep + file ranking | 8,000 | 43.2% | 2 |
| lsp (jedi symbol search) | 0 | 6.6% | 1 |
| tf-idf vector retriever | 2,209 | 56.8% | 1 |
| semble (embeddings+BM25) | 2,085 | 38.3% | 1 |
| **slicegrep 0.4** | 2,089 | **62.6%** | 1 |

Notes:

- Per-family results in [RESULTS_V2.md](benchmarks/RESULTS_V2.md). slicegrep
  loses two families: cross-file call-chain (grep+windows 57.5% vs 37.5%) and
  docstring-concept queries (tf-idf 75.0% vs 62.5%).
- semble caveats, both in its favor: these queries are keyword-shaped (semble
  targets natural-language queries), and its test-file down-ranking floors the
  test+impl family (2.5%) that this suite rewards.
- slicegrep 0.3 scored 63.9% here; 0.4 traded 1.3 points for the v3 gains.
- Reproduce: `pip install jedi && python benchmarks/bench2.py --clone --scale 240`.

### v1: definition lookups

300 generated definition lookups, three strategies. slicegrep 84.7% hit at
1,586 median tokens / 1 call, vs grep+windows 76.0% (2,384 / 3) and whole-file
54.3% (8,000 / 3). This suite exposed a v0.1 ranking bug (definition signal
inactive in default mode; 71.7% before the fix). Details:
[RESULTS_SCALED.md](benchmarks/RESULTS_SCALED.md); reproduce with
`python benchmarks/bench.py --clone --scale 300`.

---

## Quick start

```bash
# find a function
slicegrep src/app.py "def handle_request"

# whole enclosing blocks, searched recursively, under a token budget
slicegrep src/ "Scorer|def score" --boundary fn --budget 800

# co-occurring concepts — a chunk matching more of them ranks higher
slicegrep . "retry|timeout|backoff" --budget 1500

# raw JSON for tooling
slicegrep src/ "TODO" 2 2 --json
```

`fr` is installed as a shorter alias for `slicegrep` (focused read).

### As a library

```python
from slicegrep import focused_read

result = focused_read("src/", "class Scorer|def score", budget=800, boundary="fn")

print(result.render())          # ranked text report (what an LLM reads)
print(result.total_tokens)      # e.g. 612
for chunk in result.chunks:
    print(chunk.file, chunk.line_start, chunk.score, chunk.rank_reason)

data = result.to_dict()         # structured output for your own pipeline
```

---

## MCP server

Expose `focused_read` to any MCP client so the model can pull ranked code context
on its own:

```bash
pip install "slicegrep[mcp] @ git+https://github.com/haxo98098/slicegerp"
```

**Claude Desktop / Claude Code** — add to your MCP config:

```json
{
  "mcpServers": {
    "slicegrep": {
      "command": "slicegrep-mcp"
    }
  }
}
```

Or, with Claude Code's CLI:

```bash
claude mcp add slicegrep -- slicegrep-mcp
```

The model then calls a `focused_read` tool with `path`, `pattern`, and an optional
`budget` / `boundary`, and gets back the same ranked, budget-capped report — instead
of reading whole files into its context window.

---

## How the ranking works

Every candidate slice is scored, then the list is sorted, deduped, and trimmed to the
budget. Signals that **raise** a chunk's score:

- **co_occurrence / all_patterns** — the slice matches several of your `|` patterns.
- **rare_terms** — it contains distinctive identifiers, not just boilerplate.
- **definition** — the match is where a symbol is *defined*, not just used.
- **multi_match** — several hits in the same slice.

Signals that **lower** it: `declaration_only`, `test_demoted` (unless you searched for
tests), `vendor_demoted` (generated/vendored paths), `mostly_comments`.

### Negative evidence

An empty result is a real answer. `slicegrep` reports it explicitly, and distinguishes
"the pattern isn't in the file" from "it's there but fell outside the budgeted chunks":

```
NEGATIVE EVIDENCE:
  - No definition found for 'Scorer' in src/
  - Pattern 'deprecated_api' not found in src/
```

---

## CLI reference

```
slicegrep <path> <pattern> [before] [after] [options]

  <path>       file OR directory (a directory implies a recursive walk)
  <pattern>    case-insensitive regex; join alternatives with '|'
  before after context lines each side of a match (default 40 40)

options:
  --budget N        keep only the highest-ranked chunks fitting ~N tokens
  --boundary MODE   auto (fixed window) | fn (snap to enclosing function/class) | none
  --recursive, -r   force a directory walk even for a file path
  --no-dedupe       keep near-duplicate chunks (exact dups still collapse)
  --json            print raw JSON instead of the rendered report
  --version
```

Exit code is `0` when at least one chunk matched, `1` when nothing did — so shell
scripts and CI can branch on it.

---

## Development

```bash
git clone https://github.com/haxo98098/slicegerp
cd slicegrep
pip install -e ".[dev,mcp]"
pytest
```

## License

[MIT](LICENSE)
