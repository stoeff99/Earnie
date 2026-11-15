"""A heat pump in the house-profile data model 6: heating-system catalog + house thermal."""
from __future__ import annotations

from house_config.heating_systems import normalize_heating_system
from house_config.house_thermal import normalize_house_thermal

SYSTEM_ID = "wp_sys"
# 145 m², building mass mittel (90 Wh/m²K), comfort band ±0.5 K.
HOUSE_THERMAL_RAW = {"living_area_m2": 145.0, "building_mass": "mittel", "house_tolerance_c": 0.5}


def heating_system_raw(min_on_quarterhours: int = 1) -> dict:
    return {
        "id": SYSTEM_ID,
        "heat_source": {"type": "heat_pump", "heat_pump_type": "erde"},
        "heating_power_kw": 12.9,
        "min_on_quarterhours": min_on_quarterhours,
    }


def normalize_kwargs(min_on_quarterhours: int = 1) -> dict:
    """Keyword arguments of ``_normalize_consumer`` for a thermal_annual consumer."""
    system = normalize_heating_system(heating_system_raw(min_on_quarterhours), 0)
    return {
        "heating_systems": {SYSTEM_ID: system},
        "house_thermal": normalize_house_thermal(HOUSE_THERMAL_RAW, label="test"),
        "persons": 4,
    }


def raw_heat_pump(**fields) -> dict:
    """Stored consumer entry (only the consumer's own fields)."""
    return {
        "id": "waermepumpe",
        "label": "Waermepumpe",
        "type": "thermal_annual",
        "heating_system_id": SYSTEM_ID,
        **fields,
    }


def normalized_heat_pump(min_on_quarterhours: int = 1, **fields) -> dict:
    from house_config.profiles_consumers import _normalize_consumer

    return _normalize_consumer(
        raw_heat_pump(**fields), 0, "house", **normalize_kwargs(min_on_quarterhours)
    )


def save_roundtrip(min_on_quarterhours: int = 1, **fields) -> tuple[dict, dict]:
    """(saved entry, normalized again from the saved entry), like a Hauskonfigurator save."""
    from house_config.profiles_consumers import _normalize_consumer, _serialize_consumer

    kwargs = normalize_kwargs(min_on_quarterhours)
    saved = _serialize_consumer(normalized_heat_pump(min_on_quarterhours, **fields))
    return saved, _normalize_consumer(saved, 0, "house", **kwargs)
