import textwrap

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
