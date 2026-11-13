"""Display currency follows the house profile country (CH → CHF / Rp)."""
from __future__ import annotations

import pytest

from house_config import currency_display
from house_config.currency_display import (
    CHF,
    EUR,
    display_label,
    units_for_code,
    units_for_land,
)


@pytest.fixture
def swiss(monkeypatch):
    import config

    monkeypatch.setattr(
        config, "get_resolved_runtime_settings", lambda: {"_house_profile": {"land": "CH"}}
    )
    return CHF


def test_units_by_land_and_code():
    assert units_for_land("ch") is CHF
    assert units_for_land("AT") is EUR
    assert units_for_land("DE") is EUR
    assert units_for_code("CHF") is CHF
    with pytest.raises(ValueError, match="unbekanntes Land"):
        units_for_land("FR")
    with pytest.raises(ValueError, match="unbekannter Code"):
        units_for_code("USD")


def test_display_label_only_rewrites_for_chf():
    assert CHF.per_kwh == "Rp/kWh"
    assert CHF.amount(12.345) == "12.35 CHF"
    assert EUR.amount(-0.84, "+.2f") == "-0.84 €"
    assert display_label("Strompreis (Cent/kWh)", CHF) == "Strompreis (Rp/kWh)"
    assert display_label("fiktiver ct/kWh-Preis · Kosten (€)", CHF) == (
        "fiktiver Rp/kWh-Preis · Kosten (CHF)"
    )
    assert display_label("Strompreis (Cent/kWh) · 3 €", EUR) == "Strompreis (Cent/kWh) · 3 €"


def test_active_units_from_house_profile(monkeypatch):
    import config

    monkeypatch.setattr(
        config, "get_resolved_runtime_settings", lambda: {"_house_profile": {"land": "CH"}}
    )
    assert currency_display.active_units() is CHF
    monkeypatch.setattr(config, "get_resolved_runtime_settings", lambda: {})
    assert currency_display.active_units() is EUR


def test_cost_annotations_in_chf(swiss):
    from ui.chart_decorations import _cost_summary_annotations

    texts = [item["text"] for item in _cost_summary_annotations(12.34, 11.50)]
    assert texts == ["BL Ziel: 12.34 CHF", "Optimiert: 11.50 CHF", "Ersparnis: -0.84 CHF"]


def test_tariff_preview_uses_tariff_currency(swiss):
    from data.tariff_parameter_preview import tariff_parameter_rows

    chf_rows = dict(
        tariff_parameter_rows(
            {"type": "fixed_cent", "land": "CH", "currency": "CHF", "price_cent_kwh": 28.5},
            kind="import",
        )
    )
    assert chf_rows["Arbeitspreis"] == "28.500 Rp/kWh"
    # An Austrian catalog tariff stays in Cent even for a Swiss house.
    eur_rows = dict(
        tariff_parameter_rows(
            {"type": "fixed_cent", "land": "AT", "price_cent_kwh": 28.5}, kind="import"
        )
    )
    assert eur_rows["Arbeitspreis"] == "28.500 Cent/kWh"


def test_invoice_amounts_in_chf(swiss):
    from simulation.se_invoice_format import _fmt_eur

    assert _fmt_eur(14) == "14.00 CHF"
