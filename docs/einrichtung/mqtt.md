# MQTT-Anbindung

Earnie kann Telemetrie und Sollwerte zusätzlich über einen **MQTT-Broker** austauschen. MQTT ist dabei kein eigenes Smarthome-Backend. Es ist ein weiterer Übertragungsweg für dieselben EHAL-Signale, die sonst per HTTP-Push (`/ehal/telemetry/…`) und `status.json` laufen. Das gewählte Backend ([Smarthome-Backend wählen](smarthome-backend-wahl.md)) und die Signalliste auf **EHAL-Com** bleiben unverändert.

Typische Einsätze:

- **LoxBerry mit MQTT-Gateway:** Loxone schickt Werte über das Gateway, Earnie läuft z. B. in einem eigenen Proxmox-Container ([Proxmox LXC](proxmox-lxc.md)).
- **Home Assistant, Node-RED oder ioBroker:** Werte kommen über einen vorhandenen Broker (z. B. Mosquitto).

## Einrichten

1. In der Oberfläche **Daemon Control → Smarthome-Backend** öffnen. Der Bereich erscheint, sobald ein Backend verbunden ist.
2. **MQTT-Broker (optional)** aufklappen.
3. **MQTT aktivieren** anhaken und **Broker-IP / Hostname**, **Port** (Standard 1883), **Benutzername** und **Passwort** eintragen.
4. Optional das **Topic-Präfix** anpassen (Standard `earnie`).
5. Mit **Verbindung testen** prüfen, ob der Broker die Anmeldung annimmt.
6. **Speichern** und den **Optimierer-Dienst** neu starten. Erst beim Start verbindet sich Earnie mit dem Broker.

Ein leeres Passwortfeld behält das gespeicherte Passwort. Zum Entfernen **Gespeichertes Passwort entfernen** anhaken.

Die Einstellungen landen in `config.json` unter `ehal.mqtt`, das Passwort steht in `config/.env` als `EHAL_MQTT_PASSWORD`:

```json
"ehal": {
  "backend": "loxone",
  "mqtt": {
    "enabled": true,
    "host": "192.168.1.20",
    "port": 1883,
    "username": "loxberry",
    "topic_prefix": "earnie"
  }
}
```

Fehlt der Block `ehal.mqtt`, ist MQTT aus. Ist `enabled` gesetzt, aber kein `host` angegeben, bricht das Laden der Konfiguration mit einer Fehlermeldung ab. Dasselbe gilt für einen ungültigen Port oder ein Präfix mit `#`, `+` oder führendem bzw. abschließendem `/`.

## Topics

Alle Topics beginnen mit dem Präfix (hier `earnie`). Die EHAL-IDs sind dieselben wie in der Signalliste auf **EHAL-Com** und in den Loxone-Vorlagen.

| Topic | Richtung | Inhalt |
| ----- | -------- | ------ |
| `earnie/telemetry/<EHAL-ID>` | an Earnie | Ein Wert als Text, z. B. `earnie/telemetry/ess.haus.sens_ess_soc` mit `54.5`. Gleiche Regeln wie beim HTTP-Push, auch Einheiten-Suffixe. |
| `earnie/telemetry/heartbeat_smarthome` | an Earnie | Lebenszeichen des Hubs. Zählt wie der HTTP-Heartbeat als Rückmeldung des Hubs. |
| `earnie/request_optimize` | an Earnie | Beliebiger Inhalt löst einen vorgezogenen Optimierungslauf aus. |
| `earnie/status` | von Earnie (retained) | Der komplette Inhalt von `status.json` als JSON. |
| `earnie/setpoint/<EHAL-ID>` | von Earnie (retained) | Je ein Wert aus `status.json`, z. B. `earnie/setpoint/heartbeat_earnie`. Gedacht für Gateways, die ein Topic auf einen Eingang abbilden. |
| `earnie/availability` | von Earnie (retained) | `online` nach dem Verbinden, `offline` beim Beenden oder Verbindungsabbruch (Last Will). |

Earnie sendet `earnie/status` alle 10 Sekunden. Einzelne `setpoint`-Topics gehen nur bei Änderung raus. Nach jedem (Wieder-)Verbinden sendet Earnie alle Werte erneut.

Für Sollwerte gelten dieselben Regeln wie bei `status.json`, denn beide nutzen denselben Inhalt.

## LoxBerry-MQTT-Gateway

- **Telemetrie:** Im Gateway pro Signal ein Topic `earnie/telemetry/<EHAL-ID>` mit dem Loxone-Wert füllen. Die EHAL-IDs stehen in der Signalliste bzw. in den exportierten Loxone-Vorlagen.
- **Sollwerte:** `earnie/setpoint/#` abonnieren. Das Gateway reicht die Werte an den Miniserver weiter; die Namen der Virtual Inputs leitet es aus den Topics ab.
- **Heartbeat:** `heartbeat_smarthome` regelmäßig (z. B. alle 10 Sekunden) senden, damit Earnie die Verbindung als lebendig erkennt.

## Verbindungsstatus

Unter **MQTT-Broker (optional)** zeigt die Seite den letzten vom Optimierer-Dienst gemeldeten Zustand: verbunden, getrennt oder abgelehnt (z. B. falsches Passwort). Ist der Broker beim Start nicht erreichbar, startet der Dienst trotzdem und versucht die Verbindung laufend neu (alle 1 bis 60 Sekunden). Jeder Abbruch steht als Fehler im Log.

> **Hinweis:** Earnie sieht nicht, ob ein Abonnent eine retained Nachricht tatsächlich verarbeitet hat. Ob der Hub die Sollwerte übernimmt, zeigt deshalb nur der Heartbeat bzw. die Live-Diagnose auf **EHAL-Com**.

## Einschränkungen

- **Keine Verschlüsselung (TLS):** Die Verbindung läuft unverschlüsselt. Der Broker sollte daher nur im Heimnetz erreichbar sein.
- **Kein Shadow-Modus:** Im Shadow-Modus baut Earnie keine MQTT-Verbindung auf.
- **Keine Home-Assistant-Discovery:** Earnie legt keine HA-Entities automatisch an.
