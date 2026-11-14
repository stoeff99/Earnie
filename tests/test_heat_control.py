"""Heat pump next to a climate controller: comfort boost and price lock (optimizer.heat_control)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from optimizer import heat_control
from optimizer.heat_control import Carry, control_bands

_T0 = datetime(2026, 11, 14, 6, 0)


# --- rule ----------------------------------------------------------------------------------

_NOMINAL = 3.0


def _bands(on, prices, surplus=None, **kwargs):
    surplus = [0.0] * len(on) if surplus is None else surplus
    kwargs.setdefault("margin_cent", 5.0)
    kwargs.setdefault("max_lock", 8)
    return control_bands(on, prices, surplus, nominal_kw=_NOMINAL, **kwargs)


@pytest.mark.parametrize(
    ("surplus_kw", "price", "expected", "why"),
    [
        (4.5, 99.0, 1, "PV covers 150 % of the heat pump: boost at any price"),
        (4.4, 99.0, 0, "just below 150 % and expensive"),
        (1.5, 25.0, 1, "50 % PV and grid at most 5 above the cheapest"),
        (1.5, 25.1, 0, "50 % PV but grid more than 5 above the cheapest"),
        (1.4, 22.0, 1, "below 50 % PV, but grid at most 2 above the cheapest"),
        (1.4, 22.1, 0, "below 50 % PV and grid more than 2 above the cheapest"),
    ],
)
def test_boost_rule(surplus_kw, price, expected, why):
    assert heat_control.boost_slot(
        surplus_kw, price, nominal_kw=_NOMINAL, cheapest_price=20.0
    ) == bool(expected), why


def test_custom_boost_thresholds():
    rule = heat_control.BoostRule(
        pv_percent=100.0, mixed_pv_percent=30.0, mixed_margin_cent=8.0, grid_margin_cent=0.0
    )
    kwargs = {"nominal_kw": _NOMINAL, "cheapest_price": 20.0, "rule": rule}
    assert heat_control.boost_slot(3.0, 99.0, **kwargs)  # 100 % PV
    assert heat_control.boost_slot(0.9, 28.0, **kwargs)  # 30 % PV, +8
    assert not heat_control.boost_slot(0.9, 28.1, **kwargs)
    assert heat_control.boost_slot(0.0, 20.0, **kwargs)  # cheapest itself
    assert not heat_control.boost_slot(0.0, 20.1, **kwargs)
    defaults = heat_control.boost_rule({})
    assert defaults == heat_control.BoostRule()
    assert heat_control.boost_rule({"heat_boost_pv_percent": 120}).pv_percent == 120.0


def test_boost_is_independent_of_the_plan_and_fog_gives_no_boost():
    # Fog: 1.3 kW PV against ~1 kW house load, flat price far above the night minimum.
    on = [True, True, False, False]
    prices = [28.0, 28.0, 20.0, 21.0]
    bands = _bands(on, prices, surplus=[0.3, 0.3, 0.0, 0.0], max_lock=0)
    assert bands.boost == [0, 0, 1, 1]  # planned heating alone is no boost
    assert bands.enable == [1, 1, 1, 1]


def test_lock_needs_margin_to_cheapest_planned_slot():
    on = [True, False, False, False, True, False]
    prices = [20.0, 24.9, 25.0, 40.0, 22.0, 30.0]
    bands = _bands(on, prices)
    # Cheapest planned heating slot 20 → lock from 25 on, never in a planned slot.
    assert bands.enable == [1, 1, 0, 0, 1, 0]


def test_boost_slot_is_never_locked():
    on = [True, False]
    prices = [20.0, 40.0]
    bands = _bands(on, prices, surplus=[0.0, 5.0])
    assert bands.boost == [1, 1]
    assert bands.enable == [1, 1]


def test_no_lock_without_planned_heating_or_with_max_zero():
    assert _bands([False] * 3, [50.0] * 3, margin_cent=0.0).enable == [1, 1, 1]
    assert _bands([True, False], [10.0, 90.0], margin_cent=0.0, max_lock=0).enable == [1, 1]


def test_max_lock_then_release_then_lock_again():
    on = [True] + [False] * 12
    prices = [10.0] + [50.0] * 12
    bands = _bands(on, prices, max_lock=4, release_slots=4)
    assert bands.enable == [1] + [0] * 4 + [1] * 4 + [0] * 4


def test_carry_in_counts_previous_lock():
    on = [False, False, False, True]
    prices = [50.0, 50.0, 50.0, 10.0]
    bands = _bands(on, prices, max_lock=4, carry=Carry(locked_slots=3), release_slots=2)
    assert bands.enable == [0, 1, 1, 1]
    assert bands.carry_after_first == Carry(locked_slots=4, release_left=0)


# --- live memory ---------------------------------------------------------------------------


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "heat_control_state.json"
    monkeypatch.setattr(heat_control, "_state_path", lambda: str(path))
    return path


def _wp(**extra) -> dict:
    return {
        "id": "waermepumpe",
        "type": "thermal_annual",
        "nominal_power_kw": 3.0,
        "min_off_quarterhours": 4,
        "heat_lock_margin_cent": 5.0,
        "heat_lock_max_quarterhours": 2,
        **extra,
    }


def _matrix(start: datetime, prices: list[float]) -> list[dict]:
    return [
        {
            "slot_datetime": start + timedelta(minutes=15 * i),
            "k_act": price,
            "expected_p_pv": 0.0,
            "expected_p_act": 0.5,
        }
        for i, price in enumerate(prices)
    ]


def test_live_lock_memory_ends_after_max_lock(state_file):
    kws = {"waermepumpe": [0.0] * 6 + [3.0]}
    prices = [50.0] * 6 + [10.0]
    decisions = []
    for step in range(4):
        start = _T0 + timedelta(minutes=15 * step)
        out = heat_control.live_heat_controls([_wp()], kws, _matrix(start, prices))
        decisions.append(out["waermepumpe"]["enable"])
        # A second run in the same slot (out-of-band) must not advance the memory.
        again = heat_control.live_heat_controls([_wp()], kws, _matrix(start, prices))
        assert again == out
    assert decisions == [0, 0, 1, 1]


def test_live_skips_non_cycling_and_gap_resets(state_file):
    kws = {"waermepumpe": [0.0, 3.0]}
    plain = _wp(min_off_quarterhours=0)
    assert heat_control.live_heat_controls([plain], kws, _matrix(_T0, [50.0, 10.0])) == {}
    heat_control.live_heat_controls([_wp()], kws, _matrix(_T0, [50.0, 10.0]))
    heat_control.live_heat_controls([_wp()], kws, _matrix(_T0 + timedelta(minutes=15), [50.0, 10.0]))
    # Daemon down for an hour: the old lock count no longer applies.
    later = heat_control.live_heat_controls(
        [_wp()], kws, _matrix(_T0 + timedelta(hours=2), [50.0, 10.0])
    )
    assert later["waermepumpe"] == {"boost": 0, "enable": 0}


def test_live_boost_uses_measured_pv_of_slot_zero(state_file):
    kws = {"waermepumpe": [0.0, 0.0]}
    matrix = _matrix(_T0, [30.0, 20.0])
    # Slot 0 carries the live snapshot: 1.3 kW PV in fog → no boost.
    matrix[0]["expected_p_pv"] = 1.3
    assert heat_control.live_heat_controls([_wp()], kws, matrix)["waermepumpe"]["boost"] == 0
    # Sun breaks through: 6 kW PV, 0.5 kW house load → surplus covers 150 %.
    matrix[0]["expected_p_pv"] = 6.0
    assert heat_control.live_heat_controls([_wp()], kws, matrix)["waermepumpe"]["boost"] == 1


# --- writes --------------------------------------------------------------------------------


def _bound_wp() -> dict:
    return {
        **_wp(),
        "name": "Wärmepumpe",
        "ehal_bindings": {
            "heatpump.waermepumpe.set_enable": "Earnie_WP_Freigabe",
            "heatpump.waermepumpe.set_heat_boost": "Earnie_WP_Boost",
        },
    }


def test_write_values_use_heat_control_and_always_write_boost():
    from integrations.loxone_writes import _flexible_consumer_output_values

    consumer = _bound_wp()
    planned_on = {"waermepumpe": 3.0}
    controlled = _flexible_consumer_output_values(
        consumer, planned_on, {}, None, {"waermepumpe": {"boost": 1, "enable": 1}}
    )
    assert controlled == {"Earnie_WP_Freigabe": 1.0, "Earnie_WP_Boost": 1.0}
    locked = _flexible_consumer_output_values(
        consumer, {"waermepumpe": 0.0}, {}, None, {"waermepumpe": {"boost": 0, "enable": 0}}
    )
    assert locked == {"Earnie_WP_Freigabe": 0.0, "Earnie_WP_Boost": 0.0}
    # No decision (e.g. not cycling): previous enable rule, boost written as 0.
    legacy = _flexible_consumer_output_values(consumer, planned_on, {}, None, None)
    assert legacy == {"Earnie_WP_Freigabe": 1.0, "Earnie_WP_Boost": 0.0}


def test_boost_field_is_offered_for_heat_pumps_only():
    from ehal.flex_fields import consumer_fields_for_consumer

    assert "heatpump.waermepumpe.set_heat_boost" in consumer_fields_for_consumer(
        "waermepumpe", "thermal_annual"
    )
    assert not any(
        field.endswith("set_heat_boost")
        for field in consumer_fields_for_consumer("spa", "generic")
    )


# --- config and chart ----------------------------------------------------------------------


def test_lock_settings_survive_save_and_milp_conversion():
    from house_config.planning_flex_bridge import planning_thermal_to_milp
    from house_config.profiles_store import _normalize_consumer, _serialize_consumer

    raw = {
        "id": "waermepumpe",
        "label": "Waermepumpe",
        "type": "thermal_annual",
        "nominal_power_kw": 3.0,
        "living_area_m2": 145.0,
        "building_class": 3,
        "min_off_quarterhours": 4,
        "heat_lock_margin_cent": 7.5,
        "heat_lock_max_quarterhours": 6,
        "heat_boost_pv_percent": 120,
        "heat_boost_mixed_pv_percent": 40,
        "heat_boost_mixed_margin_cent": 4,
        "heat_boost_grid_margin_cent": 1.5,
    }
    saved = _serialize_consumer(_normalize_consumer(raw, 0, "house"))
    assert saved["heat_lock_margin_cent"] == 7.5
    assert saved["heat_lock_max_quarterhours"] == 6
    milp = planning_thermal_to_milp(saved)
    assert heat_control.lock_margin_cent(milp) == 7.5
    assert heat_control.max_lock_slots(milp) == 6
    assert heat_control.boost_rule(milp) == heat_control.BoostRule(120.0, 40.0, 4.0, 1.5)
    assert heat_control.uses_heat_control(milp)


def test_chart_band_series_from_planned_rows():
    from ui.chart_heat_control import heat_control_band_series

    df = pd.DataFrame(
        {
            "Wärmepumpe (kW)": [3.0, 3.0, 0.0, 0.0, 3.0],
            "Strompreis (Cent/kWh)": [99.0, 20.0, 30.0, 21.0, 26.0],
            "PV-Prognose (kW)": [0.0, 0.0, 0.0, 0.0, 6.0],
            "Verbrauch-Prognose (kW)": [0.5, 0.5, 0.5, 0.5, 0.5],
        }
    )
    consumer = {**_wp(), "name": "Wärmepumpe"}
    boost, lock = heat_control_band_series(df, consumer, start=1)
    # Cheapest 20: slots 20 and 21 cheap grid, 26 with 5.5 kW PV surplus.
    assert boost == [1, 0, 1, 1]
    assert lock == [0, 1, 0, 0]


def test_write_test_offers_boost_as_eco_comfort_select():
    from integrations.ehal_write_test_bounds import WriteTestClampError, clamp_probe_value
    from ui.ehal_write_test import BINARY_SELECT_LABELS

    assert BINARY_SELECT_LABELS["set_heat_boost"] == {0: "0 Eco", 1: "1 Komfort"}
    field = "heatpump.waermepumpe.set_heat_boost"
    assert clamp_probe_value(field, 1, max_power_kw=6.0) == 1
    assert clamp_probe_value(field, 0, max_power_kw=6.0) == 0
    with pytest.raises(WriteTestClampError):
        clamp_probe_value(field, 2, max_power_kw=6.0)
