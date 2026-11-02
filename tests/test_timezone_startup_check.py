"""Startup check: container local time must match the planning timezone."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from scripts import startup_checks as sc

_NOW = datetime(2026, 11, 2, 5, 19, tzinfo=UTC)


def _zone_offset(name: str):
    return lambda ts: datetime.fromtimestamp(ts, ZoneInfo(name)).utcoffset()


def test_matching_zone_passes():
    assert sc.timezone_mismatch(
        "Europe/Zurich", now=_NOW, system_offset=_zone_offset("Europe/Vienna")
    ) is None


def test_container_on_utc_is_reported():
    """Real case 2026-11-02: `date` in the container printed UTC, planning in Europe/Zurich."""
    message = sc.timezone_mismatch(
        "Europe/Zurich", now=_NOW, system_offset=lambda _ts: timedelta(0)
    )
    assert message is not None
    assert "UTC+00:00" in message and "UTC+01:00" in message
    assert "TZ: Europe/Zurich" in message


def test_zone_without_daylight_saving_is_reported_half_a_year_later():
    # Fixed UTC+1 matches Zurich in November but not in summer.
    message = sc.timezone_mismatch(
        "Europe/Zurich", now=_NOW, system_offset=lambda _ts: timedelta(hours=1)
    )
    assert message is not None and "2027-05-03" in message


def test_startup_logs_an_error_on_mismatch(monkeypatch, caplog):
    import config

    monkeypatch.setattr(config, "get_planning_timezone", lambda: "Europe/Zurich")
    monkeypatch.setattr(sc, "_system_utc_offset", lambda _ts: timedelta(0))
    with caplog.at_level(logging.ERROR):
        sc.run_timezone_check_on_startup()
    assert "Zeitzonen-Prüfung" in caplog.text
