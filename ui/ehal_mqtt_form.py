"""MQTT broker form (Smarthome-Backend → MQTT-Broker): ``ehal.mqtt`` + ``EHAL_MQTT_PASSWORD``."""
from __future__ import annotations

import os
from typing import Any

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


def settings_from_form(values: dict[str, Any], *, password: str) -> MqttSettings:
    """Validate form values with the same rules as config load (raises ``ValueError``)."""
    return parse_mqtt_block(
        {
            "enabled": bool(values["enabled"]),
            "host": str(values["host"]).strip(),
            "port": values["port"],
            "username": str(values["username"]).strip(),
            "topic_prefix": str(values["topic_prefix"]).strip(),
        },
        password=password,
    )


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
