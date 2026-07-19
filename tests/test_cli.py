import json

import pytest

from slicegrep.cli import main


@pytest.fixture()
def sample_file(tmp_path):
    p = tmp_path / "mod.py"
    p.write_text(
        "def handle_request(req):\n"
        "    return process(req)\n\n"
        "def process(x):\n"
        "    return x + 1\n",
        encoding="utf-8",
    )
    return p


def test_cli_finds_match(sample_file, capsys):
    rc = main([str(sample_file), "def handle_request"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "handle_request" in out
    assert "slicegrep" in out


def test_cli_no_match_exits_1(sample_file, capsys):
    rc = main([str(sample_file), "def does_not_exist"])
    assert rc == 1


def test_cli_json_output(sample_file, capsys):
    rc = main([str(sample_file), "def process", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["chunks"]
    assert data["chunks"][0]["file"].endswith("mod.py")


def test_cli_missing_path_exits_1(capsys):
    rc = main(["/no/such/path/xyz.py", "pattern"])
    assert rc == 1


def test_cli_budget_flag(sample_file, capsys):
    rc = main([str(sample_file), "def", "--budget", "40"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "budget" in out
