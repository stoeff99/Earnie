"""Solcast PV forecast via MQTT (LoxBerry plugin PV Solcast NG)."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import data.pv_forecast as pv_forecast
from data import solcast_forecast
from integrations import ehal_mqtt
from settings.config_loaders import load_ehal_params
from settings.mqtt_settings import mqtt_block_from_settings, parse_mqtt_block

_TZ = ZoneInfo("Europe/Zurich")
_BRANCH = "solcast/system_1"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "solcast.json"
    monkeypatch.setattr(solcast_forecast, "_store_path", lambda: str(path))
    return path


def _hours(path) -> dict[str, float]:
    return json.loads(path.read_text(encoding="utf-8"))["branches"][_BRANCH]["hours"]


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=_TZ)


# --- settings ------------------------------------------------------------------------------


def test_pv_solcast_roundtrips_through_config_block():
    parsed = parse_mqtt_block(
        {"pv_solcast": [{"pv_system_id": "dach_sued", "topic": _BRANCH}]}, password=""
    )
    assert parsed.pv_solcast == {"dach_sued": _BRANCH}
    block = mqtt_block_from_settings(parsed)
    assert block["pv_solcast"] == [{"pv_system_id": "dach_sued", "topic": _BRANCH}]
    assert parse_mqtt_block(block, password="").pv_solcast == {"dach_sued": _BRANCH}


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{"pv_system_id": "dach_sued"}], "beide nötig"),
        ([{"pv_system_id": "a", "topic": "solcast/#"}], "Platzhalter"),
        ([{"pv_system_id": "a", "topic": "solcast/system_1/"}], "abschließenden"),
        ([{"pv_system_id": "a", "topic": "earnie/solcast"}], "Earnie-Präfix"),
        ([{"pv_system_id": "a", "topic": "ekz"}], "Topic der Preise"),
        ([{"pv_system_id": "a", "topic": "iob"}], "zugeordnete Topic"),
        (
            [{"pv_system_id": "a", "topic": _BRANCH}, {"pv_system_id": "a", "topic": "solcast/sum"}],
            "doppelt",
        ),
        (
            [{"pv_system_id": "a", "topic": _BRANCH}, {"pv_system_id": "b", "topic": _BRANCH}],
            "schon einer PV-Anlage",
        ),
        ({"a": _BRANCH}, "Liste"),
    ],
)
def test_pv_solcast_rejects_bad_rows(rows, message):
    with pytest.raises(ValueError, match=message):
        parse_mqtt_block(
            {
                "price_topic": "ekz/prices",
                "topic_map": [{"topic": "iob/soc", "ehal_id": "ev.e_auto.sens_evcs_soc_act"}],
                "pv_solcast": rows,
            },
            password="",
        )


def test_pv_solcast_reaches_runtime_config():
    params = load_ehal_params(
        {"ehal": {"mqtt": {"pv_solcast": [{"pv_system_id": "dach_sued", "topic": _BRANCH}]}}}
    )
    assert params["EHAL_MQTT_PV_SOLCAST"] == {"dach_sued": _BRANCH}


# --- ingest --------------------------------------------------------------------------------


def test_live_values_are_dated_by_arrival_day(store):
    ingest = solcast_forecast.SolcastIngest(tz=_TZ)
    now = _at(2, 18, 55)
    ingest.on_message(_BRANCH, "today_hour_12_kwh", "0.519", retained=False, now=now)
    ingest.on_message(_BRANCH, "tomorrow_hour_09_kwh", "0.202", retained=False, now=now)
    ingest.on_message(_BRANCH, "next_24h_kwh", "3.527", retained=False, now=now)
    assert _hours(store) == {"2026-11-02 12:00": 0.519, "2026-11-03 09:00": 0.202}


def test_retained_values_wait_for_same_day_last_update(store):
    ingest = solcast_forecast.SolcastIngest(tz=_TZ)
    now = _at(3, 10)
    ingest.on_message(_BRANCH, "tomorrow_hour_10_kwh", "0.6", retained=True, now=now)
    assert not store.exists()
    ingest.on_message(
        _BRANCH, "last_update_iso", "2026-11-03T09:40:00+01:00", retained=True, now=now
    )
    ingest.on_message(_BRANCH, "today_hour_11_kwh", "0.8", retained=True, now=now)
    assert _hours(store) == {"2026-11-04 10:00": 0.6, "2026-11-03 11:00": 0.8}


def test_retained_values_from_an_earlier_day_are_ignored(store, caplog):
    ingest = solcast_forecast.SolcastIngest(tz=_TZ)
    now = _at(3, 2)
    caplog.set_level(logging.INFO, logger="data.solcast_forecast")
    ingest.on_message(_BRANCH, "tomorrow_hour_10_kwh", "0.6", retained=True, now=now)
    ingest.on_message(
        _BRANCH, "last_update_iso", "2026-11-02T16:30:00+01:00", retained=True, now=now
    )
    ingest.on_message(_BRANCH, "today_hour_11_kwh", "0.8", retained=True, now=now)
    assert "hours" not in json.loads(store.read_text(encoding="utf-8"))["branches"][_BRANCH]
    assert "retained hour values" in caplog.text
    # The plugin's next live publish counts again.
    ingest.on_message(_BRANCH, "today_hour_11_kwh", "0.7", retained=False, now=_at(3, 7, 20))
    assert _hours(store) == {"2026-11-03 11:00": 0.7}


def test_bad_value_is_logged_and_not_stored(store, caplog):
    ingest = solcast_forecast.SolcastIngest(tz=_TZ)
    ingest.on_message(_BRANCH, "today_hour_11_kwh", "n/a", retained=False, now=_at(3, 9))
    ingest.on_message(_BRANCH, "today_hour_12_kwh", "-1", retained=False, now=_at(3, 9))
    assert not store.exists()
    assert caplog.text.count("rejected") == 2


def test_carrier_subscribes_branch_and_stores_values(store, tmp_path, monkeypatch):
    from tests.test_ehal_mqtt import _OK, _SETTINGS, _FakeClient

    monkeypatch.setattr(ehal_mqtt, "_link_status_path", lambda: str(tmp_path / "mqtt.json"))
    monkeypatch.setattr(ehal_mqtt, "_planning_tz", lambda: _TZ)
    settings = replace(_SETTINGS, pv_solcast={"dach_sued": _BRANCH})
    client = _FakeClient("x")
    instance = ehal_mqtt.EhalMqttCarrier(
        settings, client_factory=lambda _cid: client, status_builder=dict, interval_s=3600
    )
    instance.start()
    try:
        instance._on_connect(client, None, None, _OK, None)
        assert (f"{_BRANCH}/+", 0) in client.subscribed[0]
        instance._on_message(
            None,
            None,
            SimpleNamespace(topic=f"{_BRANCH}/tomorrow_hour_12_kwh", payload=b"0.883", retain=False),
        )
    finally:
        instance.stop()
    assert list(_hours(store).values()) == [0.883]


# --- forecast ------------------------------------------------------------------------------


@pytest.fixture
def plan(store, monkeypatch):
    """Two PV systems; ``dach_sued`` mapped to Solcast, ``garage`` on Forecast.Solar."""
    monkeypatch.setattr(
        "data.pv_forecast.config.get",
        lambda name, default=None, cast=None: {
            "LATITUDE": 47.2,
            "LONGITUDE": 8.5,
            "EHAL_MQTT_PV_SOLCAST": {"dach_sued": _BRANCH},
        }.get(name, default),
    )
    monkeypatch.setattr("data.pv_forecast.config.get_planning_timezone", lambda: "Europe/Zurich")
    monkeypatch.setattr(
        "data.pv_forecast.config.get_planning_pv_systems",
        lambda: [
            {"id": "dach_sued", "pv_kwp": 12.0, "pv_tilt": 36.0, "pv_azimuth": -20.0},
            {"id": "garage", "pv_kwp": 2.0, "pv_tilt": 10.0, "pv_azimuth": 0.0},
        ],
    )
    calls: list[float] = []

    def fake_forecast_one(*, lat, lon, tilt, azimuth, kwp, target_hours):
        calls.append(kwp)
        return [kwp / 10] * len(target_hours), True

    monkeypatch.setattr(pv_forecast, "_forecast_one_system", fake_forecast_one)
    return SimpleNamespace(calls=calls, set_now=lambda now: monkeypatch.setattr(
        pv_forecast, "planning_now", lambda tz=None: now
    ))


def _seed(store, hours: dict[str, float]) -> None:
    store.write_text(json.dumps({"branches": {_BRANCH: {"hours": hours}}}), encoding="utf-8")


def test_mapped_system_uses_solcast_without_forecast_solar_call(store, plan):
    plan.set_now(_at(3, 10))
    _seed(store, {"2026-11-03 10:00": 0.607, "2026-11-03 11:00": 0.836})
    result = pv_forecast.get_hourly_pv_forecast_by_system_for_hours(
        [_at(3, 10), _at(3, 10, 15), _at(3, 11)]
    )
    assert result["dach_sued"] == [0.607, 0.607, 0.836]
    assert result["garage"] == [0.2, 0.2, 0.2]
    assert plan.calls == [2.0]  # only the unmapped system asked Forecast.Solar
    usage = solcast_forecast.last_plan_usage()["systems"]["dach_sued"]
    assert usage["gap_hours"] == [] and usage["solcast_slots"] == 3


def test_gap_today_falls_back_to_forecast_solar_with_warning(store, plan, caplog):
    plan.set_now(_at(3, 10))
    _seed(store, {"2026-11-03 10:00": 0.607})
    result = pv_forecast.get_hourly_pv_forecast_by_system_for_hours([_at(3, 10), _at(3, 11)])
    assert result["dach_sued"] == [0.607, 1.2]
    assert "no value for 1 hour(s) 2026-11-03 11:00" in caplog.text
    usage = solcast_forecast.last_plan_usage()["systems"]["dach_sued"]
    assert usage["gap_hours"] == ["2026-11-03 11:00"]


def test_hours_after_tomorrow_are_no_gap(store, plan, caplog):
    plan.set_now(_at(2, 22))
    _seed(store, {"2026-11-03 23:00": 0.0})
    caplog.set_level(logging.INFO, logger="data.pv_forecast")
    result = pv_forecast.get_hourly_pv_forecast_by_system_for_hours([_at(3, 23), _at(4, 6)])
    assert result["dach_sued"] == [0.0, 1.2]
    assert "no value for" not in caplog.text
    assert "1 hour(s) after tomorrow" in caplog.text
    usage = solcast_forecast.last_plan_usage()["systems"]["dach_sued"]
    assert usage["gap_hours"] == [] and usage["beyond_hours"] == 1


def test_mapping_to_unknown_pv_system_is_reported(store, plan, monkeypatch, caplog):
    monkeypatch.setattr(
        "data.pv_forecast.config.get_planning_pv_systems",
        lambda: [{"id": "garage", "pv_kwp": 2.0, "pv_tilt": 10.0, "pv_azimuth": 0.0}],
    )
    plan.set_now(_at(3, 10))
    pv_forecast.get_hourly_pv_forecast_by_system_for_hours([_at(3, 10)])
    assert "which the live scenario does not have" in caplog.text
    assert solcast_forecast.last_plan_usage()["systems"]["dach_sued"]["unknown_system"] is True
