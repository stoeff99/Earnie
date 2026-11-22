"""SoC buffer above the battery's minimum: kept out of the plan, used when it pays.

``batteries[].battery_buffer_soc`` (%, 0 = off) adds a buffer on top of ``battery_min_soc``:

- **Plan:** the MILP treats ``min_soc + buffer`` as its floor, so it never schedules the
  buffer energy.
- **Use (live):** when the battery has reached that floor and the price now is at least the
  cost of refilling (cheapest import price in the next 24 h / round-trip efficiency) plus
  ``PRICE_MARGIN_CENT``, Earnie writes Automatik with a discharge limit that stops at the hard
  minimum instead of the discharge lock. Unexpected load is then covered down to
  ``battery_min_soc``.
- **Refill:** a SoC below the floor is led back by the plan's SoC recovery
  (``milp_soc_recovery``: no further planned discharge, refill when the prices suit). It must
  be back at the floor **24 h** after it first fell below (``REFILL_HOURS``); the deadline is a
  hard constraint, bounded by what the charge power can reach.

Live only: the Scenario Explorer plans with ``battery_min_soc`` as before.
Spec: ``docs/dev/battery-buffer.md``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILENAME = "battery_buffer_state.json"
REFILL_HOURS = 24.0
# Plants report the SoC in whole percent: up to this far below the floor counts as "at it".
REPORT_TOLERANCE_PCT = 1.0
# The price now must exceed the refill cost by at least this much.
PRICE_MARGIN_CENT = 1.0

HARD_MIN_KEY = "_buffer_hard_min_soc"
FLOOR_KEY = "_buffer_floor_soc"
DEADLINE_KEY = "_buffer_refill_deadline"

_lock = threading.Lock()


# --- state: when the SoC first fell below the floor -------------------------------------------


def _state_path() -> str:
    from runtime_store.persist_paths_base import runtime_path

    return runtime_path(STATE_FILENAME)


def _read_state() -> dict[str, datetime]:
    path = _state_path()
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    entries = doc.get("below_floor_since") if isinstance(doc, dict) else None
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: Schlüssel 'below_floor_since' fehlt oder ist kein Objekt.")
    return {str(k): datetime.fromisoformat(str(v)) for k, v in entries.items()}


def _write_state(entries: dict[str, datetime]) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "schema_version": 1,
                "below_floor_since": {
                    k: v.replace(microsecond=0).isoformat() for k, v in sorted(entries.items())
                },
            },
            fh,
            indent=2,
        )
        fh.write("\n")
    os.replace(tmp, path)


def _batteries(battery_params: dict | list[dict]) -> list[dict]:
    return list(battery_params) if isinstance(battery_params, list) else [battery_params]


def _buffer_soc(bat: dict) -> float:
    return max(0.0, float(bat.get("buffer_soc") or 0.0))


def _battery_id(bat: dict, index: int) -> str:
    return str(bat.get("id") or f"ess_{index}")


def update_below_floor_since(
    battery_params: dict | list[dict],
    soc_by_id: dict[str, float],
    *,
    now: datetime,
) -> dict[str, datetime]:
    """Track per battery since when its SoC is below ``min_soc + buffer`` (live, per cycle)."""
    with _lock:
        entries = _read_state()
        changed = False
        active_ids = set()
        for index, bat in enumerate(_batteries(battery_params)):
            buffer = _buffer_soc(bat)
            if buffer <= 0.0:
                continue
            bid = _battery_id(bat, index)
            active_ids.add(bid)
            if bid not in soc_by_id:
                # Multi-ESS: a battery without its own SoC is dropped from this cycle's plan
                # (filter_planning_batteries_with_soc); its below-floor time stays as it was.
                logger.warning("Batterie-Puffer %s: kein Ist-SoC — Zustand unverändert.", bid)
                continue
            floor = float(bat["min_soc"]) + buffer
            below = float(soc_by_id[bid]) < floor - REPORT_TOLERANCE_PCT
            if below and bid not in entries:
                entries[bid] = now
                changed = True
                logger.info(
                    "Batterie-Puffer %s: SoC %.0f %% unter %.0f %% — Nachladen bis %s.",
                    bid,
                    float(soc_by_id[bid]),
                    floor,
                    (now + timedelta(hours=REFILL_HOURS)).strftime("%d.%m. %H:%M"),
                )
            elif not below and bid in entries:
                del entries[bid]
                changed = True
                logger.info("Batterie-Puffer %s: wieder aufgefüllt (SoC %.0f %%).", bid, float(soc_by_id[bid]))
        for bid in [b for b in entries if b not in active_ids]:
            del entries[bid]
            changed = True
        if changed:
            _write_state(entries)
        return dict(entries)


# --- planning: floor and refill deadline -------------------------------------------------------


def apply_buffer_to_battery_params(
    battery_params: dict | list[dict],
    below_floor_since: dict[str, datetime],
) -> dict | list[dict]:
    """Copy of the params with the plan floor ``min_soc + buffer`` and a pending refill deadline."""

    def one(bat: dict, index: int) -> dict:
        buffer = _buffer_soc(bat)
        if buffer <= 0.0:
            return bat
        out = dict(bat)
        hard = float(bat["min_soc"])
        floor = hard + buffer
        out[HARD_MIN_KEY] = hard
        out[FLOOR_KEY] = floor
        out["min_soc"] = floor
        since = below_floor_since.get(_battery_id(bat, index))
        if since is not None:
            out[DEADLINE_KEY] = (since + timedelta(hours=REFILL_HOURS)).isoformat()
        return out

    if isinstance(battery_params, list):
        return [one(bat, i) for i, bat in enumerate(battery_params)]
    return one(battery_params, 0)


def refill_pending(battery_params: dict) -> bool:
    return bool(battery_params.get(DEADLINE_KEY))


def add_buffer_refill_constraints(
    prob: Any,
    grid_vars: Any,
    matrix: list[dict[str, Any]],
    e_init_by_ess: dict[str, float],
    *,
    horizon: int,
    dt_h: float,
) -> None:
    """Hard refill deadline: back at the floor by the last slot that ends before the deadline."""
    for ess_id in grid_vars.ess_ids or []:
        params = grid_vars.battery_params_by_id[ess_id]
        if not refill_pending(params):
            continue
        deadline = datetime.fromisoformat(str(params[DEADLINE_KEY]))
        slot = _last_slot_ending_by(matrix, horizon, deadline, dt_h)
        if slot is None:
            continue  # deadline beyond the horizon: the prices decide for now
        capacity = float(params["battery_capacity_kwh"])
        e_floor = float(params[FLOOR_KEY]) / 100.0 * capacity
        e_start = float(e_init_by_ess[ess_id])
        max_charge = float(
            params.get("max_charge_power_kw", params.get("max_power_kw") or 0.0) or 0.0
        )
        efficiency = float(params.get("efficiency") or 1.0)
        reachable = e_start + max_charge * efficiency * dt_h * (slot + 1)
        target = min(e_floor, reachable)
        if target <= e_start + 1e-9:
            continue
        energy = grid_vars.e_batt_by_ess[ess_id]
        prob += energy[slot] >= target, f"battery_buffer_refill_{ess_id.replace('-', '_')}"
        logger.info(
            "Batterie-Puffer %s: bis %s auf %.1f %% nachladen (Slot %d).",
            ess_id,
            deadline.strftime("%d.%m. %H:%M"),
            target / capacity * 100.0,
            slot,
        )


def _last_slot_ending_by(
    matrix: list[dict[str, Any]], horizon: int, deadline: datetime, dt_h: float
) -> int | None:
    """Index of the last slot ending at or before ``deadline``; 0 if it has passed; None past horizon."""
    last: int | None = None
    for t in range(min(horizon, len(matrix))):
        start = matrix[t].get("slot_datetime")
        if not isinstance(start, datetime):
            raise ValueError(f"Batterie-Puffer: Matrix-Slot {t} ohne slot_datetime.")
        if _comparable(start, deadline) + timedelta(hours=dt_h) <= _comparable(deadline, start):
            last = t
        else:
            return 0 if last is None else last
    return None


def _comparable(dt: datetime, other: datetime) -> datetime:
    """``dt`` as is when both are zone-aware (real instants), else its wall-clock time."""
    if dt.tzinfo is not None and other.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


# --- live: use of the buffer -------------------------------------------------------------------


@dataclass(frozen=True)
class BufferRelease:
    """Automatik with a discharge limit down to the hard minimum instead of the lock."""

    discharge_cap_kw: float
    price_cent: float
    refill_cent: float


def buffer_release(
    battery_params: dict,
    mode: int,
    current_soc: float,
    matrix: list[dict[str, Any]],
    *,
    dt_h: float,
) -> BufferRelease | None:
    """Release the buffer for this slot, or ``None`` (rule (b): only when the price pays)."""
    from .battery import MODE_AUTOMATIK, MODE_ENTLADESPERRE

    if FLOOR_KEY not in battery_params or mode not in (MODE_AUTOMATIK, MODE_ENTLADESPERRE):
        return None
    floor = float(battery_params[FLOOR_KEY])
    hard = float(battery_params[HARD_MIN_KEY])
    soc = float(current_soc)
    if soc > floor + REPORT_TOLERANCE_PCT or soc <= hard + 1e-9:
        return None  # above the floor the plan decides; at the hard minimum nothing is left
    if not matrix:
        return None
    efficiency = float(battery_params.get("efficiency") or 1.0)
    refill = _refill_cost_cent(matrix, dt_h) / (efficiency * efficiency)
    price = float(matrix[0]["k_act"])
    if price < refill + PRICE_MARGIN_CENT:
        return None
    capacity = float(battery_params["battery_capacity_kwh"])
    max_discharge = float(
        battery_params.get("max_discharge_power_kw", battery_params.get("max_power_kw") or 0.0) or 0.0
    )
    above_kwh = (soc - hard) / 100.0 * capacity
    cap = min(max_discharge, above_kwh * efficiency / dt_h)
    if cap <= 1e-9:
        return None
    return BufferRelease(round(cap, 2), price, round(refill, 2))


def _refill_cost_cent(matrix: list[dict[str, Any]], dt_h: float) -> float:
    """Cheapest import price after this slot within ``REFILL_HOURS``."""
    slots = max(1, int(round(REFILL_HOURS / dt_h)))
    window = [float(row["k_act"]) for row in matrix[1 : 1 + slots]]
    if not window:
        return float(matrix[0]["k_act"])
    return min(window)
