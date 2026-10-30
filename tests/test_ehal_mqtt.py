"""MQTT carrier: settings validation, inbound routing, status publish (fake client, no broker)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from integrations import ehal_mqtt
from settings.config_loaders import load_ehal_params
from settings.mqtt_settings import (
    DISABLED,
    MqttSettings,
    mqtt_block_from_settings,
    parse_mqtt_block,
)

_SETTINGS = MqttSettings(
    enabled=True,
    host="192.168.1.20",
    port=1883,
    username="loxberry",
    password="test-pass",
    topic_prefix="earnie",
)


# --- settings ---------------------------------------------------------------------------


def test_no_block_means_disabled():
    assert parse_mqtt_block(None, password="") == DISABLED


def test_full_block_roundtrips_without_password():
    settings = parse_mqtt_block(
        {
            "enabled": True,
            "host": " 10.0.0.5 ",
            "port": "1884",
            "username": "u",
            "topic_prefix": "home/earnie",
        },
        password="pw",
    )
    assert settings == MqttSettings(True, "10.0.0.5", 1884, "u", "pw", "home/earnie")
    block = mqtt_block_from_settings(settings)
    assert "password" not in block
    assert parse_mqtt_block(block, password="pw") == settings


@pytest.mark.parametrize(
    "block, message",
    [
        ({"enabled": True}, "host fehlt"),
        ({"enabled": "yes", "host": "h"}, "true oder false"),
        ({"enabled": True, "host": "h", "port": 0}, "zwischen 1 und 65535"),
        ({"enabled": True, "host": "h", "port": "abc"}, "Zahl"),
        ({"enabled": True, "host": "h", "topic_prefix": "earnie/#"}, "Platzhalter"),
        ({"enabled": True, "host": "h", "topic_prefix": "/earnie"}, "führenden"),
        ({"enabled": True, "host": "h", "topic_prefix": ""}, "nicht leer"),
        ("broker", "Objekt"),
    ],
)
def test_invalid_blocks_fail_loud(block, message):
    with pytest.raises(ValueError, match=message):
        parse_mqtt_block(block, password="")


def test_disabled_block_may_omit_host():
    settings = parse_mqtt_block({"enabled": False, "port": 1883}, password="")
    assert settings.enabled is False
    assert settings.host == ""


def test_config_loader_exposes_mqtt_keys(monkeypatch):
    monkeypatch.setenv("EHAL_MQTT_PASSWORD", "secret")
    params = load_ehal_params(
        {"ehal": {"mqtt": {"enabled": True, "host": "broker", "port": 8883}}}
    )
    assert params["EHAL_MQTT_ENABLED"] is True
    assert params["EHAL_MQTT_HOST"] == "broker"
    assert params["EHAL_MQTT_PORT"] == 8883
    assert params["EHAL_MQTT_TOPIC_PREFIX"] == "earnie"


def test_config_loader_rejects_enabled_without_host():
    with pytest.raises(ValueError, match="host fehlt"):
        load_ehal_params({"ehal": {"mqtt": {"enabled": True}}})


# --- inbound ----------------------------------------------------------------------------


@pytest.fixture
def inbox(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    def _record(ehal_id, raw, peer=""):
        calls.append((ehal_id, raw, peer))
        return "ok"

    monkeypatch.setattr("runtime_store.ehal_push_inbox.record_push", _record)
    return calls


def test_telemetry_topic_goes_to_push_inbox(inbox):
    result = ehal_mqtt.handle_inbound(
        "earnie",
        "earnie/telemetry/ess.haus.sens_ess_soc",
        b" 54.5 ",
        "mqtt:broker:1883",
    )
    assert result == "ok"
    assert inbox == [("ess.haus.sens_ess_soc", "54.5", "mqtt:broker:1883")]


def test_heartbeat_records_hub_callback(inbox, monkeypatch):
    peers: list[str] = []
    monkeypatch.setattr(
        "runtime_store.loxone_callback_status.record_loxone_callback", peers.append
    )
    ehal_mqtt.handle_inbound("earnie", "earnie/telemetry/heartbeat_smarthome", b"1", "mqtt:b:1")
    assert peers == ["mqtt:b:1"]


def test_request_optimize_queues_early_run(inbox, monkeypatch):
    from integrations import ehal_request_http

    ehal_request_http.clear_optimize_request()
    result = ehal_mqtt.handle_inbound("earnie", "earnie/request_optimize", b"", "mqtt:b:1")
    assert result == "ok"
    assert ehal_request_http.consume_optimize_request() is True
    assert inbox == [("request_optimize", "1", "mqtt:b:1")]


def test_foreign_topic_is_ignored(inbox):
    assert ehal_mqtt.handle_inbound("earnie", "other/telemetry/x", b"1", "p") == "unknown_topic"
    assert inbox == []


def test_rejected_push_is_reported(monkeypatch):
    monkeypatch.setattr(
        "runtime_store.ehal_push_inbox.record_push", lambda *_a, **_k: "bad_id"
    )
    assert ehal_mqtt.handle_inbound("earnie", "earnie/telemetry/bad id", b"1", "p") == "bad_id"


# --- outbound ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, text",
    [(2.5, "2.5"), (0.0, "0"), (1730000000, "1730000000"), (-1.25, "-1.25"), (0.1234567, "0.123457")],
)
def test_format_value(value, text):
    assert ehal_mqtt.format_value(value) == text


def test_status_messages_include_json_and_one_topic_per_key():
    messages = ehal_mqtt.status_messages("earnie", {"heartbeat_earnie": 100, "ess.haus.set_ess_mode": 2.0})
    assert messages[0][0] == "earnie/status"
    assert json.loads(messages[0][1]) == {"heartbeat_earnie": 100, "ess.haus.set_ess_mode": 2.0}
    assert messages[1:] == [
        ("earnie/setpoint/ess.haus.set_ess_mode", "2"),
        ("earnie/setpoint/heartbeat_earnie", "100"),
    ]


class _FakeClient:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.published: list[tuple[str, str, int, bool]] = []
        self.subscribed: list = []
        self.will = None
        self.credentials = None
        self.connected_to = None
        self.loop_running = False

    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay, max_delay):
        pass

    def connect_async(self, host, port, keepalive):
        self.connected_to = (host, port)

    def loop_start(self):
        self.loop_running = True

    def loop_stop(self):
        self.loop_running = False

    def subscribe(self, topics):
        self.subscribed.append(topics)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def disconnect(self):
        pass


_OK = SimpleNamespace(is_failure=False)
class _Refused:
    is_failure = True

    def __str__(self) -> str:
        return "Not authorized"


_REFUSED = _Refused()


@pytest.fixture
def carrier(tmp_path, monkeypatch):
    monkeypatch.setattr(ehal_mqtt, "_link_status_path", lambda: str(tmp_path / "mqtt.json"))
    status = {"heartbeat_earnie": 1, "ess.haus.set_ess_mode": 0.0}
    clients: list[_FakeClient] = []

    def _factory(client_id):
        clients.append(_FakeClient(client_id))
        return clients[-1]

    instance = ehal_mqtt.EhalMqttCarrier(
        _SETTINGS,
        client_factory=_factory,
        status_builder=lambda: dict(status),
        interval_s=3600,
    )
    instance.start()
    yield SimpleNamespace(carrier=instance, client=clients[0], status=status)
    instance.stop()


def test_start_sets_login_will_and_connects_async(carrier):
    client = carrier.client
    assert client.credentials == ("loxberry", "test-pass")
    assert client.will == ("earnie/availability", "offline", 1, True)
    assert client.connected_to == ("192.168.1.20", 1883)
    assert client.loop_running is True
    assert carrier.carrier.is_connected() is False


def test_nothing_is_published_before_connect(carrier):
    assert carrier.carrier.publish_status_once() == 0
    assert carrier.client.published == []


def test_connect_subscribes_announces_and_publishes_full_status(carrier):
    carrier.carrier._on_connect(carrier.client, None, None, _OK, None)
    client = carrier.client
    assert client.subscribed == [[("earnie/telemetry/#", 0), ("earnie/request_optimize", 0)]]
    topics = [p[0] for p in client.published]
    assert topics == [
        "earnie/availability",
        "earnie/status",
        "earnie/setpoint/ess.haus.set_ess_mode",
        "earnie/setpoint/heartbeat_earnie",
    ]
    assert all(retain for *_rest, retain in client.published)
    assert ehal_mqtt.load_link_status()["connected"] is True


def test_only_changed_values_are_republished(carrier):
    carrier.carrier._on_connect(carrier.client, None, None, _OK, None)
    carrier.client.published.clear()
    carrier.status["heartbeat_earnie"] = 2
    sent = carrier.carrier.publish_status_once()
    assert sent == 2
    assert [p[0] for p in carrier.client.published] == [
        "earnie/status",
        "earnie/setpoint/heartbeat_earnie",
    ]


def test_reconnect_republishes_everything(carrier):
    carrier.carrier._on_connect(carrier.client, None, None, _OK, None)
    carrier.carrier._on_disconnect(carrier.client, None, None, "lost", None)
    assert carrier.carrier.is_connected() is False
    carrier.client.published.clear()
    carrier.carrier._on_connect(carrier.client, None, None, _OK, None)
    assert len(carrier.client.published) == 4


def test_refused_connect_is_recorded_and_publishes_nothing(carrier, tmp_path):
    carrier.carrier._on_connect(carrier.client, None, None, _REFUSED, None)
    assert carrier.carrier.is_connected() is False
    assert carrier.client.published == []
    status = json.loads((tmp_path / "mqtt.json").read_text(encoding="utf-8"))
    assert status["connected"] is False
    assert status["error"] == "Not authorized"


def test_disconnect_after_refusal_keeps_the_refusal_reason(carrier, tmp_path):
    carrier.carrier._on_connect(carrier.client, None, None, _REFUSED, None)
    carrier.carrier._on_disconnect(carrier.client, None, None, "Unspecified error", None)
    status = json.loads((tmp_path / "mqtt.json").read_text(encoding="utf-8"))
    assert status["error"] == "Not authorized"


def test_message_callback_routes_to_inbox(carrier, inbox):
    msg = SimpleNamespace(topic="earnie/telemetry/sens_temperature_outside", payload=b"7.5")
    carrier.carrier._on_message(carrier.client, None, msg)
    assert inbox == [("sens_temperature_outside", "7.5", "mqtt:192.168.1.20:1883")]


def test_stop_announces_offline(carrier):
    carrier.carrier._on_connect(carrier.client, None, None, _OK, None)
    carrier.carrier.stop()
    assert carrier.client.published[-1] == ("earnie/availability", "offline", 1, True)
    assert carrier.client.loop_running is False


def test_start_ehal_mqtt_skips_when_disabled():
    assert ehal_mqtt.start_ehal_mqtt(DISABLED) is None


def test_start_ehal_mqtt_skips_in_shadow_mode(monkeypatch):
    monkeypatch.setattr("runtime_store.shadow.mode.is_shadow_mode", lambda: True)
    assert ehal_mqtt.start_ehal_mqtt(_SETTINGS) is None


# --- probe ------------------------------------------------------------------------------


class _ProbeClient(_FakeClient):
    def __init__(self, client_id, *, reason=_OK, fail=None):
        super().__init__(client_id)
        self._reason = reason
        self._fail = fail
        self.on_connect = None

    def connect(self, host, port, keepalive):
        if self._fail is not None:
            raise self._fail

    def loop_start(self):
        super().loop_start()
        self.on_connect(self, None, None, self._reason, None)


def test_probe_success():
    assert ehal_mqtt.probe_broker(_SETTINGS, client_factory=_ProbeClient) is None


def test_probe_refused():
    error = ehal_mqtt.probe_broker(
        _SETTINGS, client_factory=lambda cid: _ProbeClient(cid, reason=_REFUSED)
    )
    assert error == "Broker hat die Verbindung abgelehnt: Not authorized"


def test_probe_unreachable():
    error = ehal_mqtt.probe_broker(
        _SETTINGS,
        client_factory=lambda cid: _ProbeClient(cid, fail=ConnectionRefusedError("refused")),
    )
    assert error.startswith("Broker 192.168.1.20:1883 nicht erreichbar")
