#!/usr/bin/env python
"""slicegrep retrieval benchmark v2.

Harder task families and stronger baselines than benchmarks/bench.py.

Task families (auto-generated from the corpora, seeded and reproducible):
  symbol      — find a def/class by name (v1-style)
  comprehend  — find an implementation from *docstring concept words*, not its name
  call-chain  — retrieve a function's definition AND a cross-file call site
  bug-local   — locate the function that raises a given error message
  config-flow — find where a config constant / env var originates
  test+impl   — retrieve a symbol's implementation AND a test referencing it

Strategies:
  raw-rg      — the grep output itself (file:line: content) is the context
  whole-file  — read every matching file in full (naive agent, v1)
  rg+windows  — ±60-line windows around matches (v1)
  rg+rank     — smarter naive agent: rank matching files (match count,
                filename hit, test demotion), read best files first up to cap
  lsp(jedi)   — language-server-style symbol search via jedi (the engine
                inside jedi-language-server / many Python IDEs); reads the
                definition sites it returns
  tfidf-vec   — lightweight lexical vector retriever: TF-IDF cosine over
                60-line chunks (stand-in for an embedding retriever; a neural
                model would add a heavy non-reproducible dependency)
  slicegrep   — one focused_read() call, budget 2000

Metrics per (task, strategy): tokens delivered (8k cap), ground-truth context
hit (ALL required spans present — for multi-span families that means e.g.
definition AND call site), irrelevant-token %, tool calls, latency.

"Hit" measures retrieval quality — the necessary condition for a model to
answer — not end-to-end agent task completion.

Usage:
    python benchmarks/bench2.py --corpora-dir PATH [--scale 240]
    python benchmarks/bench2.py --clone --scale 240 --md RESULTS_V2.md
"""
from __future__ import annotations

import argparse
import io
import json
import math
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slicegrep import focused_read  # noqa: E402
from slicegrep.core import (  # noqa: E402
    SEARCHABLE_EXTS,
    SKIP_DIRS,
    _python_enclosing_block,
    estimate_tokens,
)

try:
    import jedi
except ImportError:  # pragma: no cover
    jedi = None

try:
    from semble import SembleIndex
except ImportError:  # pragma: no cover
    SembleIndex = None

CONTEXT_CAP = 8000
WINDOW = 60
BUDGET = 2000
GT_HEAD_LINES = 25
SPAN_PAD = 3            # ± lines for call-site / test-site / config spans
SEED = 20260719

CORPORA = [
    ("pallets/click", "8.1.7"),
    ("pallets/flask", "3.0.0"),
    ("psf/requests", "v2.31.0"),
    ("Textualize/rich", "v13.7.0"),
]

_STOP = set("""
    the this that with from for and are was were will would should could into
    over under been being have has had does doing done which their there then
    than when where what while whose about after before because between
    return returns given class function method object value values none true
    false default optional argument arguments parameter parameters instance
    string example examples
""".split())


# --------------------------------------------------------------------------- #
# Repo cache — every file loaded once; all baseline searches run against it
# --------------------------------------------------------------------------- #

class RepoCache:
    def __init__(self, root: Path, label: str) -> None:
        self.root = root
        self.label = label
        self.files: List[Tuple[str, List[str]]] = []   # (rel_posix, lines)
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in SEARCHABLE_EXTS:
                continue
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self.files.append((f.relative_to(root).as_posix(), text.splitlines()))
        self._tfidf = None

    def lines(self, rel: str) -> List[str]:
        for r, ls in self.files:
            if r == rel:
                return ls
        raise KeyError(rel)

    def search(self, pattern: str) -> Dict[str, List[int]]:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        out: Dict[str, List[int]] = {}
        for rel, lines in self.files:
            hits = [i for i, l in enumerate(lines) if rx.search(l)]
            if hits:
                out[rel] = hits
        return out


# --------------------------------------------------------------------------- #
# Ground truth: one or more required spans
# --------------------------------------------------------------------------- #

@dataclass
class Span:
    rel: str
    start: int
    end: int
    text: str


@dataclass
class GT:
    spans: List[Span]

    @property
    def tokens(self) -> int:
        return sum(estimate_tokens(s.text) for s in self.spans)


def _def_site(cache: RepoCache, rel: str, lineno: int) -> Span:
    lines = [l + "\n" for l in cache.lines(rel)]
    block = _python_enclosing_block(lines, lineno)
    if block is None:
        start, end = lineno, min(len(lines), lineno + GT_HEAD_LINES)
    else:
        start, end, _ = block
    end = min(end, start + 1 + GT_HEAD_LINES)
    return Span(rel, start, end, "".join(lines[start:end]))


def _pad_span(cache: RepoCache, rel: str, lineno: int) -> Span:
    lines = cache.lines(rel)
    start = max(0, lineno - SPAN_PAD)
    end = min(len(lines), lineno + SPAN_PAD + 1)
    return Span(rel, start, end, "\n".join(lines[start:end]) + "\n")


def _enclosing_def_line(lines: List[str], lineno: int) -> Optional[int]:
    indent = len(lines[lineno]) - len(lines[lineno].lstrip())
    for j in range(lineno, -1, -1):
        m = re.match(r"^(\s*)(?:async\s+)?def\s+\w", lines[j])
        if m and len(m.group(1)) < indent:
            return j
    return None


# --------------------------------------------------------------------------- #
# Task generation
# --------------------------------------------------------------------------- #

_DEF_RE = re.compile(r"^(\s*)(def|class)\s+([A-Za-z][A-Za-z0-9_]*)")


def _symbols(cache: RepoCache) -> List[Tuple[str, int, str, str]]:
    out, seen = [], set()
    for rel, lines in cache.files:
        if not rel.endswith(".py") or "test" in rel.lower():
            continue
        if rel.startswith(("docs/", "examples/")):
            continue
        for i, line in enumerate(lines):
            m = _DEF_RE.match(line)
            if not m:
                continue
            name = m.group(3)
            if name.startswith("_") or len(name) < 4 or name in seen:
                continue
            seen.add(name)
            out.append((rel, i, m.group(2), name))
    return out


def gen_symbol(cache, rng, limit):
    tasks = []
    for rel, i, kind, name in _symbols(cache):
        style = rng.random()
        if style < 0.4:
            q = f"{kind} {name}"
        elif style < 0.8 or "_" not in name:
            q = name
        else:
            parts = [p for p in name.split("_") if len(p) >= 4][:2]
            q = "|".join(parts + [name]) if parts else name
        tasks.append((f"symbol: {name}", q, GT([_def_site(cache, rel, i)])))
    rng.shuffle(tasks)
    return tasks[:limit]


def gen_comprehend(cache, rng, limit):
    tasks = []
    for rel, i, kind, name in _symbols(cache):
        lines = cache.lines(rel)
        if i + 1 >= len(lines) or '"""' not in lines[i + 1] and "'''" not in lines[i + 1]:
            continue
        doc = " ".join(lines[i + 1:i + 4]).lower()
        words = [w for w in re.findall(r"[a-z]{5,}", doc)
                 if w not in _STOP and w not in name.lower()]
        uniq = list(dict.fromkeys(words))
        if len(uniq) < 2:
            continue
        picks = uniq[:3] if len(uniq) >= 3 else uniq
        q = "|".join(picks)
        tasks.append((f"comprehend: {name}", q, GT([_def_site(cache, rel, i)])))
    rng.shuffle(tasks)
    return tasks[:limit]


def gen_callchain(cache, rng, limit):
    defs = {name: (rel, i) for rel, i, kind, name in _symbols(cache) if kind == "def"}
    tasks = []
    for name, (drel, dline) in defs.items():
        for rel, lines in cache.files:
            if rel == drel or not rel.endswith(".py") or "test" in rel.lower():
                continue
            for i, line in enumerate(lines):
                if f"{name}(" in line and not _DEF_RE.match(line):
                    tasks.append((
                        f"call-chain: {name}", name,
                        GT([_def_site(cache, drel, dline),
                            _pad_span(cache, rel, i)]),
                    ))
                    break
            else:
                continue
            break
    rng.shuffle(tasks)
    return tasks[:limit]


def gen_buglocal(cache, rng, limit):
    tasks = []
    for rel, lines in cache.files:
        if not rel.endswith(".py") or "test" in rel.lower():
            continue
        for i, line in enumerate(lines):
            if not re.search(r"\braise\s+\w+", line):
                continue
            m = re.search(r"[\"']([^\"'{}]{12,})[\"']", line)
            if not m:
                continue
            msg = m.group(1).strip()
            d = _enclosing_def_line(lines, i)
            if d is None:
                continue
            tasks.append((
                f"bug-local: {msg[:40]!r}", re.escape(msg),
                GT([_def_site(cache, rel, d)]),
            ))
            break  # one per file keeps variety
    rng.shuffle(tasks)
    return tasks[:limit]


def gen_config(cache, rng, limit):
    tasks, seen = [], set()
    for rel, lines in cache.files:
        if not rel.endswith(".py") or "test" in rel.lower():
            continue
        for i, line in enumerate(lines):
            m = (re.match(r"^([A-Z][A-Z0-9_]{3,})\s*(?::[^=]+)?=\s*\S", line)
                 or re.search(r"environ(?:\.get)?\(\s*[\"']([A-Z0-9_]{3,})[\"']", line)
                 or re.search(r"getenv\(\s*[\"']([A-Z0-9_]{3,})[\"']", line))
            if not m:
                continue
            key = m.group(1)
            if key in seen:
                continue
            seen.add(key)
            tasks.append((f"config-flow: {key}", key,
                          GT([_pad_span(cache, rel, i)])))
    rng.shuffle(tasks)
    return tasks[:limit]


def gen_testimpl(cache, rng, limit):
    defs = {name: (rel, i) for rel, i, kind, name in _symbols(cache)}
    tasks = []
    for name, (drel, dline) in defs.items():
        for rel, lines in cache.files:
            if "test" not in rel.lower() or not rel.endswith(".py"):
                continue
            for i, line in enumerate(lines):
                if name in line:
                    tasks.append((
                        f"test+impl: {name}", name,
                        GT([_def_site(cache, drel, dline),
                            _pad_span(cache, rel, i)]),
                    ))
                    break
            else:
                continue
            break
    rng.shuffle(tasks)
    return tasks[:limit]


FAMILIES = [
    ("symbol", gen_symbol),
    ("comprehend", gen_comprehend),
    ("call-chain", gen_callchain),
    ("bug-local", gen_buglocal),
    ("config-flow", gen_config),
    ("test+impl", gen_testimpl),
]


# --------------------------------------------------------------------------- #
# Strategies — fn(cache, query) -> (context, tool_calls)
# --------------------------------------------------------------------------- #

def strat_raw_rg(cache, query):
    hits = cache.search(query)
    parts = []
    for rel, hs in hits.items():
        lines = cache.lines(rel)
        for h in hs:
            parts.append(f"{rel}:{h + 1}: {lines[h]}")
    return "\n".join(parts), 1


def strat_whole_file(cache, query):
    hits = cache.search(query)
    calls = 1
    parts = []
    for rel in hits:
        parts.append("\n".join(cache.lines(rel)))
        calls += 1
    return "\n".join(parts), calls


def strat_rg_windows(cache, query):
    hits = cache.search(query)
    calls = 1
    parts = []
    for rel, hs in hits.items():
        lines = cache.lines(rel)
        windows: List[Tuple[int, int]] = []
        for h in sorted(hs):
            lo, hi = max(0, h - WINDOW), min(len(lines), h + WINDOW + 1)
            if windows and lo <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
            else:
                windows.append((lo, hi))
        for lo, hi in windows:
            parts.append("\n".join(lines[lo:hi]))
            calls += 1
    return "\n".join(parts), calls


def strat_rg_rank(cache, query):
    """Smarter naive agent: rank matching files, read best-first up to cap."""
    hits = cache.search(query)
    calls = 1
    qtokens = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
    ranked = sorted(
        hits.items(),
        key=lambda kv: (
            len(kv[1])
            + (10 if any(t in Path(kv[0]).name.lower() for t in qtokens) else 0)
            - (5 if "test" in kv[0].lower() else 0)
        ),
        reverse=True,
    )
    parts, used = [], 0
    for rel, _ in ranked:
        text = "\n".join(cache.lines(rel))
        parts.append(text)
        calls += 1
        used += estimate_tokens(text)
        if used >= CONTEXT_CAP:
            break
    return "\n".join(parts), calls


def strat_lsp_jedi(cache, query):
    """Language-server symbol search: jedi Project.search on the first
    identifier-shaped token; reads each returned definition site."""
    if jedi is None:
        return "", 1
    m = re.search(r"[A-Za-z_][A-Za-z0-9_]{3,}", query)
    if not m:
        return "", 1
    ident = m.group(0)
    parts, used = [], 0
    try:
        project = jedi.Project(str(cache.root))
        for name in project.search(ident):
            try:
                mp = name.module_path
                if mp is None or cache.root not in Path(mp).parents:
                    continue
                rel = Path(mp).relative_to(cache.root).as_posix()
                span = _def_site(cache, rel, (name.line or 1) - 1)
                parts.append(span.text)
                used += estimate_tokens(span.text)
                if used >= BUDGET:
                    break
            except Exception:
                continue
    except Exception:
        pass
    return "\n".join(parts), 1


def _tfidf_index(cache):
    if cache._tfidf is not None:
        return cache._tfidf
    chunks = []          # (rel, lo, hi, counter)
    df = Counter()
    for rel, lines in cache.files:
        for lo in range(0, max(1, len(lines)), 40):
            hi = min(len(lines), lo + 60)
            toks = Counter(re.findall(r"[a-z0-9_]{3,}",
                                      "\n".join(lines[lo:hi]).lower()))
            if toks:
                chunks.append((rel, lo, hi, toks))
                df.update(toks.keys())
            if hi >= len(lines):
                break
    n = max(1, len(chunks))
    idf = {t: math.log(n / (1 + c)) for t, c in df.items()}
    cache._tfidf = (chunks, idf)
    return cache._tfidf


def strat_tfidf(cache, query):
    chunks, idf = _tfidf_index(cache)
    q = Counter(re.findall(r"[a-z0-9_]{3,}", query.lower()))
    qvec = {t: c * idf.get(t, 0.0) for t, c in q.items()}
    qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
    scored = []
    for rel, lo, hi, toks in chunks:
        dot = sum(qvec.get(t, 0.0) * c * idf.get(t, 0.0) for t, c in toks.items())
        if dot <= 0:
            continue
        dnorm = math.sqrt(sum((c * idf.get(t, 0.0)) ** 2 for t, c in toks.items())) or 1.0
        scored.append((dot / (qnorm * dnorm), rel, lo, hi))
    scored.sort(reverse=True)
    parts, used = [], 0
    for _s, rel, lo, hi in scored:
        text = "\n".join(cache.lines(rel)[lo:hi])
        parts.append(text)
        used += estimate_tokens(text)
        if used >= BUDGET:
            break
    return "\n".join(parts), 1


def _windows_of(cache) -> list:
    """Shared 60-line/40-stride windows, cached per corpus."""
    if getattr(cache, "_win", None) is None:
        wins = []
        for rel, lines in cache.files:
            for lo in range(0, max(1, len(lines)), 40):
                hi = min(len(lines), lo + 60)
                wins.append((rel, lo, hi, "\n".join(lines[lo:hi])))
                if hi >= len(lines):
                    break
        cache._win = wins
    return cache._win


def _pack_windows(scored, budget) -> str:
    parts, used = [], 0
    for _s, text in scored:
        parts.append(text)
        used += estimate_tokens(text)
        if used >= budget:
            break
    return "\n".join(parts)


try:
    import bm25s
except ImportError:  # pragma: no cover
    bm25s = None

_BM25_CACHE: Dict[str, object] = {}


def strat_bm25(cache, query):
    """BM25 (bm25s) over the shared 60-line windows."""
    if bm25s is None:
        return "", 1
    key = str(cache.root)
    entry = _BM25_CACHE.get(key)
    wins = _windows_of(cache)
    if entry is None:
        corpus = [re.findall(r"[a-z0-9_]{2,}", w[3].lower()) for w in wins]
        idx = bm25s.BM25()
        idx.index(corpus)
        _BM25_CACHE[key] = idx
        entry = idx
    q = re.findall(r"[a-z0-9_]{2,}", query.replace("\\", "").replace("|", " ").lower())
    if not q:
        return "", 1
    try:
        docs, scores = entry.retrieve([q], k=min(50, len(wins)))
    except Exception:
        return "", 1
    scored = [(scores[0][i], wins[int(docs[0][i])][3])
              for i in range(len(docs[0])) if scores[0][i] > 0]
    return _pack_windows(scored, BUDGET), 1


try:
    from model2vec import StaticModel as _StaticModel
except ImportError:  # pragma: no cover
    _StaticModel = None

_DENSE_MODEL = None
_DENSE_CACHE: Dict[str, object] = {}


def strat_dense(cache, query):
    """Dense retrieval: potion-code-16M static embeddings (the same model
    semble uses) over the shared windows, plain cosine — no BM25 fusion."""
    global _DENSE_MODEL
    if _StaticModel is None:
        return "", 1
    import numpy as np
    if _DENSE_MODEL is None:
        try:
            _DENSE_MODEL = _StaticModel.from_pretrained("minishlab/potion-code-16M-v2")
        except Exception:
            return "", 1
    wins = _windows_of(cache)
    key = str(cache.root)
    embs = _DENSE_CACHE.get(key)
    if embs is None:
        embs = _DENSE_MODEL.encode([w[3] for w in wins])
        embs = embs / (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9)
        _DENSE_CACHE[key] = embs
    qv = _DENSE_MODEL.encode([query.replace("\\", "").replace("|", " ")])[0]
    qv = qv / (np.linalg.norm(qv) + 1e-9)
    sims = embs @ qv
    order = sims.argsort()[::-1][:50]
    scored = [(float(sims[i]), wins[int(i)][3]) for i in order if sims[i] > 0]
    return _pack_windows(scored, BUDGET), 1


def _ast_chunks_of(cache) -> list:
    """Syntax-aware chunks: one per top-level function/class (Python ast),
    whole-file fallback for non-Python. Answers the tree-sitter question."""
    if getattr(cache, "_astc", None) is None:
        import ast as _ast
        chunks = []
        for rel, lines in cache.files:
            if rel.endswith(".py"):
                try:
                    tree = _ast.parse("\n".join(lines))
                    spans = [(n.lineno - 1, getattr(n, "end_lineno", n.lineno))
                             for n in _ast.walk(tree)
                             if isinstance(n, (_ast.FunctionDef,
                                               _ast.AsyncFunctionDef,
                                               _ast.ClassDef))]
                except SyntaxError:
                    spans = []
                if spans:
                    for lo, hi in spans:
                        chunks.append((rel, lo, hi, "\n".join(lines[lo:hi])))
                    continue
            chunks.append((rel, 0, len(lines), "\n".join(lines[:120])))
        cache._astc = chunks
    return cache._astc


def strat_ast_tfidf(cache, query):
    """TF-IDF cosine over syntax-aware (ast) chunks instead of fixed windows."""
    chunks = _ast_chunks_of(cache)
    q = Counter(re.findall(r"[a-z0-9_]{3,}", query.lower()))
    if not q:
        return "", 1
    df = Counter()
    toks_list = []
    for _rel, _lo, _hi, text in chunks:
        toks = Counter(re.findall(r"[a-z0-9_]{3,}", text.lower()))
        toks_list.append(toks)
        df.update(toks.keys())
    n = max(1, len(chunks))
    idf = {t: math.log(n / (1 + c)) for t, c in df.items()}
    qvec = {t: c * idf.get(t, 0.0) for t, c in q.items()}
    qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
    scored = []
    for (rel, lo, hi, text), toks in zip(chunks, toks_list):
        dot = sum(qvec.get(t, 0.0) * c * idf.get(t, 0.0) for t, c in toks.items())
        if dot <= 0:
            continue
        dn = math.sqrt(sum((c * idf.get(t, 0.0)) ** 2 for t, c in toks.items())) or 1.0
        scored.append((dot / (qnorm * dn), text))
    scored.sort(reverse=True)
    return _pack_windows(scored, BUDGET), 1


_REPOMAP_CACHE: Dict[str, tuple] = {}


def strat_repomap(cache, query):
    """Aider-style repo map: PageRank over the def/ref file graph,
    personalized by files matching the query; emits top definition sites."""
    key = str(cache.root)
    entry = _REPOMAP_CACHE.get(key)
    if entry is None:
        defs = {}          # symbol -> (rel, lineno)
        for rel, lines in cache.files:
            if not rel.endswith(".py"):
                continue
            for i, line in enumerate(lines):
                m = re.match(r"^\s*(?:def|class)\s+([A-Za-z_]\w{2,})", line)
                if m and m.group(1) not in defs:
                    defs[m.group(1)] = (rel, i)
        edges: Dict[str, Counter] = {}
        for rel, lines in cache.files:
            if not rel.endswith(".py"):
                continue
            refs = Counter()
            text = "\n".join(lines)
            for sym, (drel, _l) in defs.items():
                if drel != rel and sym in text:
                    refs[drel] += 1
            if refs:
                edges[rel] = refs
        _REPOMAP_CACHE[key] = (defs, edges)
        entry = (defs, edges)
    defs, edges = entry
    files = sorted({rel for rel, _ in cache.files if rel.endswith(".py")})
    if not files:
        return "", 1
    fidx = {f: i for i, f in enumerate(files)}
    qtok = set(re.findall(r"[a-z0-9_]{3,}",
                          query.replace("\\", "").replace("|", " ").lower()))
    # personalization: files containing any query token
    pers = [1.0 if any(t in "\n".join(cache.lines(f)).lower() for t in qtok)
            else 0.05 for f in files]
    tot = sum(pers) or 1.0
    pers = [p / tot for p in pers]
    rank = list(pers)
    for _ in range(15):
        new = [0.15 * p for p in pers]
        for src, refs in edges.items():
            if src not in fidx:
                continue
            w = sum(refs.values())
            if not w:
                continue
            share = 0.85 * rank[fidx[src]]
            for dst, c in refs.items():
                if dst in fidx:
                    new[fidx[dst]] += share * (c / w)
        rank = new
    ranked_files = sorted(files, key=lambda f: rank[fidx[f]], reverse=True)
    # emit definition sites from top files; query-matching symbols first
    parts, used, calls = [], 0, 1
    for f in ranked_files[:12]:
        lines = cache.lines(f)
        syms = [(s, l) for s, (r, l) in defs.items() if r == f]
        syms.sort(key=lambda sl: (0 if any(t in sl[0].lower() for t in qtok) else 1, sl[1]))
        for _s, l in syms[:6]:
            text = "\n".join(lines[l: min(len(lines), l + 20)])
            parts.append(text)
            used += estimate_tokens(text)
            if used >= BUDGET:
                return "\n".join(parts), calls
    return "\n".join(parts), calls


_SEMBLE_CACHE: Dict[str, object] = {}


def strat_semble(cache, query):
    """MinishLab semble: static embeddings (Model2Vec) + BM25 + RRF over
    tree-sitter chunks. Index is built once per corpus root and cached,
    mirroring how it runs as a long-lived MCP server. Query is de-regexed
    ('a|b' -> 'a b') since semble takes natural-language/code queries."""
    if SembleIndex is None:
        return "", 1
    idx = _SEMBLE_CACHE.get(str(cache.root))
    if idx is None:
        try:
            idx = SembleIndex.from_path(str(cache.root))
        except Exception:
            return "", 1
        _SEMBLE_CACHE[str(cache.root)] = idx
    q = query.replace("\\", "").replace("|", " ")
    parts, used = [], 0
    try:
        for r in idx.search(q, top_k=50):
            text = r.chunk.content
            parts.append(text)
            used += estimate_tokens(text)
            if used >= BUDGET:
                break
    except Exception:
        return "", 1
    return "\n".join(parts), 1


def strat_slicegrep(cache, query):
    result = focused_read(str(cache.root), query, budget=BUDGET)
    return result.render(), 1


STRATEGIES = [
    ("raw-rg", strat_raw_rg),
    ("whole-file", strat_whole_file),
    ("rg+windows", strat_rg_windows),
    ("rg+rank", strat_rg_rank),
    ("lsp(jedi)", strat_lsp_jedi),
    ("tfidf-vec", strat_tfidf),
    ("bm25", strat_bm25),
    ("dense-emb", strat_dense),
    ("ast-tfidf", strat_ast_tfidf),
    ("repomap", strat_repomap),
    ("semble", strat_semble),
    ("slicegrep", strat_slicegrep),
]


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def _norm(text: str) -> set:
    return {l.strip() for l in text.splitlines() if l.strip()}


def hit(context: str, gt: GT) -> bool:
    ctx = _norm(context)
    return all(_norm(s.text) <= ctx for s in gt.spans)


def relevant_tokens(context: str, gt: GT) -> int:
    ctx = _norm(context)
    chars = sum(len(l) for s in gt.spans for l in _norm(s.text) if l in ctx)
    return chars // 4


def cap(text: str) -> str:
    if estimate_tokens(text) <= CONTEXT_CAP:
        return text
    return text[: CONTEXT_CAP * 4]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpora-dir", help="dir containing pre-cloned corpora "
                    "(subdirs click/ flask/ requests/ rich/)")
    ap.add_argument("--clone", action="store_true")
    ap.add_argument("--seed", type=int, default=SEED,
                    help="task sampling seed (fresh seed = held-out run)")
    ap.add_argument("--scale", type=int, default=240,
                    help="total tasks (split across 6 families; default 240)")
    ap.add_argument("--md", help="write markdown report here")
    ap.add_argument("--json", help="write raw results here")
    args = ap.parse_args()

    tmp = None
    if args.corpora_dir:
        base = Path(args.corpora_dir)
    elif args.clone:
        tmp = tempfile.mkdtemp(prefix="slicegrep-bench2-")
        base = Path(tmp)
        for full, tag in CORPORA:
            dest = base / full.split("/")[1]
            print(f"cloning {full}@{tag} ...")
            subprocess.run(["git", "clone", "--depth", "1", "--branch", tag,
                            "--quiet", f"https://github.com/{full}", str(dest)],
                           check=True)
    else:
        ap.error("pass --corpora-dir PATH or --clone")

    print("loading corpora ...")
    caches = []
    for full, tag in CORPORA:
        d = base / full.split("/")[1]
        if d.is_dir():
            caches.append(RepoCache(d, f"{full}@{tag}"))
    if not caches:
        ap.error(f"no corpora found under {base}")
    if jedi is None:
        print("WARNING: jedi not installed — lsp(jedi) will score 0. "
              "pip install jedi")

    rng = random.Random(args.seed)
    per_family = -(-args.scale // len(FAMILIES))
    per_corpus = -(-per_family // len(caches))
    tasks = []   # (family, name, query, cache, gt)
    for fam, gen in FAMILIES:
        fam_tasks = []
        for cache in caches:
            for name, q, gt in gen(cache, rng, per_corpus):
                fam_tasks.append((fam, name, q, cache, gt))
        rng.shuffle(fam_tasks)
        tasks.extend(fam_tasks[:per_family])
    rng.shuffle(tasks)
    tasks = tasks[: args.scale]
    fam_counts = Counter(t[0] for t in tasks)
    print(f"{len(tasks)} tasks: " +
          ", ".join(f"{f}={c}" for f, c in sorted(fam_counts.items())) + "\n")

    cells = []
    for idx, (fam, name, query, cache, gt) in enumerate(tasks, 1):
        if idx % 20 == 0:
            print(f"  ... {idx}/{len(tasks)}")
        for sname, fn in STRATEGIES:
            t0 = time.perf_counter()
            try:
                context, calls = fn(cache, query)
            except Exception:
                context, calls = "", 1
            lat = (time.perf_counter() - t0) * 1000
            capped = cap(context)
            tokens = estimate_tokens(capped) if capped else 0
            ok = bool(capped) and hit(capped, gt)
            rel = relevant_tokens(capped, gt) if capped else 0
            irr = 100.0 * (1 - rel / tokens) if tokens else 100.0
            cells.append({
                "family": fam, "task": name, "corpus": cache.label,
                "strategy": sname, "tokens": tokens, "included": ok,
                "irrelevant_pct": round(irr, 1), "tool_calls": calls,
                "latency_ms": round(lat, 1),
            })

    # ---- summaries ----
    def rows_for(sub):
        return {
            "median_tokens": int(statistics.median(c["tokens"] for c in sub)),
            "hit_rate": round(100.0 * sum(c["included"] for c in sub) / len(sub), 1),
            "median_irrelevant_pct": round(
                statistics.median(c["irrelevant_pct"] for c in sub), 1),
            "median_tool_calls": int(statistics.median(c["tool_calls"] for c in sub)),
            "median_latency_ms": round(
                statistics.median(c["latency_ms"] for c in sub), 1),
            "total_time_s": round(sum(c["latency_ms"] for c in sub) / 1000, 1),
        }

    summary = {}
    fam_matrix = {}
    for sname, _ in STRATEGIES:
        sub = [c for c in cells if c["strategy"] == sname]
        summary[sname] = rows_for(sub)
        fam_matrix[sname] = {
            fam: round(100.0 * sum(c["included"] for c in sub if c["family"] == fam)
                       / max(1, sum(1 for c in sub if c["family"] == fam)), 1)
            for fam, _g in FAMILIES
        }

    print("\n=== SUMMARY (median over tasks) ===")
    print(f"{'strategy':12s} {'tokens':>7s} {'hit%':>6s} {'irrel%':>7s} "
          f"{'calls':>6s} {'lat/task':>9s} {'total':>7s}")
    for sname, _ in STRATEGIES:
        r = summary[sname]
        print(f"{sname:12s} {r['median_tokens']:>7,d} {r['hit_rate']:>5.1f}% "
              f"{r['median_irrelevant_pct']:>6.1f}% {r['median_tool_calls']:>6d} "
              f"{r['median_latency_ms']:>7.1f}ms {r['total_time_s']:>6.1f}s")

    print("\n=== HIT RATE BY FAMILY ===")
    fams = [f for f, _ in FAMILIES]
    print(f"{'strategy':12s} " + " ".join(f"{f:>11s}" for f in fams))
    for sname, _ in STRATEGIES:
        print(f"{sname:12s} " +
              " ".join(f"{fam_matrix[sname][f]:>10.1f}%" for f in fams))

    if args.md:
        out = ["# slicegrep retrieval benchmark v2\n"]
        out.append(f"{len(tasks)} generated tasks across "
                   + ", ".join(f"{f}@{t}" for f, t in CORPORA)
                   + f" — families: "
                   + ", ".join(f"{f} ({fam_counts[f]})" for f, _ in FAMILIES)
                   + f". Context cap {CONTEXT_CAP} tokens; retriever budgets "
                   + f"{BUDGET}. Seeded (SEED={SEED}); reproduce with "
                   + f"`python benchmarks/bench2.py --clone --scale {args.scale}`.\n")
        out.append("**Ground-truth context hit rate** = ALL required spans for "
                   "the task landed in the capped context (multi-span families "
                   "require e.g. the definition AND a cross-file call site). "
                   "This measures retrieval, not end-to-end task completion.\n")
        out.append("## Summary (median over tasks)\n")
        out.append("| strategy | tokens → model | hit rate | irrelevant | "
                   "tool calls | latency/task | total |")
        out.append("|---|---|---|---|---|---|---|")
        for sname, _ in STRATEGIES:
            r = summary[sname]
            out.append(f"| {sname} | {r['median_tokens']:,} | {r['hit_rate']}% "
                       f"| {r['median_irrelevant_pct']}% | {r['median_tool_calls']} "
                       f"| {r['median_latency_ms']} ms | {r['total_time_s']} s |")
        out.append("\n## Hit rate by task family\n")
        out.append("| strategy | " + " | ".join(fams) + " |")
        out.append("|---" * (len(fams) + 1) + "|")
        for sname, _ in STRATEGIES:
            out.append(f"| {sname} | " +
                       " | ".join(f"{fam_matrix[sname][f]}%" for f in fams) + " |")
        out.append(
            "\n## Notes and limitations\n\n"
            "- **lsp(jedi)** drives jedi (the engine inside "
            "jedi-language-server) via project-wide symbol search. It only "
            "receives the first identifier-shaped token of each query — like "
            "an LSP client, it cannot search error strings or concept words.\n"
            "- **tfidf-vec** is a lexical vector retriever (TF-IDF cosine "
            "over 60-line chunks): the standard lightweight stand-in for an "
            "embedding retriever. A neural embedding baseline would add a "
            "heavy model dependency; treat tfidf-vec as its floor, not its "
            "ceiling.\n"
            "- Baselines share one in-memory file cache, so latency reflects "
            "matching cost, not disk IO. slicegrep walks the tree itself; "
            "its latency includes that overhead.\n"
            "- Ground truth is auto-generated and span-based; families were "
            "designed before results were seen and none were removed "
            "after.\n")
        Path(args.md).write_text("\n".join(out), encoding="utf-8")
        print(f"\nwrote {args.md}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"summary": summary, "by_family": fam_matrix, "cells": cells},
            indent=2), encoding="utf-8")
        print(f"wrote {args.json}")

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
