"""MQTT carrier settings (``ehal.mqtt`` in config.json, password in ``.env``).

MQTT is an optional third carrier next to the HTTP telemetry push and
``status.json``: it works with any EHAL backend. No ``ehal.mqtt`` block means
MQTT is not configured. A block with ``enabled: true`` must name a broker host;
invalid values fail loud instead of being replaced.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

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
    # Optional external import prices (EKZ interval JSON) and the user tariff priced from them.
    price_topic: str = ""
    price_tariff_id: str = ""
    # Foreign topic → EHAL read ID (e.g. ioBroker states); values go to the push inbox as-is.
    topic_map: Mapping[str, str] = field(default_factory=dict)
    # Earnie PV system id → Solcast branch of the LoxBerry plugin "PV Solcast NG"
    # (e.g. ``solcast/system_1``); mapped systems take their PV forecast from it.
    pv_solcast: Mapping[str, str] = field(default_factory=dict)


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


def parse_price_topic(raw: object) -> str:
    """Exact topic of the external price feed (no wildcards); empty = no feed."""
    topic = str(raw or "").strip()
    if any(ch in topic for ch in "#+\0"):
        raise ValueError(
            f"ehal.mqtt.price_topic darf keine MQTT-Platzhalter (#, +) enthalten: {topic!r}."
        )
    return topic


def parse_topic_map(raw: object, *, topic_prefix: str, price_topic: str) -> dict[str, str]:
    """``ehal.mqtt.topic_map`` (list of ``{topic, ehal_id}``) → ``{topic: ehal_id}``.

    Each topic is exact (no wildcards), outside ``<prefix>/`` and not the price topic;
    each EHAL ID is a read signal (``sens_`` / ``get_``) and mapped at most once.
    """
    from runtime_store.loxone_push_parse import is_valid_ehal_id

    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError(f"ehal.mqtt.topic_map muss eine Liste sein, nicht {type(raw).__name__}.")
    mapping: dict[str, str] = {}
    for index, item in enumerate(raw):
        where = f"ehal.mqtt.topic_map[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} muss ein Objekt mit topic und ehal_id sein.")
        topic = str(item.get("topic") or "").strip()
        ehal_id = str(item.get("ehal_id") or "").strip()
        if not topic or not ehal_id:
            raise ValueError(f"{where}: Topic und EHAL-Signal sind beide nötig.")
        if any(ch in topic for ch in "#+\0"):
            raise ValueError(f"{where}: Topic ohne MQTT-Platzhalter (#, +) angeben: {topic!r}.")
        if topic == topic_prefix or topic.startswith(f"{topic_prefix}/"):
            raise ValueError(
                f"{where}: {topic!r} liegt unter dem Earnie-Präfix {topic_prefix!r}; "
                f"dafür direkt {topic_prefix}/telemetry/<EHAL-ID> verwenden."
            )
        if topic == price_topic:
            raise ValueError(f"{where}: {topic!r} ist schon das Topic der Preise.")
        if not is_valid_ehal_id(ehal_id):
            raise ValueError(
                f"{where}: {ehal_id!r} ist kein lesbares EHAL-Signal (sens_… / get_…)."
            )
        if topic in mapping:
            raise ValueError(f"{where}: Topic {topic!r} ist doppelt zugeordnet.")
        if ehal_id in mapping.values():
            raise ValueError(f"{where}: Signal {ehal_id!r} hat schon ein Topic.")
        mapping[topic] = ehal_id
    return mapping


def parse_pv_solcast(
    raw: object,
    *,
    topic_prefix: str,
    price_topic: str,
    topic_map: Mapping[str, str],
) -> dict[str, str]:
    """``ehal.mqtt.pv_solcast`` (list of ``{pv_system_id, topic}``) → ``{pv_system_id: topic}``.

    ``topic`` is the plugin branch (``solcast/sum`` or ``solcast/system_<n>``) whose
    ``today_hour_XX_kwh`` / ``tomorrow_hour_XX_kwh`` sub-topics Earnie reads. Each PV system
    and each branch is used at most once.
    """
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError(f"ehal.mqtt.pv_solcast muss eine Liste sein, nicht {type(raw).__name__}.")
    mapping: dict[str, str] = {}
    for index, item in enumerate(raw):
        where = f"ehal.mqtt.pv_solcast[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} muss ein Objekt mit pv_system_id und topic sein.")
        system_id = str(item.get("pv_system_id") or "").strip()
        branch = str(item.get("topic") or "").strip()
        if not system_id or not branch:
            raise ValueError(f"{where}: PV-Anlage und Solcast-Topic sind beide nötig.")
        if any(ch in branch for ch in "#+\0"):
            raise ValueError(f"{where}: Topic ohne MQTT-Platzhalter (#, +) angeben: {branch!r}.")
        if branch.startswith("/") or branch.endswith("/"):
            raise ValueError(
                f"{where}: Topic ohne führenden/abschließenden '/' angeben, nicht {branch!r}."
            )
        if branch == topic_prefix or branch.startswith(f"{topic_prefix}/"):
            raise ValueError(f"{where}: {branch!r} liegt unter dem Earnie-Präfix {topic_prefix!r}.")
        if price_topic and (price_topic == branch or price_topic.startswith(f"{branch}/")):
            raise ValueError(f"{where}: Unter {branch!r} liegt schon das Topic der Preise.")
        clash = next((t for t in topic_map if t.startswith(f"{branch}/")), None)
        if clash:
            raise ValueError(f"{where}: Unter {branch!r} liegt schon das zugeordnete Topic {clash!r}.")
        if system_id in mapping:
            raise ValueError(f"{where}: PV-Anlage {system_id!r} ist doppelt zugeordnet.")
        if branch in mapping.values():
            raise ValueError(f"{where}: Solcast-Topic {branch!r} ist schon einer PV-Anlage zugeordnet.")
        mapping[system_id] = branch
    return mapping


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
    price_topic = parse_price_topic(raw.get("price_topic"))
    price_tariff_id = str(raw.get("price_tariff_id") or "").strip()
    # A topic alone only collects prices; a tariff without a topic would never get any.
    if price_tariff_id and not price_topic:
        raise ValueError(
            "ehal.mqtt.price_tariff_id ist gesetzt, aber ehal.mqtt.price_topic fehlt: "
            "ohne Topic bekommt der Tarif keine Preise."
        )
    topic_map = parse_topic_map(
        raw.get("topic_map"), topic_prefix=prefix, price_topic=price_topic
    )
    pv_solcast = parse_pv_solcast(
        raw.get("pv_solcast"),
        topic_prefix=prefix,
        price_topic=price_topic,
        topic_map=topic_map,
    )
    return MqttSettings(
        enabled=enabled,
        host=host,
        port=port,
        username=str(raw.get("username") or "").strip(),
        password=str(password or ""),
        topic_prefix=prefix,
        price_topic=price_topic,
        price_tariff_id=price_tariff_id,
        topic_map=topic_map,
        pv_solcast=pv_solcast,
    )


def mqtt_block_from_settings(settings: MqttSettings) -> dict[str, object]:
    """``ehal.mqtt`` JSON for config.json (the password stays in ``.env``)."""
    return {
        "enabled": settings.enabled,
        "host": settings.host,
        "port": settings.port,
        "username": settings.username,
        "topic_prefix": settings.topic_prefix,
        "price_topic": settings.price_topic,
        "price_tariff_id": settings.price_tariff_id,
        "topic_map": [
            {"topic": topic, "ehal_id": ehal_id} for topic, ehal_id in settings.topic_map.items()
        ],
        "pv_solcast": [
            {"pv_system_id": system_id, "topic": branch}
            for system_id, branch in settings.pv_solcast.items()
        ],
    }
