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

### Telemetrie von Loxone an Earnie

Die Loxone-Vorlagen gibt es auch in einer MQTT-Variante. Sie schicken die Werte per UDP an das LoxBerry-MQTT-Gateway, das sie auf den Topics `earnie/telemetry/<EHAL-ID>` veröffentlicht.

1. **Daemon Control → EHAL-Com → Signalliste → Export** öffnen.
2. Bei **Telemetrie (VO) senden über** die Option **MQTT über das LoxBerry-MQTT-Gateway (UDP)** wählen.
3. **IP-Adresse des LoxBerry** prüfen (vorbelegt mit dem Broker aus **MQTT-Broker (optional)**) und den **UDP-Port des Gateways** eintragen. Der Port steht im LoxBerry unter **MQTT → Gateway** („UDP-In Port“, Standard 11884).
4. ZIP herunterladen und die `VO_Earnie_*.xml` in Loxone Config als Vorlagen für Virtuelle Ausgänge importieren.

Jeder Befehl eines virtuellen Ausgangs sieht so aus:

| Feld | Inhalt |
| ---- | ------ |
| Adresse | `/dev/udp/<LoxBerry-IP>/11884` |
| Befehl bei EIN | `earnie/telemetry/grid.meter.sens_grid_power_active <v.3>` |

Topic und Wert sind durch ein Leerzeichen getrennt. Das Gateway macht daraus eine Nachricht auf dem Topic mit dem Wert als Inhalt. Steht der Wert dagegen im Topic (z. B. die HTTP-Vorlage `/ehal/telemetry/<EHAL-ID>/<v>` an den LoxBerry geschickt), entsteht pro Wert ein neues Topic. Earnie kann diese Werte nicht lesen.

Die MQTT-Vorlagen brauchen keinen Push-Token, die Anmeldung am Broker ersetzt ihn. Der Heartbeat (`heartbeat_smarthome`) und **Earnie_Request_Optimize** (`earnie/request_optimize 1`) sind in denselben Vorlagen enthalten.

### Sollwerte von Earnie an Loxone

Die `VI_Earnie_*.xml` aus demselben Export lesen `status.json` weiterhin per HTTP direkt von Earnie. Das funktioniert unabhängig von MQTT.

Wer die Sollwerte lieber über das Gateway bezieht, abonniert dort `earnie/setpoint/#`. Das Gateway reicht die Werte an den Miniserver weiter und leitet die Namen der virtuellen Eingänge aus den Topics ab. Welche Namen dabei entstehen, zeigt das Gateway in seiner Übersicht der eingehenden Werte.

## Verbindungsstatus

Unter **MQTT-Broker (optional)** zeigt die Seite den letzten vom Optimierer-Dienst gemeldeten Zustand: verbunden, getrennt oder abgelehnt (z. B. falsches Passwort). Ist der Broker beim Start nicht erreichbar, startet der Dienst trotzdem und versucht die Verbindung laufend neu (alle 1 bis 60 Sekunden). Jeder Abbruch steht als Fehler im Log.

> **Hinweis:** Earnie sieht nicht, ob ein Abonnent eine retained Nachricht tatsächlich verarbeitet hat. Ob der Hub die Sollwerte übernimmt, zeigt deshalb nur der Heartbeat bzw. die Live-Diagnose auf **EHAL-Com**.

## Einschränkungen

- **Keine Verschlüsselung (TLS):** Die Verbindung läuft unverschlüsselt. Der Broker sollte daher nur im Heimnetz erreichbar sein.
- **Keine Home-Assistant-Discovery:** Earnie legt keine HA-Entities automatisch an.
