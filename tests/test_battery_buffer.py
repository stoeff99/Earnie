"""Battery SoC buffer: kept out of the plan, used when the price pays, refilled within 24 h."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from optimizer import battery as bat
from optimizer import battery_buffer as buf

NOW = datetime(2026, 11, 19, 12, 0)


@pytest.fixture(autouse=True)
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("EARNIE_RUNTIME_PATH", str(tmp_path))
    return tmp_path


def _battery(**extra) -> dict:
    return {
        "id": "haus",
        "battery_capacity_kwh": 18.0,
        "min_soc": 5.0,
        "max_soc": 100.0,
        "max_power_kw": 6.0,
        "max_charge_power_kw": 6.0,
        "max_discharge_power_kw": 6.0,
        "efficiency": 0.97,
        "grid_charge_before_sunrise": True,
        "buffer_soc": 10.0,
        **extra,
    }


def _matrix(prices: list[float], *, start: datetime = NOW, load: float = 1.0) -> list[dict]:
    rows = []
    for i, price in enumerate(prices):
        slot = start + timedelta(minutes=15 * i)
        rows.append({
            "hour": slot.hour,
            "date": slot.date(),
            "slot_datetime": slot,
            "expected_p_pv": 0.0,
            "expected_p_act": load,
            "k_act": price,
            "k_push_act": 5.0,
        })
    return rows


# --- configuration -----------------------------------------------------------------------------


_RAW = {
    "id": "haus",
    "label": "Haus",
    "battery_capacity_kwh": 18.0,
    "battery_max_charge_power_kw": 6.0,
    "battery_max_discharge_power_kw": 6.0,
    "battery_efficiency": 0.97,
    "battery_min_soc": 5.0,
    "battery_max_soc": 100.0,
    "threshold_power": 0.05,
    "regulates_on": "grid",
    "inverter_id": "wr",
}


def _params(spec: dict) -> dict:
    from house_config.battery_normalize import normalize_battery
    from house_config.entity_resolution import battery_params_from_planning, planning_battery_entry

    entry = planning_battery_entry(normalize_battery(spec, 0))
    return battery_params_from_planning({**entry, "inverter_efficiency": 0.97})


def test_buffer_reaches_optimizer_params_and_defaults_to_off():
    assert _params(_RAW)["buffer_soc"] == 0.0
    assert _params({**_RAW, "battery_buffer_soc": 10.0})["buffer_soc"] == 10.0


@pytest.mark.parametrize(
    ("buffer", "message"),
    [(-1.0, "negativ"), (95.0, "kleiner als battery_max_soc")],
)
def test_invalid_buffer_is_rejected(buffer, message):
    from house_config.battery_normalize import normalize_battery

    with pytest.raises(ValueError, match=message):
        normalize_battery({**_RAW, "battery_buffer_soc": buffer}, 0)


# --- state -------------------------------------------------------------------------------------


def test_below_floor_is_tracked_with_report_tolerance():
    battery = _battery()  # floor 15 %
    assert buf.update_below_floor_since(battery, {"haus": 14.0}, now=NOW) == {}  # within 1 %
    since = buf.update_below_floor_since(battery, {"haus": 12.0}, now=NOW)
    assert since == {"haus": NOW}
    later = NOW + timedelta(hours=3)
    assert buf.update_below_floor_since(battery, {"haus": 8.0}, now=later) == {"haus": NOW}
    assert buf.update_below_floor_since(battery, {"haus": 15.0}, now=later) == {}


def test_state_is_dropped_when_the_buffer_is_switched_off():
    buf.update_below_floor_since(_battery(), {"haus": 8.0}, now=NOW)
    assert buf.update_below_floor_since(_battery(buffer_soc=0.0), {"haus": 8.0}, now=NOW) == {}


def test_battery_without_soc_keeps_its_state_and_the_others_are_tracked():
    other = _battery(id="garage")
    buf.update_below_floor_since([_battery(), other], {"haus": 8.0, "garage": 50.0}, now=NOW)
    later = NOW + timedelta(hours=1)
    since = buf.update_below_floor_since([_battery(), other], {"garage": 8.0}, now=later)
    assert since == {"haus": NOW, "garage": later}


def test_deadline_compares_real_instants_across_zones():
    from zoneinfo import ZoneInfo

    zurich = ZoneInfo("Europe/Zurich")
    start = datetime(2026, 11, 19, 12, 0, tzinfo=zurich)
    matrix = [{"slot_datetime": start + timedelta(minutes=15 * i)} for i in range(8)]
    # 12:00 UTC is 13:00 in Zurich: slots ending by then are 0..3.
    deadline = datetime(2026, 11, 19, 12, 0, tzinfo=ZoneInfo("UTC"))
    assert buf._last_slot_ending_by(matrix, 8, deadline, 0.25) == 3


# --- planning ----------------------------------------------------------------------------------


def test_apply_raises_the_plan_floor_and_sets_the_deadline():
    out = buf.apply_buffer_to_battery_params(_battery(), {"haus": NOW})
    assert out["min_soc"] == 15.0
    assert out[buf.HARD_MIN_KEY] == 5.0
    assert out[buf.DEADLINE_KEY] == (NOW + timedelta(hours=24)).isoformat()
    plain = buf.apply_buffer_to_battery_params(_battery(buffer_soc=0.0), {})
    assert plain["min_soc"] == 5.0 and buf.FLOOR_KEY not in plain


def _plan(battery: dict, soc: float, matrix: list[dict]) -> list[dict]:
    from optimizer.milp import milp_horizon_schedule

    return milp_horizon_schedule(matrix, soc, battery_params=battery, k_push=5.0, consumers=[])


def test_plan_never_schedules_the_buffer():
    # 30 ct for 8 h: without a buffer the plan would empty the battery to 5 %.
    battery = buf.apply_buffer_to_battery_params(_battery(), {})
    schedule = _plan(battery, 40.0, _matrix([30.0] * 32))
    assert min(s["planned_soc_percent"] for s in schedule) == pytest.approx(15.0, abs=0.2)


def test_refill_meets_the_deadline_even_when_it_is_cheaper_later():
    # SoC 7 %, floor 15 %; 2 h left of the 24 h; it gets cheap only in 4 h.
    since = NOW - timedelta(hours=22)
    battery = buf.apply_buffer_to_battery_params(_battery(), {"haus": since})
    prices = [30.0] * 16 + [10.0] * 16
    schedule = _plan(battery, 7.0, _matrix(prices, load=0.2))
    deadline_slot = 7  # 12:00 + 8 × 15 min = 14:00
    assert schedule[deadline_slot]["planned_soc_percent"] >= 15.0 - 0.2
    # and no further discharge below the start before that
    assert min(s["planned_soc_percent"] for s in schedule) >= 7.0 - 0.2


def test_deadline_beyond_the_horizon_leaves_it_to_the_prices():
    # Nothing to use the refilled energy for inside this horizon: no refill yet.
    battery = buf.apply_buffer_to_battery_params(_battery(), {"haus": NOW})
    schedule = _plan(battery, 7.0, _matrix([30.0] * 8 + [10.0] * 8, load=0.2))
    assert max(s["planned_soc_percent"] for s in schedule) <= 7.5


def test_refill_waits_for_cheap_prices_inside_the_window():
    # Deadline in 4 h, cheap hours before it: the plan refills there, not at 30 ct now.
    battery = buf.apply_buffer_to_battery_params(_battery(), {"haus": NOW - timedelta(hours=20)})
    prices = [30.0] * 8 + [10.0] * 8 + [30.0] * 8
    schedule = _plan(battery, 7.0, _matrix(prices, load=0.2))
    assert schedule[7]["planned_soc_percent"] <= 7.5
    assert schedule[15]["planned_soc_percent"] >= 15.0 - 0.2


# --- live use (rule b) -------------------------------------------------------------------------


def _at_floor() -> dict:
    return buf.apply_buffer_to_battery_params(_battery(), {})


def test_buffer_is_released_when_the_price_beats_the_refill():
    # 30 ct now, cheapest refill in 24 h 18 ct → 18 / 0.97² ≈ 19.1 ct + 1 ct margin.
    release = buf.buffer_release(
        _at_floor(), bat.MODE_ENTLADESPERRE, 15.0, _matrix([30.0] + [18.0] * 95), dt_h=0.25
    )
    assert release is not None
    assert release.refill_cent == pytest.approx(19.13, abs=0.01)
    # Energy down to the hard minimum (10 % of 18 kWh) at most within the slot.
    assert release.discharge_cap_kw == pytest.approx(6.0)
    near_min = buf.buffer_release(
        _at_floor(), bat.MODE_ENTLADESPERRE, 6.0, _matrix([30.0] + [18.0] * 95), dt_h=0.25
    )
    assert near_min.discharge_cap_kw == pytest.approx(0.18 * 0.97 / 0.25, abs=0.01)


def test_buffer_is_kept_when_refilling_costs_about_the_same():
    assert buf.buffer_release(
        _at_floor(), bat.MODE_ENTLADESPERRE, 15.0, _matrix([20.0] + [19.5] * 95), dt_h=0.25
    ) is None


@pytest.mark.parametrize(
    ("mode", "soc"),
    [
        (bat.MODE_ENTLADESPERRE, 30.0),  # above the floor: the plan decides
        (bat.MODE_ENTLADESPERRE, 5.0),  # hard minimum: nothing left
        (bat.MODE_ZWANGS_LADEN, 15.0),  # refilling
    ],
)
def test_no_release(mode, soc):
    assert buf.buffer_release(_at_floor(), mode, soc, _matrix([35.0] + [15.0] * 95), dt_h=0.25) is None


def test_no_release_without_a_buffer():
    battery = buf.apply_buffer_to_battery_params(_battery(buffer_soc=0.0), {})
    assert buf.buffer_release(
        battery, bat.MODE_ENTLADESPERRE, 5.0, _matrix([35.0] + [15.0] * 95), dt_h=0.25
    ) is None


def test_released_slot_is_automatik_with_the_cap_on_the_plant():
    from integrations.loxone_writes import map_ess_setpoints

    release = buf.buffer_release(
        _at_floor(), bat.MODE_ENTLADESPERRE, 8.0, _matrix([30.0] + [18.0] * 95), dt_h=0.25
    )
    _, _, discharge, cmd = map_ess_setpoints(
        bat.MODE_AUTOMATIK, 0.0, 6.0, 6.0, automatik_discharge_cap_kw=release.discharge_cap_kw
    )
    assert cmd == 0
    assert discharge == pytest.approx(release.discharge_cap_kw)
