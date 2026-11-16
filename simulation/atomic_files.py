"""Atomic writes for the SE result files (``backtesting_log.json``, CSVs, JSONL sidecars).

A run killed while it writes its results (container stopped, OOM, browser session gone)
must leave either the previous complete file or the new one, never half a file that the
Scenario Explorer then fails to read. Each file is written next to its target and then
swapped in with ``os.replace``.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

logger = logging.getLogger(__name__)

_REPLACE_RETRIES = 10
_REPLACE_RETRY_DELAY_SEC = 0.2


def _replace_blocked(exc: OSError) -> bool:
    """True if another process holds the target open (Windows / SMB), so a retry may help."""
    if getattr(exc, "errno", None) in (13, 16):
        return True
    return getattr(exc, "winerror", None) in (5, 32)


def write_atomically(path: str, write: Callable[[str], None]) -> None:
    """Write ``path`` via ``write(tmp_path)`` and swap it in with one ``os.replace``.

    If the target stays blocked (Windows: the UI is reading it), the file is written in
    place after the retries and a warning names it.
    """
    tmp = f"{path}.tmp"
    try:
        write(tmp)
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except OSError as exc:
                if not _replace_blocked(exc):
                    raise
                if attempt + 1 < _REPLACE_RETRIES:
                    time.sleep(_REPLACE_RETRY_DELAY_SEC)
        logger.warning(
            "SE-Ergebnis %s ist von einem anderen Prozess geöffnet und wird nicht atomar, "
            "sondern direkt geschrieben.",
            path,
        )
        write(path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def append_lines_atomically(path: str, lines: list[str]) -> None:
    """Append ``lines`` (without newline) to a JSONL file: old content plus new, swapped in."""

    def write(tmp: str) -> None:
        with open(tmp, "w", encoding="utf-8") as out:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as existing:
                    for chunk in iter(lambda: existing.read(1 << 20), ""):
                        out.write(chunk)
            for line in lines:
                out.write(line + "\n")

    write_atomically(path, write)
