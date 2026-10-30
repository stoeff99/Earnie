"""MQTT carrier for the EHAL push contract (third carrier next to HTTP push / status.json).

Backend-neutral: the hub (Loxone via the LoxBerry MQTT gateway, Home Assistant,
Node-RED, …) talks MQTT instead of HTTP, Earnie's read/write paths stay the same.

Topics under ``<prefix>`` (``ehal.mqtt.topic_prefix``, default ``earnie``):

- ``<prefix>/telemetry/<EHAL-ID>`` (subscribe): plain value, same as
  ``GET /ehal/telemetry/<EHAL-ID>/<value>`` → push inbox.
- ``<prefix>/request_optimize`` (subscribe): any payload queues an early optimize.
- ``<prefix>/status`` (publish, retained): the ``status.json`` payload as JSON.
- ``<prefix>/setpoint/<EHAL-ID>`` (publish, retained): one value per
  ``status.json`` key, for gateways that map one topic to one input.
- ``<prefix>/availability`` (publish, retained): ``online`` / ``offline`` (last will).

Earnie cannot see whether a subscriber consumed a retained message, so a publish
is never counted as a hub fetch; only the hub heartbeat
(``telemetry/heartbeat_smarthome``) records the hub callback, as on HTTP.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from settings.mqtt_settings import MqttSettings

logger = logging.getLogger(__name__)

TOPIC_TELEMETRY = "telemetry"
TOPIC_REQUEST_OPTIMIZE = "request_optimize"
TOPIC_STATUS = "status"
TOPIC_SETPOINT = "setpoint"
TOPIC_AVAILABILITY = "availability"
PAYLOAD_ONLINE = "online"
PAYLOAD_OFFLINE = "offline"
STATUS_INTERVAL_S = 10.0
KEEPALIVE_S = 60
LINK_STATUS_FILENAME = "ehal_mqtt_status.json"

RESULT_OK = "ok"
RESULT_UNKNOWN_TOPIC = "unknown_topic"


def topic(prefix: str, *parts: str) -> str:
    return "/".join((prefix, *parts))


def format_value(value: float | int) -> str:
    """Plain decimal text (no exponent), trailing zeros stripped: ``2.5``, ``0``, ``1730000000``."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def peer_label(settings: MqttSettings) -> str:
    return f"mqtt:{settings.host}:{settings.port}"


def handle_inbound(prefix: str, msg_topic: str, payload: bytes, peer: str) -> str:
    """Route one received message; returns ``record_push`` result or ``unknown_topic``."""
    from ehal.push_signals import HEARTBEAT_ID, REQUEST_OPTIMIZE_ID
    from runtime_store.ehal_push_inbox import record_push

    text = payload.decode("utf-8", errors="replace").strip()
    if msg_topic == topic(prefix, TOPIC_REQUEST_OPTIMIZE):
        from integrations.ehal_request_http import signal_optimize_request

        signal_optimize_request()
        record_push(REQUEST_OPTIMIZE_ID, "1", peer)
        try:
            from runtime_store.shadow.hooks import record_event

            record_event("trigger:request_optimize")
        except Exception:  # noqa: BLE001 — same as the HTTP path
            pass
        logger.info("MQTT request_optimize received — early optimize queued.")
        return RESULT_OK
    telemetry_root = topic(prefix, TOPIC_TELEMETRY) + "/"
    if not msg_topic.startswith(telemetry_root):
        return RESULT_UNKNOWN_TOPIC
    ehal_id = msg_topic[len(telemetry_root):]
    result = record_push(ehal_id, text, peer)
    if result in ("ok", "new"):
        if ehal_id == HEARTBEAT_ID:
            from runtime_store.loxone_callback_status import record_loxone_callback

            record_loxone_callback(peer)
        return result
    logger.warning("MQTT telemetry rejected (%s): topic=%s", result, msg_topic)
    return result


def status_messages(prefix: str, payload: Mapping[str, float | int]) -> list[tuple[str, str]]:
    """``(topic, text)`` for the full status JSON and one topic per status key."""
    messages = [
        (topic(prefix, TOPIC_STATUS), json.dumps(dict(payload), sort_keys=True)),
    ]
    for key in sorted(payload):
        messages.append((topic(prefix, TOPIC_SETPOINT, str(key)), format_value(payload[key])))
    return messages


def _link_status_path() -> str:
    from runtime_store.persist_paths import runtime_path

    return runtime_path(LINK_STATUS_FILENAME)


def _write_link_status(doc: dict[str, Any]) -> None:
    """Daemon → UI (separate process): last connect state of the carrier."""
    path = _link_status_path()
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("MQTT link status not written: %s", exc)


def load_link_status() -> dict[str, Any] | None:
    try:
        with open(_link_status_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_paho_client(client_id: str) -> Any:
    import paho.mqtt.client as mqtt

    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
    )


class EhalMqttCarrier:
    """One broker connection: subscribes telemetry, publishes status every interval."""

    def __init__(
        self,
        settings: MqttSettings,
        *,
        client_factory: Callable[[str], Any] = make_paho_client,
        status_builder: Callable[[], Mapping[str, float | int]] | None = None,
        interval_s: float = STATUS_INTERVAL_S,
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._status_builder = status_builder
        self._interval_s = float(interval_s)
        self._client: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._connected = False
        self._refused = False
        self._last_sent: dict[str, str] = {}

    # --- broker callbacks (paho callback API v2) -------------------------------------------

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if getattr(reason_code, "is_failure", False):
            logger.error(
                "MQTT broker %s:%s refused the connection: %s",
                self.settings.host,
                self.settings.port,
                reason_code,
            )
            self._refused = True
            self._record_link(connected=False, error=str(reason_code))
            return
        self._refused = False
        prefix = self.settings.topic_prefix
        client.subscribe(
            [
                (topic(prefix, TOPIC_TELEMETRY, "#"), 0),
                (topic(prefix, TOPIC_REQUEST_OPTIMIZE), 0),
            ]
        )
        client.publish(topic(prefix, TOPIC_AVAILABILITY), PAYLOAD_ONLINE, qos=1, retain=True)
        with self._lock:
            self._connected = True
            # A fresh session gets every value again (retained state may be gone).
            self._last_sent.clear()
        logger.info(
            "MQTT connected to %s:%s (prefix %s)",
            self.settings.host,
            self.settings.port,
            prefix,
        )
        self._record_link(connected=True, error="")
        self.publish_status_once()

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        with self._lock:
            self._connected = False
        if self._stop.is_set():
            return
        if self._refused:
            # The broker closes a refused session; keep its reason ("Not authorized").
            return
        logger.error(
            "MQTT connection to %s:%s lost (%s) — reconnecting",
            self.settings.host,
            self.settings.port,
            reason_code,
        )
        self._record_link(connected=False, error=str(reason_code))

    def _on_message(self, _client, _userdata, msg) -> None:
        try:
            handle_inbound(
                self.settings.topic_prefix,
                str(msg.topic),
                bytes(msg.payload or b""),
                peer_label(self.settings),
            )
        except Exception:  # noqa: BLE001 — the paho network thread must keep running
            logger.exception("MQTT message on %s failed", msg.topic)

    # --- lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        settings = self.settings
        client = self._client_factory(f"earnie-{settings.topic_prefix}-{os.getpid()}")
        if settings.username:
            client.username_pw_set(settings.username, settings.password or None)
        client.will_set(
            topic(settings.topic_prefix, TOPIC_AVAILABILITY),
            PAYLOAD_OFFLINE,
            qos=1,
            retain=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client = client
        self._record_link(connected=False, error="")
        # Async: an unreachable broker must not block daemon start; paho keeps retrying.
        client.connect_async(settings.host, settings.port, keepalive=KEEPALIVE_S)
        client.loop_start()
        self._thread = threading.Thread(
            target=self._publish_loop, name="ehal-mqtt-status", daemon=True
        )
        self._thread.start()
        logger.info(
            "MQTT carrier started: broker %s:%s, prefix %s",
            settings.host,
            settings.port,
            settings.topic_prefix,
        )

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.publish(
                    topic(self.settings.topic_prefix, TOPIC_AVAILABILITY),
                    PAYLOAD_OFFLINE,
                    qos=1,
                    retain=True,
                )
                client.disconnect()
            finally:
                client.loop_stop()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s + 1.0)
        self._record_link(connected=False, error="")

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    # --- publish --------------------------------------------------------------------------

    def _build_status(self) -> Mapping[str, float | int]:
        if self._status_builder is not None:
            return self._status_builder()
        from integrations.loxone_status_json import build_loxone_status_payload

        return build_loxone_status_payload()

    def publish_status_once(self) -> int:
        """Publish the status JSON plus every changed per-key value; returns topics sent."""
        if not self.is_connected():
            return 0
        sent = 0
        for msg_topic, text in status_messages(self.settings.topic_prefix, self._build_status()):
            with self._lock:
                unchanged = self._last_sent.get(msg_topic) == text
            if unchanged:
                continue
            self._client.publish(msg_topic, text, qos=1, retain=True)
            with self._lock:
                self._last_sent[msg_topic] = text
            sent += 1
        return sent

    def _publish_loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.publish_status_once()
            except Exception:  # noqa: BLE001 — keep publishing on the next tick
                logger.exception("MQTT status publish failed")

    def _record_link(self, *, connected: bool, error: str) -> None:
        _write_link_status(
            {
                "ts": _utc_now(),
                "host": self.settings.host,
                "port": self.settings.port,
                "topic_prefix": self.settings.topic_prefix,
                "connected": connected,
                "error": error,
            }
        )


_carrier: EhalMqttCarrier | None = None
_carrier_lock = threading.Lock()


def settings_from_config() -> MqttSettings:
    import config

    return MqttSettings(
        enabled=bool(config.get("EHAL_MQTT_ENABLED")),
        host=str(config.get("EHAL_MQTT_HOST") or ""),
        port=int(config.get("EHAL_MQTT_PORT")),
        username=str(config.get("EHAL_MQTT_USERNAME") or ""),
        password=str(config.get("EHAL_MQTT_PASSWORD") or ""),
        topic_prefix=str(config.get("EHAL_MQTT_TOPIC_PREFIX")),
    )


def start_ehal_mqtt(settings: MqttSettings | None = None) -> EhalMqttCarrier | None:
    """Start the carrier when ``ehal.mqtt.enabled``; idempotent. Skipped in Shadow Mode."""
    from runtime_store.shadow.mode import is_shadow_mode

    resolved = settings if settings is not None else settings_from_config()
    if not resolved.enabled:
        logger.info("MQTT carrier not configured (ehal.mqtt.enabled=false)")
        return None
    if is_shadow_mode():
        logger.info("Shadow Mode: MQTT carrier not started (%s)", resolved.host)
        return None
    global _carrier
    with _carrier_lock:
        if _carrier is not None:
            return _carrier
        carrier = EhalMqttCarrier(resolved)
        carrier.start()
        _carrier = carrier
    atexit.register(stop_ehal_mqtt)
    return carrier


def stop_ehal_mqtt() -> None:
    global _carrier
    with _carrier_lock:
        carrier = _carrier
        _carrier = None
    if carrier is not None:
        carrier.stop()


def probe_broker(
    settings: MqttSettings,
    *,
    timeout_s: float = 5.0,
    client_factory: Callable[[str], Any] = make_paho_client,
) -> str | None:
    """Connect once (UI "Verbindung testen"); ``None`` on success, else a German error."""
    done = threading.Event()
    outcome: dict[str, str] = {}

    def _on_connect(_client, _userdata, _flags, reason_code, _properties) -> None:
        if getattr(reason_code, "is_failure", False):
            outcome["error"] = f"Broker hat die Verbindung abgelehnt: {reason_code}"
        done.set()

    client = client_factory(f"earnie-probe-{os.getpid()}")
    if settings.username:
        client.username_pw_set(settings.username, settings.password or None)
    client.on_connect = _on_connect
    try:
        client.connect(settings.host, settings.port, keepalive=10)
    except OSError as exc:
        return f"Broker {settings.host}:{settings.port} nicht erreichbar: {exc}"
    client.loop_start()
    try:
        if not done.wait(timeout_s):
            return f"Keine Antwort vom Broker {settings.host}:{settings.port} innerhalb {timeout_s:g} s."
        return outcome.get("error")
    finally:
        client.disconnect()
        client.loop_stop()
