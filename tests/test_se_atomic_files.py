"""SE result files: a run killed while writing leaves the previous complete file."""
from __future__ import annotations

import pytest

from simulation.atomic_files import append_lines_atomically, write_atomically


def test_failed_write_keeps_the_previous_file(tmp_path):
    target = tmp_path / "backtesting_log.json"
    target.write_text('{"old": true}', encoding="utf-8")

    def broken(tmp: str) -> None:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write('{"new": ')  # cut off
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        write_atomically(str(target), broken)
    assert target.read_text(encoding="utf-8") == '{"old": true}'
    assert not (tmp_path / "backtesting_log.json.tmp").exists()


def test_write_replaces_the_file(tmp_path):
    target = tmp_path / "x.csv"
    target.write_text("old", encoding="utf-8")
    write_atomically(str(target), lambda tmp: open(tmp, "w", encoding="utf-8").write("new"))
    assert target.read_text(encoding="utf-8") == "new"


def test_append_keeps_old_lines(tmp_path):
    target = tmp_path / "events.jsonl"
    append_lines_atomically(str(target), ['{"a": 1}'])
    append_lines_atomically(str(target), ['{"b": 2}', '{"c": 3}'])
    assert target.read_text(encoding="utf-8").splitlines() == ['{"a": 1}', '{"b": 2}', '{"c": 3}']
