"""Offline failure-policy regressions. No vendor, Hermes, or Discord calls."""
import builtins
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import gather as G
import post_discord as P
import run_brief as R

TODAY = dt.date(2026, 11, 17)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected transport, child process, or sleep")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(G.yf, "download", forbidden)
    monkeypatch.setattr(P.requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(P.time, "sleep", forbidden)


@pytest.fixture
def block():
    return {
        "date": str(TODAY), "as_of": "2026-11-17T06:00:00-05:00",
        "event_gate": {"error": "calendar test failure"},
        "index_recap": {"nq_intraday": {"error": "vendor timeout"}},
        "macro_pulse": {"^VIX": {"error": "vendor timeout"},
                        "^VIX9D": {"error": "vendor timeout"}},
        "sector_rotation": {"error": "vendor timeout"},
        "gex": {s: {"error": "vendor timeout"} for s in G.GEX_SYMBOLS},
        "finnhub": {"error": "vendor timeout"},
    }


@pytest.mark.parametrize("marker", [
    "NQ intraday: UNAVAILABLE", "^VIX: UNAVAILABLE", "^VIX9D: UNAVAILABLE",
    "Sector rotation: UNAVAILABLE", "Finnhub headlines: UNAVAILABLE",
])
def test_failed_section_is_visible(block, marker):
    text = G.render_md(block)
    assert f"{marker} — vendor timeout" in text


@pytest.mark.parametrize("operation,label", [
    (lambda: G._treasury_curve_rows(2026), "Treasury curve"),
    (G._fred_yields, "FRED 2Y"),
    (G._cnbc_yields, "CNBC yields"),
    (G._yahoo_chart_yields, "Yahoo 5Y yield"),
    (G._cnbc_vol, "CNBC volatility"),
    (G._yahoo_vol, "Yahoo ^VIX volatility"),
    (lambda: G._cboe_index("VIX"), "Cboe VIX"),
])
@pytest.mark.parametrize("failure", ["timeout", "http"])
def test_source_failure_is_typed_for_chain_fallback(monkeypatch, operation, label, failure):
    monkeypatch.setattr(G, "_fred_key", lambda: "synthetic")
    if failure == "timeout":
        monkeypatch.setattr(httpx, "get", Mock(side_effect=httpx.ReadTimeout("synthetic request detail")))
    else:
        response = httpx.Response(503, request=httpx.Request("GET", "https://example.invalid"))
        monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    with pytest.raises(G.SourceUnavailable) as caught:
        operation()
    assert str(caught.value).startswith(f"{label} unavailable")
    assert "synthetic request detail" not in str(caught.value)
    assert "https://" not in str(caught.value)


@pytest.mark.parametrize("csv_text", ["Date,2 Yr\nbad,4.0\n", "Date,2 Yr\n11/16/2026,bad\n"])
def test_bad_treasury_row_stops_run(monkeypatch, csv_text):
    response = httpx.Response(200, text=csv_text, request=httpx.Request("GET", "https://example.invalid"))
    monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    with pytest.raises(RuntimeError, match="Treasury curve unavailable"):
        G._treasury_curve_rows(2026)


@pytest.mark.parametrize("operation", [G._cnbc_yields, G._yahoo_chart_yields, G._cnbc_vol, G._yahoo_vol])
def test_source_parse_failure_stops_run(monkeypatch, operation):
    response = httpx.Response(200, json={}, request=httpx.Request("GET", "https://example.invalid"))
    monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    with pytest.raises(RuntimeError, match="unavailable: KeyError"):
        operation()


@pytest.mark.parametrize("operation", [G.pull_daily, G._intraday_prior_close])
def test_roll_and_daily_unexpected_error_propagates(monkeypatch, operation):
    monkeypatch.setattr(G.yf, "download", Mock(side_effect=RuntimeError("unexpected")))
    with pytest.raises(RuntimeError, match="unexpected"):
        operation("NQ=F")


def test_roll_parse_failure_cannot_publish_unverified_change(monkeypatch):
    monkeypatch.setattr(G.yf, "download", Mock(side_effect=ValueError("bad bars")))
    with pytest.raises(RuntimeError, match="Contract-roll verification unavailable"):
        G._intraday_prior_close("NQ=F")


@pytest.mark.parametrize("operation", [lambda: G.pull_daily("SPY"), G.pull_intraday_nq])
def test_price_parse_failure_has_visible_marker(monkeypatch, operation):
    monkeypatch.setattr(G.yf, "download", Mock(side_effect=ValueError("bad bars")))
    assert "unavailable: ValueError" in operation()["error"]


@pytest.mark.parametrize("failed_symbol", ["SPY", "XLK"])
def test_sector_failure_cannot_become_partial_ranking(monkeypatch, block, failed_symbol):
    def daily(symbol):
        return ({"error": "vendor timeout"} if symbol == failed_symbol else
                {"chg_pct": 1.0, "recent_closes": [100.0, 101.0]})

    monkeypatch.setattr(G, "pull_daily", daily)
    block["sector_rotation"] = G.sector_rotation()
    assert "ranked" not in block["sector_rotation"]
    text = G.render_md(block)
    assert "Sector rotation: UNAVAILABLE" in text
    assert failed_symbol in text and "vendor timeout" in text


def test_chain_timeout_withholds_gamma_in_rendered_block(monkeypatch, block):
    monkeypatch.setattr(G, "_quote", lambda _: {"last": 100})
    monkeypatch.setattr(G, "_expirations", lambda _: ["2026-11-20"])
    monkeypatch.setattr(G, "_chain", Mock(side_effect=httpx.ReadTimeout("timeout")))
    block["gex"] = G.gex_tradier(TODAY)
    text = G.render_md(block)
    assert text.count("GAMMA READ UNAVAILABLE") == len(G.GEX_SYMBOLS)
    assert "2026-11-20" in text
    assert "total +" not in text


def test_unexpected_chain_error_propagates(monkeypatch):
    monkeypatch.setattr(G, "_chain", Mock(side_effect=AssertionError("unexpected")))
    with pytest.raises(AssertionError, match="unexpected"):
        G._chains_parallel("SPY", ["2026-11-20"])


def test_quote_timeout_is_visible(monkeypatch, block):
    monkeypatch.setattr(G, "_quote", Mock(side_effect=httpx.ReadTimeout("request details")))
    block["gex"] = G.gex_tradier(TODAY)
    assert "gamma read unavailable: ReadTimeout" in G.render_md(block)


@pytest.mark.parametrize("calendar", ["events: [", "[]", "events:\n  - date: not-a-date\n    name: release\n"])
def test_calendar_failures_remain_visible(monkeypatch, tmp_path, block, calendar):
    path = tmp_path / "calendar.yaml"
    path.write_text(calendar, encoding="utf-8")
    monkeypatch.setattr(G, "T1_CALENDAR", path)
    block["event_gate"] = G.event_gate(TODAY)
    text = G.render_md(block)
    assert "EVENT CALENDAR UNAVAILABLE" in text or "skipped as malformed" in text


@pytest.mark.parametrize("failure", ["timeout", "http"])
def test_headline_failure_is_visible_without_request_details(monkeypatch, block, failure):
    if failure == "timeout":
        monkeypatch.setattr(httpx, "get", Mock(side_effect=httpx.ReadTimeout("request details")))
    else:
        response = httpx.Response(401, request=httpx.Request("GET", "https://example.invalid"))
        monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    block["finnhub"] = G.finnhub_news("synthetic")
    text = G.render_md(block)
    assert "Finnhub headlines: UNAVAILABLE" in text
    assert "request details" not in text and "https://" not in text


def test_bad_gamma_total_stops_render():
    with pytest.raises(ValueError, match="Gamma total unreadable"):
        G._bn(None)


def test_unreadable_archive_preserves_original(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path)
    path = tmp_path / f"{TODAY}.json"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cannot be archived safely"):
        G._archive_previous_run(str(TODAY))
    assert path.read_text(encoding="utf-8") == "broken"
    assert not (tmp_path / "runs").exists()


def test_gather_failure_stops_before_compose_or_delivery(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "alert_failure", Mock())
    # Exercise a permitted live date; historical dates are now refused earlier.
    date = dt.datetime.now(R.ET).date().isoformat()
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "load_settings", dict)
    for name in ("STATE", "LOGS", "DATA", "BRIEFS", "PROJECT"):
        monkeypatch.setattr(config, name, tmp_path)
    monkeypatch.setattr(G, "DATA_DIR", tmp_path)
    monkeypatch.setattr(G, "BRIEFS_DIR", tmp_path)
    monkeypatch.setattr(G, "main", Mock(side_effect=RuntimeError("CNBC yields unavailable: ReadTimeout")))
    import compose
    import deliver
    composer = Mock(side_effect=AssertionError("must not compose"))
    sender = Mock(side_effect=AssertionError("must not post"))
    monkeypatch.setattr(compose, "run", composer)
    monkeypatch.setattr(deliver, "deliver_production", sender)
    assert R.main(["--date", date, "--ignore-window", "--force-weekend", "--force-holiday"]) == 3
    assert "gather failed" in (tmp_path / f"{date}.log").read_text(encoding="utf-8")
    composer.assert_not_called()
    sender.assert_not_called()


@pytest.mark.parametrize("image", [False, True])
def test_malformed_retry_delay_warns_and_retains_retry(monkeypatch, tmp_path, caplog, image):
    response = Mock(status_code=429)
    response.json.side_effect = ValueError("bad retry delay")
    post = Mock(side_effect=[response, Mock(status_code=204)])
    monkeypatch.setattr(P.requests, "post", post)
    sleep = Mock()
    monkeypatch.setattr(P.time, "sleep", sleep)
    with caplog.at_level(logging.WARNING):
        if image:
            path = tmp_path / "image.png"
            path.write_bytes(b"synthetic")
            P._send_file("https://example.invalid", path)
        else:
            P._send("https://example.invalid", "brief")
    assert "bad retry delay" in caplog.text
    sleep.assert_called_once_with(2.5)
    assert post.call_count == 2


def test_missing_stale_calendar_warns(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config, "DATA", tmp_path)
    with caplog.at_level(logging.WARNING):
        assert P._calendar_events_today(dt.date(1999, 1, 1)) == []
    assert "Stale-check calendar unavailable" in caplog.text


def test_stale_config_import_failure_is_loud(monkeypatch):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "config":
            raise ImportError("unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(RuntimeError, match="Stale-check configuration unavailable"):
        P._calendar_events_today(TODAY)


def test_valid_archive_still_uses_own_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path)
    path = tmp_path / f"{TODAY}.json"
    text = json.dumps({"as_of": "2026-11-17T06:00:00-05:00"})
    path.write_text(text, encoding="utf-8")
    assert G._archive_previous_run(str(TODAY)) == "0600"
    assert (tmp_path / "runs" / f"{TODAY}-0600.json").read_text(encoding="utf-8") == text


@pytest.mark.parametrize("operation,symbol", [(G._cnbc_yields, "US2Y"), (G._cnbc_vol, "VIX")])
def test_bad_cnbc_level_cannot_silently_drop_instrument(monkeypatch, operation, symbol):
    response = httpx.Response(
        200, json={"FormattedQuoteResult": {"FormattedQuote": [{"symbol": symbol, "last": "bad"}]}},
        request=httpx.Request("GET", "https://example.invalid"),
    )
    monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    with pytest.raises(RuntimeError, match="unavailable: ValueError"):
        operation()


def test_bad_cboe_row_cannot_silently_drop_session(monkeypatch):
    response = httpx.Response(
        200, text="DATE,OPEN,HIGH,LOW,CLOSE\n11/16/2026,1,1,1,bad\n",
        request=httpx.Request("GET", "https://example.invalid"),
    )
    monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    with pytest.raises(ValueError, match="Cboe VIX history unreadable"):
        G._cboe_index("VIX")


@pytest.mark.parametrize("image", [False, True])
def test_unexpected_fanout_error_propagates(monkeypatch, tmp_path, image):
    failure = Mock(side_effect=TypeError("unexpected sender error"))
    with pytest.raises(TypeError, match="unexpected sender error"):
        if image:
            path = tmp_path / "image.png"
            path.write_bytes(b"synthetic")
            monkeypatch.setattr(P, "_send_file", failure)
            P.post_image(path, webhook="https://example.invalid")
        else:
            monkeypatch.setattr(P, "_post_one", failure)
            P.post("brief", webhook="https://example.invalid")


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("private detail"), ValueError("bad CSV")])
def test_treasury_primary_failure_uses_fred(monkeypatch, failure):
    monkeypatch.setattr(G, "_treasury_curve_rows", Mock(side_effect=failure))
    monkeypatch.setattr(G, "_fred_yields", lambda: {"10Y": [("2026-11-16", 4.2),
                                                               ("2026-11-13", 4.1)]})
    result = G.treasury_yields(TODAY)
    assert result["source"] == "FRED (DGS series)"
    assert result["tenors"]["10Y"]["last"] == 4.2
    assert result["last_date"] == "2026-11-16"
    assert result["failures"][0][0] == "US Treasury par yield curve"
    assert "private detail" not in str(result)


def test_exhausted_vol_chain_renders_every_source_failure(monkeypatch, block):
    monkeypatch.setattr(G, "_cnbc_yields", dict)
    monkeypatch.setattr(G, "_yahoo_chart_yields", dict)
    monkeypatch.setattr(G, "_treasury_curve_rows", lambda _: [])
    monkeypatch.setattr(G, "_fred_yields", dict)
    response = httpx.Response(503, request=httpx.Request("GET", "https://example.invalid"))
    monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    monkeypatch.setattr(G, "_cnbc_vol", Mock(side_effect=httpx.ReadTimeout("private detail")))
    monkeypatch.setattr(G, "_yahoo_vol", Mock(side_effect=ValueError("private parse detail")))
    monkeypatch.setattr(G, "pull_daily", lambda _: {"error": "empty"})
    block["macro_pulse"] = G.macro_pulse(TODAY)
    block["source_crosscheck"] = G.source_crosscheck()
    text = G.render_md(block)
    for marker in ["^VIX: UNAVAILABLE", "^VIX9D: UNAVAILABLE", "CNBC volatility",
                   "timeout", "Yahoo volatility", "ValueError", "yfinance ^VIX",
                   "empty", "Cboe VIX", "HTTP 503", "TERM STRUCTURE:",
                   "VOL CURVE: UNAVAILABLE", "Treasury yields: UNAVAILABLE"]:
        assert marker in text
    assert "private detail" not in text and "private parse detail" not in text


def test_fred_failure_reaches_yfinance(monkeypatch):
    monkeypatch.setattr(G, "_treasury_curve_rows", lambda _: [])
    monkeypatch.setattr(G, "_fred_yields", Mock(side_effect=httpx.ReadTimeout("private")))
    monkeypatch.setattr(G, "pull_daily", lambda _: {"last": 4.2, "last_date": "2026-11-16"})
    result = G.treasury_yields(TODAY)
    assert result["source"].startswith("yfinance")
    assert set(result["tenors"]) == {"5Y", "10Y", "30Y"}
    assert result["failures"][1][0] == "FRED (DGS series)"


def test_january_prior_year_error_keeps_current_row(monkeypatch):
    monkeypatch.setattr(G, "_treasury_curve_rows", Mock(side_effect=[
        [(dt.date(2026, 1, 2), {"10Y": 4.2})], httpx.ReadTimeout("private")]))
    result = G.treasury_yields(dt.date(2026, 1, 5))
    assert result["tenors"]["10Y"]["last"] == 4.2
    assert result["tenors"]["10Y"]["prev"] is None
    assert result["failures"][0][0] == "Treasury prior year"


def test_live_yield_fallback_retains_source_date(monkeypatch):
    monkeypatch.setattr(G, "_cnbc_yields", Mock(side_effect=httpx.ReadTimeout("private")))
    monkeypatch.setattr(G, "_yahoo_chart_yields", lambda: {
        "10Y": {"last": 4.2, "chg": 0.1, "as_of": "2026-11-16T20:00:00+00:00"}})
    result = G.live_yields(TODAY)
    assert result["last_date"] == "2026-11-16"
    assert result["stale"] is True
    assert result["source"].startswith("Yahoo")


def test_cboe_failure_uses_relayed_term_structure(monkeypatch, block):
    monkeypatch.setattr(G, "live_yields", lambda _: {})
    monkeypatch.setattr(G, "treasury_yields", lambda _: {})
    monkeypatch.setattr(G, "_cnbc_vol", lambda: {
        "^VIX": {"last": 20.0, "chg": 1.0, "last_date": str(TODAY), "source": "CNBC"},
        "^VIX9D": {"last": 19.0, "chg": 1.0, "last_date": str(TODAY), "source": "CNBC"}})
    monkeypatch.setattr(G, "_cboe_index", Mock(side_effect=httpx.ReadTimeout("private")))
    block["macro_pulse"] = G.macro_pulse(TODAY)
    assert block["macro_pulse"]["term_structure"]["spread_9d_minus_30d"] == -1.0
    text = G.render_md(block)
    assert "Dated relayed/daily quotes" in text
    assert "VOL CURVE: UNAVAILABLE" in text


def test_cboe_settled_fallback_is_not_presented_as_live(monkeypatch, block):
    monkeypatch.setattr(G, "live_yields", lambda _: {})
    monkeypatch.setattr(G, "treasury_yields", lambda _: {})
    monkeypatch.setattr(G, "_cnbc_vol", dict)
    monkeypatch.setattr(G, "_yahoo_vol", dict)
    monkeypatch.setattr(G, "pull_daily", lambda _: {"error": "empty"})
    monkeypatch.setattr(G, "_cboe_index", lambda _: [("2026-11-16", 20.0)])
    block["macro_pulse"] = G.macro_pulse(TODAY)
    assert block["macro_pulse"]["^VIX"]["mode"] == "EOD"
    text = G.render_md(block)
    assert "EOD) STALE / not current" in text
    assert "has since traded" not in text


@pytest.mark.parametrize("operation,source", [
    (lambda: G.treasury_yields(TODAY), "_treasury_curve_rows"),
    (lambda: G.live_yields(TODAY), "_cnbc_yields"),
    (lambda: G.macro_pulse(TODAY), "_cnbc_yields"),
    (G.cboe_vol_official, "_cboe_index"),
    (G.source_crosscheck, "_cnbc_yields"),
])
@pytest.mark.parametrize("failure", [RuntimeError("unexpected"), AssertionError("unexpected")])
def test_chain_unexpected_exception_still_propagates(monkeypatch, operation, source, failure):
    monkeypatch.setattr(G, source, Mock(side_effect=failure))
    with pytest.raises(type(failure), match="unexpected"):
        operation()


def test_treasury_http_503_reaches_fred(monkeypatch):
    response = httpx.Response(503, request=httpx.Request("GET", "https://example.invalid"))
    monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    monkeypatch.setattr(G, "_fred_yields", lambda: {"10Y": [("2026-11-16", 4.2)]})
    result = G.treasury_yields(TODAY)
    assert result["source"] == "FRED (DGS series)"
    assert "HTTP 503" in result["failures"][0][1]


@pytest.mark.parametrize("stamp", [None, True, "yesterday", float("nan")])
def test_missing_yahoo_time_is_not_replaced_by_host_clock(stamp):
    with pytest.raises(ValueError, match="timestamp missing or invalid"):
        G._yahoo_time({"regularMarketTime": stamp})


@pytest.mark.parametrize("operation", [G._yahoo_chart_yields, G._yahoo_vol])
def test_yahoo_quote_uses_source_timestamp_in_exchange_time(monkeypatch, operation):
    stamp = G.pd.Timestamp("2026-11-17T01:00:00Z").timestamp()
    response = httpx.Response(200, json={"chart": {"result": [{
        "meta": {"regularMarketPrice": 20.0, "regularMarketTime": stamp},
    }]}}, request=httpx.Request("GET", "https://example.invalid"))
    monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    result = operation()
    for value in result.values():
        assert (value.get("as_of") or value["last_date"]).startswith("2026-11-16")


def test_partial_vol_primary_uses_yahoo_for_missing_leg(monkeypatch):
    monkeypatch.setattr(G, "live_yields", lambda _: {})
    monkeypatch.setattr(G, "treasury_yields", lambda _: {})
    monkeypatch.setattr(G, "_cnbc_vol", lambda: {
        "^VIX": {"last": 20.0, "last_date": str(TODAY), "source": "CNBC"}})
    monkeypatch.setattr(G, "_yahoo_vol", lambda: {
        "^VIX9D": {"last": 19.0, "last_date": str(TODAY), "source": "Yahoo"}})
    monkeypatch.setattr(G, "_cboe_index", lambda _: [])
    result = G.macro_pulse(TODAY)
    assert result["^VIX"]["source"] == "CNBC"
    assert result["^VIX9D"]["source"] == "Yahoo"


def test_yfinance_transport_failure_reaches_cboe(monkeypatch):
    monkeypatch.setattr(G, "live_yields", lambda _: {})
    monkeypatch.setattr(G, "treasury_yields", lambda _: {})
    monkeypatch.setattr(G, "_cnbc_vol", dict)
    monkeypatch.setattr(G, "_yahoo_vol", dict)
    monkeypatch.setattr(G, "pull_daily", Mock(side_effect=G.YahooRequestError("private detail")))
    monkeypatch.setattr(G, "_cboe_index", lambda _: [("2026-11-16", 20.0)])
    result = G.macro_pulse(TODAY)
    assert result["^VIX"]["mode"] == "EOD"
    assert result["^VIX"]["last_date"] == "2026-11-16"
    assert "private detail" not in str(result)


@pytest.mark.parametrize("date", [None, "2026-11-13"])
def test_term_structure_cannot_guess_matching_previous_close(date):
    result = G._vol_term_structure(
        {"last": 20.0, "chg": 1.0, "last_date": str(TODAY)},
        {"last": 19.0, "last_date": date})
    assert result is not None
    assert result["aligned"] is False
    assert result.get("spread_9d_minus_30d") is None
