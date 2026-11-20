"""Chart 2 as an account view (Account view P2, spec docs/dev/chart2-account-view.md)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ui.chart_account_render import (
    SAVING_AXIS_NAME,
    SAVING_PANEL_NAME,
    account_slots,
    build_account_figure,
    energy_difference_hint,
)
from ui.chart_account_view import (
    ANCHOR_NOW,
    ANCHOR_START,
    SLOT_COUNTED,
    SLOT_PENDING,
    SLOT_PLAN,
    SLOT_SKIPPED,
    AccountSlots,
    build_account_view,
    gap_runs,
)
from ui.chart_colors import CHART_MISSING_SLOT_FILL

_TZ = ZoneInfo("Europe/Vienna")
_START = datetime(2026, 11, 4, 8, 0, tzinfo=_TZ)
_QUARTER = timedelta(minutes=15)


def _slots(
    with_eur, without_eur, *, anchor, base_kwh=None, opt_kwh=None, states=None, **after
) -> AccountSlots:
    count = len(with_eur)
    return AccountSlots(
        with_eur=tuple(with_eur),
        without_eur=tuple(without_eur),
        baseline_kwh=tuple(base_kwh or [0.0] * count),
        optimized_kwh=tuple(opt_kwh or [0.0] * count),
        states=tuple(states or [SLOT_COUNTED] * anchor + [SLOT_PLAN] * (count - anchor)),
        anchor=anchor,
        **after,
    )


# Two past slots save 0,30 €, two plan slots save 0,10 €.
_EXAMPLE = _slots([0.1, 0.2, 0.3, 0.3], [0.3, 0.3, 0.35, 0.35], anchor=2)


def test_both_anchor_modes_differ_by_the_saving_so_far():
    now = build_account_view(_EXAMPLE, ANCHOR_NOW)
    start = build_account_view(_EXAMPLE, ANCHOR_START)

    assert now.optimized == start.optimized
    shifts = [a - b for a, b in zip(now.baseline, start.baseline)]
    assert shifts == pytest.approx([0.3] * 5)
    assert now.saving_so_far_eur == pytest.approx(0.3)
    assert now.saving_after_eur == pytest.approx(0.1)
    assert start.saving_total_eur == pytest.approx(0.4)


def test_lines_meet_at_jetzt_and_the_gaps_are_the_savings():
    now = build_account_view(_EXAMPLE, ANCHOR_NOW)
    start = build_account_view(_EXAMPLE, ANCHOR_START)

    assert now.optimized[2] == pytest.approx(now.baseline[2])
    assert now.gap[0] == pytest.approx(0.3)  # saved from the window start to Jetzt
    assert now.gap[-1] == pytest.approx(0.1)  # expected from Jetzt to the end
    assert start.gap[0] == pytest.approx(0.0)
    assert start.gap[2] == pytest.approx(0.3)
    assert start.gap[-1] == pytest.approx(0.4)
    assert start.optimized[0] == start.baseline[0] == 0.0


def test_balance_falls_when_paying_and_rises_with_feed_in():
    view = build_account_view(_slots([0.5, -0.2], [0.5, -0.2], anchor=0), ANCHOR_START)

    assert view.optimized == pytest.approx((0.0, -0.5, -0.3))


def test_consumption_gap_is_zero_at_the_horizon_end_and_flat_left_of_jetzt():
    slots = _slots(
        [0.1] * 4,
        [0.1] * 4,
        anchor=2,
        base_kwh=[0.0, 0.0, 1.0, 0.5],
        opt_kwh=[0.0, 0.0, 0.5, 1.0],
        baseline_after_window_kwh=2.0,
        optimized_after_window_kwh=1.5,
    )

    view = build_account_view(slots, ANCHOR_NOW)

    # Remaining baseline minus optimized: 0.5 after the window, +0.5 and −0.5 inside it.
    assert view.consumption_gap_kwh == pytest.approx((0.5, 0.5, 0.5, 0.0, 0.5))
    assert view.plan_energy_diff_kwh == pytest.approx(0.5)
    assert view.plan_energy_kwh == pytest.approx(3.0)
    no_tail = build_account_view(
        _slots([0.1] * 2, [0.1] * 2, anchor=0, base_kwh=[1.0, 0.0], opt_kwh=[0.0, 1.0]),
        ANCHOR_START,
    )
    assert no_tail.consumption_gap_kwh[-1] == 0.0


def test_window_fully_in_the_past_is_anchored_at_its_end():
    slots = _slots([0.1, 0.2], [0.3, 0.1], anchor=2)

    now = build_account_view(slots, ANCHOR_NOW)
    start = build_account_view(slots, ANCHOR_START)

    assert not now.has_future
    assert now.optimized[-1] == pytest.approx(now.baseline[-1])
    assert now.gap[0] == pytest.approx(0.1)
    assert start.gap[-1] == pytest.approx(0.1)
    assert now.saving_after_eur == 0.0
    assert not now.flag_energy_difference


def test_gap_runs_split_where_the_lines_cross():
    # Saves 0,20 € in the first slot, loses 0,40 € in the second: the lines cross.
    view = build_account_view(_slots([0.0, 0.4], [0.2, 0.0], anchor=0), ANCHOR_START)

    runs = gap_runs(view)

    assert [run.saving for run in runs] == [True, False]
    crossing = runs[0].positions[-1]
    assert 1.0 < crossing < 2.0
    assert runs[1].positions[0] == crossing
    assert runs[0].optimized[-1] == pytest.approx(runs[0].baseline[-1])


def test_ist_bei_jetzt_colours_saving_on_both_sides():
    runs = gap_runs(build_account_view(_EXAMPLE, ANCHOR_NOW))

    assert [run.saving for run in runs] == [True, True]
    assert runs[0].positions[-1] == 2.0 and runs[1].positions[0] == 2.0


@pytest.mark.parametrize(
    ("diff_kwh", "energy_kwh", "flagged"),
    [(0.4, 1.0, False), (0.6, 40.0, False), (0.6, 20.0, True), (-1.0, 10.0, True)],
)
def test_energy_difference_is_flagged_from_both_thresholds(diff_kwh, energy_kwh, flagged):
    slots = _slots(
        [0.0, 0.0], [0.0, 0.0], anchor=1,
        base_kwh=[0.0, energy_kwh + diff_kwh], opt_kwh=[0.0, energy_kwh],
    )
    view = build_account_view(slots, ANCHOR_NOW)

    assert view.flag_energy_difference is flagged
    hint = energy_difference_hint(view)
    assert (hint is not None) is flagged
    if flagged:
        assert ("mehr" if diff_kwh > 0 else "weniger") in hint


def test_invalid_input_fails_loudly():
    with pytest.raises(ValueError, match="not a number"):
        _slots([float("nan")], [0.0], anchor=0)
    with pytest.raises(ValueError, match="unknown slot states"):
        _slots([0.0], [0.0], anchor=0, states=["later"])
    with pytest.raises(ValueError, match="anchor"):
        _slots([0.0], [0.0], anchor=2, states=[SLOT_PLAN])
    with pytest.raises(ValueError, match="anchor mode"):
        build_account_view(_slots([0.0], [0.0], anchor=0), "middle")


def _savings_view(states, ledger_extra=None):
    return {
        "hourly_optimized_cost_euro": [9.0, 9.0, 0.3, 0.3],
        "hourly_matched_baseline_cost_euro": [9.0, 9.0, 0.35, 0.35],
        "hourly_optimized_consumption_kwh": [5.0, 5.0, 1.0, 1.0],
        "hourly_matched_baseline_consumption_kwh": [5.0, 5.0, 1.0, 1.2],
        "ledger_savings": {
            "slot_with_eur": [0.1, 0.0, 0.0, 0.0],
            "slot_without_eur": [0.3, 0.0, 0.0, 0.0],
            "slot_states": states,
            "closed_days": [],
            "open_day_start": None,
            "open_day_end": None,
            "soc_correction_euro": None,
            **(ledger_extra or {}),
        },
    }


def test_ledger_left_of_jetzt_and_plan_right_of_it():
    slots = account_slots(
        _savings_view([SLOT_COUNTED, SLOT_PENDING, SLOT_PLAN, SLOT_PLAN]),
        history_slot_count=2,
        slot_count=4,
    )

    assert slots.with_eur == (0.1, 0.0, 0.3, 0.3)
    assert slots.without_eur == (0.3, 0.0, 0.35, 0.35)
    assert slots.baseline_kwh == (0.0, 0.0, 1.0, 1.2)
    assert slots.states[:2] == (SLOT_COUNTED, SLOT_PENDING)


def test_slots_left_of_jetzt_need_the_ledger():
    view = _savings_view([SLOT_PLAN] * 4)
    del view["ledger_savings"]
    with pytest.raises(ValueError, match="ledger"):
        account_slots(view, history_slot_count=2, slot_count=4)
    with pytest.raises(ValueError, match="window 3"):
        account_slots(_savings_view([SLOT_PLAN] * 4), history_slot_count=0, slot_count=3)


def _frame() -> pd.DataFrame:
    starts = [_START + _QUARTER * index for index in range(4)]
    return pd.DataFrame({"slot_datetime": starts, "Uhrzeit": [s.strftime("%H:%M") for s in starts]})


def test_figure_marks_skipped_slots_and_keeps_ledger_labels():
    ledger_extra = {
        "closed_days": [
            {
                "day_start": (_START - timedelta(days=1)).isoformat(),
                "day_end": (_START + _QUARTER).isoformat(),
                "saving_adjusted_eur": -0.4,
                "incomplete": True,
            }
        ],
        "open_day_start": (_START + _QUARTER).isoformat(),
        "open_day_end": (_START + timedelta(days=1)).isoformat(),
        "soc_correction_euro": 0.12,
    }
    view_data = _savings_view([SLOT_COUNTED, SLOT_SKIPPED, SLOT_PLAN, SLOT_PLAN], ledger_extra)
    slots = account_slots(view_data, history_slot_count=2, slot_count=4)
    view = build_account_view(slots, ANCHOR_NOW)

    fig = build_account_figure(
        _frame(), slots, view, ledger=view_data["ledger_savings"], chart_window=None
    )

    marked = [shape for shape in fig.layout.shapes if shape.fillcolor == CHART_MISSING_SLOT_FILL]
    assert len(marked) == 2  # one skipped slot, in both panels
    texts = [annotation.text for annotation in fig.layout.annotations]
    assert "Ergebnis: 0,40 € mehr ausgegeben · unvollständig" in texts
    assert "SoC: 0,12 € gutgeschrieben" in texts
    assert "Bisher: 0,20 € gespart" in texts
    assert "Erwartet: 0,10 € gespart" in texts
    names = {trace.name for trace in fig.data}
    assert {"Mit Earnie (gebucht)", "Ohne Earnie (gebucht)", "Optimiert (Plan)"} <= names
    assert any(str(name).startswith("Verbrauch ") for name in names)


def test_window_without_past_reads_plan():
    slots = account_slots(_savings_view([SLOT_PLAN] * 4), history_slot_count=0, slot_count=4)
    view = build_account_view(slots, ANCHOR_START)

    fig = build_account_figure(_frame(), slots, view, ledger=None, chart_window=None)

    assert [a.text for a in fig.layout.annotations] == ["Plan: 0,10 € gespart"]


# --- Account view P3: the analysis page's 7 / 28-day window -----------------------------


def _period_ledger(states, with_eur, without_eur, closed_days=()):
    return {
        "slot_with_eur": list(with_eur),
        "slot_without_eur": list(without_eur),
        "slot_states": list(states),
        "closed_days": list(closed_days),
        "open_day_start": None,
        "open_day_end": None,
        "soc_correction_euro": None,
        "hint": None,
    }


def test_period_is_anchored_at_its_end_without_an_energy_control():
    from ui.chart_account_render import period_account_slots

    slots = period_account_slots(
        _period_ledger([SLOT_COUNTED] * 3, [0.1, 0.2, 0.1], [0.3, 0.1, 0.2])
    )
    view = build_account_view(slots, ANCHOR_NOW)

    assert slots.anchor == 3 and not view.has_future
    assert view.saving_so_far_eur == pytest.approx(0.2)
    assert set(view.consumption_gap_kwh) == {0.0}


def test_period_soc_correction_counts_closed_days_fully_inside():
    from ui.chart_account_render import period_soc_correction_eur

    day = timedelta(days=1)
    closed = [
        {"day_start": (_START - day).isoformat(), "day_end": _START.isoformat(),
         "saving_eur": 1.0, "saving_adjusted_eur": 1.2, "incomplete": False},
        {"day_start": _START.isoformat(), "day_end": (_START + day).isoformat(),
         "saving_eur": 0.5, "saving_adjusted_eur": 0.4, "incomplete": True},
    ]
    total, count = period_soc_correction_eur(
        _period_ledger([], [], [], closed), _START - timedelta(hours=1), _START + 2 * day
    )

    assert count == 1
    assert total == pytest.approx(-0.1)


def test_period_figure_has_the_saving_panel_and_marks_only_incomplete_days():
    from ui.chart_account_render import period_account_slots

    closed = [
        {"day_start": _START.isoformat(), "day_end": (_START + 2 * _QUARTER).isoformat(),
         "saving_eur": 0.1, "saving_adjusted_eur": 0.1, "incomplete": False},
        {"day_start": (_START + 2 * _QUARTER).isoformat(), "day_end": (_START + 4 * _QUARTER).isoformat(),
         "saving_eur": 0.1, "saving_adjusted_eur": 0.1, "incomplete": True},
    ]
    ledger = _period_ledger(
        [SLOT_SKIPPED, SLOT_SKIPPED, SLOT_COUNTED, SLOT_COUNTED],
        [0.0, 0.0, 0.1, 0.4],
        [0.0, 0.0, 0.3, 0.1],
        closed,
    )
    slots = period_account_slots(ledger)
    view = build_account_view(slots, ANCHOR_NOW)

    fig = build_account_figure(_frame(), slots, view, ledger=ledger, chart_window=None, period=True)

    texts = [annotation.text for annotation in fig.layout.annotations]
    assert "Zeitraum: 0,10 € mehr ausgegeben" in texts
    assert texts.count("unvollständig") == 1
    assert not any(text.startswith("Ergebnis") or text.startswith("SoC") for text in texts)
    saving = next(trace for trace in fig.data if trace.name == SAVING_PANEL_NAME)
    assert list(saving.y) == pytest.approx([0.0, 0.0, 0.0, 0.2, -0.1])
    # The same saving on the right axis of the balance panel.
    right = next(trace for trace in fig.data if trace.name == SAVING_AXIS_NAME)
    assert right.yaxis == "y2"
    assert list(right.y) == pytest.approx(list(saving.y))
    assert fig.layout.yaxis2.side == "right"
    assert fig.layout.yaxis2.title.text == "Ersparnis (€)"
    assert fig.layout.yaxis.title.text == "Kontostand seit Fensterstart (€)"
    assert not any(str(trace.name).startswith("Verbrauch ") for trace in fig.data)
    # Two skipped slots in a row are one orange area per panel.
    marked = [shape for shape in fig.layout.shapes if shape.fillcolor == CHART_MISSING_SLOT_FILL]
    assert len(marked) == 2



def test_period_axes_use_the_house_currency(monkeypatch):
    from house_config import currency_display
    from ui.chart_account_render import period_account_slots

    monkeypatch.setattr(currency_display, "active_units", lambda: currency_display.CHF)
    monkeypatch.setattr("ui.chart_account_render.active_units", lambda: currency_display.CHF)
    ledger = _period_ledger(
        [SLOT_COUNTED, SLOT_COUNTED, SLOT_COUNTED, SLOT_COUNTED],
        [0.1, 0.1, 0.1, 0.1],
        [0.2, 0.2, 0.2, 0.2],
        [],
    )
    slots = period_account_slots(ledger)
    fig = build_account_figure(
        _frame(), slots, build_account_view(slots, ANCHOR_NOW), ledger=ledger, chart_window=None, period=True
    )

    assert fig.layout.yaxis.title.text == "Kontostand seit Fensterstart (CHF)"
    assert fig.layout.yaxis2.title.text == "Ersparnis (CHF)"
    assert fig.layout.yaxis3.title.text == "CHF"
    assert all("€" not in str(trace.hovertemplate) for trace in fig.data if trace.hovertemplate)


def test_saving_line_adds_the_battery_value_at_each_day_close():
    from ui.chart_account_render import period_account_slots, saving_with_battery_value

    closed = [
        # Fully inside: its SoC correction (0.5) is added from its close on.
        {"day_start": _START.isoformat(), "day_end": (_START + 2 * _QUARTER).isoformat(),
         "saving_eur": 0.1, "saving_adjusted_eur": 0.6, "incomplete": False},
        # Started before the window: left out, like the caption under the chart.
        {"day_start": (_START - 4 * _QUARTER).isoformat(), "day_end": _START.isoformat(),
         "saving_eur": 0.0, "saving_adjusted_eur": 9.0, "incomplete": False},
    ]
    ledger = _period_ledger(
        [SLOT_COUNTED] * 4, [0.1, 0.1, 0.1, 0.1], [0.2, 0.2, 0.2, 0.2], closed
    )
    ledger.update(
        open_day_start=(_START + 2 * _QUARTER).isoformat(),
        open_day_end=(_START + 100 * _QUARTER).isoformat(),
        saving_euro=0.2,
        soc_correction_euro=0.3,
    )
    slots = period_account_slots(ledger)
    view = build_account_view(slots, ANCHOR_NOW)
    times = [_START + k * _QUARTER for k in range(5)]

    values = saving_with_battery_value(view, times, ledger)

    # Paid saving 0.1 per slot; +0.5 from the close at boundary 2; +0.3 (today) after the last slot.
    assert values == pytest.approx([0.0, 0.1, 0.7, 0.8, 1.2])


def test_aligned_ranges_put_both_zeros_at_the_same_height():
    from ui.chart_account_render import aligned_ranges

    (l_lo, l_hi), (r_lo, r_hi) = aligned_ranges([-35.0, 3.5], [-3.0, 14.0])

    assert -l_lo / (l_hi - l_lo) == pytest.approx(-r_lo / (r_hi - r_lo))
    assert l_lo <= -35.0 and l_hi >= 3.5
    assert r_lo <= -3.0 and r_hi >= 14.0
