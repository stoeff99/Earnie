"""Normalize thermal_annual consumer fields for house profiles."""
from __future__ import annotations

from house_config.heat_source import reject_legacy_heat_pump_type
from house_config.heating_systems import (
    HEATING_SYSTEM_ID_KEY,
    apply_heating_system,
    reject_inline_heating_fields,
    reject_measured_rod_fields,
    resolve_heating_system,
)
from house_config.house_cooling import COOLING_KEY, normalize_cooling
from house_config.house_thermal import (
    consumer_thermal_from_house,
    reject_consumer_building_fields,
)
from house_config.profile_schema_defaults import consumer_default


def int_or_default(value: object, default: int) -> int:
    """``int(value)``; only a missing / ``None`` / empty value yields ``default`` (0 is kept)."""
    if value is None or value == "":
        return default
    return int(value)


def _raw_thermal_field(raw: dict, key: str):
    if raw.get(key) not in (None, ""):
        return raw.get(key)
    nested = raw.get("thermal")
    if isinstance(nested, dict) and nested.get(key) not in (None, ""):
        return nested.get(key)
    return None


def _label(raw: dict) -> str:
    return f"thermal_annual '{raw.get('id') or '?'}'"


def _solar_thermal_fields(raw: dict) -> dict:
    """Solar-thermal collector of the consumer (heat generation, not the building)."""
    tilt = _raw_thermal_field(raw, "solar_thermal_tilt_deg")
    return {
        "solar_thermal_area_m2": float(
            _raw_thermal_field(raw, "solar_thermal_area_m2")
            or consumer_default("solar_thermal_area_m2")
        ),
        "solar_thermal_tilt_deg": float(
            consumer_default("solar_thermal_tilt_deg") if tilt is None else tilt
        ),
        "solar_thermal_azimuth_deg": float(
            _raw_thermal_field(raw, "solar_thermal_azimuth_deg")
            or consumer_default("solar_thermal_azimuth_deg")
        ),
    }


def _build_thermal_block(raw: dict, house_thermal: dict, persons: int) -> dict:
    """``spec['thermal']``: the house's building block plus the consumer's collector.

    The heat generation (``heat_source``, ``heat_storage``) comes from the heating-system
    catalog, see ``apply_heating_system``.
    """
    reject_legacy_heat_pump_type(raw, label=_label(raw))
    return {
        **consumer_thermal_from_house(house_thermal, persons),
        **_solar_thermal_fields(raw),
    }


# Heat pump boost / lock settings (optimizer.heat_control); kept on save and in the UI.
HEAT_CONTROL_KEYS: tuple[str, ...] = (
    "heat_lock_margin_cent",
    "heat_lock_max_quarterhours",
    "heat_boost_pv_percent",
    "heat_boost_mixed_pv_percent",
    "heat_boost_mixed_margin_cent",
    "heat_boost_grid_margin_cent",
    "heat_preheat_enabled",
)
_BOOST_KEYS = HEAT_CONTROL_KEYS[2:6]


def preheat_storage_kwh(consumer: dict) -> float:
    """Electric kWh the building can take in advance (pre-heating across midnight).

    Heat capacity (Wohnfläche × Bauweise) × comfort band (``house_tolerance_c``) as
    heat, divided by the heating system's JAZ (``heat_source``). 0 unless
    ``heat_preheat_enabled``.
    """
    from house_config.heat_source import heat_source_efficiency
    from optimizer.thermal_house import DEFAULT_HOUSE_TOLERANCE_C, house_capacity_kwh_per_k

    if not bool(consumer.get("heat_preheat_enabled", False)):
        return 0.0
    thermal = consumer.get("thermal") if isinstance(consumer.get("thermal"), dict) else consumer
    capacity = house_capacity_kwh_per_k(
        float(thermal.get("living_area_m2") or 0.0), thermal.get("building_mass")
    )
    tolerance = float(thermal.get("house_tolerance_c") or DEFAULT_HOUSE_TOLERANCE_C)
    heat_source = thermal.get("heat_source")
    if not isinstance(heat_source, dict):
        raise ValueError(
            f"Wärmepumpe '{consumer.get('id')}': Vorheizen braucht ein Heizsystem "
            "(heat_source) für die JAZ."
        )
    jaz = heat_source_efficiency(heat_source)
    return round(capacity * tolerance / jaz, 3)


def normalize_thermal_annual_consumer(
    raw: dict,
    spec: dict,
    *,
    copy_loxone_binding,
    heating_systems: dict | None,
    house_thermal: dict | None,
    persons: int,
) -> None:
    """Fill ``spec['thermal']`` and related flex fields for thermal_annual.

    The building fields come from the house profile (``house_thermal``, see
    ``house_config.house_thermal``); building fields on the consumer fail loud. The reference
    heating system (``heating_system_id``) is resolved against the catalog and placed into the
    spec; inline generation fields and measured-rod fields fail loud.
    """
    reject_consumer_building_fields(raw, label=_label(raw))
    if house_thermal is None:
        raise ValueError(
            f"{_label(raw)}: Hausprofil ohne Gebäude-Wärmedaten (thermal). Im "
            "Hauskonfigurator „Heizung betrachten“ einschalten und den Abschnitt „Gebäude / "
            "Wärmebedarf“ ausfüllen."
        )
    reject_inline_heating_fields(raw, label=_label(raw))
    reject_measured_rod_fields(raw, label=_label(raw))
    system = resolve_heating_system(
        heating_systems, raw.get(HEATING_SYSTEM_ID_KEY), label=_label(raw)
    )
    spec[HEATING_SYSTEM_ID_KEY] = system["id"]
    spec["thermal"] = _build_thermal_block(raw, house_thermal, persons)
    # The absent-mode setback is a house property; the core reads it on the consumer.
    spec["absent_mode_enabled"] = bool(house_thermal["absent_mode_enabled"])
    apply_heating_system(spec, system)
    if "optimizer_flex" in raw:
        spec["optimizer_flex"] = bool(raw["optimizer_flex"])
    window = raw.get("thermal_flex_window")
    if isinstance(window, dict) and window:
        spec["thermal_flex_window"] = dict(window)
    if "max_on_quarterhours" in raw:
        spec["max_on_quarterhours"] = max(1, int(raw.get("max_on_quarterhours", 16) or 16))
    if "min_off_quarterhours" in raw:
        spec["min_off_quarterhours"] = max(0, int(raw.get("min_off_quarterhours") or 0))
    if raw.get("heat_lock_margin_cent") is not None:
        spec["heat_lock_margin_cent"] = max(0.0, float(raw["heat_lock_margin_cent"]))
    if raw.get("heat_lock_max_quarterhours") is not None:
        spec["heat_lock_max_quarterhours"] = max(0, int(raw["heat_lock_max_quarterhours"]))
    for key in _BOOST_KEYS:
        if raw.get(key) is not None:
            spec[key] = max(0.0, float(raw[key]))
    if "heat_preheat_enabled" in raw:
        spec["heat_preheat_enabled"] = bool(raw["heat_preheat_enabled"])
    if "max_pulses_per_day" in raw:
        spec["max_pulses_per_day"] = max(1, int(raw.get("max_pulses_per_day", 4) or 4))
    min_on = int(spec.get("min_on_quarterhours", 1) or 1)
    if int(spec.get("min_off_quarterhours") or 0) > 1 and int(
        spec.get("max_on_quarterhours", 16)
    ) < min_on:
        raise ValueError(
            f"Wärmepumpe '{raw.get('id')}': max_on_quarterhours "
            f"({spec['max_on_quarterhours']}) ist kleiner als min_on_quarterhours ({min_on})."
        )
    if raw.get(COOLING_KEY) is not None:
        spec[COOLING_KEY] = normalize_cooling(
            raw[COOLING_KEY], label=_label(raw), heat_source=spec["thermal"].get("heat_source")
        )
    copy_loxone_binding(raw, spec)
