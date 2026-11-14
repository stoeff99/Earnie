"""Regression: slot 0 must carry the measured PV per system, not the forecast.

The MILP power balance reads PV per system (``house_balance_pv_kw`` → inverter model).
The live snapshot used to overwrite only the total, so on 2026-11-14 the optimizer
planned 12:15 with 3.48 kW forecast instead of 8.14 kW measured and force-discharged
the battery into the grid (and saw a surplus in fog that was not there).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from data.live_consumption import apply_live_snapshot_to_matrix, live_pv_by_system
from optimizer.milp_inverter import house_balance_pv_kw

_INVERTERS = [
    {
        "id": "haupt_wechselrichter",
        "type": "hybrid",
        "ac_connection": "grid_tied",
        "efficiency": 0.97,
        "max_ac_power_kw": 12.0,
        "max_pv_input_kw": 12.76,
    },
    {
        "id": "batterie_wechselrichter",
        "type": "battery",
        "ac_connection": "grid_tied",
        "efficiency": 0.91,
        "max_ac_power_kw": 6.0,
        "max_ac_charge_kw": 6.0,
    },
]
_PV = [{"id": "dach_sued", "kwp": 12.76, "inverter_id": "haupt_wechselrichter"}]


@pytest.fixture(autouse=True)
def _plant(monkeypatch):
    from optimizer import milp_defaults

    monkeypatch.setattr(milp_defaults, "_default_inverter_params", lambda: list(_INVERTERS))
    monkeypatch.setattr(milp_defaults, "_default_pv_systems", lambda: list(_PV))


def _matrix(forecast_kw: float) -> list[dict]:
    return [
        {
            "slot_datetime": datetime(2026, 11, 14, 12, 15),
            "expected_p_pv": forecast_kw,
            "expected_p_pv_by_system": {"dach_sued": forecast_kw},
            "expected_p_act": 0.5,
        }
    ]


def _snapshot(pv_kw: float) -> dict:
    return {"pv_kw": pv_kw, "baseload_kw": 1.15, "house_kw": 1.15, "flex_kw": {}}


@pytest.mark.parametrize(("forecast_kw", "live_kw"), [(3.48, 8.14), (9.129, 1.21)])
def test_power_balance_uses_measured_pv_in_slot_zero(forecast_kw, live_kw):
    row = apply_live_snapshot_to_matrix(_matrix(forecast_kw), _snapshot(live_kw))[0]
    assert row["expected_p_pv"] == live_kw
    # Measured PV is AC: stored DC-side (÷ η 0.97) so the model reproduces it exactly.
    assert row["expected_p_pv_by_system"]["dach_sued"] == pytest.approx(live_kw / 0.97)
    assert house_balance_pv_kw(row, inverters=_INVERTERS, pv_systems=_PV) == pytest.approx(
        live_kw
    )


def test_forecast_rows_stay_untouched():
    matrix = _matrix(3.48) + _matrix(6.0)
    updated = apply_live_snapshot_to_matrix(matrix, _snapshot(8.14))
    assert updated[1]["expected_p_pv_by_system"] == {"dach_sued": 6.0}
    assert matrix[0]["expected_p_pv_by_system"] == {"dach_sued": 3.48}  # input not mutated


def test_measured_ac_conversion_without_inverters_and_unknown_system():
    from optimizer.milp_inverter import measured_ac_to_model_pv_by_system

    assert measured_ac_to_model_pv_by_system({"x": 2.0}, inverters=[], pv_systems=[]) == {"x": 2.0}
    with pytest.raises(ValueError, match="unbekannte PV-id"):
        measured_ac_to_model_pv_by_system({"x": 2.0}, inverters=_INVERTERS, pv_systems=_PV)


def test_several_systems_split_by_forecast_share():
    assert live_pv_by_system({"sued": 3.0, "west": 1.0}, 8.0) == {"sued": 6.0, "west": 2.0}
    assert live_pv_by_system({"sued": 0.0, "west": 0.0}, 2.0) == {"sued": 1.0, "west": 1.0}
    assert live_pv_by_system({"sued": 2.0}, -0.1) == {"sued": 0.0}
