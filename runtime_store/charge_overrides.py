"""One-off charge job per EV ("Ladeauftrag"): target SoC and ready-by time for one plug-in.

``runtime/charge_overrides.json`` holds at most one job per consumer id. While a job is
active it replaces the weekday / weekend ``ready_by_hour`` and the configured target SoC of
that EV in the Live planning (``optimizer/charging_resolve.py``); everything else stays
auto-planned. A job ends by itself:

- when the car is unplugged after it was plugged in during the job,
- when its ready-by time has passed,
- or when the user cancels it on the Monitor page.

Live only: the Scenario Explorer replays history and never reads this file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

OVERRIDES_FILENAME = "charge_overrides.json"
SCHEMA_VERSION = 1
SOURCE_MONITOR = "monitor"

_lock = threading.Lock()


@dataclass(frozen=True)
class ChargeOverride:
    """An active one-off charge job. ``ready_by`` is naive local time like the matrix slots."""

    consumer_id: str
    target_soc_percent: float
    ready_by: datetime
    created_at: datetime
    source: str = SOURCE_MONITOR
    plug_seen: bool = False

    def to_doc(self) -> dict[str, Any]:
        return {
            "target_soc_percent": round(float(self.target_soc_percent), 1),
            "ready_by": self.ready_by.replace(second=0, microsecond=0).isoformat(),
            "created_at": self.created_at.replace(microsecond=0).isoformat(),
            "source": self.source,
            "plug_seen": bool(self.plug_seen),
        }

    @classmethod
    def from_doc(cls, consumer_id: str, doc: dict[str, Any]) -> "ChargeOverride":
        return cls(
            consumer_id=str(consumer_id),
            target_soc_percent=float(doc["target_soc_percent"]),
            ready_by=datetime.fromisoformat(str(doc["ready_by"])),
            created_at=datetime.fromisoformat(str(doc["created_at"])),
            source=str(doc.get("source") or SOURCE_MONITOR),
            plug_seen=bool(doc.get("plug_seen")),
        )


def _path() -> str:
    from runtime_store.persist_paths_base import runtime_path

    return runtime_path(OVERRIDES_FILENAME)


def _read_all() -> dict[str, ChargeOverride]:
    path = _path()
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    jobs = doc.get("overrides") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        raise ValueError(f"{path}: Schlüssel 'overrides' fehlt oder ist kein Objekt.")
    return {cid: ChargeOverride.from_doc(cid, job) for cid, job in jobs.items()}


def _write_all(jobs: dict[str, ChargeOverride]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "schema_version": SCHEMA_VERSION,
                "overrides": {cid: job.to_doc() for cid, job in sorted(jobs.items())},
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
        fh.write("\n")
    os.replace(tmp, path)


def validate(target_soc_percent: float, ready_by: datetime, *, now: datetime) -> None:
    """Raise ``ValueError`` with a user-facing German message for an unusable job."""
    if not 1.0 <= float(target_soc_percent) <= 100.0:
        raise ValueError("Ziel-SOC muss zwischen 1 und 100 % liegen.")
    if ready_by <= now:
        raise ValueError("„Fertig bis“ muss in der Zukunft liegen.")


def set_override(
    consumer_id: str,
    *,
    target_soc_percent: float,
    ready_by: datetime,
    now: datetime | None = None,
    source: str = SOURCE_MONITOR,
) -> ChargeOverride:
    """Create or replace the job of ``consumer_id``."""
    stamp = now or datetime.now()
    validate(target_soc_percent, ready_by, now=stamp)
    job = ChargeOverride(
        consumer_id=str(consumer_id),
        target_soc_percent=float(target_soc_percent),
        ready_by=ready_by.replace(second=0, microsecond=0),
        created_at=stamp,
        source=source,
    )
    with _lock:
        jobs = _read_all()
        jobs[job.consumer_id] = job
        _write_all(jobs)
    logger.info(
        "Ladeauftrag %s: Ziel %.0f %% bis %s (%s).",
        job.consumer_id,
        job.target_soc_percent,
        job.ready_by.strftime("%d.%m. %H:%M"),
        source,
    )
    return job


def clear_override(consumer_id: str, *, reason: str) -> bool:
    """Remove the job of ``consumer_id``; True if there was one."""
    with _lock:
        jobs = _read_all()
        if jobs.pop(str(consumer_id), None) is None:
            return False
        _write_all(jobs)
    logger.info("Ladeauftrag %s beendet: %s.", consumer_id, reason)
    return True


def get_override(consumer_id: str) -> ChargeOverride | None:
    """The stored job, active or not (Monitor display)."""
    with _lock:
        return _read_all().get(str(consumer_id))


def active_override(
    consumer_id: str,
    *,
    now: datetime,
    plugged_in: bool | None,
) -> ChargeOverride | None:
    """The job that applies now, after ending it on unplug or a passed ready-by time.

    ``plugged_in``: the car's plug state, or ``None`` if the wallbox reports none (then only
    the ready-by time ends the job).
    """
    cid = str(consumer_id)
    with _lock:
        jobs = _read_all()
        job = jobs.get(cid)
        if job is None:
            return None
        end_reason = None
        if now >= _align(now, job.ready_by):
            end_reason = "Fertig-bis-Zeit erreicht"
        elif plugged_in is False and job.plug_seen:
            end_reason = "Auto abgesteckt"
        if end_reason is not None:
            jobs.pop(cid)
            _write_all(jobs)
        elif plugged_in and not job.plug_seen:
            job = ChargeOverride(**{**job.__dict__, "plug_seen": True})
            jobs[cid] = job
            _write_all(jobs)
    if end_reason is not None:
        logger.info("Ladeauftrag %s beendet: %s.", cid, end_reason)
        return None
    return job


def _align(reference: datetime, dt: datetime) -> datetime:
    from optimizer.charging_schedule import _align_like

    return _align_like(reference, dt)
