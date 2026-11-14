"""Monitor chart: comfort boost and lock bands of heat pumps next to a climate controller.

The bands apply :func:`optimizer.heat_control.control_bands` to the planned slots of the
chart (from ``history_slot_count`` on), so price and decision can be read together. The
live lock memory (maximum lock time across cycles) is not part of the chart: it starts
fresh at the first planned slot.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from house_config.currency_display import active_units
from optimizer.heat_control import (
    control_bands,
    lock_margin_cent,
    max_lock_slots,
    uses_heat_control,
)
from ui.chart_slot_axis import ChartSlotAxis

PRICE_COLUMN = "Strompreis (Cent/kWh)"
BAND_BASE = 100.5
BAND_HEIGHT = 3.0
COLOR_BOOST = "#f59e0b"
COLOR_LOCK = "#6b7280"


def heat_control_band_series(
    df: pd.DataFrame, consumer: dict, *, start: int
) -> tuple[list[int], list[int]] | None:
    """(boost, lock) per slot from ``start`` on, or None without the needed columns."""
    power_col = f"{consumer['name']} (kW)"
    if power_col not in df.columns or PRICE_COLUMN not in df.columns or start >= len(df):
        return None
    rows = df.iloc[start:]
    kws = pd.to_numeric(rows[power_col], errors="coerce").fillna(0.0)
    prices = pd.to_numeric(rows[PRICE_COLUMN], errors="coerce").ffill().bfill()
    if prices.isna().any():
        return None
    bands = control_bands(
        [float(kw) > 1e-3 for kw in kws],
        [float(price) for price in prices],
        margin_cent=lock_margin_cent(consumer),
        max_lock=max_lock_slots(consumer),
    )
    return bands.boost, [1 - flag for flag in bands.enable]


def add_heat_control_bands(
    fig: go.Figure,
    df: pd.DataFrame,
    axis: ChartSlotAxis,
    *,
    history_slot_count: int | None,
    consumers: list[dict] | None = None,
) -> None:
    """Thin bands above the SoC/price axis: orange = Komfort-Boost, grey = Sperre."""
    if consumers is None:
        from ui.chart_consumer_stack import _chart_flex_consumers

        consumers = _chart_flex_consumers()
    start = max(0, int(history_slot_count or 0))
    unit = active_units().per_kwh
    for consumer in consumers:
        if not uses_heat_control(consumer):
            continue
        series = heat_control_band_series(df, consumer, start=start)
        if series is None:
            continue
        indices = list(range(start, len(df)))
        x = list(axis.at(slice(start, len(df)), 0.5))
        widths = [axis.bar_width_ms(1.0, index) for index in indices]
        prices = pd.to_numeric(df[PRICE_COLUMN].iloc[start:], errors="coerce").tolist()
        for label, flags, color in (
            ("Komfort-Boost", series[0], COLOR_BOOST),
            ("Sperre", series[1], COLOR_LOCK),
        ):
            if not any(flags):
                continue
            fig.add_trace(
                go.Bar(
                    x=x,
                    y=[BAND_HEIGHT if flag else 0.0 for flag in flags],
                    base=BAND_BASE,
                    width=widths,
                    yaxis="y2",
                    marker_color=color,
                    opacity=0.85,
                    name=f"{consumer['name']}: {label}",
                    customdata=prices,
                    hovertemplate=(
                        f"{consumer['name']}: {label}<br>Preis: %{{customdata:.2f}} {unit}"
                        "<extra></extra>"
                    ),
                )
            )
