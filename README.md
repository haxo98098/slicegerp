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

### Benchmark

**300 generated code-lookup tasks** across four real codebases
([click](https://github.com/pallets/click) 8.1.7, [flask](https://github.com/pallets/flask) 3.0.0,
[requests](https://github.com/psf/requests) 2.31.0, [rich](https://github.com/Textualize/rich) 13.7.0),
three retrieval strategies, 8k-token context cap per lookup, seeded sampling so the
run is reproducible.

> Across 300 reproducible lookups in four real Python projects, slicegrep delivered
> the target definition **84.7%** of the time using a median **1,586 tokens** —
> 8.7 points above grep+windows and 30.4 points above capped whole-file retrieval.

**Definition hit rate** = the required definition landed inside the delivered
context. This measures *retrieval* quality — the necessary condition for the model
to answer — not end-to-end agent task completion. Median over tasks:

| strategy | tokens → model | definition hit rate | irrelevant code | tool calls | total time |
|---|---|---|---|---|---|
| whole-file reads | 8,000 | 54.3% | 99.7% | 3 | 55.0 s |
| grep + window reads | 2,384 | 76.0% | 97.9% | 3 | 52.6 s |
| **slicegrep** | **1,586** | **84.7%** | **95.9%** | **1** | 68.8 s |

slicegrep trades roughly 20 ms of extra local retrieval time per lookup for a higher
context hit rate, fewer tool calls, and substantially fewer input tokens — trivial
beside an LLM request that takes seconds. On the 10 curated click tasks the gap is
wider still (90% vs 20/60%, [RESULTS.md](benchmarks/RESULTS.md)). Full scaled
report, including limitations: [RESULTS_SCALED.md](benchmarks/RESULTS_SCALED.md).
Reproduce either:

```bash
python benchmarks/bench.py --clone                # 10 curated tasks
python benchmarks/bench.py --clone --scale 300    # 300 generated tasks
```

Honesty note: this benchmark caught a real ranking bug at v0.1 (the definition
signal never fired in default mode — hit rate was 71.7% before the fix; see
CHANGELOG). A benchmark that never embarrasses its own tool isn't measuring
anything.

### Benchmark v2 — harder tasks, stronger baselines

240 tasks across six families (symbol lookup, docstring-concept comprehension,
cross-file call-chain, bug localization from error strings, config/data-flow,
test+implementation) against seven strategies, including a file-ranking grep agent,
a language-server baseline (jedi), and a TF-IDF vector retriever. Multi-span
families require *all* spans (e.g. definition AND cross-file call site) in context.

| strategy | tokens → model | hit rate | tool calls |
|---|---|---|---|
| raw ripgrep output | 271 | 0.0% | 1 |
| whole-file reads | 8,000 | 36.6% | 6 |
| grep + window reads | 6,345 | 57.3% | 7 |
| grep + file ranking | 8,000 | 43.2% | 2 |
| lsp (jedi symbol search) | 0 | 6.6% | 1 |
| tf-idf vector retriever | 2,209 | 56.8% | 1 |
| **slicegrep 0.2** | **2,015** | **63.9%** | **1** |

The v2 benchmark drove the v0.2 release: retrieval objectives (guaranteed
definition + cross-file caller + test slots in the budget), diversity-aware
packing, and a TF-IDF semantic rerank blended into lexical scoring. Measured
effect: overall 60.8% → 63.9%, test+impl 30.0% → 47.5%, symbol 80.0% → 85.0%.

Still not everywhere, and [RESULTS_V2.md](benchmarks/RESULTS_V2.md) says so
plainly: grep+windows keeps cross-file call-chain (57.5% vs 40.0%) because giant
windows are more likely to capture *two* required spans, and the TF-IDF retriever
keeps docstring-concept queries (75.0% vs 60.0%). Those remain the open gaps. LSP
is strong only on pure symbol lookups and structurally blind to string/concept
queries; raw grep output alone almost never contains the definition.

```bash
pip install jedi   # for the lsp baseline
python benchmarks/bench2.py --clone --scale 240
```

### Benchmark v3 — real sessions from git history (slicegrep loses this one)

v3 removes the "synthetic tasks favor your tool" objection entirely: 80 real
changes mined from the corpora's own git history. The repo is reconstructed at
the parent commit, the query comes only from the commit message (what you'd know
*before* finding the code), and ground truth is the exact regions the real fix
touched. **Session hit** = at least half of those regions retrieved under the cap.

| strategy | tokens → model | session hit | mean coverage | tool calls |
|---|---|---|---|---|
| raw ripgrep output | 5,535 | 0.0% | 0.0% | 1 |
| whole-file reads | 8,000 | 6.2% | 6.8% | 37 |
| grep + window reads | 8,000 | 7.5% | 8.8% | 47 |
| grep + file ranking | 8,000 | 20.0% | 20.8% | 2 |
| lsp (jedi) | 0 | 2.5% | 1.2% | 1 |
| **tf-idf vector retriever** | 2,240 | **22.5%** | 20.4% | 1 |
| slicegrep 0.2 | 2,115 | 16.2% | 18.8% | 1 |

slicegrep does **not** win here: the TF-IDF retriever (22.5%) and the
file-ranking grep agent (20.0%) beat it (16.2%). The likely mechanism: commit
messages are vague, and slicegrep requires a literal regex hit for a region to
even be a *candidate* — regions the fix touched that don't contain any query
word are invisible to it, while vocabulary-similarity retrieval can still land
nearby. Semantic *ranking* (added in 0.2) isn't enough; real sessions need
semantic *recall*. That's the top of the roadmap, and these numbers stay
published until it's beaten fairly.

Everyone's numbers collapse versus the lookup benchmarks (best: 22.5% vs 63.9%)
— retrieving what a real fix will touch, from a commit message alone, is simply
a much harder problem. Reproduce: full clones + `python benchmarks/bench3.py
--corpora-dir <dir>`.

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
