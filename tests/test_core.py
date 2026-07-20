import textwrap
from pathlib import Path

import pytest

from slicegrep import Result, focused_read
from slicegrep.core import _split_pattern_top_level


SAMPLE = textwrap.dedent(
    '''\
    import os


    class Scorer:
        """Scores chunks."""

        def score(self, chunk):
            total = 0
            for token in chunk:
                total += self.weight(token)
            return total

        def weight(self, token):
            return len(token)


    def unrelated_helper():
        # nothing to see here
        return 42


    def retry_with_backoff(fn, timeout=5):
        for attempt in range(3):
            try:
                return fn()
            except TimeoutError:
                backoff = 2 ** attempt
        raise TimeoutError(timeout)
    '''
)


@pytest.fixture()
def sample_file(tmp_path):
    p = tmp_path / "sample.py"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_finds_matching_chunk(sample_file):
    result = focused_read(str(sample_file), "def score", before=1, after=3)
    assert isinstance(result, Result)
    assert result.chunks
    assert any("def score" in c.code for c in result.chunks)


def test_returns_no_chunks_when_absent(sample_file):
    result = focused_read(str(sample_file), "def nonexistent_symbol_xyz")
    assert result.chunks == []
    assert any("not found" in a for a in result.negative_evidence)


def test_co_occurrence_ranks_higher(sample_file):
    # A chunk containing retry+timeout+backoff should outrank an isolated hit.
    result = focused_read(str(sample_file), "retry|timeout|backoff", before=6, after=6)
    top = result.chunks[0]
    assert "co_occurrence" in top.rank_reason
    assert "retry_with_backoff" in top.code


def test_boundary_fn_snaps_to_whole_function(sample_file):
    result = focused_read(str(sample_file), "total \\+=", boundary="fn")
    top = result.chunks[0]
    # The whole score() body, including its header, should be present.
    assert "def score" in top.code
    assert "return total" in top.code


def test_budget_truncates_rather_than_returning_zero(sample_file):
    # A budget smaller than any single chunk must still return one (truncated).
    result = focused_read(str(sample_file), "def score", before=40, after=40, budget=5)
    assert len(result.chunks) == 1
    assert "truncated to budget" in result.chunks[0].code


def test_budget_keeps_within_cap_across_many_matches(sample_file):
    result = focused_read(str(sample_file), "def", before=2, after=2, budget=60)
    assert result.total_tokens <= 60 or len(result.chunks) == 1


def test_recursive_directory_walk(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "c.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")

    result = focused_read(str(tmp_path), "def (alpha|beta|gamma)")
    files = {c.file for c in result.chunks}
    assert result.files_searched >= 2
    # node_modules is skipped, so gamma must not appear.
    assert not any("node_modules" in f for f in files)
    assert any("a.py" in f for f in files)
    assert any("b.py" in f for f in files)


def test_grouped_alternation_is_one_pattern():
    # (retry|backoff)_delay must not be sheared into two broken fragments.
    parts = _split_pattern_top_level("(retry|backoff)_delay")
    assert parts == ["(retry|backoff)_delay"]


def test_top_level_split():
    assert _split_pattern_top_level("a|b|c") == ["a", "b", "c"]
    assert _split_pattern_top_level("foo\\|bar") == ["foo\\|bar"]


def test_invalid_regex_degrades_to_literal(sample_file):
    # An unbalanced paren should not raise; it degrades to a literal search.
    result = focused_read(str(sample_file), "score(")
    assert isinstance(result, Result)


def test_render_and_to_dict_roundtrip(sample_file):
    result = focused_read(str(sample_file), "def score", budget=500)
    text = result.render()
    assert "slicegrep" in text
    assert "QUERY" in text
    data = result.to_dict()
    assert data["chunks"]
    assert data["chunks"][0]["code"]
    assert "total_tokens" in data


def test_empty_pattern_raises(sample_file):
    with pytest.raises(ValueError):
        focused_read(str(sample_file), "   ")


# --------------------------------------------------------------------------- #
# v0.2: retrieval objectives, diversity packing, semantic rerank
# --------------------------------------------------------------------------- #

def _make_multifile_repo(tmp_path):
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir(); tests.mkdir()
    (src / "widget.py").write_text(
        "def make_widget(size):\n"
        '    """Build a widget of the given size."""\n'
        "    return {'size': size}\n" + "\n" * 2 +
        "".join(f"# filler widget note {i}\n" for i in range(40)),
        encoding="utf-8")
    (src / "app.py").write_text(
        "from widget import make_widget\n\n"
        "def run():\n"
        "    w = make_widget(3)\n"
        "    return w\n" + "".join(f"# app filler {i}\n" for i in range(40)),
        encoding="utf-8")
    (tests / "test_widget.py").write_text(
        "from widget import make_widget\n\n"
        "def test_make_widget():\n"
        "    assert make_widget(2)['size'] == 2\n"
        + "".join(f"# test filler {i}\n" for i in range(40)),
        encoding="utf-8")
    return tmp_path


def test_objective_auto_covers_def_caller_and_test(tmp_path):
    repo = _make_multifile_repo(tmp_path)
    result = focused_read(str(repo), "make_widget", budget=600)
    files = {Path(c.file).name for c in result.chunks}
    assert "widget.py" in files          # definition
    assert "app.py" in files             # cross-file caller
    assert "test_widget.py" in files     # test

def test_objective_single_is_pure_score_order(tmp_path):
    repo = _make_multifile_repo(tmp_path)
    auto = focused_read(str(repo), "make_widget", budget=600)
    single = focused_read(str(repo), "make_widget", budget=600,
                          objective="single")
    # single mode must not *reserve* slots; it may still include several
    # files by score, but auto must cover at least as many
    assert len({c.file for c in auto.chunks}) >= len({c.file for c in single.chunks})

def test_semantic_rerank_favors_concept_vocabulary(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(
        "def refresh_cache(ttl):\n"
        '    """Refresh the cache when the expiry ttl elapses."""\n'
        "    expire = ttl\n"
        "    return expire\n"
        + "\n".join(f"x{i} = {i}  # cache" for i in range(80)) + "\n"
        + "def other():\n    # cache mention only\n    return 1\n",
        encoding="utf-8")
    result = focused_read(str(f), "cache|expiry|refresh", budget=400)
    assert result.chunks, "expected at least one chunk"
    top = result.chunks[0]
    assert "refresh_cache" in top.code


def test_nl_query_expands_three_plus_words(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(
        "def guarantee_definition_slot(budget):\n"
        '    """Reserve budget for the definition chunk."""\n'
        "    return budget\n",
        encoding="utf-8")
    r = focused_read(str(tmp_path), "how does budget guarantee definition slots",
                     budget=400)
    assert r.chunks and "guarantee_definition_slot" in r.chunks[0].code

def test_two_word_query_stays_exact(tmp_path):
    from slicegrep.core import _expand_nl_query
    # "class Context" must NOT shatter into bare common words
    assert _expand_nl_query("class Context", ["class Context"]) == ["class Context"]
