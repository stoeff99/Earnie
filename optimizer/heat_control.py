"""Heat pump control next to a room/climate controller: comfort boost and price lock.

Loxone (or another controller) stays the comfort master and decides *whether* the
house needs heat. Earnie adds *when* via two 0/1 signals per cycling heat pump
(``min_off_quarterhours`` > 1):

* ``set_heat_boost`` = 1 in slots where the plan runs the heat pump (cheap or PV
  surplus). Loxone switches the rooms from Eco to Comfort, so heat is drawn into the
  building mass now.
* ``set_enable`` = 0 (lock) only in slots where the plan does not heat and the import
  price is at least ``heat_lock_margin_cent`` above the cheapest planned heating slot
  of the horizon. A lock never lasts longer than ``heat_lock_max_quarterhours``; after
  that the heat pump is released for ``RELEASE_SLOTS`` before the next lock may start.
  ``heat_lock_max_quarterhours`` = 0 never locks.

The rule is a pure function over the plan (:func:`control_bands`); the live daemon
carries the running lock / release count from one quarter-hour slot to the next in a
small runtime file, so a lock cannot be stretched by re-planning every cycle.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from optimizer.heatpump_cycle import uses_cycle_pause

logger = logging.getLogger(__name__)

STATE_FILENAME = "heat_control_state.json"
RELEASE_SLOTS = 4
DEFAULT_LOCK_MARGIN_CENT = 5.0
DEFAULT_MAX_LOCK_QUARTERHOURS = 8
_SLOT = timedelta(minutes=15)
_lock = threading.Lock()


@dataclass(frozen=True)
class Carry:
    """Lock / release count at the start of slot 0."""

    locked_slots: int = 0
    release_left: int = 0


@dataclass(frozen=True)
class Bands:
    boost: list[int]
    enable: list[int]
    carry_after_first: Carry


def uses_heat_control(consumer: dict) -> bool:
    return str(consumer.get("type") or "") == "thermal_annual" and uses_cycle_pause(consumer)


def lock_margin_cent(consumer: dict) -> float:
    value = consumer.get("heat_lock_margin_cent")
    return DEFAULT_LOCK_MARGIN_CENT if value is None else max(0.0, float(value))


def max_lock_slots(consumer: dict) -> int:
    value = consumer.get("heat_lock_max_quarterhours")
    return DEFAULT_MAX_LOCK_QUARTERHOURS if value is None else max(0, int(value))


def control_bands(
    on: Sequence[bool],
    prices: Sequence[float],
    *,
    margin_cent: float,
    max_lock: int,
    carry: Carry = Carry(),
    release_slots: int = RELEASE_SLOTS,
) -> Bands:
    """Boost and enable per slot from the planned on/off and the import price."""
    if len(on) != len(prices):
        raise ValueError("Wärmepumpen-Steuerung: Plan und Preise müssen gleich lang sein.")
    boost = [1 if flag else 0 for flag in on]
    planned_prices = [float(p) for p, flag in zip(prices, on) if flag]
    if max_lock <= 0 or not planned_prices:
        # Without a planned heating slot there is no price to compare against.
        return Bands(boost, [1] * len(on), Carry())
    threshold = min(planned_prices) + float(margin_cent)
    run, release = carry.locked_slots, carry.release_left
    enable: list[int] = []
    after_first = Carry()
    for index, (flag, price) in enumerate(zip(on, prices)):
        wants_lock = not flag and float(price) >= threshold
        if release > 0:
            enable.append(1)
            release -= 1
            run = 0
        elif wants_lock and run >= max_lock:
            enable.append(1)
            run = 0
            release = release_slots - 1
        elif wants_lock:
            enable.append(0)
            run += 1
        else:
            enable.append(1)
            run = 0
        if index == 0:
            after_first = Carry(locked_slots=run, release_left=release)
    return Bands(boost, enable, after_first)


# --- live ----------------------------------------------------------------------------------


def _state_path() -> str:
    from runtime_store.persist_paths import runtime_path

    return runtime_path(STATE_FILENAME)


def _load_state() -> dict[str, dict[str, Any]]:
    try:
        with open(_state_path(), encoding="utf-8") as handle:
            return dict(json.load(handle).get("consumers") or {})
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Wärmepumpen-Steuerung: {STATE_FILENAME} nicht lesbar ({exc}).") from exc


def _save_state(consumers: dict[str, dict[str, Any]]) -> None:
    path = _state_path()
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"consumers": consumers}, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _carry_in(entry: dict[str, Any] | None, slot: datetime) -> Carry:
    """Same slot again → same carry-in; next slot → previous result; gap → fresh."""
    if not entry:
        return Carry()
    previous = datetime.fromisoformat(str(entry["slot"]))
    if previous == slot:
        return Carry(**entry["carry_in"])
    if previous + _SLOT == slot:
        return Carry(**entry["carry_after"])
    return Carry()


def _slot_start(row: dict[str, Any]) -> datetime:
    slot = row.get("slot_datetime")
    if not isinstance(slot, datetime):
        raise ValueError("Wärmepumpen-Steuerung: Matrix-Slot ohne slot_datetime.")
    return slot.replace(tzinfo=None, second=0, microsecond=0)


def live_heat_controls(
    consumers: Sequence[dict],
    kw_horizon: dict[str, Sequence[float]],
    matrix: Sequence[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Slot-0 ``{"boost", "enable"}`` per heat-controlled consumer; updates the state file."""
    targets = [c for c in consumers if uses_heat_control(c) and str(c["id"]) in kw_horizon]
    if not targets or not matrix:
        return {}
    slot = _slot_start(matrix[0])
    out: dict[str, dict[str, int]] = {}
    with _lock:
        state = _load_state()
        for consumer in targets:
            cid = str(consumer["id"])
            kws = list(kw_horizon[cid])
            horizon = min(len(kws), len(matrix))
            carry = _carry_in(state.get(cid), slot)
            bands = control_bands(
                [kw > 1e-3 for kw in kws[:horizon]],
                [float(row["k_act"]) for row in matrix[:horizon]],
                margin_cent=lock_margin_cent(consumer),
                max_lock=max_lock_slots(consumer),
                carry=carry,
            )
            out[cid] = {"boost": bands.boost[0], "enable": bands.enable[0]}
            state[cid] = {
                "slot": slot.isoformat(),
                "carry_in": carry.__dict__,
                "carry_after": bands.carry_after_first.__dict__,
            }
            logger.info(
                "Wärmepumpe %s: Boost=%d Freigabe=%d (Sperre seit %d Slots, Freigabe-Rest %d)",
                cid,
                bands.boost[0],
                bands.enable[0],
                bands.carry_after_first.locked_slots,
                bands.carry_after_first.release_left,
            )
        _save_state(state)
    return out
