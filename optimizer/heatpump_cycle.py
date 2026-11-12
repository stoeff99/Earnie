"""Cycle memory of an on/off heat pump with a mandatory pause (``min_off_quarterhours``).

Older heat pumps run 15–45 min, then lock the compressor for a fixed pause. The
horizon MILP enforces that pattern inside the plan; this module carries the
*current* state into slot 0: a running heat pump has used part of its maximum run,
a stopped one may still sit in its pause.

The daemon observes the heat pump once per cycle (``sens_consumer_active`` when
mapped, else the measured ``sens_power_act`` against half the nominal power) and
stores the last on/off transition in a runtime file. Transition times are only
known to cycle precision (one slot). Only consumers with ``min_off_quarterhours``
> 1 take part; all others keep the previous behaviour.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILENAME = "heatpump_cycle_state.json"
_lock = threading.Lock()


def min_off_slots(consumer: dict) -> int:
    return max(0, int(consumer.get("min_off_quarterhours") or 0))


def uses_cycle_pause(consumer: dict) -> bool:
    return min_off_slots(consumer) > 1


# --- state file ----------------------------------------------------------------------------


def _state_path() -> str:
    from runtime_store.persist_paths import runtime_path

    return runtime_path(STATE_FILENAME)


def load_state() -> dict[str, dict[str, Any]]:
    try:
        with open(_state_path(), encoding="utf-8") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Wärmepumpen-Takt: {STATE_FILENAME} nicht lesbar ({exc}).") from exc
    return dict(doc.get("consumers") or {})


def _save_state(consumers: dict[str, dict[str, Any]]) -> None:
    path = _state_path()
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"consumers": consumers}, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def record_observation(cid: str, running: bool, now: datetime) -> None:
    """Store the on/off state; ``since`` moves only on a transition we saw."""
    stamp = now.isoformat(timespec="seconds")
    with _lock:
        consumers = load_state()
        previous = consumers.get(cid)
        if previous is None:
            entry = {"running": running, "since": stamp, "since_known": False}
        elif bool(previous.get("running")) != running:
            entry = {"running": running, "since": stamp, "since_known": True}
        else:
            entry = dict(previous)
        entry["observed_at"] = stamp
        consumers[cid] = entry
        _save_state(consumers)


# --- live observation ----------------------------------------------------------------------


def read_running(consumer: dict) -> bool | None:
    """Heat pump running now? ``None`` when neither signal is mapped or readable."""
    from optimizer.powerstation_live import _digital_active_from_consumer

    digital = _digital_active_from_consumer(consumer)
    if digital is not None:
        return digital
    from integrations.ehal_live import resolve_consumer_live_power_kw

    power_kw = resolve_consumer_live_power_kw(consumer)
    if power_kw is None:
        return None
    return float(power_kw) >= 0.5 * float(consumer["nominal_power_kw"])


def observe_live_consumers(consumers: list[dict], now: datetime) -> None:
    """Daemon, once per cycle before planning: record each cycling heat pump's state."""
    for consumer in consumers:
        if not uses_cycle_pause(consumer):
            continue
        cid = str(consumer["id"])
        running = read_running(consumer)
        if running is None:
            logger.warning(
                "Wärmepumpe %s: Taktgedächtnis inaktiv — weder sens_consumer_active noch "
                "sens_power_act lesbar; die Pflichtpause gilt nur innerhalb des Plans.",
                cid,
            )
            continue
        record_observation(cid, running, now)


# --- carry-in for the MILP -----------------------------------------------------------------


def carry_in_for(
    consumer: dict,
    now: datetime,
    *,
    dt_h: float,
    state: dict[str, dict[str, Any]] | None = None,
) -> dict[str, int | bool]:
    """Slot-0 carry-in: ``running`` + ``run_elapsed_slots`` or ``pause_left_slots``.

    Empty when the consumer does not cycle, no fresh observation exists (older
    than two slots, e.g. daemon restart) or a stop time was never seen.
    """
    if not uses_cycle_pause(consumer):
        return {}
    entry = (load_state() if state is None else state).get(str(consumer["id"]))
    if not entry:
        return {}
    slot = timedelta(hours=dt_h)
    observed_at = datetime.fromisoformat(str(entry["observed_at"]))
    if _naive(now) - _naive(observed_at) > 2 * slot:
        return {}
    since = datetime.fromisoformat(str(entry["since"]))
    elapsed = max(0, math.floor((_naive(now) - _naive(since)) / slot))
    if entry.get("running"):
        return {"running": True, "run_elapsed_slots": elapsed}
    if not entry.get("since_known"):
        return {}
    pause_left = max(0, min_off_slots(consumer) - elapsed)
    return {"running": False, "pause_left_slots": pause_left} if pause_left else {}


def _naive(moment: datetime) -> datetime:
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment
