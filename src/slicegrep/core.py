"""slicegrep core engine — token-frugal grep-and-extract for code.

`focused_read()` greps a file or directory for a pattern, extracts only the
surrounding slices, **ranks** them (co-occurrence, rare terms, definition vs.
usage), **dedupes** near-identical chunks, caps the total to a **token budget**,
and reports **negative evidence** (patterns/symbols it did *not* find).

That is the workflow you normally simulate with grep-then-read — but in one call
and a fraction of the tokens, which is exactly what an LLM coding agent wants
when it reads a codebase.

Everything here is standard-library only.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = [
    "Chunk",
    "Result",
    "focused_read",
    "SKIP_DIRS",
    "SEARCHABLE_EXTS",
]

# Directories that are almost never what you want to read: VCS metadata, build
# output, dependency caches, editor state.
SKIP_DIRS = {
    ".git", ".svn", ".hg", ".idea", ".vs", ".vscode",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "build", "dist", "bin", "obj", "out", "target",
    "venv", ".venv", "env", ".tox", ".eggs", "site-packages",
}

# Extensions searched during a recursive walk.
SEARCHABLE_EXTS = {
    ".py", ".pyi", ".pyw",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx", ".hh", ".inl",
    ".cs", ".java", ".kt", ".kts", ".scala", ".swift", ".m", ".mm",
    ".go", ".rs", ".rb", ".php", ".lua", ".dart", ".ex", ".exs",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".sql", ".r", ".jl", ".hs", ".ml", ".clj", ".vim",
    ".txt", ".md", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".xml", ".html", ".css", ".scss", ".less",
}

_MAX_FILE_BYTES = 1_000_000


# --------------------------------------------------------------------------- #
# Token estimation
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 chars/token)."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Language-aware block boundaries
# --------------------------------------------------------------------------- #

_LANG_PATTERNS = {
    "c_like": {
        "open": re.compile(
            r"^\s*(?:(?:static|virtual|const|inline|unsigned|signed|extern|template"
            r"|public|private|protected|friend|explicit|constexpr|noexcept"
            r"|decltype|auto|void|int|char|bool|float|double|size_t|uint\w+|int\w+"
            r"|class|struct|enum|union|namespace)\s+)*"
            r"(?:class|struct|enum|union|namespace|if|else|for|while|do|switch|case"
            r"|try|catch|finally|with|def|fn|func|function|proc|method|impl|trait"
            r"|interface|type|module|package)\b"
        ),
        "close": re.compile(r"^\s*[\}\)]"),
    },
    "python": {
        "open": re.compile(
            r"^(?:class|def|async\s+def|if|elif|else|for|while|try|except|finally"
            r"|with|match|case)\b"
        ),
        "close": re.compile(r"^\S"),
    },
    "brace": {
        "open": re.compile(r"\{"),
        "close": re.compile(r"\}"),
    },
}

_EXT_LANG = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".c": "c_like", ".h": "c_like", ".cpp": "c_like", ".hpp": "c_like",
    ".cc": "c_like", ".cxx": "c_like", ".hxx": "c_like", ".hh": "c_like",
    ".cs": "c_like", ".java": "c_like", ".kt": "c_like", ".scala": "c_like",
    ".go": "c_like", ".rs": "c_like", ".swift": "c_like", ".m": "c_like",
    ".mm": "c_like", ".inl": "c_like",
    ".js": "c_like", ".ts": "c_like", ".jsx": "c_like", ".tsx": "c_like",
    ".php": "c_like", ".rb": "c_like", ".lua": "c_like", ".dart": "c_like",
}


def _detect_lang(filepath: str) -> str:
    return _EXT_LANG.get(Path(filepath).suffix.lower(), "brace")


# --------------------------------------------------------------------------- #
# Pattern compilation
# --------------------------------------------------------------------------- #

def _split_pattern_top_level(pattern: str) -> List[str]:
    """Split a query on ``|`` ONLY at the top level of the regex.

    A grouped pattern like ``(retry|backoff)_delay`` is ONE pattern — a naive
    ``str.split('|')`` would shear it into ``(retry`` and ``backoff)_delay``,
    two broken fragments that crash compilation. Escapes and nesting inside
    parens/brackets are respected; a ``|`` inside them never splits.
    """
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            buf.append(pattern[i:i + 2])
            i += 2
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth = max(0, depth - 1)
        if c == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _compile_or_escape(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern:
    """Compile a fragment; an invalid regex degrades to a literal search rather
    than killing the whole query."""
    try:
        return re.compile(pattern, flags)
    except re.error:
        return re.compile(re.escape(pattern), flags)


def _compile_multi_pattern(patterns: List[str], flags: int = re.IGNORECASE) -> re.Pattern:
    """Join pattern fragments into a single alternation so a line can be tested
    against all of them in one pass. One bad fragment must not kill the query."""
    if not patterns:
        return re.compile(r"(?!)", flags)  # matches nothing
    parts = [f"(?:{p})" for p in patterns]
    try:
        return re.compile("|".join(parts), flags)
    except re.error:
        parts = [f"(?:{_compile_or_escape(p, flags).pattern})" for p in patterns]
        return re.compile("|".join(parts), flags)


_PY_HEADER_RE = re.compile(r"^(\s*)(?:async\s+def\s|def\s|class\s)")


def _extract_symbol(line: str) -> Optional[str]:
    m = re.search(
        r"(?:class|struct|enum|def|fn|func|function|proc|method|impl|trait|interface"
        r"|void|int|char|bool|auto|static|virtual|const)\s+(\w+)",
        line,
    )
    if m:
        return m.group(1)
    m = re.search(r"\b(\w+)\s*\(", line)
    if m:
        return m.group(1)
    return None


def _python_enclosing_block(lines: List[str], target: int) -> Optional[Tuple[int, int, Optional[str]]]:
    """Return ``(start, end, symbol)`` for the innermost def/class enclosing
    ``target``, or ``None`` if the match sits inside no def/class. ``end`` is
    exclusive and spans the whole block body by indentation."""
    m0 = _PY_HEADER_RE.match(lines[target])
    if m0:
        header = target
        header_indent = len(m0.group(1))
    else:
        target_indent = None
        for k in range(target, -1, -1):
            if lines[k].strip():
                target_indent = len(lines[k]) - len(lines[k].lstrip())
                break
        if target_indent is None:
            return None
        header = None
        header_indent = 0
        for i in range(target - 1, -1, -1):
            m = _PY_HEADER_RE.match(lines[i])
            if m and len(m.group(1)) < target_indent:
                header = i
                header_indent = len(m.group(1))
                break
        if header is None:
            return None

    end = len(lines)
    for j in range(header + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[j]) - len(lines[j].lstrip())
        if indent <= header_indent:
            end = j
            break
    return (header, end, _extract_symbol(lines[header]))


def _find_enclosing_boundary(lines: List[str], target: int, lang: str) -> Tuple[int, int, Optional[str]]:
    if lang == "python":
        block = _python_enclosing_block(lines, target)
        if block is not None:
            return block
        return (max(0, target - 20), min(len(lines), target + 21), None)

    patterns = _LANG_PATTERNS.get(lang, _LANG_PATTERNS["brace"])
    depth = 0
    for i in range(target, -1, -1):
        line = lines[i]
        opens = len(patterns["open"].findall(line))
        closes = len(patterns["close"].findall(line))
        depth += closes - opens
        if depth <= 0 and patterns["open"].search(line):
            end = i
            d = 0
            for j in range(i, min(len(lines), i + 500)):
                d += len(_LANG_PATTERNS["brace"]["open"].findall(lines[j]))
                d -= len(_LANG_PATTERNS["brace"]["close"].findall(lines[j]))
                if d <= 0 and j > i:
                    end = j
                    break
            return (i, min(end, target + 100), _extract_symbol(line))
    return (max(0, target - 20), min(len(lines), target + 21), None)


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

_COMMON_TERMS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "then",
    "return", "void", "int", "char", "bool", "float", "double", "auto",
    "class", "struct", "enum", "public", "private", "protected", "static",
    "virtual", "const", "inline", "true", "false", "null", "nullptr",
    "this", "self", "new", "delete", "using", "namespace", "import",
    "export", "def", "if", "else", "for", "while", "and", "or", "not",
    "none", "pass", "try", "except", "finally", "with", "lambda", "yield",
    "async", "await",
})


def _rarity(term: str) -> float:
    t = term.lower()
    if t in _COMMON_TERMS:
        return 0.1
    if len(t) <= 2:
        return 0.2
    if len(t) <= 4:
        return 0.4
    if t.isupper():
        return 0.8
    if "_" in t or "-" in t:
        return 0.7
    return 0.5


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Chunk:
    """A ranked slice of a file that matched the query."""

    file: str
    line_start: int
    line_end: int
    code: str
    patterns: List[str]
    matches: int
    symbol: str = ""
    score: float = 0.0
    rank_reason: List[str] = field(default_factory=list)
    hash: str = ""
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = hashlib.md5(self.code.encode("utf-8", "replace")).hexdigest()[:8]
        if not self.tokens:
            self.tokens = estimate_tokens(self.code)

    def header(self) -> str:
        sym = f" fn={self.symbol}" if self.symbol else ""
        pats = ",".join(self.patterns[:5])
        return (
            f"[{Path(self.file).name}:{self.line_start}-{self.line_end}"
            f"{sym} matches={self.matches} patterns={pats}"
            f" hash={self.hash} score={int(self.score)}]"
        )

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "symbol": self.symbol or None,
            "patterns": self.patterns,
            "matches": self.matches,
            "score": int(self.score),
            "rank_reason": self.rank_reason,
            "hash": self.hash,
            "tokens": self.tokens,
            "code": self.code,
        }


class _Scorer:
    def __init__(self, patterns: List[str]) -> None:
        self.compiled = [_compile_or_escape(p) for p in patterns]
        self.pattern_strs = patterns

    def score(self, chunk: Chunk) -> Chunk:
        reasons: List[str] = []
        score = 0.0

        score += min(chunk.matches * 5, 25)
        if chunk.matches >= 3:
            reasons.append(f"multi_match({chunk.matches})")

        lines = chunk.code.splitlines()

        matched = set()
        for line in lines:
            for i, pat in enumerate(self.compiled):
                if pat.search(line):
                    matched.add(i)
        if len(matched) >= 2:
            score += 15
            reasons.append("co_occurrence")
        if len(self.compiled) > 1 and len(matched) == len(self.compiled):
            score += 10
            reasons.append("all_patterns")

        rare = 0
        for pat in self.compiled:
            for line in lines:
                if pat.search(line):
                    for term in re.findall(r"\w+", line):
                        if _rarity(term) > 0.6:
                            rare += 1
        if rare:
            score += min(rare * 3, 15)
            reasons.append("rare_terms")

        if chunk.symbol:
            for line in lines:
                if chunk.symbol in line and any(kw in line for kw in (
                    "class", "struct", "def", "fn", "func", "function", "proc",
                    "void", "int", "char", "bool", "auto", "impl", "trait",
                )):
                    score += 12
                    reasons.append("definition")
                    break

        if chunk.symbol:
            has_body = any(("{" in ln or ":" in ln) for ln in lines)
            if not has_body and chunk.symbol in chunk.code:
                score -= 5
                reasons.append("declaration_only")

        joined = " ".join(self.pattern_strs).lower()
        if "test" in chunk.file.lower() and "test" not in joined:
            score -= 8
            reasons.append("test_demoted")

        if any(s in chunk.file.lower() for s in (
            "vendor", "node_modules", "__pycache__", ".git", "build", "dist",
            "generated", "auto-generated", ".min.js", ".bundle",
        )):
            score -= 15
            reasons.append("vendor_demoted")

        commentish = sum(
            1 for ln in lines if ln.strip().startswith(("//", "/*", "*", "#", "--"))
        )
        if lines and commentish / len(lines) > 0.7:
            score -= 10
            reasons.append("mostly_comments")

        chunk.score = max(0.0, score)
        chunk.rank_reason = reasons
        return chunk


class _Deduplicator:
    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold

    @staticmethod
    def _line_set(text: str) -> set:
        return {l.strip() for l in text.splitlines() if l.strip()}

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def dedupe(self, chunks: List[Chunk]) -> Tuple[List[Chunk], List[Tuple[Chunk, str]]]:
        kept: List[Chunk] = []
        kept_sets: List[set] = []
        removed: List[Tuple[Chunk, str]] = []
        seen_hashes: set = set()
        for c in chunks:
            if c.hash in seen_hashes:
                removed.append((c, f"exact_dup_of_{c.hash}"))
                continue
            lset = self._line_set(c.code)
            dup = False
            for i, kset in enumerate(kept_sets):
                sim = self._jaccard(lset, kset)
                if sim >= self.threshold:
                    removed.append((c, f"near_dup_{int(sim * 100)}%_of_{kept[i].hash}"))
                    dup = True
                    break
            if not dup:
                kept.append(c)
                kept_sets.append(lset)
                seen_hashes.add(c.hash)
        return kept, removed


class _NegativeEvidence:
    """Reports what was NOT found. An empty result is a real answer, not a
    failed search — negative evidence makes that explicit and about the FILE,
    not merely about which chunks survived budget/dedupe."""

    def __init__(self, patterns: List[str], search_path: str) -> None:
        self.patterns = patterns
        self.search_path = search_path
        self.absences: List[str] = []
        self._checked: set = set()

    def check_definition(self, symbol: str, chunks: List[Chunk]) -> None:
        if symbol in self._checked:
            return
        self._checked.add(symbol)
        for c in chunks:
            if c.symbol == symbol:
                for line in c.code.splitlines():
                    if symbol in line and any(kw in line for kw in (
                        "class", "struct", "def", "fn", "func", "function",
                        "proc", "void", "int", "char", "bool", "auto", "impl",
                        "trait",
                    )):
                        return
        self.absences.append(f"No definition found for '{symbol}' in {self.search_path}")

    def check_pattern(self, pattern: str, chunks: List[Chunk], full_text: str = "") -> None:
        rx = _compile_or_escape(pattern)
        if any(rx.search(c.code) for c in chunks):
            return
        if full_text and rx.search(full_text):
            self.absences.append(
                f"Pattern '{pattern}' IS in {self.search_path} but its match "
                f"fell outside the selected chunks (budget/dedupe)"
            )
        else:
            self.absences.append(f"Pattern '{pattern}' not found in {self.search_path}")


@dataclass
class Result:
    """The outcome of a :func:`focused_read` call."""

    query: List[str]
    chunks: List[Chunk]
    negative_evidence: List[str] = field(default_factory=list)
    files_searched: int = 1
    files_matched: int = 0
    budget: int = 0
    deduped: int = 0

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for c in self.chunks)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "chunks": [c.to_dict() for c in self.chunks],
            "negative_evidence": self.negative_evidence,
            "total_tokens": self.total_tokens,
            "files_searched": self.files_searched,
            "files_matched": self.files_matched,
            "budget": self.budget,
            "deduped": self.deduped,
        }

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def render(self) -> str:
        """Render the human/LLM-facing text report."""
        parts: List[str] = []
        recursive = self.files_searched > 1
        head = (
            f"=== slicegrep{' recursive' if recursive else ''}: "
            f"{len(self.chunks)} chunk(s), ~{self.total_tokens} tokens"
            f"{f' / {self.budget} budget' if self.budget else ''}"
        )
        if recursive:
            head += f", {self.files_searched} files searched, {self.files_matched} matched"
        head += " ==="
        parts.append(head)
        parts.append(f"\nQUERY:\n{'|'.join(self.query)}")

        if len(self.chunks) > 1:
            parts.append("\nRANKING:")
            for i, c in enumerate(self.chunks[:10], 1):
                reason = ", ".join(c.rank_reason) if c.rank_reason else "direct_match"
                sym = f" fn={c.symbol}" if c.symbol else ""
                parts.append(f"  {i}. {Path(c.file).name}:{c.line_start}{sym} — {reason}")

        parts.append("\n---")
        for c in self.chunks:
            parts.append(f"\n{c.header()}\n{c.code}")

        if self.deduped:
            parts.append(f"\n[DEDUPED: {self.deduped} near-duplicate chunk(s) removed]")

        if self.negative_evidence:
            parts.append("\nNEGATIVE EVIDENCE:")
            parts.extend(f"  - {a}" for a in self.negative_evidence)

        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def _apply_budget(chunks: List[Chunk], budget: int) -> List[Chunk]:
    if budget <= 0 or not chunks:
        return chunks
    fitted: List[Chunk] = []
    used = 0
    for c in chunks:
        if used + c.tokens <= budget:
            fitted.append(c)
            used += c.tokens
        # keep scanning: a smaller lower-ranked chunk may still fit
    if not fitted:
        # The best chunk alone exceeds the budget. Zero chunks is the worst
        # possible answer — truncate the best one to fit instead.
        top = chunks[0]
        keep = max(200, budget * 4)
        top.code = top.code[:keep] + "\n... (truncated to budget)"
        top.tokens = estimate_tokens(top.code)
        fitted = [top]
    return fitted


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_file(
    filepath: str,
    patterns: List[str],
    before: int,
    after: int,
    boundary: str,
    dedupe_threshold: float,
) -> Tuple[List[Chunk], Optional[_NegativeEvidence], str]:
    path = Path(filepath)
    if not path.is_file():
        return [], None, ""

    text = _read_text(path)
    lines = text.splitlines(keepends=True)

    compiled = [_compile_or_escape(p) for p in patterns]
    combined = _compile_multi_pattern(patterns)

    matches_by_line: Dict[int, List[str]] = {}
    for i, line in enumerate(lines):
        if not combined.search(line):
            continue
        for pat in compiled:
            if pat.search(line):
                matches_by_line.setdefault(i, []).append(pat.pattern)

    neg = _NegativeEvidence(patterns, filepath)
    if not matches_by_line:
        return [], neg, text

    lang = _detect_lang(filepath)
    chunks: List[Chunk] = []
    for target in sorted(matches_by_line):
        if boundary == "fn":
            start, end, symbol = _find_enclosing_boundary(lines, target, lang)
        else:  # "auto" / "none" both use a fixed window
            start = max(0, target - before)
            end = min(len(lines), target + after + 1)
            symbol = None
        chunks.append(Chunk(
            file=filepath,
            line_start=start + 1,
            line_end=end,
            code="".join(lines[start:end]),
            patterns=matches_by_line[target],
            matches=len(matches_by_line[target]),
            symbol=symbol or "",
        ))

    scorer = _Scorer(patterns)
    chunks = [scorer.score(c) for c in chunks]
    chunks.sort(key=lambda c: c.score, reverse=True)

    chunks, _ = _Deduplicator(dedupe_threshold).dedupe(chunks)
    return chunks, neg, text


def _iter_files(root: Path):
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if f.suffix.lower() not in SEARCHABLE_EXTS:
            continue
        try:
            if f.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield f


def focused_read(
    path: str,
    pattern: str,
    *,
    before: int = 40,
    after: int = 40,
    budget: int = 0,
    boundary: str = "auto",
    recursive: bool = False,
    dedupe: bool = True,
) -> Result:
    """Grep ``path`` for ``pattern`` and return ranked, budget-capped slices.

    Parameters
    ----------
    path:
        A file or a directory. A directory (or ``recursive=True``) triggers a
        recursive walk of source files, skipping vendored/build dirs and files
        over 1 MB.
    pattern:
        A regex, case-insensitive. Join alternatives with ``|``; a chunk that
        matches *more* of them ranks higher (co-occurrence scoring). Grouped
        alternations like ``(a|b)_c`` are treated as one pattern.
    before, after:
        Context lines each side of a match (ignored when ``boundary="fn"``).
    budget:
        Keep only the highest-ranked chunks fitting ~this many tokens. ``0``
        means no cap. This is the token lever.
    boundary:
        ``"auto"`` (fixed window) or ``"fn"`` (snap each chunk to its whole
        enclosing function/class).
    recursive:
        Force a directory walk even when ``path`` is a single file's directory.
    dedupe:
        Collapse near-duplicate chunks (exact duplicates always collapse).

    Returns
    -------
    Result
        Ranked :class:`Chunk` list plus negative evidence. Call
        :meth:`Result.render` for the text report or :meth:`Result.to_dict`
        for structured data.
    """
    patterns = _split_pattern_top_level(pattern)
    if not patterns:
        raise ValueError("pattern is empty after parsing")

    target = Path(path)
    threshold = 2.0 if not dedupe else 0.7

    if recursive or target.is_dir():
        root = target if target.is_dir() else target.parent
        all_chunks: List[Chunk] = []
        searched = 0
        matched = 0
        for f in _iter_files(root):
            searched += 1
            try:
                chunks, _neg, _text = _extract_file(
                    str(f), patterns, before, after, boundary, threshold
                )
            except Exception:
                continue
            if chunks:
                matched += 1
                all_chunks.extend(chunks)
        all_chunks.sort(key=lambda c: c.score, reverse=True)
        all_chunks = _apply_budget(all_chunks, budget)
        return Result(
            query=patterns,
            chunks=all_chunks,
            negative_evidence=[],
            files_searched=max(searched, 1),
            files_matched=matched,
            budget=budget,
        )

    chunks, neg, text = _extract_file(
        str(target), patterns, before, after, boundary, threshold
    )
    pre_budget = len(chunks)
    chunks = _apply_budget(chunks, budget)

    absences: List[str] = []
    if neg is not None:
        for c in chunks:
            if c.symbol:
                neg.check_definition(c.symbol, chunks)
        for pat in patterns:
            neg.check_pattern(pat, chunks, full_text=text)
        absences = neg.absences

    return Result(
        query=patterns,
        chunks=chunks,
        negative_evidence=absences,
        files_searched=1,
        files_matched=1 if chunks else 0,
        budget=budget,
        deduped=max(0, pre_budget - len(chunks)) if budget else 0,
    )
