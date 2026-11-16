"""AppTest: Monitor panel „Ladeauftrag (einmalig)“ — set, show and cancel a one-off charge job."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import config
from runtime_store import charge_overrides as store
from runtime_store import optimize_request

_SCRIPT = Path(__file__).parent / "scripts" / "run_form_charge_override.py"

_EV = {
    "id": "wallbox",
    "name": "Wallbox",
    "nominal_power_kw": 11.0,
    "daily_target_source": "config",
    "charging_schedule": {
        "enabled": True,
        "target_soc_percent": 70.0,
        "weekday": {"car_available_from_hour": 23, "ready_by_hour": 3, "daily_rest_soc": 1.0},
        "weekend": {"car_available_from_hour": 0, "ready_by_hour": 3, "daily_rest_soc": 1.0},
    },
}


@pytest.fixture
def ev_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("EARNIE_RUNTIME_PATH", str(tmp_path))
    monkeypatch.setattr(config, "get_flexible_consumers", lambda optimizer_only=False: [dict(_EV)])
    calls: list[int] = []
    monkeypatch.setattr(optimize_request, "request_optimize", lambda **_kw: calls.append(1))
    return calls


def test_panel_hidden_without_a_config_ev(monkeypatch, tmp_path):
    monkeypatch.setenv("EARNIE_RUNTIME_PATH", str(tmp_path))
    monkeypatch.setattr(config, "get_flexible_consumers", lambda optimizer_only=False: [])
    at = AppTest.from_file(str(_SCRIPT)).run()
    assert not at.exception
    assert not at.expander


def test_set_job_writes_it_and_requests_a_replan(ev_setup):
    at = AppTest.from_file(str(_SCRIPT)).run()
    assert not at.exception
    assert any("Kein Ladeauftrag" in c.value for c in at.caption)
    tomorrow = (datetime.now() + timedelta(days=1)).date()
    at.number_input(key="charge_override_soc_wallbox").set_value(90.0)
    at.date_input(key="charge_override_day_wallbox").set_value(tomorrow)
    at.button[0].click().run()
    assert not at.exception
    job = store.get_override("wallbox")
    assert job is not None and job.target_soc_percent == 90.0 and job.ready_by.date() == tomorrow
    assert ev_setup == [1]
    assert any("Ladeauftrag gespeichert" in s.value for s in at.success)


def test_past_ready_by_shows_an_error_and_writes_nothing(ev_setup):
    at = AppTest.from_file(str(_SCRIPT)).run()
    yesterday = (datetime.now() - timedelta(days=1)).date()
    at.date_input(key="charge_override_day_wallbox").set_value(yesterday)
    at.button[0].click().run()
    assert any("Zukunft" in e.value for e in at.error)
    assert store.get_override("wallbox") is None
    assert ev_setup == []


def test_cancel_removes_the_job(ev_setup):
    store.set_override(
        "wallbox", target_soc_percent=85, ready_by=datetime.now() + timedelta(hours=10)
    )
    at = AppTest.from_file(str(_SCRIPT)).run()
    assert any("Ladeauftrag aktiv: Ziel 85 %" in c.value for c in at.caption)
    cancel = next(b for b in at.button if b.label == "Ladeauftrag aufheben")
    cancel.click().run()
    assert store.get_override("wallbox") is None
    assert any("aufgehoben" in s.value for s in at.success)
