"""Startup-Prüfungen nach Bootstrap (z. B. Loxone-Smoke-Test, Tarif-Plausibilität)."""
from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from house_config.tariff_plausibility import (
    collect_tariff_plausibility_errors,
    format_tariff_plausibility_errors,
)
from integrations.loxone_connectivity import loxone_env_configured, verify_loxone_setup
from runtime_store.env_vars import read_env
from runtime_store.persist_paths import (
    resolve_backtesting_scenarios_json_path,
    resolve_grid_fees_json_path,
    resolve_house_profiles_json_path,
    resolve_tariffs_json_path,
    resolve_tariffs_schema_template_path,
)

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_flag(suffix: str, *, default: bool = False) -> bool:
    raw = read_env(suffix)
    if not raw:
        return default
    return raw.lower() in _TRUTHY


def prepare_loxone_push_inbox_for_startup(
    *,
    start_http_fn: Callable[[], None] | None = None,
    wait_for_link: bool | None = None,
) -> None:
    """Hydrate push inbox, optionally start VO HTTP, then wait for link.

    Call before ``run_loxone_verify_on_startup`` so last-known (≤5 min) and a
    fresh VO cycle can populate the inbox. ``wait_for_link`` defaults to True
    when ``EARNIE_PUSH_TOKEN`` (or legacy ``EARNIE_PILOT_PUSH_TOKEN``) is set.
    """
    from integrations.ehal_request_http import push_token
    from runtime_store.ehal_push_inbox import (
        hydrate_memory_from_disk,
        wait_for_push_link,
    )

    hydrate_memory_from_disk()
    if start_http_fn is not None:
        start_http_fn()
    if wait_for_link is None:
        wait_for_link = bool(push_token())
    if wait_for_link:
        wait_for_push_link()


def run_loxone_verify_on_startup() -> None:
    """
    Liest alle konfigurierten Loxone push/Live-IOs (ohne Merker-Roundtrip).

    Call after ``prepare_loxone_push_inbox_for_startup`` so the inbox is hydrated
    and (when tokenized) the push link has been waited for. Missing VO values are
    warnings; strict mode still aborts only on non-warning failures.

    EARNIE_SKIP_LOXONE_VERIFY=1 → überspringen
    EARNIE_VERIFY_LOXONE_ON_START=0 → überspringen
    EARNIE_STRICT_LOXONE_VERIFY=1 → bei Fehler mit Exit-Code 1 abbrechen
    """
    from runtime_store.shadow.mode import is_shadow_mode

    if is_shadow_mode():
        logger.info("Shadow Mode: Loxone-Verify beim Start übersprungen")
        return
    if _env_flag("SKIP_LOXONE_VERIFY"):
        logger.info(
            "Loxone-Startup-Prüfung übersprungen (EARNIE_SKIP_LOXONE_VERIFY)."
        )
        return
    if not _env_flag("VERIFY_LOXONE_ON_START", default=True):
        logger.info(
            "Loxone-Startup-Prüfung übersprungen "
            "(EARNIE_VERIFY_LOXONE_ON_START=0)."
        )
        return
    if not loxone_env_configured():
        logger.info(
            "Loxone-Startup-Prüfung übersprungen (LOXONE_* in .env nicht vollständig)."
        )
        return

    from integrations.loxone_connectivity import probe_current_loxone_credentials
    from runtime_store.loxone_auth_error import persist_loxone_auth_error

    ok, detail = probe_current_loxone_credentials()
    if not ok:
        if detail.startswith("Loxone auth failed"):
            code = 401 if "HTTP 401" in detail else 403
            persist_loxone_auth_error(
                message=detail,
                http_status=code,
                source="startup_checks",
            )
            logger.error(
                "Loxone-Startup-Prüfung: %s Marker-Prüfung übersprungen.",
                detail,
            )
            if _env_flag("STRICT_LOXONE_VERIFY"):
                raise SystemExit(1)
            return
        logger.warning(
            "Loxone-Startup-Prüfung: Miniserver nicht erreichbar (%s). "
            "Marker-Prüfung wird trotzdem versucht.",
            detail,
        )

    try:
        ok, results = verify_loxone_setup()
    except (FileNotFoundError, ValueError, KeyError) as exc:
        logger.error("Loxone-Startup-Prüfung: ungültige Konfiguration: %s", exc)
        if _env_flag("STRICT_LOXONE_VERIFY"):
            raise SystemExit(1) from exc
        return

    failed = 0
    for item in results:
        target = f" ({item.io_name})" if item.io_name else ""
        if item.passed:
            logger.info("[loxone-verify] OK %s%s: %s", item.label, target, item.detail)
        elif item.severity == "warning":
            logger.warning(
                "[loxone-verify] WARNUNG %s%s: %s", item.label, target, item.detail
            )
        else:
            failed += 1
            logger.error(
                "[loxone-verify] FEHLER %s%s: %s", item.label, target, item.detail
            )

    if ok:
        logger.info(
            "Loxone-Startup-Prüfung: alle %s Prüfungen erfolgreich.", len(results)
        )
        return

    message = (
        f"Loxone-Startup-Prüfung: {failed} von {len(results)} Prüfungen fehlgeschlagen."
    )
    if _env_flag("STRICT_LOXONE_VERIFY"):
        logger.error("%s Abbruch (EARNIE_STRICT_LOXONE_VERIFY).", message)
        raise SystemExit(1)
    logger.error("%s Optimierung startet trotzdem.", message)


def run_live_scenario_entity_check_on_startup() -> None:
    """Prüft, dass das Live-Szenario Entitäts-IDs enthält (kein Bootstrap-Minimalstand)."""
    from house_config.scenario_resolution import find_scenario_settings, get_live_scenario_id
    from runtime_store.config_load import load_config_or_exit

    config = load_config_or_exit()
    raw = getattr(config, "_raw_config", None)
    if not isinstance(raw, dict):
        return
    scenario_id = get_live_scenario_id(raw)
    try:
        settings = find_scenario_settings(
            resolve_backtesting_scenarios_json_path(),
            scenario_id,
        )
    except ValueError as exc:
        logger.error(
            "Live-Szenario-Startup-Prüfung: %s "
            "Bitte backtesting_scenarios.json mit dem Live-Szenario deployen.",
            exc,
        )
        if _env_flag("STRICT_TARIFF_VALIDATE"):
            raise SystemExit(1) from exc
        return

    missing = []
    bat_ids = settings.get("battery_ids")
    has_bat = isinstance(bat_ids, list) and any(str(x or "").strip() for x in bat_ids)
    if not has_bat and str(settings.get("battery_id", "") or "").strip():
        has_bat = True  # legacy singular; bootstrap migrates to battery_ids[]
    if not has_bat:
        missing.append("battery_ids")
    pv_ids = settings.get("pv_system_ids")
    has_pv = isinstance(pv_ids, list) and any(str(x or "").strip() for x in pv_ids)
    if not has_pv:
        missing.append("pv_system_ids")
    for key in ("import_tariff_id", "export_tariff_id", "house_profile_id"):
        if not str(settings.get(key, "") or "").strip():
            missing.append(key)
    if not missing:
        logger.info(
            "Live-Szenario-Startup-Prüfung: '%s' mit Entitäts-IDs vorhanden.",
            scenario_id,
        )
        run_live_topology_check_on_startup()
        return

    message = (
        f"Live-Szenario '{scenario_id}' unvollständig (Bootstrap-Minimalstand?): "
        f"fehlende IDs: {', '.join(missing)}. "
        "Bitte backtesting_scenarios.json und Komponenten-Sidecars prüfen."
    )
    if _env_flag("STRICT_TARIFF_VALIDATE"):
        logger.error("%s Abbruch (EARNIE_STRICT_TARIFF_VALIDATE).", message)
        raise SystemExit(1)
    logger.error("%s Optimierung startet trotzdem.", message)


def run_live_topology_check_on_startup() -> None:
    """Refuse daemon start when Topology P1 live rules fail (always strict)."""
    from house_config.topology_live import validate_live_topology_from_config_dir

    try:
        validate_live_topology_from_config_dir()
    except ValueError as exc:
        logger.error("Live-Topology-Startup-Prüfung: %s Abbruch.", exc)
        raise SystemExit(1) from exc
    logger.info("Live-Topology-Startup-Prüfung: OK.")


def run_tariff_plausibility_on_startup() -> None:
    """
    Validiert tariffs.json und Szenario-Referenzen beim Worker-Start.

    EARNIE_SKIP_TARIFF_VALIDATE=1 → überspringen
    EARNIE_STRICT_TARIFF_VALIDATE=1 → bei Fehler mit Exit-Code 1 abbrechen
    """
    if _env_flag("SKIP_TARIFF_VALIDATE"):
        logger.info(
            "Tarif-Startup-Prüfung übersprungen (EARNIE_SKIP_TARIFF_VALIDATE)."
        )
        return

    errors = collect_tariff_plausibility_errors(
        tariffs_path=resolve_tariffs_json_path(),
        scenarios_path=resolve_backtesting_scenarios_json_path(),
        schema_path=resolve_tariffs_schema_template_path(),
        with_edibert=True,
        grid_fees_path=resolve_grid_fees_json_path(),
        house_profiles_path=resolve_house_profiles_json_path(),
    )
    if not errors:
        logger.info("Tarif-Startup-Prüfung: tariffs.json plausibel.")
        return

    message = format_tariff_plausibility_errors(errors)
    if _env_flag("STRICT_TARIFF_VALIDATE"):
        logger.error(
            "%s Abbruch (EARNIE_STRICT_TARIFF_VALIDATE).", message
        )
        raise SystemExit(1)
    logger.error("%s Optimierung startet trotzdem.", message)


# Half a year later: catches a system zone with other (or no) daylight saving time.
_TZ_CHECK_OFFSETS_DAYS = (0, 182)


def _system_utc_offset(ts: float) -> timedelta:
    """UTC offset of the process' local time (container ``TZ`` / ``/etc/localtime``)."""
    return timedelta(seconds=time.localtime(ts).tm_gmtoff)


def timezone_mismatch(
    planning_timezone: str,
    *,
    now: datetime | None = None,
    system_offset: Callable[[float], timedelta] | None = None,
) -> str | None:
    """German error text when local time differs from the planning timezone, else ``None``.

    Earnie stores run times as naive local time (``completed_at``) and reads them in the
    planning timezone; a container on UTC shifts the history chart by the offset.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(planning_timezone)
    offset_of = system_offset or _system_utc_offset
    base = now or datetime.now(UTC)
    for days in _TZ_CHECK_OFFSETS_DAYS:
        moment = base + timedelta(days=days)
        planning = moment.astimezone(tz).utcoffset()
        system = offset_of(moment.timestamp())
        if planning != system:
            return (
                f"Systemzeit des Containers (UTC{_format_offset(system)}) passt nicht zur "
                f"Planungszeitzone {planning_timezone} (UTC{_format_offset(planning)}) "
                f"am {moment.astimezone(tz):%Y-%m-%d}. Die Historie im Monitor ist dadurch "
                "verschoben (leerer Bereich vor „Jetzt“). Abhilfe: in der Compose-Datei unter "
                f"environment TZ: {planning_timezone} setzen und den Container neu erstellen "
                "(docker compose … up -d)."
            )
    return None


def _format_offset(offset: timedelta | None) -> str:
    minutes = int((offset or timedelta()).total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def run_timezone_check_on_startup() -> None:
    """Logs an error when the container's local time is not the planning timezone."""
    import config

    message = timezone_mismatch(config.get_planning_timezone())
    if message is None:
        logger.info("Zeitzonen-Prüfung: Systemzeit = Planungszeitzone.")
        return
    logger.error("Zeitzonen-Prüfung: %s", message)


def main() -> int:
    """CLI-Einstieg (gleiche Logik wie beim Worker-Start)."""
    import config as cfg
    import logger_config
    from runtime_store.persist_paths import log_file

    cfg.reinit_config()
    logger_config.setup_logging(log_file=log_file(), level=logging.INFO)
    run_tariff_plausibility_on_startup()
    run_live_scenario_entity_check_on_startup()
    run_loxone_verify_on_startup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
