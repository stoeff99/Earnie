# Mapped MQTT topics publish on change

Status: implemented. Code: `runtime_store/ehal_push_inbox.py` (`is_on_change`, `is_stale`),
`integrations/ehal_mqtt.py` (`mqtt_session_connected`, `_on_mapped_message`).

## Problem

Mapped foreign topics (`ehal.mqtt.topic_map`, e.g. ioBroker states) went into the push inbox
like Loxone VO pushes and got the VO repeat contract: stale after 3 × the repeat interval
(30 s), then held for `LAST_KNOWN_MAX_S` (5 min), then a read error. ioBroker and most MQTT
sources publish only on change, so a parked EV's SoC (unchanged for days) vanished 5.5 min
after each message, and Live-Lesen showed "Kein Mapping".

## Rules

| Part | Rule |
|---|---|
| Mark | `record_push(..., publish="on_change")` stores `"publish": "on_change"` on the row. Only mapped topics set it; the mark follows the latest push. `earnie/telemetry/<EHAL-ID>` keeps the repeat contract. |
| Valid | Not stale while the MQTT session is connected and the value is at most `ON_CHANGE_MAX_AGE_S` (7 days) old. |
| Session down | Stale: the 5 min last-known hold applies, then a read error (fail loud). Changes during an outage arrive only if the source publishes retained. |
| Values | No zero rule: 0 is a plain value, and a quiet power value holds (no "0 angenommen"). |
| Hub link | `link_alive` ignores on-change rows (their age says nothing about the VO link). |
| Session state | Daemon: the running carrier. Without a carrier (UI process): `ehal_mqtt_status.json` (`connected`), only while MQTT is enabled and the file's host, port and topic prefix match the current settings. Missing file = not connected. |
| UI | Signalliste: OK while valid, age shown with "(bei Änderung)". Live-Lesen: an expired value is shown with its read state ("Lesefehler") and a Detail note, not "Kein Mapping". |
