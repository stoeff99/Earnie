"""Fixed EUR→CHF rate: converts EPEX market prices for CHF tariffs only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from data.edibert_slot_prices import import_cents, pricing_reference

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = str(REPO_ROOT / "share" / "config" / "tariffs.json")
EKZ_SPOT = "ch_ekz_elektrizitaetswerke_des_kantons_zuerich_dynamischer_tarif"
EKZ_FIXED = "ch_ekz_elektrizit_tswerke_des_kantons_z_rich_ekz_mix_standard"


def _cents(tariff_id: str, profile: dict, epex: list[float]) -> list[float]:
    ref = pricing_reference(
        tariffs_path=CATALOG,
        grid_fees_path=None,
        import_tariff_id=tariff_id,
        export_tariff_id=None,
        timezone_name="Europe/Zurich",
        profile=profile,
    )
    start = datetime(2026, 7, 1, 10)
    slots = [start + timedelta(minutes=15 * i) for i in range(len(epex))]
    return import_cents(slots, epex, ref)


def test_rate_scales_epex_part_of_chf_spot_tariff():
    epex = [10.0, 10.0, 10.0, 10.0]
    plain = _cents(EKZ_SPOT, {"land": "CH"}, epex)
    doubled = _cents(EKZ_SPOT, {"land": "CH"}, [20.0] * 4)
    converted = _cents(EKZ_SPOT, {"land": "CH", "eur_chf_rate": 2.0}, epex)
    # A rate of 2 prices 10 EUR-cent like 20 Rp.
    assert converted == pytest.approx(doubled)
    assert converted != pytest.approx(plain)


def test_rate_leaves_fixed_chf_tariff_untouched():
    epex = [10.0] * 4
    assert _cents(EKZ_FIXED, {"land": "CH", "eur_chf_rate": 2.0}, epex) == pytest.approx(
        _cents(EKZ_FIXED, {"land": "CH"}, epex)
    )


def test_missing_rate_warns_once(caplog):
    from data import edibert_slot_prices

    edibert_slot_prices._LOGGED_WARNINGS.discard((EKZ_SPOT, "eur_chf_rate"))
    _cents(EKZ_SPOT, {"land": "CH"}, [10.0] * 4)
    _cents(EKZ_SPOT, {"land": "CH"}, [10.0] * 4)
    assert caplog.text.count("Wechselkurs EUR → CHF") == 1


def _profile(**extra) -> dict:
    return {
        "id": "home",
        "label": "Home",
        "annual_kwh": 10000.0,
        "latitude": 47.2,
        "longitude": 8.5,
        "netznutzung": {"area_id": "user_no_grid_fee"},
        "consumers": [],
        **extra,
    }


def test_profile_rate_validation_and_roundtrip(tmp_path):
    from house_config.profiles_store import (
        clear_house_profiles_doc_cache,
        load_house_profiles_document,
        normalize_house_profiles_document,
        save_house_profiles_document,
    )

    def doc(**extra):
        return {"profiles": [_profile(**extra)], "earnie_data_model": 4}

    with pytest.raises(ValueError, match="nur für Land CH"):
        normalize_house_profiles_document(doc(land="AT", eur_chf_rate=0.94))
    with pytest.raises(ValueError, match="muss > 0"):
        normalize_house_profiles_document(doc(land="CH", eur_chf_rate=0))
    path = tmp_path / "house_profiles.json"
    save_house_profiles_document(str(path), doc(land="CH", eur_chf_rate=0.9412))
    on_disk = json.loads(path.read_text(encoding="utf-8"))["profiles"][0]
    assert on_disk["eur_chf_rate"] == pytest.approx(0.9412)
    clear_house_profiles_doc_cache()
    loaded = load_house_profiles_document(str(path))["profiles"]["home"]
    assert loaded["eur_chf_rate"] == pytest.approx(0.9412)
    save_house_profiles_document(str(path), doc(land="CH"))
    assert "eur_chf_rate" not in json.loads(path.read_text(encoding="utf-8"))["profiles"][0]
