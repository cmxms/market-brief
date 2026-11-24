"""Synthetic spot regressions ported without the quarantine redesign.

No model, vendor or delivery calls; all prices are synthetic fixtures.
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gather as G
import run_brief as R
import validate as V

NOW = pd.Timestamp("2026-11-23T08:35:00-05:00")


def bars(stamps, values, multi=False):
    frame = pd.DataFrame({"Close": values}, index=pd.DatetimeIndex(stamps))
    if multi:
        frame.columns = pd.MultiIndex.from_tuples([("Close", "SPY")])
    return frame


@pytest.mark.parametrize("multi", [False, True])
def test_premarket_current_session_timezone_and_shape(monkeypatch, multi):
    frame = bars(["2026-11-20T21:00:00Z", "2026-11-23T13:30:00Z",
                  "2026-11-23T14:30:00Z"], [100, 105, 110], multi)
    download = Mock(return_value=frame)
    monkeypatch.setattr(G.yf, "download", download)
    result = G._premarket_spot("SPY", NOW.date(), NOW)
    assert result["last"] == 105
    assert result["spot_time"] == "2026-11-23T08:30:00-05:00"
    assert download.call_args.kwargs == {"period": "2d", "interval": "1m", "prepost": True,
                                         "progress": False, "auto_adjust": False, "threads": False}


@pytest.mark.parametrize("frame", [None, pd.DataFrame(),
    bars(["2026-11-20T08:00:00-05:00"], [100]),
    bars(["2026-11-23T03:59:00-05:00"], [100]),
    bars(["2026-11-23T09:30:00-05:00"], [100]),
    bars(["2026-11-23T08:36:00-05:00"], [100]),
    bars(["2026-11-23T08:30:00"], [100]),
    bars(["2026-11-23T08:30:00-05:00"], [float("nan")]),
    bars(["2026-11-23T08:30:00-05:00"], [float("inf")]),
    bars(["2026-11-23T08:30:00-05:00"], [-1]),
    bars(["2026-11-23T08:30:00-05:00"], [0]),
])
def test_premarket_rejects_missing_stale_invalid_or_undated(monkeypatch, frame):
    monkeypatch.setattr(G.yf, "download", lambda *a, **kw: frame)
    with pytest.raises(G.SourceUnavailable, match="missing premarket print"):
        G._premarket_spot("SPY", NOW.date(), NOW)


@pytest.mark.parametrize("yahoo,tradier,expected", [
    (True, "2026-11-20T16:00:00-05:00", 105),
    (True, "2026-11-23T08:34:00-05:00", 100),
    (True, "2026-11-23T08:29:00-05:00", 105),
    (True, "2026-11-23T08:30:00-05:00", 105),
    (True, "2026-11-23T08:36:00-05:00", 105),
    (True, "2026-11-23T08:04:00-05:00", 105),
    (True, None, 105),
    (False, "2026-11-23T08:34:00-05:00", 100),
    (False, "2026-11-20T16:00:00-05:00", None),
    (False, None, None),
])
def test_newest_valid_spot_and_no_close_substitution(monkeypatch, yahoo, tradier, expected):
    monkeypatch.setattr(G.pd.Timestamp, "now", lambda **kw: NOW)
    quote = {"last": 100, "close": 99,
             "trade_date": int(pd.Timestamp(tradier).timestamp() * 1000) if tradier else None}
    monkeypatch.setattr(G, "_quote", lambda s: quote)
    monkeypatch.setattr(G.yf, "download", lambda *a, **kw: (
        bars(["2026-11-23T08:30:00-05:00"], [105]) if yahoo else pd.DataFrame()))
    monkeypatch.setattr(G, "_expirations", lambda s: ["2026-11-27"])
    monkeypatch.setattr(G, "_chains_parallel", lambda *a: ({"2026-11-27": []}, []))
    compute = Mock(return_value={"regime": "negative"})
    monkeypatch.setattr(G, "_gex_from_options", compute)
    result = G.gex_tradier(NOW.date())
    for value in result.values():
        assert value["spot"] == expected
        assert value["chain_spot"] == 100
        assert value["chain_spot_source"] == "Tradier last"
        assert value["chain_spot_time"] == tradier
        assert value["spot_source"] == ("yfinance 1m prepost" if expected == 105 else
                                         "Tradier last" if expected == 100 else None)
        assert value["spot_time"] == ("2026-11-23T08:30:00-05:00" if expected == 105 else
                                       tradier if expected == 100 else None)
    assert all(call.args[1] == 100 for call in compute.call_args_list)
    warning = R._data_warning({"gex": result}, NOW)
    assert bool(warning) == (expected is None)
    if warning:
        assert "missing premarket print" in warning


def test_close_only_chain_cannot_supply_comparison(monkeypatch):
    monkeypatch.setattr(G.pd.Timestamp, "now", lambda **kw: NOW)
    monkeypatch.setattr(G, "_quote", lambda s: {
        "close": 99, "trade_date": int(NOW.timestamp() * 1000)})
    monkeypatch.setattr(G.yf, "download", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr(G, "_expirations", lambda s: ["2026-11-27"])
    monkeypatch.setattr(G, "_chains_parallel", lambda *a: ({"2026-11-27": []}, []))
    monkeypatch.setattr(G, "_gex_from_options", lambda *a, **kw: {"regime": "negative"})
    result = G.gex_tradier(NOW.date())
    for quote in result.values():
        assert quote["spot"] is None
        assert quote["spot_time"] is None and quote["spot_source"] is None
        assert quote["chain_spot"] == 99 and quote["chain_spot_source"] == "Tradier close"
    warning = R._data_warning({"gex": result}, NOW)
    assert warning and "missing premarket print" in warning


@pytest.mark.parametrize("stamp", [None, "2026-11-20T16:00:00-05:00",
                                     "2026-11-23T08:36:00-05:00", "2026-11-23T08:30:00"])
def test_retained_spot_is_gated_by_timestamp_not_block_stamp(stamp):
    quote = {"spot": 100, "spot_time": stamp,
             "near_term": {"regime": "negative"}, "full_chain": {"regime": "negative"}}
    payload = {"as_of": NOW.isoformat(), "gex": {s: quote for s in ("SPY", "QQQ")}}
    warning = R._data_warning(payload, NOW)
    assert warning and "missing premarket print" in warning


def test_retained_render_keeps_chain_diagnostic_not_regime(monkeypatch):
    monkeypatch.setattr(G.pd.Timestamp, "now", lambda **kw: NOW)
    quote = {"spot": 100, "spot_time": "2026-11-20T16:00:00-05:00",
             "near_term": {"regime": "negative"}, "full_chain": {"regime": "negative"}}
    block = {"date": "2026-11-23", "as_of": NOW.isoformat(), "event_gate": {},
             "index_recap": {}, "macro_pulse": {}, "finnhub": {},
             "gex": {s: quote for s in ("SPY", "QQQ")}}
    text = G.render_md(block)
    assert "chain spot: 100" in text and "2026-11-20T16:00:00-05:00" in text
    assert "missing premarket print" in text
    assert "regime **negative**" not in text


@pytest.mark.parametrize("source", ["close", "Tradier close"])
def test_close_cannot_masquerade_as_current_session_print(source):
    quote = {"spot": 100, "spot_time": "2026-11-23T08:30:00-05:00", "spot_source": source}
    assert V.gex_spot_reason(quote, NOW)


@pytest.mark.parametrize("stamp,source,valid", [
    ("2026-11-23T03:59:00-05:00", "Tradier last", False),
    ("2026-11-23T04:00:00-05:00", "yfinance 1m prepost", True),
    ("2026-11-23T09:29:00-05:00", "yfinance 1m prepost", True),
    ("2026-11-23T09:30:00-05:00", "yfinance 1m prepost", True),
    ("2026-11-23T09:30:00-05:00", "Tradier last", True),
    ("2026-11-23T19:59:00-05:00", "Tradier last", True),
    ("2026-11-23T20:00:00-05:00", "Tradier last", False),
    ("2026-11-20T19:59:00-05:00", "Tradier last", False),
    ("2026-11-23T13:30:00+00:00", "Tradier last", True),
])
def test_comparison_session_boundaries(stamp, source, valid):
    quote = {"spot": 100, "spot_time": stamp, "spot_source": source}
    # Isolate session boundaries from the separate age guard.
    now = max(pd.Timestamp(stamp), pd.Timestamp("2026-11-23T04:00:00-05:00"))
    assert (V.gex_spot_reason(quote, now) is None) == valid


@pytest.mark.parametrize("price", [None, True, 0, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_comparison_price_is_withheld(price):
    assert V.gex_spot_reason({"spot": price, "spot_time": NOW.isoformat()}, NOW)


@pytest.mark.parametrize("multi", [False, True])
def test_composition_after_open_selects_newest_print(monkeypatch, multi):
    now = pd.Timestamp("2026-11-23T10:00:00-05:00")
    # Deliberately unordered, with a future bar that must not be selected.
    frame = bars(["2026-11-23T09:59:00-05:00", "2026-11-23T09:14:00-05:00",
                  "2026-11-23T10:01:00-05:00"], [110, 105, 115], multi)
    monkeypatch.setattr(G.yf, "download", lambda *a, **kw: frame)
    result = G._premarket_spot("SPY", now.date(), now)
    assert result == {"last": 110, "spot_time": "2026-11-23T09:59:00-05:00",
                      "spot_source": "yfinance 1m prepost"}


@pytest.mark.parametrize("stamp,valid", [
    ("2026-11-23T07:35:00-05:00", False),
    ("2026-11-23T09:14:00-05:00", False),
    ("2026-11-23T09:29:59-05:00", False),
    ("2026-11-23T09:30:00-05:00", True),
    ("2026-11-23T09:59:00-05:00", True),
    ("2026-11-23T10:00:00-05:00", True),
    ("2026-11-23T10:00:01-05:00", False),
])
@pytest.mark.parametrize("source", ["yfinance 1m prepost", "Tradier last"])
def test_comparison_age_at_composition(monkeypatch, stamp, valid, source):
    now = pd.Timestamp("2026-11-23T10:00:00-05:00")
    quote = {"spot": 100, "spot_time": stamp, "spot_source": source,
             "near_term": {"regime": "negative"}, "full_chain": {"regime": "negative"}}
    reason = V.gex_spot_reason(quote, now)
    assert (reason is None) == valid
    payload = {"as_of": now.isoformat(), "gex": {s: quote for s in G.GEX_SYMBOLS}}
    assert bool(R._data_warning(payload, now)) == (not valid)
    if source == "yfinance 1m prepost":
        monkeypatch.setattr(G.yf, "download", lambda *a, **kw: bars([stamp], [100]))
        if valid:
            assert G._premarket_spot("SPY", now.date(), now)["spot_time"] == stamp
        else:
            with pytest.raises(G.SourceUnavailable, match="missing premarket print"):
                G._premarket_spot("SPY", now.date(), now)
