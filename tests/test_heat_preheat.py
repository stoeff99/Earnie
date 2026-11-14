"""Pre-heating across midnight: the building stores heat for the next day's target."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pulp
import pytest

from house_config.planning_flex_bridge import planning_thermal_to_milp
from house_config.profiles_thermal_annual import preheat_storage_kwh
from optimizer.thermal_flex_context import add_cumulative_heat_targets


def _two_day_lp(storage_kwh: float) -> tuple[float, float]:
    prob = pulp.LpProblem("preheat", pulp.LpMinimize)
    day1 = pulp.LpVariable("d1", lowBound=0, upBound=10)
    day2 = pulp.LpVariable("d2", lowBound=0, upBound=10)
    prob += 1.0 * day1 + 3.0 * day2  # day 1 cheap (PV), day 2 expensive
    add_cumulative_heat_targets(
        prob, [(day1, 4.0), (day2, 4.0)], storage_kwh=storage_kwh, prefix="t"
    )
    assert pulp.LpStatus[prob.solve(pulp.PULP_CBC_CMD(msg=False))] == "Optimal"
    return day1.varValue, day2.varValue


def test_cumulative_targets_move_heat_forward_up_to_storage():
    assert _two_day_lp(0.0) == pytest.approx((4.0, 4.0))  # plain per-day rule
    assert _two_day_lp(2.0) == pytest.approx((6.0, 2.0))
    assert _two_day_lp(50.0) == pytest.approx((8.0, 0.0))


def test_heat_never_comes_late():
    prob = pulp.LpProblem("late", pulp.LpMinimize)
    day1 = pulp.LpVariable("d1", lowBound=0, upBound=10)
    day2 = pulp.LpVariable("d2", lowBound=0, upBound=10)
    prob += 3.0 * day1 + 1.0 * day2  # day 1 expensive: still must cover day 1
    add_cumulative_heat_targets(prob, [(day1, 4.0), (day2, 4.0)], storage_kwh=5.0, prefix="t")
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    assert (day1.varValue, day2.varValue) == pytest.approx((4.0, 4.0))


def _wp(**extra) -> dict:
    return {
        "id": "waermepumpe",
        "type": "thermal_annual",
        "nominal_power_kw": 3.0,
        "living_area_m2": 145.0,
        "building_mass": "mittel",
        "house_tolerance_c": 0.5,
        "heat_pump_type": "erde",
        **extra,
    }


def test_storage_capacity_of_the_house():
    from data.heating_need import heat_pump_jaz

    # 145 m² × 90 Wh/m²K = 13.05 kWh/K; × 0.5 K; ÷ JAZ.
    expected = round(13.05 * 0.5 / heat_pump_jaz("erde"), 3)
    assert preheat_storage_kwh(_wp(heat_preheat_enabled=True)) == pytest.approx(expected)
    assert preheat_storage_kwh(_wp()) == 0.0  # opt-in
    assert planning_thermal_to_milp(_wp(heat_preheat_enabled=True))[
        "preheat_storage_kwh"
    ] == pytest.approx(expected)
    assert planning_thermal_to_milp(_wp())["preheat_storage_kwh"] == 0.0


def test_preheat_flag_survives_save():
    from house_config.profiles_store import _normalize_consumer, _serialize_consumer

    raw = {
        "id": "waermepumpe",
        "label": "Waermepumpe",
        "type": "thermal_annual",
        "nominal_power_kw": 3.0,
        "living_area_m2": 145.0,
        "building_class": 3,
        "heat_preheat_enabled": True,
    }
    saved = _serialize_consumer(_normalize_consumer(raw, 0, "house"))
    assert saved["heat_preheat_enabled"] is True
    assert planning_thermal_to_milp(saved)["preheat_storage_kwh"] > 0


def _solve_two_days(consumer: dict) -> list[float]:
    """Day 1 afternoon cheap, day 2 morning expensive; equal heat need per day."""
    from optimizer.cbc_solver import solve_with_strict_fallback
    from optimizer.milp import _add_milp_objective
    from optimizer.milp_consumers import filter_feasible_consumers
    from optimizer.milp_horizon import _build_milp_model
    from optimizer.thermal_flex_context import add_thermal_flex_constraints

    start = datetime(2026, 11, 14, 22, 0)
    n = 16  # 22:00–24:00 on day 1, 00:00–02:00 on day 2 (8 + 8 slots)
    matrix = []
    for index in range(n):
        slot = start + timedelta(minutes=15 * index)
        matrix.append(
            {
                "hour": slot.hour,
                "date": slot.date(),
                "slot_datetime": slot,
                "k_act": 10.0 if index < 8 else 40.0,
                "price_buy": 10.0 if index < 8 else 40.0,
                "expected_p_act": 0.5,
                "expected_p_pv": 0.0,
                "consumption_mode": "live_snapshot",
            }
        )
    day1, day2 = date(2026, 11, 14), date(2026, 11, 15)
    contexts = {
        consumer["id"]: {
            "daily_targets": {day1: 1.5, day2: 1.5},
            "slot_energy_kwh": [1.5 / 8] * n,
            "targets_slot_aligned": True,
        }
    }
    battery = {
        "min_soc": 10.0,
        "max_soc": 10.0,
        "max_power_kw": 0.0,
        "battery_capacity_kwh": 10.0,
        "efficiency": 0.95,
    }
    remaining = {consumer["id"]: 3.0}
    planned = filter_feasible_consumers(
        [consumer], remaining, matrix, list(range(n)), False, {}, {}
    )
    model = _build_milp_model(matrix, n, battery, 10.0, planned, 0.0, remaining, {})
    _add_milp_objective(model, matrix, 3.5, {}, wear_cent_per_kwh=0.0)
    add_thermal_flex_constraints(model, matrix, list(range(n)), contexts)
    assert solve_with_strict_fallback(model.prob, msg=False) == "Optimal"
    kw = consumer["nominal_power_kw"]
    return [(var.varValue or 0.0) * kw * 0.25 for var in model.consumer_on[consumer["id"]]]


def test_milp_preheats_in_cheap_evening_for_next_day():
    base = {
        "id": "waermepumpe",
        "label": "Wärmepumpe",
        "type": "thermal_annual",
        "nominal_power_kw": 3.0,
        "min_on_quarterhours": 1,
        "max_on_quarterhours": 16,
        "max_pulses_per_day": 14,
        "living_area_m2": 145.0,
        "building_mass": "mittel",
        "house_tolerance_c": 0.5,
        "heat_pump_type": "erde",
    }
    plain = _solve_two_days(planning_thermal_to_milp(base))
    assert sum(plain[8:]) == pytest.approx(1.5)  # day 2 must heat itself
    preheat = _solve_two_days(planning_thermal_to_milp({**base, "heat_preheat_enabled": True}))
    assert sum(preheat[:8]) > 1.5 + 0.7  # part of day 2's heat delivered cheap on day 1
    assert sum(preheat[8:]) < 1.5 - 0.7
