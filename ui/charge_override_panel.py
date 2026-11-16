"""Monitor: one-off charge job per EV ("Ladeauftrag") — target SoC and ready-by time.

Writes ``runtime/charge_overrides.json`` (``runtime_store.charge_overrides``) and asks the
daemon to re-plan. Auto-planning stays on; the job only replaces this plug-in's target and
deadline and ends on unplug or at its ready-by time.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import streamlit as st

import config
from optimizer.charging_schedule import charging_schedule_enabled, config_day_schedule
from runtime_store import charge_overrides as store


def override_consumers(consumers: list[dict]) -> list[dict]:
    """EVs whose charge target comes from the house profile (the job replaces it)."""
    return [
        c
        for c in consumers
        if charging_schedule_enabled(c) and c.get("daily_target_source", "config") == "config"
    ]


def default_ready_by(consumer: dict, now: datetime) -> datetime:
    """Next configured ready-by hour after ``now`` (weekday / weekend), else now + 8 h."""
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        hour = config_day_schedule(consumer, datetime.combine(day, time(12))).get("ready_by_hour")
        if hour is None:
            continue
        candidate = datetime.combine(day, time(int(hour) % 24))
        if candidate > now:
            return candidate
    return (now + timedelta(hours=8)).replace(minute=0, second=0, microsecond=0)


def default_target_soc(consumer: dict) -> float:
    sched = consumer.get("charging_schedule") or {}
    return float(sched.get("target_soc_percent") or 80.0)


def job_status_text(job: store.ChargeOverride | None) -> str:
    if job is None:
        return "Kein Ladeauftrag — Earnie plant nach den Werten im Hauskonfigurator."
    waiting = "" if job.plug_seen else " · wartet auf das Anstecken"
    return (
        f"Ladeauftrag aktiv: Ziel {job.target_soc_percent:.0f} % bis "
        f"{job.ready_by.strftime('%a %d.%m. %H:%M')}{waiting}. "
        "Endet beim Abstecken oder zur Fertig-bis-Zeit."
    )


def _request_replan(done_text: str) -> None:
    from runtime_store.optimize_request import OptimizeRequestError, request_optimize

    try:
        request_optimize()
    except OptimizeRequestError as exc:
        st.warning(f"{done_text} Der Optimierer-Dienst übernimmt es beim nächsten Takt ({exc}).")
        return
    st.success(f"{done_text} Der Optimierer-Dienst plant gleich neu.")


def _render_consumer(consumer: dict, now: datetime) -> None:
    cid = str(consumer["id"])
    name = consumer.get("name") or cid
    job = store.get_override(cid)
    st.markdown(f"**{name}**")
    st.caption(job_status_text(job))
    ready_default = job.ready_by if job else default_ready_by(consumer, now)
    with st.form(f"charge_override_{cid}", border=False):
        cols = st.columns(3)
        target = cols[0].number_input(
            "Ziel-SOC (%)",
            min_value=1.0,
            max_value=100.0,
            step=5.0,
            value=float(job.target_soc_percent) if job else default_target_soc(consumer),
            key=f"charge_override_soc_{cid}",
        )
        ready_day: date = cols[1].date_input(
            "Fertig am", value=ready_default.date(), key=f"charge_override_day_{cid}"
        )
        ready_time: time = cols[2].time_input(
            "Fertig um",
            value=ready_default.time(),
            step=timedelta(minutes=15),
            key=f"charge_override_time_{cid}",
        )
        submitted = st.form_submit_button("Ladeauftrag setzen")
    if submitted:
        try:
            store.set_override(
                cid,
                target_soc_percent=float(target),
                ready_by=datetime.combine(ready_day, ready_time),
                now=datetime.now(),
            )
        except ValueError as exc:
            st.error(str(exc))
            return
        _request_replan("Ladeauftrag gespeichert.")
    if job is not None and st.button("Ladeauftrag aufheben", key=f"charge_override_clear_{cid}"):
        store.clear_override(cid, reason="im Monitor aufgehoben")
        _request_replan("Ladeauftrag aufgehoben.")


def render_charge_override_panel() -> None:
    """Expander „Ladeauftrag (einmalig)“ on the Monitor page; nothing without such an EV."""
    consumers = override_consumers(config.get_flexible_consumers(optimizer_only=True))
    if not consumers:
        return
    with st.expander("🔌 Ladeauftrag (einmalig)", expanded=any(
        store.get_override(str(c["id"])) is not None for c in consumers
    )):
        st.caption(
            "Ersetzt für dieses Anstecken Ziel-SOC und Fertig-bis-Zeit aus dem Hauskonfigurator; "
            "Earnie plant weiter die günstigsten Zeiten. Sofort laden mit Höchststrom: "
            "Loxone-Eingang „Sofort laden“."
        )
        now = datetime.now()
        for consumer in consumers:
            _render_consumer(consumer, now)
