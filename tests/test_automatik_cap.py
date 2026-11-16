"""Automatik discharge cap: keep battery energy the plan reserved for a more expensive slot."""
from __future__ import annotations

import pytest

from optimizer import battery as bat
from optimizer.automatik_cap import (
    AUTOMATIK_CAP_KEY,
    AUTOMATIK_CAP_REASON_KEY,
    DischargeCap,
    attach_to_milp_plan,
    attach_to_schedule,
    automatik_cap_for_mode,
    discharge_caps_from_plan,
    lowest_cap_kw,
)
from optimizer.consistency import check_mode_battery_alignment
from optimizer.sim_chart_rows import _chart_row_from_schedule_slot, finalize_chart_row_energy

THRESHOLD = 0.3
MAX_KW = 6.0
E_MAX = 18.0


def _caps(discharge, prices, energy=None, *, grid_charge=None, e_max=E_MAX, e_end=0.0):
    """Plan helper: battery half full, empty at the end unless ``energy`` says otherwise."""
    n = len(discharge)
    if energy is None:
        energy = [5.0] * (n - 1) + [e_end]
    return discharge_caps_from_plan(
        discharge,
        grid_charge if grid_charge is not None else [False] * n,
        prices,
        energy,
        energy_min_kwh=0.0,
        energy_max_kwh=e_max,
        efficiency=0.97,
        threshold_kw=THRESHOLD,
        max_discharge_kw=MAX_KW,
    )


# --- rule --------------------------------------------------------------------------------------


def test_midday_partial_discharge_is_capped_for_the_evening_peak():
    # 14:00 at 25.4 ct: battery 0.73 kW, rest from grid; the battery is next used at 31–32.5 ct.
    caps = _caps([0.73, 0.0, 0.5, 0.5], [25.4, 26.0, 32.5, 31.0])
    assert caps[0] == DischargeCap(1.03, 3, 31.0)


def test_slot_without_planned_discharge_gets_only_the_margin():
    # 13:15: plan charges from PV, no discharge; an unplanned heat pump may take 0.3 kW at most.
    caps = _caps([0.0, 0.5], [25.0, 32.5])
    assert caps[0].cap_kw == pytest.approx(THRESHOLD)


def test_cheapest_later_use_sets_the_value():
    # The plan still discharges at 25.4 ct later: a kWh taken now at 27 ct only replaces that.
    assert _caps([0.5, 0.5, 0.5], [27.0, 25.4, 33.0])[0] is None
    assert _caps([0.5, 0.5, 0.5], [24.0, 25.4, 33.0])[0].reserved_slot == 1


def test_most_expensive_slot_is_not_capped():
    caps = _caps([0.73, 0.5, 0.5], [25.4, 32.5, 31.0])
    assert caps[0] is not None
    assert caps[1] is None  # 32.5 ct: the battery's next use is cheaper
    assert caps[2] is None


def test_price_margin():
    assert _caps([0.5, 0.5], [30.0, 30.9])[0] is None
    assert _caps([0.5, 0.5], [30.0, 31.0])[0] is not None


def test_later_slot_must_discharge_above_the_threshold():
    assert _caps([0.5, 0.2], [25.0, 35.0])[0] is None


def test_later_grid_charge_caps_the_value():
    # Night grid charge at 15 ct (≈ 15.9 ct stored) before the 32 ct use: taking energy now at
    # 20 ct is cheaper than re-buying it → no cap; at 14 ct it is worth keeping.
    discharge = [0.5, 0.0, 0.5]
    grid = [False, True, False]
    assert _caps(discharge, [20.0, 15.0, 32.0], grid_charge=grid)[0] is None
    assert _caps(discharge, [14.0, 15.0, 32.0], grid_charge=grid)[0] is not None


def test_full_battery_before_the_expensive_slot_means_no_cap():
    # PV fills the battery at slot 1; the 32 ct discharge at slot 2 uses refilled energy.
    assert _caps([0.5, 0.0, 0.5], [27.0, 20.0, 32.0], [5.0, E_MAX, 0.0])[0] is None
    # Even a fill after the expensive slot means PV would have replaced the kWh: no cap.
    assert _caps([0.5, 0.5, 0.0], [27.0, 32.0, 20.0], [5.0, 4.5, E_MAX])[0] is None
    # Never full again: the cap holds.
    assert _caps([0.5, 0.5, 0.0], [27.0, 32.0, 20.0], [5.0, 4.5, 0.0])[0] is not None


def test_battery_full_at_the_end_of_this_slot_means_no_cap():
    assert _caps([0.5, 0.5], [25.0, 32.0], [E_MAX, 0.0])[0] is None


def test_energy_left_at_the_horizon_end_means_no_cap():
    assert _caps([0.5, 0.5], [25.0, 32.0], e_end=3.0)[0] is None


def test_cap_never_reaches_the_maximum():
    # Planned discharge plus margin at or above the limit: no cap (unchanged behaviour).
    assert _caps([5.8, 6.0], [25.0, 35.0])[0] is None


def test_mismatched_lists_fail_loud():
    with pytest.raises(ValueError, match="length"):
        discharge_caps_from_plan(
            [0.5], [False], [25.0, 30.0], [5.0],
            energy_min_kwh=0.0, energy_max_kwh=E_MAX, efficiency=0.97,
            threshold_kw=THRESHOLD, max_discharge_kw=MAX_KW,
        )


# --- plan / schedule plumbing ------------------------------------------------------------------


def test_attach_and_read_back():
    plan: dict = {}
    attach_to_milp_plan(plan, [DischargeCap(1.03, 12, 32.5), None])
    assert plan[AUTOMATIK_CAP_KEY] == 1.03
    assert plan[AUTOMATIK_CAP_REASON_KEY] == {"slot": 12, "price_cent": 32.5}
    assert automatik_cap_for_mode(bat.MODE_AUTOMATIK, plan) == 1.03
    assert automatik_cap_for_mode(bat.MODE_ENTLADESPERRE, plan) is None

    schedule = [{}, {}]
    attach_to_schedule(schedule, [None, DischargeCap(0.3, 5, 31.0)])
    assert AUTOMATIK_CAP_KEY not in schedule[0]
    assert schedule[1][AUTOMATIK_CAP_KEY] == 0.3


def test_no_cap_leaves_plan_untouched():
    plan: dict = {}
    attach_to_milp_plan(plan, [None])
    assert plan == {}


# --- battery behaviour in Automatik ------------------------------------------------------------


def test_capped_automatik_covers_only_the_cap():
    # Deficit 3 kW (heat pump), cap 1.03 kW → battery −1.03, grid the rest.
    action = bat.battery_plan_kw_from_control(
        bat.MODE_AUTOMATIK, 0.0, 0.5, 0.5, 3.0, MAX_KW, automatik_discharge_cap_kw=1.03
    )
    assert action == pytest.approx(-1.03)


def test_cap_does_not_limit_pv_charging_or_small_deficits():
    charge = bat.battery_plan_kw_from_control(
        bat.MODE_AUTOMATIK, 0.0, 4.0, 0.5, 0.0, MAX_KW, automatik_discharge_cap_kw=0.3
    )
    assert charge == pytest.approx(3.5)
    small = bat.battery_plan_kw_from_control(
        bat.MODE_AUTOMATIK, 0.0, 0.0, 0.2, 0.0, MAX_KW, automatik_discharge_cap_kw=0.3
    )
    assert small == pytest.approx(-0.2)


def test_command_text_names_the_cap_and_round_trips():
    text = bat.steuerbefehl_for_mode(bat.MODE_AUTOMATIK, 0.0, 1.03)
    assert text == "Automatikbetrieb (Entladung max. 1.03 kW)"
    assert bat.automatik_cap_from_steuerbefehl(text) == pytest.approx(1.03)
    assert bat.steuerbefehl_for_mode(bat.MODE_AUTOMATIK, 0.0) == "Automatikbetrieb"
    assert bat.automatik_cap_from_steuerbefehl("Automatikbetrieb") is None
    assert bat.automatik_cap_from_steuerbefehl("Entladesperre aktiv") is None


# --- simulation (forecast-mode booking) --------------------------------------------------------

_BATTERY = {
    "battery_capacity_kwh": 18.0,
    "efficiency": 0.97,
    "max_power_kw": MAX_KW,
    "min_soc": 0.0,
    "max_soc": 100.0,
    "control": "full",
}


def _row(pv: float, load: float) -> dict:
    return {"expected_p_pv": pv, "expected_p_act": load, "k_act": 25.0, "hour": 14}


def test_schedule_slot_books_the_cap_and_finalize_keeps_it():
    # Plan: battery 0.73 kW at 25 ct, cap 1.03 kW. Actual load 3 kW (heat pump unplanned).
    slot = {
        "milp_plan": {"p_charge": 0.0, "p_discharge": 0.73, "p_grid_buy": 0.0, "p_grid_sell": 0.0},
        "consumer_powers": {},
        "consumer_pv_follow": {},
        "planned_soc_percent": 49.0,
        AUTOMATIK_CAP_KEY: 1.03,
    }
    soc, chart_row, mode, target = _chart_row_from_schedule_slot(
        _row(0.0, 3.0), 50.0, _BATTERY, [], slot
    )
    assert mode == bat.MODE_AUTOMATIK
    assert chart_row["Steuerbefehl"] == "Automatikbetrieb (Entladung max. 1.03 kW)"
    assert chart_row["Geplante Batterie-Aktion (kW)"] == pytest.approx(-1.03)
    assert chart_row["Netzbezug (kW)"] == pytest.approx(1.97)

    finalize_chart_row_energy(chart_row, mode, target, 50.0, _BATTERY)
    assert chart_row["Geplante Batterie-Aktion (kW)"] == pytest.approx(-1.03)
    assert check_mode_battery_alignment([chart_row], _BATTERY) == []


# --- real solve --------------------------------------------------------------------------------


def _solve_matrix() -> list[dict]:
    from datetime import datetime, timedelta

    start = datetime(2026, 11, 16, 13, 15)
    # 31.5 ct now: grid charging for 33 ct does not pay after losses.
    prices = [31.5] + [33.0] * 6 + [20.0] * 5
    rows = []
    for i, price in enumerate(prices):
        slot = start + timedelta(minutes=15 * i)
        rows.append({
            "hour": slot.hour,
            "date": slot.date(),
            "slot_datetime": slot,
            "expected_p_pv": 2.0 if i == 0 else 0.0,
            "expected_p_act": 1.0,
            "k_act": price,
            "k_push_act": 5.0,
        })
    return rows


def test_live_plan_and_schedule_carry_the_cap():
    from optimizer.milp import milp_horizon_schedule, milp_optimizer

    battery = {**_BATTERY, "battery_capacity_kwh": 10.0}
    # 12 % of 10 kWh: not enough for the six 33 ct slots, so every kWh is worth 33 ct.
    mode, _, _, _, _, plan, _ = milp_optimizer(
        _solve_matrix(), 13, 12.0, battery_params=battery, k_push=5.0, verbose=False, consumers=[]
    )
    assert mode == bat.MODE_AUTOMATIK  # PV surplus in slot 0
    assert plan[AUTOMATIK_CAP_KEY] == pytest.approx(bat.power_threshold_kw(MAX_KW))
    assert plan[AUTOMATIK_CAP_REASON_KEY]["price_cent"] == pytest.approx(33.0)

    schedule = milp_horizon_schedule(
        _solve_matrix(), 12.0, battery_params=battery, k_push=5.0, consumers=[]
    )
    assert schedule[0][AUTOMATIK_CAP_KEY] == pytest.approx(bat.power_threshold_kw(MAX_KW))
    assert all(AUTOMATIK_CAP_KEY not in slot for slot in schedule[1:7])  # the peak itself


def test_plenty_of_energy_means_no_cap():
    from optimizer.milp import milp_optimizer

    battery = {**_BATTERY, "battery_capacity_kwh": 10.0}
    # 90 %: the plan leaves energy unused at the end, so a kWh now costs nothing later.
    _, _, _, _, _, plan, _ = milp_optimizer(
        _solve_matrix(), 13, 90.0, battery_params=battery, k_push=5.0, verbose=False, consumers=[]
    )
    assert AUTOMATIK_CAP_KEY not in plan


def test_tightest_cap_wins():
    assert lowest_cap_kw(None, None) is None
    assert lowest_cap_kw(2.0, None) == 2.0
    assert lowest_cap_kw(None, 0.3) == 0.3
    assert lowest_cap_kw(2.0, 0.3) == 0.3
