"""Display currency from the house profile country: CH → CHF / Rp, AT/DE → € / Cent.

Only labels change. Prices stay in hundredths of the currency (Cent or Rappen) and
amounts in whole units (€ or CHF), so no value is ever converted. Internal row keys
such as ``"Strompreis (Cent/kWh)"`` stay as they are; use ``display_label`` to show
them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CurrencyUnits:
    code: str
    """ISO code (``EUR`` / ``CHF``)."""
    major: str
    """Amount unit shown after a value (``€`` / ``CHF``)."""
    minor: str
    """Hundredth of the currency (``Cent`` / ``Rp``)."""

    @property
    def per_kwh(self) -> str:
        return f"{self.minor}/kWh"

    def amount(self, value: float, fmt: str = ".2f") -> str:
        return f"{value:{fmt}} {self.major}"


EUR = CurrencyUnits(code="EUR", major="€", minor="Cent")
CHF = CurrencyUnits(code="CHF", major="CHF", minor="Rp")

_UNITS_BY_LAND = {"AT": EUR, "DE": EUR, "CH": CHF}
_UNITS_BY_CODE = {"EUR": EUR, "CHF": CHF}


def units_for_land(land: str) -> CurrencyUnits:
    key = str(land or "").strip().upper()
    if key not in _UNITS_BY_LAND:
        raise ValueError(f"Währung: unbekanntes Land {land!r} (erwartet AT, DE oder CH).")
    return _UNITS_BY_LAND[key]


def units_for_code(code: str) -> CurrencyUnits:
    key = str(code or "").strip().upper()
    if key not in _UNITS_BY_CODE:
        raise ValueError(f"Währung: unbekannter Code {code!r} (erwartet EUR oder CHF).")
    return _UNITS_BY_CODE[key]


def active_units() -> CurrencyUnits:
    """Units of the active house profile.

    Without a house profile (greenfield bootstrap) the profile country defaults to
    AT everywhere else (timezone, region), so this shows € / Cent as before.
    """
    import config

    profile = config.get_resolved_runtime_settings().get("_house_profile")
    land = profile.get("land") if isinstance(profile, dict) else None
    return units_for_land(land or "AT")


def display_label(text: str, units: CurrencyUnits | None = None) -> str:
    """Rewrite a Cent/€ label (e.g. an internal row key) into the active currency."""
    units = units or active_units()
    if units is EUR:
        return text
    return (
        text.replace("Cent/kWh", units.per_kwh)
        .replace("ct/kWh", units.per_kwh)
        .replace("€", units.major)
    )
