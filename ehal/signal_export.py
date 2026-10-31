"""Build a ZIP of Loxone VO/VI templates from the Signal list."""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from typing import Literal

from ehal.push_signals import Signal, request_optimize_signal
from ehal.signal_list import signals_for_export
from scripts.pilot_vi_template_gen import render_template as render_vi
from scripts.pilot_vo_template_gen import TOKEN_PLACEHOLDER
from scripts.pilot_vo_template_gen import mqtt_udp_address
from scripts.pilot_vo_template_gen import render_mqtt_udp_status_template
from scripts.pilot_vo_template_gen import render_mqtt_udp_template
from scripts.pilot_vo_template_gen import render_status_template
from scripts.pilot_vo_template_gen import render_template as render_vo

ExportMode = Literal["needed", "all"]

_README = """Earnie Signal-list export (VO / VI)

1. Copy VO_Earnie_*.xml into Loxone Config Virtual Output templates.
2. Copy VI_Earnie_*.xml into Virtual Input templates.
3. Replace PUSH_TOKEN_HIER_ERSETZEN in VO addresses with value of EARNIE_PUSH_TOKEN (to be set in .env) oder lassen Sie es weg, wenn kein Token benötigt wird.
4. Wire VO sources to real measurements; VI Checks poll status.json on Earnie.

Mode: {mode}
Host: {host}:{port}
"""

_README_MQTT = """Earnie Signal-list export (VO over MQTT / VI)

1. Copy VO_Earnie_*.xml into Loxone Config Virtual Output templates.
   They send UDP to the LoxBerry MQTT gateway ({udp_address}); each command is
   "<topic> <value>", e.g. "{prefix}/telemetry/<EHAL-ID> <v.3>". The gateway publishes
   the value on that topic and Earnie (ehal.mqtt, prefix "{prefix}") receives it.
   Check the gateway's UDP input port under LoxBerry MQTT -> Gateway.
2. Copy VI_Earnie_*.xml into Virtual Input templates. VI Checks poll status.json on
   Earnie ({host}:{port}) over HTTP, as without MQTT. Earnie also publishes every value
   retained on {prefix}/setpoint/<EHAL-ID> and {prefix}/status for other subscribers.
3. No push token: the broker login (Smarthome-Backend -> MQTT-Broker) protects the
   telemetry instead.
4. Wire VO sources to real measurements.

Mode: {mode}
"""


@dataclass(frozen=True)
class MqttUdpTarget:
    """VO telemetry via the LoxBerry MQTT gateway instead of HTTP to Earnie."""

    host: str
    udp_port: int
    topic_prefix: str


def normalize_export_host(raw: str, *, port_field: str = "HTTP-Port") -> str:
    """Host for template addresses: strip a pasted ``http(s)://`` and trailing ``/``.

    The templates add scheme and port themselves; a port or path left in the field
    would end up twice in the address, so it fails loud instead.
    """
    host = str(raw or "").strip()
    lowered = host.lower()
    for scheme in ("http://", "https://"):
        if lowered.startswith(scheme):
            host = host[len(scheme):]
            break
    host = host.rstrip("/")
    if ":" in host or "/" in host:
        raise ValueError(
            f"Host {raw!r}: nur IP-Adresse oder Hostnamen eintragen, ohne Port und "
            f"Pfad – den Port im Feld „{port_field}“ setzen."
        )
    return host


def build_export_zip(
    house: dict,
    components: dict,
    *,
    host: str,
    port: int,
    mode: ExportMode = "needed",
    token: str = TOKEN_PLACEHOLDER,
    mqtt: MqttUdpTarget | None = None,
) -> bytes:
    """Return ZIP bytes with VO/VI XML, CSV lists, and a short README.

    ``mqtt``: VO telemetry goes over UDP to the LoxBerry MQTT gateway; VI stays HTTP
    (``status.json`` on ``host:port``).
    """
    reads, writes = signals_for_export(house, components, mode=mode)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_vo_group(zf, reads, host=host, port=port, token=token, mqtt=mqtt)
        _write_vi_group(zf, writes, host=host, port=port)
        if mqtt is None:
            readme = _README.format(mode=mode, host=host, port=port)
        else:
            readme = _README_MQTT.format(
                mode=mode,
                host=host,
                port=port,
                prefix=mqtt.topic_prefix,
                udp_address=mqtt_udp_address(mqtt.host, mqtt.udp_port),
            )
        zf.writestr("README.txt", readme)
    return buf.getvalue()


def _group_signals(signals: list[Signal]) -> dict[str, list[Signal]]:
    groups: dict[str, list[Signal]] = {}
    for sig in signals:
        groups.setdefault(sig.group, []).append(sig)
    return groups


def _bom(text: str) -> bytes:
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def _write_vo_group(
    zf: zipfile.ZipFile,
    signals: list[Signal],
    *,
    host: str,
    port: int,
    token: str,
    mqtt: MqttUdpTarget | None,
) -> None:
    rows: list[list[str]] = []
    for group, items in _group_signals(signals).items():
        name = f"VO_Earnie_{group}.xml"
        if mqtt is None:
            text = render_vo(group, items, host=host, port=port, token=token)
        else:
            text = render_mqtt_udp_template(
                group,
                items,
                host=mqtt.host,
                udp_port=mqtt.udp_port,
                topic_prefix=mqtt.topic_prefix,
            )
        zf.writestr(name, _bom(text))
        for sig in items:
            rows.append(
                [
                    name,
                    sig.title,
                    sig.ehal_id,
                    sig.old_name,
                    "digital" if sig.digital else "analog",
                ]
            )
    # Optional ad-hoc optimize: a POST control command, not a telemetry push.
    status = request_optimize_signal()
    name = f"VO_Earnie_{status.group}.xml"
    if mqtt is None:
        status_text = render_status_template(host=host, port=port)
    else:
        status_text = render_mqtt_udp_status_template(
            host=mqtt.host, udp_port=mqtt.udp_port, topic_prefix=mqtt.topic_prefix
        )
    zf.writestr(name, _bom(status_text))
    rows.append([name, status.title, status.ehal_id, status.old_name, "digital"])
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, delimiter=";")
    writer.writerow(["Datei", "VO-Cmd-Titel", "EHAL-ID", "Bisheriger Name", "Typ"])
    writer.writerows(rows)
    zf.writestr("Earnie-VO-Signalliste.csv", "\ufeff" + csv_buf.getvalue())


def _write_vi_group(
    zf: zipfile.ZipFile,
    signals: list[Signal],
    *,
    host: str,
    port: int,
) -> None:
    rows: list[list[str]] = []
    for group, items in _group_signals(signals).items():
        name = f"VI_Earnie_{group}.xml"
        zf.writestr(name, _bom(render_vi(group, items, host=host, port=port)))
        for sig in items:
            rows.append([name, sig.title, sig.ehal_id, sig.old_name, "analog"])
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, delimiter=";")
    writer.writerow(["Datei", "VI-Cmd-Titel", "EHAL-ID", "Bisheriger Name", "Typ"])
    writer.writerows(rows)
    zf.writestr("Earnie-VI-Signalliste.csv", "\ufeff" + csv_buf.getvalue())
