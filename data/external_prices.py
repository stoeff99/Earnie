"""External import prices from an MQTT feed (EKZ dynamic tariff, 15-min intervals).

The feed replaces the EPEX market price of exactly one user spot tariff
(``ehal.mqtt.price_tariff_id``); Edibert still applies that tariff's markup, fee and VAT.
The EKZ series already contains grid use and levies, so that tariff gets no
``netznutzung`` on top (see :mod:`data.edibert_slot_prices`).

Slots the feed has not published yet are estimated from EPEX with a linear fit
``external ≈ a + b · EPEX`` over stored (EPEX, external) pairs of the same slots. Too few
pairs is an error, never a guess.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

STORE_FILENAME = "external_import_prices.json"
KEEP_DAYS = 21
MIN_FIT_PAIRS = 24
# The EKZ LoxBerry plugin copies the last published value into every unpublished slot.
FORWARD_FILL_MIN_RUN = 4
_SLOT = timedelta(minutes=15)
_lock = threading.Lock()


@dataclass(frozen=True)
class ParsedFeed:
    """Published slots (UTC slot start → Rp/kWh) of one feed message."""

    slots: dict[datetime, float]
    publication: datetime | None
    dropped_forward_fill: int


@dataclass(frozen=True)
class ExternalMarket:
    """Market input for the external tariff: published values plus estimated gaps."""

    market: dict[datetime, float]
    estimated: int
    fit: tuple[float, float] | None


def _parse_ts(raw: object, where: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError(f"Externe Preise: ungültiger Zeitstempel {raw!r} ({where}).") from exc
    if stamp.tzinfo is None:
        raise ValueError(f"Externe Preise: Zeitstempel ohne Zeitzone {raw!r} ({where}).")
    return stamp.astimezone(UTC)


def parse_ekz_intervals(payload: bytes | str) -> ParsedFeed:
    """``ekz/ems/tariffs/intervals`` JSON → published 15-min slots in Rp/kWh.

    Uses ``chf_per_kwh_sum`` (energy, grid use and levies, without VAT). ``total_chf`` adds a
    per-hour fee to a per-kWh price and is ignored. A trailing run of identical values is the
    plugin's copy of the last published slot: only its first slot counts.
    """
    text = payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Externe Preise: kein gültiges JSON ({exc}).") from exc
    intervals = doc.get("intervals") if isinstance(doc, dict) else None
    if not isinstance(intervals, list):
        raise ValueError("Externe Preise: Feld 'intervals' (Liste) fehlt.")
    rows: list[tuple[datetime, float]] = []
    for index, item in enumerate(intervals):
        where = f"intervals[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"Externe Preise: {where} ist kein Objekt.")
        start = _parse_ts(item.get("start_timestamp"), where)
        end = _parse_ts(item.get("end_timestamp"), where)
        if end - start != _SLOT:
            raise ValueError(f"Externe Preise: {where} ist kein 15-Minuten-Intervall.")
        try:
            chf = float(item["chf_per_kwh_sum"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Externe Preise: {where} ohne gültiges chf_per_kwh_sum.") from exc
        rows.append((start, chf * 100.0))
    rows.sort()
    run = 1
    while run < len(rows) and rows[-run - 1][1] == rows[-1][1]:
        run += 1
    dropped = run - 1 if run >= FORWARD_FILL_MIN_RUN else 0
    kept = rows[: len(rows) - dropped]
    publication = doc.get("publication_timestamp")
    return ParsedFeed(
        slots=dict(kept),
        publication=_parse_ts(publication, "publication_timestamp") if publication else None,
        dropped_forward_fill=dropped,
    )


# --- store (runtime file, shared by daemon and UI) ------------------------------------------


def _store_path() -> str:
    from runtime_store.persist_paths import runtime_path

    return runtime_path(STORE_FILENAME)


def load_store() -> dict[str, Any]:
    try:
        with open(_store_path(), encoding="utf-8") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        return {"slots": {}, "pairs": {}}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Externe Preise: {STORE_FILENAME} nicht lesbar ({exc}).") from exc
    doc.setdefault("slots", {})
    doc.setdefault("pairs", {})
    return doc


def _save_store(doc: dict[str, Any]) -> None:
    path = _store_path()
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _prune(entries: dict[str, Any], now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(days=KEEP_DAYS)
    return {key: value for key, value in entries.items() if datetime.fromisoformat(key) >= cutoff}


def ingest_ekz_payload(payload: bytes | str, *, now: datetime | None = None) -> ParsedFeed:
    """Parse one feed message and merge its published slots into the store."""
    feed = parse_ekz_intervals(payload)
    stamp = now or datetime.now(UTC)
    with _lock:
        doc = load_store()
        slots = dict(doc["slots"])
        slots.update({slot.isoformat(): value for slot, value in feed.slots.items()})
        doc["slots"] = _prune(slots, stamp)
        doc["pairs"] = _prune(dict(doc["pairs"]), stamp)
        doc["received_at"] = stamp.isoformat()
        if feed.publication is not None:
            doc["publication"] = feed.publication.isoformat()
        _save_store(doc)
    last = max(feed.slots) if feed.slots else None
    logger.info(
        "External prices: %d slots stored up to %s (%d copied slots ignored)",
        len(feed.slots),
        last.isoformat() if last else "-",
        feed.dropped_forward_fill,
    )
    return feed


def store_summary() -> dict[str, Any]:
    """For the UI: number of slots, last published slot end, last receive time."""
    doc = load_store()
    slots = sorted(datetime.fromisoformat(key) for key in doc["slots"])
    return {
        "slots": len(slots),
        "until": (slots[-1] + _SLOT).isoformat() if slots else None,
        "received_at": doc.get("received_at"),
        "publication": doc.get("publication"),
        "pairs": len(doc["pairs"]),
    }


# --- pricing -------------------------------------------------------------------------------


def fit_epex(pairs: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Least squares ``external = a + b · epex``; too few or flat pairs is an error."""
    if len(pairs) < MIN_FIT_PAIRS:
        raise ValueError(
            f"Externe Preise: nur {len(pairs)} Viertelstunden mit externem Preis und EPEX "
            f"(mindestens {MIN_FIT_PAIRS}) – fehlende Slots lassen sich noch nicht aus EPEX "
            "schätzen. Den Preis-Feed (Smarthome-Backend → MQTT-Broker) prüfen."
        )
    xs = [epex for epex, _ in pairs]
    ys = [value for _, value in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 1e-9:
        raise ValueError("Externe Preise: EPEX ohne Schwankung – keine Schätzung möglich.")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var_x
    return mean_y - slope * mean_x, slope


def external_market(
    utc_slots: Sequence[datetime], epex: Mapping[datetime, float]
) -> ExternalMarket:
    """Market input for the external tariff on ``utc_slots`` (UTC slot starts).

    Records new (EPEX, external) pairs for the fit, then estimates unpublished slots.
    """
    with _lock:
        doc = load_store()
        published = {datetime.fromisoformat(key): float(v) for key, v in doc["slots"].items()}
        pairs = {datetime.fromisoformat(key): tuple(v) for key, v in doc["pairs"].items()}
        new_pairs = {
            slot: (float(epex[slot]), published[slot])
            for slot in utc_slots
            if slot in published and slot in epex and pairs.get(slot) != (float(epex[slot]), published[slot])
        }
        if new_pairs:
            pairs.update(new_pairs)
            doc["pairs"] = _prune(
                {slot.isoformat(): list(pair) for slot, pair in pairs.items()},
                datetime.now(UTC),
            )
            _save_store(doc)
    market = {slot: published[slot] for slot in utc_slots if slot in published}
    missing = [slot for slot in utc_slots if slot not in published]
    if not missing:
        return ExternalMarket(market=market, estimated=0, fit=None)
    a, b = fit_epex(list(pairs.values()))
    for slot in missing:
        if slot not in epex:
            raise ValueError(
                f"Externe Preise: Slot {slot.isoformat()} hat weder einen externen Preis "
                "noch einen EPEX-Preis für die Schätzung."
            )
        market[slot] = a + b * float(epex[slot])
    return ExternalMarket(market=market, estimated=len(missing), fit=(a, b))


def external_tariff_id() -> str:
    """User tariff priced from the feed (``ehal.mqtt.price_tariff_id``); empty = none."""
    import config

    return str(config.get("EHAL_MQTT_PRICE_TARIFF_ID") or "").strip()
