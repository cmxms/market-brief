"""Reader-visible regressions using synthetic data and forbidden transports."""
import datetime as dt
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gather as G


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network call")

    monkeypatch.setattr(G.httpx, "get", forbidden)
    monkeypatch.setattr(G.yf, "download", forbidden)


@pytest.fixture
def block():
    return {"date": "2026-11-17", "as_of": "2026-11-17T06:00:00-05:00",
            "event_gate": {}, "index_recap": {}, "macro_pulse": {},
            "gex": {s: {"error": "synthetic unavailable"} for s in G.GEX_SYMBOLS},
            "finnhub": {}}


@pytest.mark.parametrize("today,stale", [
    (dt.date(2026, 9, 8), False),
    (dt.date(2026, 9, 15), True),
    (dt.date(2026, 9, 5), False),
])
def test_eod_freshness_counts_missing_sessions(monkeypatch, today, stale):
    monkeypatch.setattr(G, "_treasury_curve_rows", lambda _: [
        (dt.date(2026, 9, 4), {"10Y": 4.0}),
        (dt.date(2026, 9, 3), {"10Y": 4.1})])
    assert G.treasury_yields(today)["stale"] is stale


def test_normal_tuesday_with_monday_close_is_fresh(monkeypatch):
    monkeypatch.setattr(G, "_treasury_curve_rows", lambda _: [
        (dt.date(2026, 11, 16), {"10Y": 4.0})])
    assert G.treasury_yields(dt.date(2026, 11, 17))["stale"] is False


def test_market_holidays_include_observed_shifts_and_good_friday():
    expected = {dt.date.fromisoformat(s) for s in [
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]}
    assert G._market_holidays(2026) == expected
    assert dt.date(2027, 7, 5) in G._market_holidays(2027)


@pytest.mark.parametrize("section,count", [("general", 14), ("themed", 12)])
def test_every_collected_headline_is_rendered(block, section, count):
    items = [{"headline": f"Synthetic headline {i:02d}", "source": "synthetic",
              "trusted": True, "dt": None} for i in range(count)]
    block["finnhub"][section] = items
    text = G.render_md(block)
    for item in items:
        assert item["headline"] in text


@pytest.mark.parametrize("failed", ["2026-11-19", "2026-12-18"])
def test_failed_expiry_names_affected_views(monkeypatch, block, failed):
    exps = ["2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-12-18"]
    monkeypatch.setattr(G, "_quote", lambda _: {"last": 100})
    monkeypatch.setattr(G, "_expirations", lambda _: exps)
    monkeypatch.setattr(G, "_chain", lambda _, e: [] if e == failed else [
        {"strike": 100, "option_type": "call", "open_interest": 100,
         "greeks": {"gamma": 0.01}}])
    block["gex"] = G.gex_tradier(dt.date(2026, 11, 17))
    text = G.render_md(block)
    assert "GEX full-chain view unavailable" in text
    assert failed in text
    if failed == exps[0]:
        assert "GEX near-term view unavailable" in text
    else:
        assert "near-term (4 exp" in text


@pytest.mark.parametrize("missing,label", [("near_term", "near-term"), ("full_chain", "full-chain")])
def test_missing_gex_view_is_named(block, missing, label):
    healthy = G._gex_from_options([], 100) | {"n_expirations": 1}
    block["gex"] = {s: {"spot": 100, "near_term": healthy, "full_chain": healthy}
                    for s in G.GEX_SYMBOLS}
    for v in block["gex"].values():
        v[missing] = None
    assert f"GEX {label} view unavailable" in G.render_md(block)


def test_crosscheck_uses_supplied_samples_without_http_and_reports_mismatch(block):
    samples = {"CNBC yields": {"10Y": {"last": 4.0}},
               "Yahoo yields": {"10Y": {"last": 4.2}},
               "CNBC volatility": {"^VIX": {"last": 20.0}},
               "Yahoo volatility": {"^VIX": {"last": 20.1}}}
    result = G.source_crosscheck(samples)
    assert result["yields"]["rows"]["10Y"]["primary"] == 4.0
    assert result["yields"]["all_match"] is False
    block["source_crosscheck"] = result
    assert "10Y: CNBC 4.0 vs Yahoo 4.2" in G.render_md(block)


def test_partial_samples_do_not_refetch_missing_counterpart():
    result = G.source_crosscheck({"CNBC yields": {"10Y": {"last": 4.0}}})
    assert result["yields"]["n"] == 0
    assert any(source == "Yahoo yields" for source, _ in result["failures"])


def test_crosscheck_last_resort_fetch_is_labelled(block, monkeypatch):
    for name in ("_cnbc_yields", "_yahoo_chart_yields", "_cnbc_vol", "_yahoo_vol"):
        monkeypatch.setattr(G, name, lambda: {"synthetic": {"last": 1.0}})
    block["source_crosscheck"] = G.source_crosscheck()
    assert "not the published sample" in G.render_md(block)


def test_gather_passes_exact_macro_samples(monkeypatch):
    samples = {"CNBC yields": {"10Y": {"last": 4.0}}}
    macro = {"source_samples": samples}
    monkeypatch.setattr(G, "macro_pulse", lambda _, **kwargs: macro)
    for name in ("event_gate", "gex_tradier"):
        monkeypatch.setattr(G, name, lambda _: {})
    for name in ("index_recap", "sector_rotation"):
        monkeypatch.setattr(G, name, dict)
    monkeypatch.setattr(G, "finnhub_news", lambda _: {})
    crosscheck = Mock(return_value={})
    monkeypatch.setattr(G, "source_crosscheck", crosscheck)
    G.gather(dt.date(2026, 11, 17))
    crosscheck.assert_called_once_with(samples)


def test_macro_retains_selected_samples(monkeypatch):
    yields = {"10Y": {"last": 4.0, "chg": 0.1, "time": "2026-11-17T06:00:00-05:00"}}
    vol = {t: {"last": 20.0, "last_date": "2026-11-17", "source": "CNBC"}
           for t in G.VOL_TICKERS}
    monkeypatch.setattr(G, "_cnbc_yields", lambda: yields)
    monkeypatch.setattr(G, "_cnbc_vol", lambda: vol)
    monkeypatch.setattr(G, "treasury_yields", lambda _: {})
    monkeypatch.setattr(G, "cboe_vol_official", dict)
    result = G.macro_pulse(dt.date(2026, 11, 17))
    assert result["source_samples"]["CNBC yields"] == yields
    assert result["source_samples"]["CNBC volatility"] == vol
    assert result["yields"]["tenors"]["10Y"]["last"] == yields["10Y"]["last"]
    assert result["^VIX"]["last"] == vol["^VIX"]["last"]


@pytest.mark.parametrize("missing", ["near_term", "full_chain"])
def test_gex_computation_failure_is_isolated_and_named(monkeypatch, block, missing):
    exps = ["2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-12-18"]
    monkeypatch.setattr(G, "_quote", lambda _: {"last": 100})
    monkeypatch.setattr(G, "_expirations", lambda _: exps)
    monkeypatch.setattr(G, "_chains_parallel", lambda *_: ({e: [] for e in exps}, []))
    healthy = G._gex_from_options([], 100)

    def compute(*args, expirations_requested, **kwargs):
        if expirations_requested == (4 if missing == "near_term" else 5):
            raise ValueError("private parse detail")
        return healthy

    monkeypatch.setattr(G, "_gex_from_options", compute)
    block["gex"] = G.gex_tradier(dt.date(2026, 11, 17))
    text = G.render_md(block)
    label = missing.replace("_", "-")
    assert f"GEX {label} view unavailable — Tradier chain computation failed: ValueError" in text
    assert "view shown" in text
    assert "private parse detail" not in text


def test_eod_only_sample_does_not_trigger_live_refetch(monkeypatch):
    monkeypatch.setattr(G, "live_yields", lambda _: {})
    monkeypatch.setattr(G, "treasury_yields", lambda _: {
        "source": "US Treasury par yield curve", "tenors": {"10Y": {"last": 4.0}}})
    monkeypatch.setattr(G, "_cnbc_vol", dict)
    monkeypatch.setattr(G, "_yahoo_vol", dict)
    monkeypatch.setattr(G, "cboe_vol_official", dict)
    monkeypatch.setattr(G, "pull_daily", lambda _: {"error": "synthetic unavailable"})
    macro = G.macro_pulse(dt.date(2026, 11, 17))
    result = G.source_crosscheck(macro["source_samples"])
    assert result["sample_basis"] == "Gathered samples"
    assert result["yields"]["all_match"] is None


def test_observed_new_year_crossing_year_boundary():
    # The brief explicitly specifies Friday observance for Saturday holidays.
    assert dt.date(2027, 12, 31) in G._market_holidays(2027)
    assert G._sessions_behind(dt.date(2027, 12, 30), dt.date(2028, 1, 3)) == 0


def test_daily_crosscheck_downloads_each_relay_once_and_checks_published_level(monkeypatch, block):
    cnbc_yields = Mock(return_value={"10Y": {"last": 4.0, "chg": 0.1,
                                           "time": "2026-11-17T06:00:00-05:00"}})
    yahoo_yields = Mock(return_value={"10Y": {"last": 4.2, "chg": 0.1}})
    cnbc_vol = Mock(return_value={t: {"last": 20.0, "last_date": "2026-11-17",
                                    "source": "CNBC"} for t in G.VOL_TICKERS})
    yahoo_vol = Mock(return_value={t: {"last": 20.5, "last_date": "2026-11-17",
                                     "source": "Yahoo(intraday)"} for t in G.VOL_TICKERS})
    for name, fetch in [("_cnbc_yields", cnbc_yields), ("_yahoo_chart_yields", yahoo_yields),
                        ("_cnbc_vol", cnbc_vol), ("_yahoo_vol", yahoo_vol)]:
        monkeypatch.setattr(G, name, fetch)
    monkeypatch.setattr(G, "treasury_yields", lambda _: {})
    monkeypatch.setattr(G, "cboe_vol_official", dict)
    for name in ("event_gate", "gex_tradier"):
        monkeypatch.setattr(G, name, lambda _: {})
    for name in ("index_recap", "sector_rotation"):
        monkeypatch.setattr(G, name, dict)
    monkeypatch.setattr(G, "finnhub_news", lambda _: {})
    result = G.gather(dt.date(2026, 11, 17))
    for fetch in (cnbc_yields, yahoo_yields, cnbc_vol, yahoo_vol):
        fetch.assert_called_once_with()
    macro, checked = result["macro_pulse"], result["source_crosscheck"]
    assert checked["yields"]["rows"]["10Y"]["primary"] == macro["yields"]["tenors"]["10Y"]["last"]
    assert checked["vol"]["rows"]["^VIX"]["primary"] == macro["^VIX"]["last"]
    assert checked["yields"]["all_match"] is False
    assert checked["vol"]["all_match"] is False
