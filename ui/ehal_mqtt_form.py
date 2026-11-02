"""MQTT broker form (Smarthome-Backend → MQTT-Broker): ``ehal.mqtt`` + ``EHAL_MQTT_PASSWORD``."""
from __future__ import annotations

import os
from typing import Any

import pandas as pd
import streamlit as st

from settings.mqtt_settings import (
    DEFAULT_PORT,
    DEFAULT_TOPIC_PREFIX,
    PASSWORD_ENV,
    MqttSettings,
    mqtt_block_from_settings,
    parse_mqtt_block,
    read_mqtt_password,
)
from ui.flash import flash
from ui.house_config_io import load_main_config, save_main_config


def _mqtt_block(data: dict) -> dict[str, Any]:
    ehal = data.get("ehal") if isinstance(data.get("ehal"), dict) else {}
    mqtt = ehal.get("mqtt")
    return dict(mqtt) if isinstance(mqtt, dict) else {}


def stored_mqtt_block() -> dict[str, Any]:
    """``ehal.mqtt`` as saved in config.json (empty when MQTT was never configured)."""
    return _mqtt_block(load_main_config())


def settings_from_form(values: dict[str, Any], *, password: str) -> MqttSettings:
    """Validate form values with the same rules as config load (raises ``ValueError``)."""
    return parse_mqtt_block(
        {
            "enabled": bool(values["enabled"]),
            "host": str(values["host"]).strip(),
            "port": values["port"],
            "username": str(values["username"]).strip(),
            "topic_prefix": str(values["topic_prefix"]).strip(),
            "price_topic": str(values.get("price_topic") or "").strip(),
            "price_tariff_id": str(values.get("price_tariff_id") or "").strip(),
            "topic_map": topic_map_from_rows(values.get("topic_map") or []),
        },
        password=password,
    )


_MAP_TOPIC = "topic"
_MAP_EHAL_ID = "ehal_id"


def _cell(value: object) -> str:
    # Empty data_editor cells come back as None or NaN.
    if value is None or (isinstance(value, float) and value != value):
        return ""
    return str(value).strip()


def topic_map_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Editor rows → ``ehal.mqtt.topic_map``; fully empty rows are dropped, half-filled stay
    (validation reports them)."""
    out: list[dict[str, str]] = []
    for row in rows:
        topic, ehal_id = _cell(row.get(_MAP_TOPIC)), _cell(row.get(_MAP_EHAL_ID))
        if topic or ehal_id:
            out.append({_MAP_TOPIC: topic, _MAP_EHAL_ID: ehal_id})
    return out


def mapping_signal_options() -> list[str]:
    """EHAL read signals of the configured components (Signalliste), for the topic map."""
    from ehal.signal_catalog import iter_catalog_entries
    from ui.house_config_io import _load_components_document, load_house_profiles

    entries = iter_catalog_entries(load_house_profiles(), _load_components_document())
    return sorted({entry.ehal_id for entry in entries if entry.direction == "read"})


def _render_topic_map_status(mapping: list[dict[str, Any]], known: list[str]) -> None:
    from runtime_store.ehal_push_inbox import load_inbox

    if not mapping:
        return
    inbox = load_inbox()
    lines = []
    for row in mapping:
        ehal_id = str(row.get(_MAP_EHAL_ID) or "")
        topic = str(row.get(_MAP_TOPIC) or "")
        last = inbox.get(ehal_id) or {}
        if ehal_id not in known:
            state = "**Signal gibt es in der Signalliste nicht mehr**"
        elif last.get("last_ts"):
            state = f"zuletzt `{last.get('raw')}` um {last.get('last_ts')}"
        else:
            state = "noch kein Wert empfangen"
        lines.append(f"- `{topic}` → `{ehal_id}`: {state}")
    st.markdown("\n".join(lines))


def resolve_form_password(entered: str, *, clear: bool) -> str:
    """Empty field keeps the stored password; ``clear`` removes it."""
    if clear:
        return ""
    return entered if entered else read_mqtt_password()


def persist_mqtt_settings(settings: MqttSettings) -> None:
    """Password → ``.env`` (only when it changed), rest → ``config.json`` ``ehal.mqtt``."""
    from runtime_store.dotenv_io import upsert_dotenv_keys

    if settings.password != read_mqtt_password():
        upsert_dotenv_keys({PASSWORD_ENV: settings.password})
        # This process re-reads the config right below; the daemon loads .env at start.
        os.environ[PASSWORD_ENV] = settings.password
    data = load_main_config()
    ehal = data.get("ehal") if isinstance(data.get("ehal"), dict) else {}
    ehal = dict(ehal)
    ehal["mqtt"] = mqtt_block_from_settings(settings)
    data["ehal"] = ehal
    save_main_config(data)


def _render_link_caption() -> None:
    from integrations.ehal_mqtt import load_link_status

    status = load_link_status()
    if not status:
        st.caption("Optimierer-Dienst hat noch keine MQTT-Verbindung gemeldet.")
        return
    where = f"{status.get('host')}:{status.get('port')}"
    if status.get("connected"):
        st.caption(f"Optimierer-Dienst: verbunden mit **{where}** (Stand {status.get('ts')}).")
    elif status.get("error"):
        st.caption(
            f"Optimierer-Dienst: **nicht verbunden** mit {where} — {status.get('error')} "
            f"(Stand {status.get('ts')})."
        )
    else:
        st.caption(f"Optimierer-Dienst: nicht verbunden mit {where} (Stand {status.get('ts')}).")


_NO_PRICE_TARIFF = ""


def price_tariff_options() -> list[str]:
    """User import tariffs that can take the external series (spot, 15-min settlement)."""
    from house_config.tariffs_store import ORIGIN_KEY, ORIGIN_USER
    from ui.house_config_io import load_tariffs

    tariffs = load_tariffs().get("import_tariffs") or {}
    return sorted(
        tid
        for tid, spec in tariffs.items()
        if spec.get(ORIGIN_KEY) == ORIGIN_USER and spec.get("type") == "spot_hourly"
    )


def _render_price_feed_caption() -> None:
    from data.external_prices import store_summary

    try:
        summary = store_summary()
    except ValueError as exc:
        st.caption(str(exc))
        return
    if not summary["slots"]:
        st.caption("Noch keine externen Preise empfangen.")
        return
    st.caption(
        f"Externe Preise: {summary['slots']} Viertelstunden gespeichert, veröffentlicht bis "
        f"{summary['until']} (zuletzt empfangen {summary['received_at']}); "
        f"{summary['pairs']} Viertelstunden mit EPEX zum Vergleich."
    )


def render_mqtt_connection_form(*, form_key: str = "ehal_mqtt_form") -> None:
    """Broker address, port and login for the MQTT carrier."""
    stored = _mqtt_block(load_main_config())
    has_password = bool(read_mqtt_password())
    st.caption(
        "Optional, zusätzlich zum gewählten Backend: Telemetrie kommt über "
        "`<Präfix>/telemetry/<EHAL-ID>`, Sollwerte gehen auf `<Präfix>/status` und "
        "`<Präfix>/setpoint/<EHAL-ID>`. Das Passwort steht in `.env`, der Rest in "
        "`config.json` unter `ehal.mqtt`. Änderungen gelten nach einem Neustart des "
        "Optimierer-Dienstes."
    )
    _render_link_caption()
    _render_price_feed_caption()
    with st.form(form_key):
        enabled = st.checkbox(
            "MQTT aktivieren",
            key=f"{form_key}_enabled",
            value=bool(stored.get("enabled", False)),
        )
        host = st.text_input(
            "Broker-IP / Hostname",
            key=f"{form_key}_host",
            value=str(stored.get("host") or ""),
            help="z. B. 192.168.1.20 (LoxBerry mit MQTT-Gateway) oder mqtt.local",
        )
        port = st.number_input(
            "Port",
            key=f"{form_key}_port",
            min_value=1,
            max_value=65535,
            step=1,
            value=int(stored.get("port") or DEFAULT_PORT),
        )
        username = st.text_input(
            "Benutzername",
            key=f"{form_key}_username",
            value=str(stored.get("username") or ""),
        )
        password = st.text_input(
            "Passwort",
            key=f"{form_key}_password",
            type="password",
            placeholder="gespeichert — leer lassen, um es zu behalten" if has_password else "",
        )
        clear_password = st.checkbox(
            "Gespeichertes Passwort entfernen",
            key=f"{form_key}_clear_password",
            value=False,
            disabled=not has_password,
        )
        topic_prefix = st.text_input(
            "Topic-Präfix",
            key=f"{form_key}_topic_prefix",
            value=str(stored.get("topic_prefix") or DEFAULT_TOPIC_PREFIX),
        )
        st.markdown("**Externe Bezugspreise (optional)**")
        price_topic = st.text_input(
            "Topic der Preise (EKZ-Format)",
            key=f"{form_key}_price_topic",
            value=str(stored.get("price_topic") or ""),
            help="z. B. ekz/ems/tariffs/intervals (LoxBerry-Plugin für EKZ-Dynamik).",
        )
        matching = price_tariff_options()
        options = [_NO_PRICE_TARIFF, *matching]
        current = str(stored.get("price_tariff_id") or "")
        if current and current not in options:
            options.append(current)
        price_tariff_id = st.selectbox(
            "Benutzertarif für diese Preise",
            options=options,
            index=options.index(current),
            format_func=lambda tid: tid or "—",
            key=f"{form_key}_price_tariff_id",
            help=(
                "Spot-Benutzertarif (Konfiguration → Tarife) mit Abrechnung je Viertelstunde, "
                "ohne Aufschlag und mit der USt (CH 8,1 %). Statt EPEX rechnet er mit den "
                "externen Preisen; Netznutzung kommt nicht dazu."
            ),
        )
        if not matching:
            st.caption(
                "Noch kein passender Benutzertarif. Unter **Konfiguration → Tarife** einen "
                "Bezugstarif anlegen: Typ Spot, Land CH, Aufschlag und Abwicklungsgebühr 0, "
                "Preise ohne USt, USt 8,1 %. Bis dahin sammelt Earnie mit dem Topic nur die Preise."
            )
        st.markdown("**Fremde Topics zuordnen (optional)**")
        st.caption(
            "Werte anderer Systeme (z. B. ioBroker) direkt einem EHAL-Signal zuordnen. Earnie "
            "abonniert das Topic und behandelt jeden Wert wie `<Präfix>/telemetry/<EHAL-ID>`: "
            "einfache Zahl, in der Einheit, die die Signalliste für das Signal zeigt."
        )
        stored_map = list(stored.get("topic_map") or [])
        known = mapping_signal_options()
        signal_options = sorted({*known, *(str(r.get(_MAP_EHAL_ID) or "") for r in stored_map)} - {""})

        map_rows = st.data_editor(
            pd.DataFrame(
                [
                    {_MAP_TOPIC: r.get(_MAP_TOPIC, ""), _MAP_EHAL_ID: r.get(_MAP_EHAL_ID, "")}
                    for r in stored_map
                ],
                columns=[_MAP_TOPIC, _MAP_EHAL_ID],
                dtype="object",
            ),
            key=f"{form_key}_topic_map",
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            column_config={
                _MAP_TOPIC: st.column_config.TextColumn(
                    "Quell-Topic",
                    help="Exaktes Topic, z. B. ioBroker/tesla-motors/0/<VIN>/charge_state/battery_level",
                    width="large",
                ),
                _MAP_EHAL_ID: st.column_config.SelectboxColumn(
                    "EHAL-Signal", options=signal_options, width="medium"
                ),
            },
        )
        _render_topic_map_status(stored_map, known)
        col_save, col_test = st.columns(2)
        submitted = col_save.form_submit_button(
            "Speichern", type="primary", key=f"{form_key}_submit"
        )
        test = col_test.form_submit_button("Verbindung testen", key=f"{form_key}_test")

    if not submitted and not test:
        return
    values = {
        "enabled": enabled,
        "host": host,
        "port": int(port),
        "username": username,
        "topic_prefix": topic_prefix,
        "price_topic": price_topic,
        "price_tariff_id": price_tariff_id,
        "topic_map": map_rows.to_dict("records"),
    }
    try:
        settings = settings_from_form(
            values,
            password=resolve_form_password(password, clear=clear_password),
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    if test:
        if not settings.host:
            st.error("Für den Test ist eine Broker-IP / ein Hostname nötig.")
            return
        from integrations.ehal_mqtt import probe_broker

        with st.spinner(f"Verbinde mit {settings.host}:{settings.port}…"):
            error = probe_broker(settings)
        if error:
            st.error(error)
        else:
            st.success(f"Verbindung zu {settings.host}:{settings.port} erfolgreich.")
        return
    persist_mqtt_settings(settings)
    state = "aktiviert" if settings.enabled else "deaktiviert"
    flash("success", f"MQTT-Einstellungen gespeichert (MQTT {state}).")
    st.rerun()
