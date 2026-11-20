"""Mapped foreign MQTT topics publish on change: no repeat, valid while the session is up."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations import ehal_mqtt
from runtime_store import ehal_push_inbox as inbox

SOC_ID = "ev.wallbox.sens_evcs_soc_act"
POWER_ID = "consumer.sauna.sens_power_act"
NOW = datetime(2026, 11, 20, 7, 45, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EARNIE_RUNTIME_PATH", str(tmp_path))
    monkeypatch.delenv(inbox.REPEAT_ENV, raising=False)
    monkeypatch.delenv(inbox.REPEAT_ENV_LEGACY, raising=False)
    inbox.reset_memory_for_tests()
    yield
    inbox.reset_memory_for_tests()


@pytest.fixture
def link(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(up=True)
    monkeypatch.setattr(ehal_mqtt, "mqtt_session_connected", lambda: state.up)
    return state


def _push(ehal_id: str, raw: str, *, ago: timedelta, on_change: bool = True) -> None:
    inbox.record_push(
        ehal_id,
        raw,
        "mqtt:broker:1883",
        now=NOW - ago,
        publish=inbox.PUBLISH_ON_CHANGE if on_change else "",
    )


def test_on_change_value_stays_valid_for_days(link):
    _push(SOC_ID, "80", ago=timedelta(days=4))
    assert inbox.read_push_value(SOC_ID, now=NOW) == (80.0, inbox.STATE_OK)


def test_repeated_push_keeps_the_30_s_rule(link):
    _push(SOC_ID, "80", ago=timedelta(minutes=95), on_change=False)
    assert inbox.read_push_value(SOC_ID, now=NOW) == (None, inbox.STATE_READ_ERROR)


def test_on_change_value_fails_after_the_max_age(link):
    _push(SOC_ID, "80", ago=timedelta(seconds=inbox.ON_CHANGE_MAX_AGE_S + 60))
    assert inbox.read_push_value(SOC_ID, now=NOW)[0] is None


def test_on_change_value_fails_when_the_session_is_down(link):
    _push(SOC_ID, "80", ago=timedelta(hours=2))
    link.up = False
    assert inbox.read_push_value(SOC_ID, now=NOW) == (None, inbox.STATE_READ_ERROR)


def test_on_change_power_is_kept_not_assumed_zero(link):
    # A constant load is not re-sent; its value holds instead of "0 angenommen".
    _push(POWER_ID, "2.4", ago=timedelta(hours=3))
    _push("heartbeat_smarthome", "1", ago=timedelta(seconds=5), on_change=False)
    assert inbox.read_push_value(POWER_ID, now=NOW) == (2.4, inbox.STATE_OK)


def test_on_change_zero_is_a_plain_value(link):
    _push(POWER_ID, "0", ago=timedelta(hours=3))
    assert inbox.read_push_value(POWER_ID, now=NOW) == (0.0, inbox.STATE_OK)
    link.up = False
    assert inbox.derive_state(POWER_ID, inbox.memory_snapshot()[POWER_ID], link=True, now=NOW) == (
        inbox.STATE_READ_ERROR,
        None,
    )


def test_on_change_rows_do_not_prove_the_hub_link(link):
    _push(SOC_ID, "80", ago=timedelta(seconds=1))
    assert not inbox.link_alive(inbox.memory_snapshot(), now=NOW)


def test_mark_follows_the_latest_push(link):
    _push(SOC_ID, "80", ago=timedelta(hours=1))
    _push(SOC_ID, "81", ago=timedelta(minutes=50), on_change=False)
    assert not inbox.is_on_change(inbox.memory_snapshot()[SOC_ID])


def test_mapped_topic_is_recorded_as_on_change(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.test_ehal_mqtt import _SETTINGS, _FakeClient

    topic = "ioBroker/tesla-motors/0/VIN/charge_state/battery_level"
    monkeypatch.setattr(ehal_mqtt, "_link_status_path", lambda: str(tmp_path / "mqtt.json"))
    settings = replace(_SETTINGS, topic_map={topic: SOC_ID})
    carrier = ehal_mqtt.EhalMqttCarrier(settings, client_factory=_FakeClient, interval_s=3600)
    carrier._on_message(None, None, SimpleNamespace(topic=topic, payload=b"80"))
    assert inbox.is_on_change(inbox.memory_snapshot()[SOC_ID])


def test_session_state_comes_from_the_status_file_without_a_carrier(tmp_path, monkeypatch):
    path = tmp_path / "mqtt.json"
    monkeypatch.setattr(ehal_mqtt, "_link_status_path", lambda: str(path))
    assert not ehal_mqtt.mqtt_session_connected()  # no file: not connected
    ehal_mqtt._write_link_status({"connected": True})
    assert ehal_mqtt.mqtt_session_connected()
    ehal_mqtt._write_link_status({"connected": False})
    assert not ehal_mqtt.mqtt_session_connected()


def test_signal_list_shows_quiet_on_change_topic_as_ok(link):
    from ehal.signal_list import _read_match

    _push(SOC_ID, "80", ago=timedelta(days=2))
    kind, text = _read_match(SOC_ID, inbox.memory_snapshot())
    assert kind == "ok"
    assert "bei Änderung" in text


def test_live_read_shows_an_expired_value_instead_of_no_mapping(link):
    import ui.loxone_debug  # noqa: F401  (import order: loxone_debug first)
    from ui.loxone_debug_rows import _try_push_live_row

    _push(SOC_ID, "80", ago=timedelta(hours=2))
    link.up = False
    row = _try_push_live_row(SOC_ID, read_at="07:45:00")
    assert row is not None
    assert row["Wert"] in ("80", "80,0")
    assert row["Status"] == inbox.STATE_READ_ERROR
    assert "nur bei Änderung" in row["Detail"]
