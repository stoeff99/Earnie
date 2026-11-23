"""Cancelling a Szenario-Explorer run: marker file, worker check, subprocess runner."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from simulation import backtesting_progress as bp
from ui import backtesting_runner as runner


@pytest.fixture
def progress_dir(tmp_path) -> str:
    return str(tmp_path / ".backtesting_progress")


def test_cancel_marker_round_trip(progress_dir):
    assert bp.cancel_requested(progress_dir) is False
    bp.request_cancel(progress_dir)
    assert bp.cancel_requested(progress_dir) is True
    worker = bp.worker_progress_path(progress_dir, "live")
    assert bp.worker_cancel_requested(worker) is True
    bp.clear_cancel_request(progress_dir)
    assert bp.cancel_requested(progress_dir) is False
    assert bp.worker_cancel_requested(worker) is False


def test_prepare_keeps_an_early_cancel_and_clear_removes_it(progress_dir):
    bp.request_cancel(progress_dir)
    bp.prepare_progress_dir(progress_dir)
    assert bp.cancel_requested(progress_dir) is True
    bp.clear_progress_dir(progress_dir)
    assert bp.cancel_requested(progress_dir) is False


def test_request_cancel_without_progress_path_fails_loud():
    with pytest.raises(ValueError):
        bp.request_cancel(None)


def test_progress_printer_stops_the_worker_at_the_next_hour(progress_dir):
    from scripts.run_backtesting import _make_progress_printer

    bp.prepare_progress_dir(progress_dir)
    printer = _make_progress_printer("Live", bp.worker_progress_path(progress_dir, "live"))
    printer(1, 10)
    bp.request_cancel(progress_dir)
    with pytest.raises(bp.BacktestingCancelled):
        printer(2, 10)


def _fake_command(monkeypatch, code: str) -> None:
    monkeypatch.setattr(
        runner,
        "build_backtesting_command",
        lambda **_kwargs: [sys.executable, "-c", code],
    )


def test_cancelled_subprocess_ends_with_the_cancel_exit_code(progress_dir, monkeypatch):
    marker = Path(progress_dir) / bp.CANCEL_MARKER_NAME
    # Stands in for run_backtesting: waits for the marker like its workers, then exits 130.
    code = (
        "import os, sys, time\n"
        f"marker = {str(marker)!r}\n"
        "for _ in range(600):\n"
        "    if os.path.exists(marker):\n"
        "        print('abgebrochen'); sys.exit(130)\n"
        "    time.sleep(0.05)\n"
        "sys.exit(1)\n"
    )
    _fake_command(monkeypatch, code)
    run = runner.start_backtesting_subprocess(progress_file=progress_dir, output_dir=progress_dir, references=())
    assert run.running()
    runner.cancel_backtesting_run(run)
    assert run.cancel_requested is True
    exit_code, output = runner.finish_backtesting_run(run)
    assert exit_code == bp.EXIT_CANCELLED
    assert "abgebrochen" in output
    assert not marker.exists()


def test_start_clears_a_stale_cancel_marker(progress_dir, monkeypatch):
    bp.request_cancel(progress_dir)
    marker = Path(progress_dir) / bp.CANCEL_MARKER_NAME
    _fake_command(monkeypatch, f"import os, sys; sys.exit(130 if os.path.exists({str(marker)!r}) else 0)")
    run = runner.start_backtesting_subprocess(progress_file=progress_dir, output_dir=progress_dir, references=())
    assert runner.finish_backtesting_run(run) == (0, "")


def test_run_backtesting_subprocess_still_blocks_and_reports(progress_dir, monkeypatch):
    _fake_command(monkeypatch, "print('fertig')")
    seen: list[dict] = []
    exit_code, output = runner.run_backtesting_subprocess(
        progress_file=progress_dir, output_dir=progress_dir, references=(), on_progress=seen.append
    )
    assert exit_code == 0
    assert "fertig" in output


def test_parallel_run_stops_with_cancelled_not_a_failure(monkeypatch):
    """A cancelled worker ends the pool and the run as cancelled, not as a failed scenario."""
    from concurrent.futures import ThreadPoolExecutor

    import pandas as pd

    from scripts import run_backtesting as rb

    def _worker(name, *_args, **_kwargs):
        raise bp.BacktestingCancelled(f"{name}: abgebrochen")

    monkeypatch.setattr(rb, "ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr(rb, "_run_scenario_worker", _worker)
    with pytest.raises(bp.BacktestingCancelled):
        rb._run_parallel_backtesting(
            [],
            {"a": {}, "b": {}},
            {},
            pd.Timestamp("2026-01-01"),
            pd.Timestamp("2026-01-31"),
            None,
            2,
            "sunrise_window",
            "perfect",
            None,
            None,
            progress_file=None,
            ladder_specs=[],
        )


def _parallel_run_with_failing_reference(monkeypatch, progress_file):
    import time
    from concurrent.futures import ThreadPoolExecutor

    import pandas as pd

    from scripts import run_backtesting as rb

    def _failing_reference(*_args, **_kwargs):
        raise ValueError("Ziel nicht lieferbar")

    def _long_scenario(name, *args, **_kwargs):
        progress = args[8]
        for _ in range(500):
            if bp.cancel_requested(progress):
                raise bp.BacktestingCancelled(f"{name}: abgebrochen")
            time.sleep(0.01)
        raise AssertionError("scenario was not stopped")

    monkeypatch.setattr(rb, "ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr(rb, "_run_reference_worker", _failing_reference)
    monkeypatch.setattr(rb, "_run_scenario_worker", _long_scenario)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="Referenz 'L1' .*Ziel nicht lieferbar"):
        rb._run_parallel_backtesting(
            [("l1:live", {}, "L1", "l1_live")],
            {"live": {}},
            {"live": "Live"},
            pd.Timestamp("2026-01-01"),
            pd.Timestamp("2026-01-31"),
            None,
            2,
            "sunrise_window",
            "perfect",
            None,
            None,
            progress_file=progress_file,
            ladder_specs=[],
        )
    return time.monotonic() - started


def test_failed_task_stops_the_other_workers_at_once(progress_dir, monkeypatch):
    """Regression: a reference failing after a minute surfaced only when Live had finished."""
    elapsed = _parallel_run_with_failing_reference(monkeypatch, progress_dir)
    assert elapsed < 3.0
    assert bp.cancel_requested(progress_dir) is False  # the next run must not start cancelled


def test_failed_task_stops_the_others_without_a_progress_file(monkeypatch):
    assert _parallel_run_with_failing_reference(monkeypatch, None) < 3.0


def _fake_proc(root: Path, pid: int, cmdline: str, state: str = "S (sleeping)") -> Path:
    proc = root / str(pid)
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())
    (proc / "status").write_text(f"Name:\tpython\nState:\t{state}\n", encoding="utf-8")
    return proc


@pytest.fixture
def fake_proc_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(runner, "_PROC_ROOT", root)
    monkeypatch.setattr(runner, "_reap", lambda _pid: None)
    return root


def _write_pid(progress_dir: str, pid: int) -> Path:
    path = Path(progress_dir) / runner.RUN_PID_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")
    return path


def test_leftover_run_stops_on_the_cancel_marker(progress_dir, fake_proc_root, monkeypatch):
    import shutil

    proc = _fake_proc(fake_proc_root, 4242, "python scripts/run_backtesting.py --workers 5")
    pid_file = _write_pid(progress_dir, 4242)

    def _sleep(_sec):  # the leftover run sees the marker and ends
        if bp.cancel_requested(progress_dir):
            shutil.rmtree(proc, ignore_errors=True)

    monkeypatch.setattr(runner.time, "sleep", _sleep)
    monkeypatch.setattr(runner.os, "killpg", lambda *_a: pytest.fail("no signal needed"), raising=False)
    assert runner.stop_leftover_backtesting_run(progress_dir) is True
    assert not pid_file.exists()
    assert bp.cancel_requested(progress_dir) is False


def test_leftover_run_ignoring_cancel_gets_terminated(progress_dir, fake_proc_root, monkeypatch):
    import shutil
    import signal

    proc = _fake_proc(fake_proc_root, 4243, "python -m scripts.run_backtesting")
    _write_pid(progress_dir, 4243)
    sent: list[tuple[int, int]] = []

    def _killpg(pid, sig):
        sent.append((pid, sig))
        shutil.rmtree(proc, ignore_errors=True)

    monkeypatch.setattr(runner, "_LEFTOVER_CANCEL_WAIT_SEC", 0.05)
    monkeypatch.setattr(runner.time, "sleep", lambda _sec: None)
    monkeypatch.setattr(runner.os, "killpg", _killpg, raising=False)
    assert runner.stop_leftover_backtesting_run(progress_dir) is True
    assert sent == [(4243, signal.SIGTERM)]


def test_pid_reused_by_another_process_is_left_alone(progress_dir, fake_proc_root, monkeypatch):
    _fake_proc(fake_proc_root, 4244, "/usr/bin/sshd -D")
    pid_file = _write_pid(progress_dir, 4244)
    monkeypatch.setattr(runner.os, "killpg", lambda *_a: pytest.fail("must not signal"), raising=False)
    assert runner.stop_leftover_backtesting_run(progress_dir) is False
    assert not pid_file.exists()


def test_finished_leftover_run_zombie_is_not_signalled(progress_dir, fake_proc_root, monkeypatch):
    _fake_proc(fake_proc_root, 4245, "python scripts/run_backtesting.py", state="Z (zombie)")
    _write_pid(progress_dir, 4245)
    monkeypatch.setattr(runner.os, "killpg", lambda *_a: pytest.fail("must not signal"), raising=False)
    assert runner.stop_leftover_backtesting_run(progress_dir) is False


def test_started_run_records_its_pid_and_finish_removes_it(progress_dir, monkeypatch):
    _fake_command(monkeypatch, "print('ok')")
    run = runner.start_backtesting_subprocess(
        progress_file=progress_dir, output_dir=progress_dir, references=()
    )
    pid_file = Path(progress_dir) / runner.RUN_PID_FILENAME
    assert pid_file.read_text(encoding="utf-8") == str(run.proc.pid)
    runner.finish_backtesting_run(run)
    assert not pid_file.exists()
