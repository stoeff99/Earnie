"""Automatik discharge cap: keep battery energy the plan needs for a more expensive slot.

In Automatik the plant covers every deficit from the battery up to the discharge limit.
The plan may have meant only part of it (battery 0.7 kW, grid 1.9 kW), and loads the plan
did not expect (a heat pump that starts on its own) drain the battery as well.

A kWh taken from the battery now is missing later. The next plan then gives up the
battery's *cheapest* later use (or charges a little more from the grid, or leaves less
unused at the end). That price is the stored energy's marginal value. When it is higher
than the price now, by at least ``PRICE_MARGIN_CENT``, the written discharge limit is the
plan's discharge for this slot plus a margin (the battery power threshold), and load the
plan did not expect is bought now. Otherwise the limit stays at the maximum.

Marginal value over the slots after this one, until the battery is planned full:

- a planned discharge: its price,
- a planned grid charge: its price divided by the round-trip efficiency,
- a full battery (PV refills it) or energy left at the horizon end: 0 (not scarce).

Single battery only (the limit is written for the primary battery).
Spec: ``docs/dev/automatik-discharge-cap.md``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Key of the cap in the slot-0 ``milp_plan`` and in each horizon schedule slot.
AUTOMATIK_CAP_KEY = "automatik_discharge_cap_kw"
# Slot index and price of the later use the cap keeps the energy for (slot-0 plan only).
AUTOMATIK_CAP_REASON_KEY = "automatik_discharge_cap_reason"

# The stored energy's marginal value must exceed the price now by at least this much.
PRICE_MARGIN_CENT = 1.0
# Planned energy this close to the maximum counts as a full battery.
FULL_TOLERANCE_KWH = 0.01


@dataclass(frozen=True)
class DischargeCap:
    """Automatik discharge limit for one slot; the later slot that sets the energy's value."""

    cap_kw: float
    reserved_slot: int
    reserved_price_cent: float


def discharge_caps_from_plan(
    discharge_kw: Sequence[float],
    grid_charge: Sequence[bool],
    price_cent: Sequence[float],
    energy_kwh: Sequence[float],
    *,
    energy_min_kwh: float,
    energy_max_kwh: float,
    efficiency: float,
    threshold_kw: float,
    max_discharge_kw: float,
) -> list[DischargeCap | None]:
    """Cap per slot (``None``: no cap) from one solved plan.

    ``energy_kwh[t]`` is the planned energy at the end of slot ``t``; ``grid_charge[t]``
    whether the plan charges from the grid in slot ``t``.
    """
    n = len(discharge_kw)
    if not (len(grid_charge) == len(price_cent) == len(energy_kwh) == n):
        raise ValueError("discharge_caps_from_plan: horizon lists differ in length")
    if threshold_kw < 0.0 or max_discharge_kw <= 0.0 or not 0.0 < efficiency <= 1.0:
        raise ValueError(
            f"discharge_caps_from_plan: threshold {threshold_kw} kW, max discharge "
            f"{max_discharge_kw} kW, efficiency {efficiency}"
        )
    if n == 0:
        return []

    def full(t: int) -> bool:
        return float(energy_kwh[t]) >= energy_max_kwh - FULL_TOLERANCE_KWH

    not_scarce: tuple[float, int | None] = (0.0, None)
    # Marginal value (price, slot) of stored energy over slots >= t; None: no later use.
    left_at_end = float(energy_kwh[-1]) > energy_min_kwh + FULL_TOLERANCE_KWH
    tail: tuple[float, int | None] | None = not_scarce if left_at_end else None
    caps: list[DischargeCap | None] = [None] * n
    for t in range(n - 1, -1, -1):
        later = not_scarce if full(t) else tail
        if later is not None and later[1] is not None:
            if later[0] >= float(price_cent[t]) + PRICE_MARGIN_CENT:
                cap = min(max_discharge_kw, max(0.0, float(discharge_kw[t])) + threshold_kw)
                if cap < max_discharge_kw - 1e-9:
                    caps[t] = DischargeCap(round(cap, 2), later[1], float(later[0]))
        value: float | None = None
        if float(discharge_kw[t]) > threshold_kw:
            value = float(price_cent[t])
        elif grid_charge[t]:
            value = float(price_cent[t]) / (efficiency * efficiency)
        if full(t):
            tail = not_scarce
        elif value is not None and (later is None or value < later[0]):
            tail = (value, t)
        else:
            tail = later
    return caps


def plan_discharge_caps(
    model: Any,
    matrix: list[dict[str, Any]],
    battery_params: dict,
) -> list[DischargeCap | None]:
    """Caps for every slot of a solved horizon model (single battery only)."""
    from .battery import power_threshold_kw

    horizon = int(model.horizon)
    ess_ids = list(getattr(model, "ess_ids", None) or [])
    if len(ess_ids) > 1:
        logger.debug("Automatik discharge cap: %d batteries, no cap", len(ess_ids))
        return [None] * horizon
    max_discharge = float(battery_params["max_power_kw"])
    if max_discharge <= 0.0:
        return [None] * horizon
    threshold = power_threshold_kw(max_discharge)
    e_min = model.e_batt[0].lowBound
    e_max = model.e_batt[0].upBound
    if e_min is None or e_max is None:
        raise ValueError("Automatik discharge cap: e_batt without bounds")

    def value(var) -> float:
        return float(var.varValue or 0.0)

    return discharge_caps_from_plan(
        [value(model.p_discharge[t]) for t in range(horizon)],
        [
            value(model.p_charge[t]) > threshold and value(model.p_grid_buy[t]) > threshold
            for t in range(horizon)
        ],
        [float(matrix[t]["k_act"]) for t in range(horizon)],
        [value(model.e_batt[t]) for t in range(horizon)],
        energy_min_kwh=float(e_min),
        energy_max_kwh=float(e_max),
        efficiency=float(battery_params["efficiency"]),
        threshold_kw=threshold,
        max_discharge_kw=max_discharge,
    )


def attach_to_milp_plan(milp_plan: dict, caps: list[DischargeCap | None]) -> None:
    """Slot-0 cap (and its reason) into the live ``milp_plan``."""
    cap = caps[0] if caps else None
    if cap is None:
        return
    milp_plan[AUTOMATIK_CAP_KEY] = cap.cap_kw
    milp_plan[AUTOMATIK_CAP_REASON_KEY] = {
        "slot": cap.reserved_slot,
        "price_cent": round(cap.reserved_price_cent, 2),
    }


def attach_to_schedule(schedule: list[dict], caps: list[DischargeCap | None]) -> None:
    """Cap per horizon schedule slot (open-loop / forecast-mode booking)."""
    for slot, cap in zip(schedule, caps):
        if cap is not None:
            slot[AUTOMATIK_CAP_KEY] = cap.cap_kw


def automatik_cap_for_mode(mode: int, plan_or_slot: dict | None) -> float | None:
    """The cap of a plan or schedule slot, only when its command is Automatik."""
    from .battery import MODE_AUTOMATIK

    if mode != MODE_AUTOMATIK or not plan_or_slot:
        return None
    raw = plan_or_slot.get(AUTOMATIK_CAP_KEY)
    return None if raw is None else float(raw)


def lowest_cap_kw(*caps: float | None) -> float | None:
    """The tightest of several discharge caps (virtual reserve floor, plan); ``None``: none."""
    present = [float(c) for c in caps if c is not None]
    return min(present) if present else None
