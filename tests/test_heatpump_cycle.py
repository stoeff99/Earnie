"""Cycling on/off heat pump: mandatory pause (min_off) in the MILP and live cycle memory."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from house_config.planning_flex_bridge import planning_thermal_to_milp
from optimizer import heatpump_cycle
from optimizer.thermal_flex_context import _max_on_slots_cycling

_DAY = date(2026, 11, 12)


def _wp(**overrides) -> dict:
    raw = {
        "id": "waermepumpe",
        "label": "Wärmepumpe",
        "type": "thermal_annual",
        "nominal_power_kw": 3.0,
        "min_on_quarterhours": 1,
        "max_on_quarterhours": 3,
        "min_off_quarterhours": 4,
        "max_pulses_per_day": 14,
    }
    raw.update(overrides)
    return planning_thermal_to_milp(raw)


def _solve(consumer: dict, n: int, ctx: dict, *, want_kwh: float = 100.0) -> list[int]:
    """Solve a flat-price horizon; the default want exceeds what the cycle allows."""
    from optimizer.cbc_solver import solve_with_strict_fallback
    from optimizer.milp import _add_milp_objective
    from optimizer.milp_consumers import filter_feasible_consumers
    from optimizer.milp_horizon import _build_milp_model
    from optimizer.thermal_flex_context import add_thermal_flex_constraints

    matrix = [
        {
            "hour": (index // 4) % 24,
            "date": _DAY,
            "slot_datetime": datetime.combine(_DAY, time(0)) + timedelta(minutes=15 * index),
            "k_act": 20.0,
            "price_buy": 0.20,
            "expected_p_act": 0.5,
            "expected_p_pv": 0.0,
            "consumption_mode": "live_snapshot",
        }
        for index in range(n)
    ]
    contexts = {
        consumer["id"]: {
            "daily_targets": {_DAY: want_kwh},
            "slot_energy_kwh": [want_kwh / n] * n,
            "targets_slot_aligned": True,
            **ctx,
        }
    }
    battery = {
        "min_soc": 10.0,
        "max_soc": 100.0,
        "max_power_kw": 5.0,
        "battery_capacity_kwh": 10.0,
        "efficiency": 0.95,
    }
    remaining = {consumer["id"]: want_kwh}
    planned = filter_feasible_consumers(
        [consumer], remaining, matrix, list(range(n)), False, {}, {}
    )
    model = _build_milp_model(matrix, n, battery, 50.0, planned, 0.0, remaining, {})
    _add_milp_objective(model, matrix, 3.5, {}, wear_cent_per_kwh=0.0)
    add_thermal_flex_constraints(model, matrix, list(range(n)), contexts)
    assert solve_with_strict_fallback(model.prob, msg=False) == "Optimal"
    return [
        1 if (var.varValue or 0.0) > 0.5 else 0 for var in model.consumer_on[consumer["id"]]
    ]


def _runs_and_gaps(on: list[int]) -> tuple[list[int], list[int]]:
    runs: list[int] = []
    gaps: list[int] = []
    index = 0
    while index < len(on):
        start = index
        while index < len(on) and on[index] == on[start]:
            index += 1
        (runs if on[start] else gaps).append(index - start)
    # Leading/trailing OFF stretches are not pauses between runs.
    if 1 not in on:
        return runs, []
    first_on = on.index(1)
    last_on = len(on) - 1 - on[::-1].index(1)
    inner = on[first_on : last_on + 1]
    inner_gaps: list[int] = []
    index = 0
    while index < len(inner):
        start = index
        while index < len(inner) and inner[index] == inner[start]:
            index += 1
        if not inner[start]:
            inner_gaps.append(index - start)
    return runs, inner_gaps


# --- MILP ----------------------------------------------------------------------------------


def test_cycle_cap_is_greedy_run_max_pause_min():
    assert _max_on_slots_cycling(28, max_hours=3, min_off=4, max_pulses=14) == 12
    assert _max_on_slots_cycling(28, max_hours=3, min_off=4, max_pulses=2) == 6
    assert _max_on_slots_cycling(10, max_hours=3, min_off=4, max_pulses=14, pause_left=3) == 3
    assert _max_on_slots_cycling(10, max_hours=3, min_off=4, max_pulses=14, run_left=1) == 1 + 3


def test_milp_keeps_mandatory_pause_and_reaches_cycle_cap():
    on = _solve(_wp(), 28, {})
    runs, gaps = _runs_and_gaps(on)
    assert sum(on) == 12  # the cap is exactly what the cycle can deliver
    assert all(1 <= run <= 3 for run in runs)
    assert all(gap >= 4 for gap in gaps)


def test_carry_in_pause_blocks_first_slots_and_beats_forced_floor():
    on = _solve(
        _wp(),
        12,
        {"cycle_carry_in": {"running": False, "pause_left_slots": 3}, "forced_indices": [1, 5]},
    )
    assert on[:3] == [0, 0, 0]
    assert on[5] == 1


def test_carry_in_running_ends_run_and_starts_pause():
    on = _solve(_wp(), 12, {"cycle_carry_in": {"running": True, "run_elapsed_slots": 2}})
    # One slot of the 45-min run is left, then the 60-min lock.
    assert on[1:5] == [0, 0, 0, 0]


def test_without_min_off_no_pause_constraint():
    # 9 of 12 slots at 0.75 kWh each only fit with 1-slot gaps between 45-min runs.
    on = _solve(_wp(min_off_quarterhours=0), 12, {}, want_kwh=6.75)
    _, gaps = _runs_and_gaps(on)
    assert gaps and min(gaps) == 1


# --- config --------------------------------------------------------------------------------


def test_normalize_keeps_cycle_fields_and_rejects_max_below_min():
    from house_config.profiles_thermal_annual import normalize_thermal_annual_consumer

    spec: dict = {}
    raw = {
        "id": "waermepumpe",
        "nominal_power_kw": 3.0,
        "min_on_quarterhours": 1,
        "max_on_quarterhours": 3,
        "min_off_quarterhours": 4,
    }
    normalize_thermal_annual_consumer(raw, spec, copy_loxone_binding=lambda *_: None)
    assert spec["max_on_quarterhours"] == 3
    assert spec["min_off_quarterhours"] == 4
    with pytest.raises(ValueError, match="kleiner als min_on_quarterhours"):
        normalize_thermal_annual_consumer(
            {**raw, "min_on_quarterhours": 4}, {}, copy_loxone_binding=lambda *_: None
        )
    assert _wp()["min_off_quarterhours"] == 4


# --- live cycle memory ---------------------------------------------------------------------


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "heatpump_cycle_state.json"
    monkeypatch.setattr(heatpump_cycle, "_state_path", lambda: str(path))
    return path


def _at(hour: int, minute: int) -> datetime:
    return datetime.combine(_DAY, time(hour, minute))


def test_pause_left_after_observed_stop(state_file):
    wp = _wp()
    heatpump_cycle.record_observation("waermepumpe", True, _at(10, 0))
    heatpump_cycle.record_observation("waermepumpe", False, _at(10, 30))
    heatpump_cycle.record_observation("waermepumpe", False, _at(10, 45))
    carry = heatpump_cycle.carry_in_for(wp, _at(10, 45), dt_h=0.25)
    assert carry == {"running": False, "pause_left_slots": 3}
    heatpump_cycle.record_observation("waermepumpe", False, _at(11, 30))
    assert heatpump_cycle.carry_in_for(wp, _at(11, 30), dt_h=0.25) == {}


def test_running_elapsed_and_unknown_or_stale_state(state_file):
    wp = _wp()
    # First observation OFF: stop time unknown → no pause carry-in.
    heatpump_cycle.record_observation("waermepumpe", False, _at(9, 0))
    assert heatpump_cycle.carry_in_for(wp, _at(9, 0), dt_h=0.25) == {}
    heatpump_cycle.record_observation("waermepumpe", True, _at(9, 15))
    heatpump_cycle.record_observation("waermepumpe", True, _at(9, 45))
    assert heatpump_cycle.carry_in_for(wp, _at(9, 45), dt_h=0.25) == {
        "running": True,
        "run_elapsed_slots": 2,
    }
    # Daemon down for an hour: observation too old.
    assert heatpump_cycle.carry_in_for(wp, _at(10, 45), dt_h=0.25) == {}
    # Non-cycling consumers never carry in.
    assert heatpump_cycle.carry_in_for(_wp(min_off_quarterhours=0), _at(9, 45), dt_h=0.25) == {}


def test_observe_records_only_readable_cycling_consumers(state_file, monkeypatch, caplog):
    readings = {"waermepumpe": True, "blind": None}
    monkeypatch.setattr(heatpump_cycle, "read_running", lambda c: readings[c["id"]])
    consumers = [_wp(), {**_wp(), "id": "blind"}, {**_wp(), "id": "plain", "min_off_quarterhours": 0}]
    heatpump_cycle.observe_live_consumers(consumers, _at(8, 0))
    state = heatpump_cycle.load_state()
    assert set(state) == {"waermepumpe"}
    assert state["waermepumpe"]["running"] is True
    assert "Taktgedächtnis inaktiv" in caplog.text
