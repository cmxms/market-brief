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
def test_required_source_failure_stops_gather(monkeypatch, operation, label, failure):
    monkeypatch.setattr(G, "_fred_key", lambda: "synthetic")
    if failure == "timeout":
        monkeypatch.setattr(httpx, "get", Mock(side_effect=httpx.ReadTimeout("synthetic request detail")))
    else:
        response = httpx.Response(503, request=httpx.Request("GET", "https://example.invalid"))
        monkeypatch.setattr(httpx, "get", Mock(return_value=response))
    with pytest.raises(RuntimeError) as caught:
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
    assert R.main(["--date", str(TODAY), "--ignore-window"]) == 3
    assert "gather failed" in (tmp_path / f"{TODAY}.log").read_text(encoding="utf-8")
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
