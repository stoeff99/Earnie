"""Live-Lesen: received values of non-activated consumer signals are flagged as unused."""
from __future__ import annotations

from ui import ehal_live_read_style as style

_POWER = "heatpump.waermepumpe.sens_power_act"
_ACTIVE = "heatpump.waermepumpe.sens_consumer_active"


def _row(field: str, wert: str, status: str) -> dict[str, str]:
    return {"EHAL-Feld": field, "Wert": wert, "Status": status, "Detail": "", "Zuletzt gelesen": ""}


def test_received_value_of_inactive_signal_is_marked():
    rows = style.mark_not_activated([_row(_POWER, "3.17", "OK")], frozenset({_POWER}))
    assert rows[0]["Status"] == style.NOT_ACTIVATED_STATUS
    assert rows[0]["Detail"].startswith("Empfang: OK; Wert kommt an")
    assert rows[0]["Wert"] == "3.17"


def test_held_value_keeps_its_receipt_state_in_detail():
    rows = style.mark_not_activated([_row(_ACTIVE, "0.0", "0 (gehalten)")], frozenset({_ACTIVE}))
    assert rows[0]["Status"] == style.NOT_ACTIVATED_STATUS
    assert "Empfang: 0 (gehalten)" in rows[0]["Detail"]


def test_activated_and_never_sent_rows_are_unchanged():
    rows = [
        _row(_POWER, "3.17", "OK"),
        _row(_ACTIVE, "0.0", "0 angenommen (nie gesendet)"),
        _row("heatpump.waermepumpe.sens_temperature_heat_storage_low", "", "Kein Mapping"),
    ]
    inactive = frozenset({_ACTIVE, "heatpump.waermepumpe.sens_temperature_heat_storage_low"})
    assert style.mark_not_activated(rows, inactive) == rows


def test_not_activated_rows_are_amber():
    assert style.live_read_text_color(style.NOT_ACTIVATED_STATUS, optional=True) == style._COLOR_STALE


def test_inactive_ids_come_from_consumer_catalog_entries(monkeypatch):
    from ehal import signal_catalog

    def _entry(kind, ehal_id, direction="read", activated=False):
        return signal_catalog.CatalogEntry(
            component_kind=kind, entity_id="x", consumer_type="", binding_key=ehal_id,
            ehal_id=ehal_id, group="", direction=direction, digital=False,
            is_required=False, is_activated=activated,
        )

    entries = [
        _entry("consumer", _POWER),
        _entry("consumer", _ACTIVE, activated=True),
        _entry("consumer", "heatpump.waermepumpe.set_enable", direction="write"),
        _entry("plant", "sens_power_consumers"),
    ]
    monkeypatch.setattr(signal_catalog, "iter_catalog_entries", lambda _h, _c: entries)
    monkeypatch.setattr("ui.house_config_io.load_house_profiles", dict)
    monkeypatch.setattr("ui.house_config_io._load_components_document", dict)
    assert style.inactive_consumer_read_ids() == frozenset({_POWER})
