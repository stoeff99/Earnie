"""External import prices (EKZ via MQTT): parser, store, EPEX fit and the import seam."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from data import external_prices as ext
from data.edibert_slot_prices import pricing_reference, price_points

_SLOT = timedelta(minutes=15)
_CET = timezone(timedelta(hours=1))
_DAY = datetime(2026, 11, 1, tzinfo=_CET).astimezone(UTC)


def _interval(start: datetime, chf: float) -> dict:
    local = start.astimezone(_CET)
    return {
        "start_timestamp": local.isoformat(),
        "end_timestamp": (local + _SLOT).isoformat(),
        "chf_per_kwh_sum": chf,
        "chf_m_per_hour": 0.0111,
        "total_chf": chf + 0.0111,
        "month_hours_used": 720,
    }


def _payload(values: list[float], *, start: datetime = _DAY) -> str:
    intervals = [_interval(start + _SLOT * i, v) for i, v in enumerate(values)]
    return json.dumps(
        {
            "interval_count": len(intervals),
            "intervals": intervals,
            "publication_timestamp": "2026-11-01T06:13:03+01:00",
        }
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / ext.STORE_FILENAME
    monkeypatch.setattr(ext, "_store_path", lambda: str(path))
    return path


# --- parser --------------------------------------------------------------------------------


def test_parser_uses_chf_per_kwh_sum_in_rappen():
    feed = ext.parse_ekz_intervals(_payload([0.22364, 0.21731]))
    assert list(feed.slots.values()) == pytest.approx([22.364, 21.731])
    assert min(feed.slots) == _DAY
    assert feed.publication == datetime(2026, 11, 1, 5, 13, 3, tzinfo=UTC)


def test_parser_drops_the_copied_tail_of_unpublished_slots():
    # Real EKZ message of 2026-11-01: the next day repeats the last published value 96 times.
    published = [0.21, 0.22, 0.23, 0.21221]
    feed = ext.parse_ekz_intervals(_payload(published + [0.21221] * 96))
    assert len(feed.slots) == len(published)
    assert feed.dropped_forward_fill == 96
    assert max(feed.slots) == _DAY + _SLOT * 3


def test_parser_keeps_a_short_run_of_equal_prices():
    feed = ext.parse_ekz_intervals(_payload([0.20, 0.21, 0.21, 0.21]))
    assert len(feed.slots) == 4
    assert feed.dropped_forward_fill == 0


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not json", "kein gültiges JSON"),
        (json.dumps({"hourly": []}), "'intervals'"),
        (
            json.dumps(
                {
                    "intervals": [
                        {
                            "start_timestamp": "2026-11-01T00:00:00+01:00",
                            "end_timestamp": "2026-11-01T01:00:00+01:00",
                            "chf_per_kwh_sum": 0.2,
                        }
                    ]
                }
            ),
            "15-Minuten",
        ),
        (
            json.dumps(
                {
                    "intervals": [
                        {
                            "start_timestamp": "2026-11-01T00:00:00",
                            "end_timestamp": "2026-11-01T00:15:00",
                            "chf_per_kwh_sum": 0.2,
                        }
                    ]
                }
            ),
            "ohne Zeitzone",
        ),
    ],
)
def test_parser_rejects_bad_messages(payload, message):
    with pytest.raises(ValueError, match=message):
        ext.parse_ekz_intervals(payload)


# --- store ---------------------------------------------------------------------------------


def test_ingest_merges_messages_and_reports_the_horizon(store):
    now = _DAY + timedelta(hours=7)
    ext.ingest_ekz_payload(_payload([0.20] + [0.21] * 3, start=_DAY), now=now)
    ext.ingest_ekz_payload(_payload([0.25, 0.26], start=_DAY + _SLOT * 4), now=now)
    summary = ext.store_summary()
    assert summary["slots"] == 6
    assert summary["until"] == (_DAY + _SLOT * 6).isoformat()
    assert summary["publication"] == "2026-11-01T05:13:03+00:00"


def test_ingest_prunes_old_slots(store):
    old = _DAY - timedelta(days=ext.KEEP_DAYS + 1)
    ext.ingest_ekz_payload(_payload([0.30, 0.31], start=old), now=old)
    ext.ingest_ekz_payload(_payload([0.20, 0.21]), now=_DAY)
    assert ext.store_summary()["slots"] == 2


# --- fit and market ------------------------------------------------------------------------


def test_fit_recovers_a_linear_relation():
    pairs = [(x, 3.0 + 0.8 * x) for x in range(30)]
    a, b = ext.fit_epex(pairs)
    assert (a, b) == (pytest.approx(3.0), pytest.approx(0.8))


def test_fit_needs_enough_pairs():
    with pytest.raises(ValueError, match="mindestens 24"):
        ext.fit_epex([(1.0, 2.0)] * 5)


def test_market_uses_published_values_and_estimates_the_rest(store):
    slots = [_DAY + _SLOT * i for i in range(40)]
    epex = {slot: 10.0 + i for i, slot in enumerate(slots)}
    # 32 published slots follow external = 2 + 1.5 · EPEX exactly; 8 are missing.
    published = [(2.0 + 1.5 * epex[slot]) / 100 for slot in slots[:32]]
    ext.ingest_ekz_payload(_payload(published), now=_DAY)

    result = ext.external_market(slots, epex)

    assert result.estimated == 8
    assert result.fit == (pytest.approx(2.0), pytest.approx(1.5))
    assert result.market[slots[0]] == pytest.approx(2.0 + 1.5 * 10.0)
    assert result.market[slots[-1]] == pytest.approx(2.0 + 1.5 * epex[slots[-1]])
    assert ext.store_summary()["pairs"] == 32


def test_market_without_pairs_is_an_error(store):
    slots = [_DAY + _SLOT * i for i in range(4)]
    with pytest.raises(ValueError, match="noch nicht aus EPEX"):
        ext.external_market(slots, {slot: 10.0 for slot in slots})


def test_market_gap_without_epex_is_an_error(store):
    slots = [_DAY + _SLOT * i for i in range(30)]
    ext.ingest_ekz_payload(_payload([(5.0 + i) / 100 for i in range(29)]), now=_DAY)
    epex = {slot: float(i) for i, slot in enumerate(slots[:29])}
    with pytest.raises(ValueError, match="weder einen externen Preis"):
        ext.external_market(slots, epex)


# --- import seam ---------------------------------------------------------------------------


def _tariffs(tmp_path, **overrides) -> str:
    tariff = {
        "id": "ekz_dyn", "label": "EKZ dynamisch", "type": "spot_hourly", "land": "CH",
        "supplier_id": "ekz", "prices_include_vat": False, "vat_percent": 8.1,
        "markup_percent": 0.0, "settlement_fee_cent_kwh": 0.0, "settlement_mtu": "15min",
        **overrides,
    }
    path = tmp_path / "tariffs.json"
    path.write_text(
        json.dumps({"tariff_format": 1, "import_tariffs": [tariff], "export_tariffs": []}),
        encoding="utf-8",
    )
    return str(path)


def _ref(tariffs_path: str) -> dict:
    return pricing_reference(
        tariffs_path=tariffs_path, grid_fees_path=None, import_tariff_id="ekz_dyn",
        export_tariff_id=None, timezone_name="Europe/Zurich", profile={"land": "CH"},
    )


def test_import_seam_prices_the_external_tariff_from_the_feed(store, tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "external_tariff_id", lambda: "ekz_dyn")
    ext.ingest_ekz_payload(_payload([0.22364, 0.21731]), now=_DAY)
    slots = [datetime(2026, 11, 1, 0, 0), datetime(2026, 11, 1, 0, 15)]  # naive = house time

    points = price_points("import", slots, [30.0, 30.0], _ref(_tariffs(tmp_path)))

    # EKZ value + 8.1 % VAT; the EPEX value 30.0 is not used.
    assert [p.cent for p in points] == pytest.approx([22.364 * 1.081, 21.731 * 1.081])


def test_other_tariffs_keep_pricing_from_epex(store, tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "external_tariff_id", lambda: "some_other_tariff")
    ext.ingest_ekz_payload(_payload([0.22364]), now=_DAY)
    points = price_points(
        "import", [datetime(2026, 11, 1, 0, 0)], [30.0], _ref(_tariffs(tmp_path))
    )
    assert points[0].cent == pytest.approx(30.0 * 1.081)


def test_external_tariff_must_settle_per_quarter_hour(store, tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "external_tariff_id", lambda: "ekz_dyn")
    ext.ingest_ekz_payload(_payload([0.22364]), now=_DAY)
    ref = _ref(_tariffs(tmp_path, settlement_mtu="60min"))
    with pytest.raises(ValueError, match="settlement_mtu 15min"):
        price_points("import", [datetime(2026, 11, 1, 0, 0)], [30.0], ref)


def test_estimated_slots_are_reported(store, tmp_path, monkeypatch):
    monkeypatch.setattr(ext, "external_tariff_id", lambda: "ekz_dyn")
    slots_utc = [_DAY + _SLOT * i for i in range(28)]
    epex = [10.0 + i for i in range(28)]
    ext.ingest_ekz_payload(_payload([(1.0 + e) / 100 for e in epex[:26]]), now=_DAY)
    local = [s.astimezone(ZoneInfo("Europe/Zurich")).replace(tzinfo=None) for s in slots_utc]
    warnings: list[str] = []

    points = price_points("import", local, epex, _ref(_tariffs(tmp_path)), warnings_out=warnings)

    assert len(points) == 28
    assert points[-1].cent == pytest.approx((1.0 + epex[-1]) * 1.081)
    assert any("2 Viertelstunden ohne externen Preis aus EPEX geschätzt" in w for w in warnings)
