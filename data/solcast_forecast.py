"""PV forecast from the LoxBerry plugin "PV Solcast NG" via MQTT.

The plugin publishes one topic per value under a branch (``solcast/sum``,
``solcast/system_<n>``): ``today_hour_XX_kwh`` / ``tomorrow_hour_XX_kwh`` is the forecast
energy of the local clock hour ``XX:00–XX+1:00`` (kWh in one hour = mean kW), plus
``last_update_iso`` (time of the plugin's last Solcast API call).

The topics carry no date: "today" and "tomorrow" are resolved against the local date when
the message arrives. A retained message may come from before midnight, so retained hour
values are only taken once the branch's ``last_update_iso`` shows the current local day
(the plugin re-publishes during the day); until then they are dropped with a log line.

Values are stored by absolute local hour in a runtime file shared by daemon and UI.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

STORE_FILENAME = "solcast_pv_forecast.json"
KEEP_DAYS = 3
FIELD_LAST_UPDATE = "last_update_iso"
_HOUR_FIELD = re.compile(r"^(today|tomorrow)_hour_(\d{2})_kwh$")
_HOUR_KEY_FMT = "%Y-%m-%d %H:00"
_lock = threading.Lock()


def hour_key(moment: datetime, tz: ZoneInfo) -> str:
    """Local clock hour of ``moment`` (naive = already local) as store key."""
    local = moment.astimezone(tz) if moment.tzinfo is not None else moment
    return local.strftime(_HOUR_KEY_FMT)


def parse_hour_field(name: str) -> tuple[int, int] | None:
    """``today_hour_09_kwh`` → (day offset 0, hour 9); other fields → ``None``."""
    match = _HOUR_FIELD.match(name)
    if not match:
        return None
    hour = int(match.group(2))
    if hour > 23:
        return None
    return (0 if match.group(1) == "today" else 1), hour


def parse_kwh(text: str) -> float:
    try:
        value = float(text.strip())
    except ValueError as exc:
        raise ValueError(f"keine Zahl: {text[:32]!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"ungültige Energie {value!r} kWh")
    return value


# --- store ---------------------------------------------------------------------------------


def _store_path() -> str:
    from runtime_store.persist_paths import runtime_path

    return runtime_path(STORE_FILENAME)


def load_store() -> dict[str, Any]:
    try:
        with open(_store_path(), encoding="utf-8") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        return {"branches": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Solcast-Prognose: {STORE_FILENAME} nicht lesbar ({exc}).") from exc
    doc.setdefault("branches", {})
    return doc


def _save_store(doc: dict[str, Any]) -> None:
    path = _store_path()
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _prune(hours: dict[str, float], today: date) -> dict[str, float]:
    cutoff = (today - timedelta(days=KEEP_DAYS)).strftime(_HOUR_KEY_FMT)
    return {key: value for key, value in hours.items() if key >= cutoff}


def _store_hours(branch: str, values: Mapping[str, float], now: datetime) -> None:
    with _lock:
        doc = load_store()
        entry = dict(doc["branches"].get(branch) or {})
        hours = dict(entry.get("hours") or {})
        hours.update(values)
        entry["hours"] = _prune(hours, now.date())
        entry["received_at"] = now.isoformat(timespec="seconds")
        doc["branches"][branch] = entry
        _save_store(doc)


def _store_last_update(branch: str, last_update: datetime) -> None:
    with _lock:
        doc = load_store()
        entry = dict(doc["branches"].get(branch) or {})
        entry["last_update"] = last_update.isoformat()
        doc["branches"][branch] = entry
        _save_store(doc)


# --- MQTT ingest ---------------------------------------------------------------------------


@dataclass
class SolcastIngest:
    """Turns branch sub-topic messages into dated hour values (one per MQTT session)."""

    tz: ZoneInfo
    # Retained hour values per branch, waiting for a same-day ``last_update_iso``.
    _pending: dict[str, dict[tuple[int, int], float]] = field(default_factory=dict)
    _confirmed: set[str] = field(default_factory=set)
    _rejected: set[str] = field(default_factory=set)

    def on_message(
        self,
        branch: str,
        name: str,
        payload: str,
        *,
        retained: bool,
        now: datetime | None = None,
    ) -> None:
        stamp = (now or datetime.now(self.tz)).astimezone(self.tz)
        if name == FIELD_LAST_UPDATE:
            self._on_last_update(branch, payload, stamp)
            return
        parsed = parse_hour_field(name)
        if parsed is None:
            return
        try:
            kwh = parse_kwh(payload)
        except ValueError as exc:
            logger.warning("Solcast %s/%s rejected: %s", branch, name, exc)
            return
        if retained and branch not in self._confirmed:
            if branch in self._rejected:
                return
            self._pending.setdefault(branch, {})[parsed] = kwh
            return
        _store_hours(branch, {self._key(parsed, stamp): kwh}, stamp)

    def _key(self, parsed: tuple[int, int], stamp: datetime) -> str:
        offset, hour = parsed
        day = stamp.date() + timedelta(days=offset)
        return f"{day.isoformat()} {hour:02d}:00"

    def _on_last_update(self, branch: str, payload: str, stamp: datetime) -> None:
        try:
            last_update = datetime.fromisoformat(payload.strip())
        except ValueError:
            logger.warning("Solcast %s/%s rejected: %r", branch, FIELD_LAST_UPDATE, payload[:40])
            return
        if last_update.tzinfo is None:
            logger.warning("Solcast %s/%s without time zone: %r", branch, FIELD_LAST_UPDATE, payload)
            return
        _store_last_update(branch, last_update)
        pending = self._pending.pop(branch, {})
        if last_update.astimezone(self.tz).date() == stamp.date():
            self._confirmed.add(branch)
            self._rejected.discard(branch)
            if pending:
                _store_hours(
                    branch, {self._key(parsed, stamp): kwh for parsed, kwh in pending.items()}, stamp
                )
            return
        # Retained values from an earlier day: "today" would land on the wrong date.
        self._rejected.add(branch)
        if pending:
            logger.info(
                "Solcast %s: %d retained hour values from %s ignored (waiting for today's publish)",
                branch,
                len(pending),
                last_update.isoformat(),
            )


# --- forecast ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SolcastSeries:
    """Solcast kW per target slot; ``None`` where the store has no value for the hour."""

    values: list[float | None]


def solcast_series(branch: str, target_hours: Sequence[datetime], tz: ZoneInfo) -> SolcastSeries:
    entry = (load_store()["branches"].get(branch) or {}).get("hours") or {}
    values: list[float | None] = []
    for slot in target_hours:
        kwh = entry.get(hour_key(slot, tz))
        values.append(None if kwh is None else round(float(kwh), 3))
    return SolcastSeries(values=values)


def is_within_plugin_reach(slot: datetime, tz: ZoneInfo, now: datetime) -> bool:
    """The plugin publishes today and tomorrow; later slots are never covered."""
    local = slot.astimezone(tz) if slot.tzinfo is not None else slot
    return local.date() <= now.astimezone(tz).date() + timedelta(days=1)


def record_plan_usage(usage: Mapping[str, Mapping[str, Any]], now: datetime) -> None:
    """Remember the last planning cycle's Solcast use per PV system (for the UI)."""
    with _lock:
        doc = load_store()
        doc["last_plan"] = {"at": now.isoformat(timespec="seconds"), "systems": dict(usage)}
        _save_store(doc)


def store_summary(branch: str) -> dict[str, Any]:
    """For the UI: last plugin update, last receive time and the stored hour range."""
    entry = load_store()["branches"].get(branch) or {}
    hours = sorted((entry.get("hours") or {}).keys())
    return {
        "last_update": entry.get("last_update"),
        "received_at": entry.get("received_at"),
        "first_hour": hours[0] if hours else None,
        "last_hour": hours[-1] if hours else None,
        "hours": len(hours),
    }


def last_plan_usage() -> dict[str, Any]:
    return dict(load_store().get("last_plan") or {})
