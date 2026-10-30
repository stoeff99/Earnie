"""MQTT carrier settings (``ehal.mqtt`` in config.json, password in ``.env``).

MQTT is an optional third carrier next to the HTTP telemetry push and
``status.json``: it works with any EHAL backend. No ``ehal.mqtt`` block means
MQTT is not configured. A block with ``enabled: true`` must name a broker host;
invalid values fail loud instead of being replaced.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

PASSWORD_ENV = "EHAL_MQTT_PASSWORD"
DEFAULT_PORT = 1883
DEFAULT_TOPIC_PREFIX = "earnie"


@dataclass(frozen=True)
class MqttSettings:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    topic_prefix: str


DISABLED = MqttSettings(
    enabled=False,
    host="",
    port=DEFAULT_PORT,
    username="",
    password="",
    topic_prefix=DEFAULT_TOPIC_PREFIX,
)


def read_mqtt_password() -> str:
    return str(os.getenv(PASSWORD_ENV) or "")


def parse_port(raw: object) -> int:
    """Broker TCP port 1–65535; anything else raises."""
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ehal.mqtt.port muss eine Zahl sein, nicht {raw!r}.") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"ehal.mqtt.port muss zwischen 1 und 65535 liegen, nicht {port}.")
    return port


def parse_topic_prefix(raw: object) -> str:
    """Topic prefix without leading/trailing ``/`` and without MQTT wildcards."""
    prefix = str(raw or "").strip()
    if not prefix:
        raise ValueError("ehal.mqtt.topic_prefix darf nicht leer sein.")
    if prefix.startswith("/") or prefix.endswith("/"):
        raise ValueError(
            f"ehal.mqtt.topic_prefix ohne führenden/abschließenden '/' angeben, nicht {prefix!r}."
        )
    if any(ch in prefix for ch in "#+\0"):
        raise ValueError(
            f"ehal.mqtt.topic_prefix darf keine MQTT-Platzhalter (#, +) enthalten: {prefix!r}."
        )
    return prefix


def parse_mqtt_block(raw: object, *, password: str) -> MqttSettings:
    """Validate an ``ehal.mqtt`` block; ``None`` (no block) → MQTT disabled."""
    if raw is None:
        return DISABLED
    if not isinstance(raw, dict):
        raise ValueError(f"ehal.mqtt muss ein Objekt sein, nicht {type(raw).__name__}.")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"ehal.mqtt.enabled muss true oder false sein, nicht {enabled!r}.")
    host = str(raw.get("host") or "").strip()
    if enabled and not host:
        raise ValueError(
            "ehal.mqtt.enabled ist true, aber ehal.mqtt.host fehlt "
            "(Smarthome-Backend → MQTT-Broker)."
        )
    port = parse_port(raw["port"]) if "port" in raw else DEFAULT_PORT
    prefix = (
        parse_topic_prefix(raw["topic_prefix"])
        if "topic_prefix" in raw
        else DEFAULT_TOPIC_PREFIX
    )
    return MqttSettings(
        enabled=enabled,
        host=host,
        port=port,
        username=str(raw.get("username") or "").strip(),
        password=str(password or ""),
        topic_prefix=prefix,
    )


def mqtt_block_from_settings(settings: MqttSettings) -> dict[str, object]:
    """``ehal.mqtt`` JSON for config.json (the password stays in ``.env``)."""
    return {
        "enabled": settings.enabled,
        "host": settings.host,
        "port": settings.port,
        "username": settings.username,
        "topic_prefix": settings.topic_prefix,
    }
