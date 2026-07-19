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
pip install slicegrep
```

- **Zero dependencies** for the core (standard library only). Python 3.8+.
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
pip install "slicegrep[mcp]"
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
git clone https://github.com/CHANGEME/slicegrep
cd slicegrep
pip install -e ".[dev,mcp]"
pytest
```

## License

[MIT](LICENSE)
