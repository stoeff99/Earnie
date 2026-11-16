"""One-off charge job (Ladeauftrag): store, expiry and its effect on the Live charging context."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from optimizer import charging_context as cc
from runtime_store import charge_overrides as store
from ui import charge_override_panel as panel

NOW = datetime(2026, 11, 16, 18, 0)


@pytest.fixture(autouse=True)
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("EARNIE_RUNTIME_PATH", str(tmp_path))
    return tmp_path


def _ev() -> dict:
    return {
        "id": "wallbox",
        "name": "Wallbox",
        "nominal_power_kw": 11.0,
        "daily_target_source": "config",
        "charging_schedule": {
            "enabled": True,
            "forecast_when_absent": False,
            "target_soc_percent": 70.0,
            "charging_efficiency": 1.0,
            "weekday": {"car_available_from_hour": 23, "ready_by_hour": 3, "daily_rest_soc": 1.0},
            "weekend": {"car_available_from_hour": 0, "ready_by_hour": 3, "daily_rest_soc": 1.0},
        },
        "ehal_bindings": {
            "sens_evcs_connected": "EV_Da",
            "sens_evcs_soc_act": "EV_SOC",
        },
    }


def _matrix(start: datetime, hours: int = 36) -> list:
    return [
        {"slot_datetime": start + timedelta(hours=i), "hour": (start + timedelta(hours=i)).hour}
        for i in range(hours)
    ]


def _resolve(consumer: dict, *, plugged: int, soc: float = 40.0, start: datetime = NOW) -> dict:
    def _io(name: str):
        return {"EV_Da": plugged, "EV_SOC": soc}.get(name)

    with patch.object(cc.ehal_live, "read_generic_value", side_effect=_io), patch.object(
        cc, "_loxone_ready_raw", return_value=None
    ), patch.object(
        cc.ehal_live, "resolve_consumer_battery_capacity_kwh", return_value=75.0
    ), patch.object(cc, "resolve_get_evcs_limit_soc", return_value=70.0, create=True):
        return cc.resolve_charging_context(consumer, _matrix(start), None, logged_simulation=False)


# --- store ------------------------------------------------------------------------------------


def test_set_get_and_clear():
    job = store.set_override("wallbox", target_soc_percent=90, ready_by=NOW + timedelta(hours=14), now=NOW)
    assert store.get_override("wallbox") == job
    assert store.clear_override("wallbox", reason="test") is True
    assert store.get_override("wallbox") is None
    assert store.clear_override("wallbox", reason="test") is False


@pytest.mark.parametrize(
    ("soc", "ready_by", "message"),
    [(0, NOW + timedelta(hours=1), "Ziel-SOC"), (120, NOW + timedelta(hours=1), "Ziel-SOC"), (80, NOW, "Zukunft")],
)
def test_invalid_job_is_rejected(soc, ready_by, message):
    with pytest.raises(ValueError, match=message):
        store.set_override("wallbox", target_soc_percent=soc, ready_by=ready_by, now=NOW)
    assert store.get_override("wallbox") is None


def test_job_ends_at_its_ready_by_time():
    store.set_override("wallbox", target_soc_percent=90, ready_by=NOW + timedelta(hours=2), now=NOW)
    assert store.active_override("wallbox", now=NOW + timedelta(hours=1), plugged_in=True) is not None
    assert store.active_override("wallbox", now=NOW + timedelta(hours=2), plugged_in=True) is None
    assert store.get_override("wallbox") is None


def test_job_waits_for_the_car_and_ends_on_unplug():
    store.set_override("wallbox", target_soc_percent=90, ready_by=NOW + timedelta(hours=14), now=NOW)
    # Created while the car is away: unplugged does not end it yet.
    assert store.active_override("wallbox", now=NOW, plugged_in=False) is not None
    job = store.active_override("wallbox", now=NOW + timedelta(minutes=15), plugged_in=True)
    assert job is not None and job.plug_seen is True
    assert store.active_override("wallbox", now=NOW + timedelta(hours=1), plugged_in=False) is None
    assert store.get_override("wallbox") is None


def test_damaged_file_fails_loud(runtime):
    (runtime / store.OVERRIDES_FILENAME).write_text('{"schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="overrides"):
        store.get_override("wallbox")


# --- Live charging context ---------------------------------------------------------------------


def test_without_job_the_configured_window_applies():
    ctx = _resolve(_ev(), plugged=1)
    assert ctx["deadline"] == datetime(2026, 11, 17, 3, 0)
    assert "charge_override" not in ctx


def test_job_replaces_deadline_and_target_soc():
    store.set_override("wallbox", target_soc_percent=90, ready_by=datetime(2026, 11, 17, 7, 30), now=NOW)
    ctx = _resolve(_ev(), plugged=1, soc=40.0)
    assert ctx["active"] is True
    assert ctx["deadline"] == datetime(2026, 11, 17, 7, 30)
    assert ctx["use_time_window"] is False
    assert ctx["target_kwh"] == pytest.approx(0.5 * 75.0, abs=0.01)  # 40 % → 90 %
    assert ctx["charge_override"]["target_soc_percent"] == 90.0
    assert ctx["source_label"].startswith("Ladeauftrag (einmalig)")


def test_job_does_not_plan_while_the_car_is_away():
    store.set_override("wallbox", target_soc_percent=90, ready_by=datetime(2026, 11, 17, 7, 30), now=NOW)
    ctx = _resolve(_ev(), plugged=0)
    assert ctx["active"] is False
    assert store.get_override("wallbox") is not None  # still waiting for the car


def test_job_bypasses_the_fulfilled_plug_cycle_latch():
    contexts = {"wallbox": {"active": True, "plugged_in": True, "target_kwh": 10.0, "charge_override": {}}}
    out = cc.apply_plug_cycle_fulfilled_contexts(contexts, {"wallbox": True})
    assert out["wallbox"]["active"] is True


# --- Monitor panel helpers ---------------------------------------------------------------------


def test_panel_lists_only_config_driven_evs():
    loxone_ev = {**_ev(), "id": "lox", "daily_target_source": "loxone"}
    heat_pump = {"id": "wp", "daily_target_source": "thermal_annual"}
    assert [c["id"] for c in panel.override_consumers([_ev(), loxone_ev, heat_pump])] == ["wallbox"]


def test_panel_default_ready_by_is_the_next_configured_hour():
    assert panel.default_ready_by(_ev(), NOW) == datetime(2026, 11, 17, 3, 0)
    saturday_evening = datetime(2026, 11, 21, 20, 0)
    assert panel.default_ready_by(_ev(), saturday_evening) == datetime(2026, 11, 22, 3, 0)


def test_panel_status_text():
    assert "Kein Ladeauftrag" in panel.job_status_text(None)
    job = store.set_override("wallbox", target_soc_percent=90, ready_by=NOW + timedelta(hours=14), now=NOW)
    assert "wartet auf das Anstecken" in panel.job_status_text(job)
