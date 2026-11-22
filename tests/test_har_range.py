"""Synthetic/offline HAR regressions; no external data, writer or delivery."""
import datetime as dt
import hashlib
import inspect
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_compose_env as environment
from test_compose_env import BODY, DATE, outputs

import gather as G
import har_range as H

composer = environment.composer


@pytest.fixture(scope="module")
def history():
    days = [d.date() for d in pd.date_range("2022-01-01", "2026-01-01")
            if H._eligible(d.date())][:430]
    rng = np.random.default_rng(421)
    spans = np.exp(6 + rng.normal(0, 0.25, len(days)))
    parts = []
    for day, span in zip(days, spans):
        start = pd.Timestamp(dt.datetime.combine(day - dt.timedelta(days=1), dt.time(18)), tz=H.ET)
        times = start + pd.to_timedelta(np.arange(4, 1380, 5), unit="m")
        parts.append(pd.DataFrame({"ts_event": times, "open": 10000., "high": 10000. + span,
                                   "low": 10000., "close": 10000., "adj_offset": 0.,
                                   "symbol": "NQ=F", "source": "vendor"}))
    target = days[-1] + dt.timedelta(days=1)
    while not H._eligible(target):
        target += dt.timedelta(days=1)
    return pd.concat(parts, ignore_index=True), days, spans, target


@pytest.fixture
def source(history, monkeypatch, tmp_path):
    bars, _days, _spans, target = history
    path = tmp_path / "synthetic.parquet"
    read = Mock(return_value=bars)
    monkeypatch.setattr(H.pd, "read_parquet", read)
    return path, read, target


def test_reproducible_and_sane_band(source):
    path, _, target = source
    first = H.forecast_range(path, target)
    assert first == H.forecast_range(path, target)
    assert first.as_of < first.target_session == target
    assert first.sample_size == 400
    assert first.percentile is None  # fewer than 100 earlier rolling forecasts
    assert 0 < first.band80_low < first.range_points < first.band80_high
    assert 5 < first.band80_high - first.band80_low < 2000


@pytest.mark.parametrize("offset", [0, 1, 1380, 2880])
def test_target_and_future_prices_cannot_change_result(source, history, offset):
    path, read, target = source
    original = H.forecast_range(path, target)
    bars = history[0]
    cutoff = pd.Timestamp(dt.datetime.combine(target - dt.timedelta(days=1), dt.time(18)), tz=H.ET)
    future = bars.iloc[-1:].copy()
    future["ts_event"] = cutoff + pd.Timedelta(minutes=offset)
    future["high"] = float("nan")
    future["low"] = -1e20
    future["symbol"] = "NOT-NQ"
    # Even ordering after the boundary is irrelevant.
    read.return_value = pd.concat([future, bars, future], ignore_index=True)
    assert H.forecast_range(path, target) == original


def test_incomplete_prior_session_is_rejected_even_with_old_override(source, history, monkeypatch):
    path, read, target = source
    bars = history[0]
    last = H._daily_ranges(bars, target).index[-1]
    read.return_value = bars.loc[bars.ts_event < pd.Timestamp(f"{last} 12:00", tz=H.ET)]
    monkeypatch.setenv("MARKET_BRIEF_HAR_MAX_STALE_SESSIONS", "1")
    with pytest.raises(H.HarRangeUnavailable, match="stale.*1 eligible sessions missing"):
        H.forecast_range(path, target)


def test_short_window_never_falls_back(source, history):
    path, read, target = source
    bars, days, _, _ = history
    read.return_value = bars.loc[bars.ts_event >= pd.Timestamp(f"{days[10]} 18:00", tz=H.ET)]
    with pytest.raises(H.HarRangeUnavailable, match="fewer than 400"):
        H.forecast_range(path, target)


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError, ValueError, ImportError])
def test_missing_unreadable_or_engine_failure(source, failure):
    path, read, target = source
    read.side_effect = failure("private path or sensitive diagnostic must not leak")
    with pytest.raises(H.HarRangeUnavailable) as exc:
        H.forecast_range(path, target)
    assert "private" not in str(exc.value)
    assert "missing or unreadable" in str(exc.value) or "engine" in str(exc.value)


@pytest.mark.parametrize("path", [None, "", "relative/data.parquet"])
def test_path_configuration_required(path):
    with pytest.raises(H.HarRangeUnavailable, match="path"):
        H.forecast_range(path, dt.date(2026, 11, 17))


def test_stale_allowance_api_is_removed():
    assert "max_stale_sessions" not in inspect.signature(H.forecast_range).parameters


@pytest.mark.parametrize("defect", ["naive", "duplicate", "order", "missing_column"])
def test_malformed_history_is_soft_failure(source, history, defect):
    path, read, target = source
    bars = history[0].copy()
    if defect == "naive":
        bars.ts_event = bars.ts_event.dt.tz_localize(None)
    elif defect == "duplicate":
        bars = pd.concat([bars.iloc[:1], bars], ignore_index=True)
    elif defect == "order":
        bars = bars.iloc[::-1]
    else:
        bars = bars.drop(columns="symbol")
    read.return_value = bars
    with pytest.raises(H.HarRangeUnavailable):
        H.forecast_range(path, target)


@pytest.mark.parametrize("column,value", [("symbol", "ES=F"), ("adj_offset", 1.),
                                          ("high", float("nan")), ("source", "different")])
def test_invalid_or_identity_seam_session_is_excluded(history, column, value):
    bars, days, _, target = history
    bad = bars.copy()
    bad.loc[bad.index[-1], column] = value
    assert H._daily_ranges(bad, target).index[-1] == days[-2]


def test_fit_matches_independent_corrected_formula(history):
    values = history[2]
    n = len(values)
    features = np.array([[1, np.log(values[i-1]), np.log(np.mean(values[i-5:i])),
                          np.log(np.mean(values[i-22:i]))] for i in range(22, n + 1)])
    x = features[-401:-1]
    y = np.log(values[-400:])
    beta = np.linalg.solve(x.T @ x, x.T @ y)
    errors = y - x @ beta
    shift = sum(errors ** 2) / 396 / 2
    mu = features[-1] @ beta
    point, low, high = H._estimate(values, n)
    assert point == pytest.approx(np.exp(mu + shift))
    assert [low, high] == pytest.approx(np.exp(mu + np.quantile(errors, [.1, .9])))
    assert point > np.exp(mu)


@pytest.mark.parametrize("slope", [.01, -.01])
def test_extrapolation_preserves_model_and_residual_band(source, history, monkeypatch, slope):
    # Persistent, varying synthetic ranges force both upper and lower extrapolation.
    values = np.exp(3 + slope * np.arange(430) + np.random.default_rng(8).normal(0, .001, 430))
    days = history[1]
    monkeypatch.setattr(H, "_daily_ranges", lambda *args: pd.Series(values, index=days))
    n = len(values)
    features = np.array([[1, np.log(values[i-1]), np.log(values[i-5:i].mean()),
                          np.log(values[i-22:i].mean())] for i in range(22, n + 1)])
    x, y = features[-401:-1], np.log(values[-400:])
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    residuals = y - x @ beta
    mu = features[-1] @ beta
    expected = np.exp(mu + (residuals @ residuals) / 396 / 2)
    bounds = np.exp(mu + np.quantile(residuals, [.1, .9]))
    assert expected > values[-400:].max() if slope > 0 else expected < values[-400:].min()
    result = H.forecast_range(source[0], source[2])
    assert result.range_points == pytest.approx(expected)
    assert [result.band80_low, result.band80_high] == pytest.approx(bounds)
    assert result.extrapolated is True
    assert G.har_range_line(asdict(result)).endswith(" — outside training range")


def test_exact_tie_survives_forecast_to_render(source, monkeypatch):
    monkeypatch.setattr(H, "_estimate", lambda *args: (792.5, 442.5, 1207.5))
    estimate = H.forecast_range(source[0], source[2])
    assert estimate.range_points == 792.5
    assert G.har_range_line(asdict(estimate)) == (
        "Volatility (HAR, NQ): ~795 pts expected range (10–90: 445–1210)")


def test_real_parquet_engine_round_trip(history, tmp_path):
    bars, _, _, target = history
    path = tmp_path / "synthetic.parquet"
    bars.to_parquet(path, engine="pyarrow")
    result = H.forecast_range(path, target)
    assert result.sample_size == 400
    assert result.range_points > 0
    assert result.extrapolated is False


def test_calendar_and_session_boundary(history):
    bars, days, _, target = history
    assert not H._eligible(dt.date(2026, 11, 27))
    assert not H._eligible(dt.date(2026, 12, 24))
    daily = H._daily_ranges(bars, target)
    assert list(daily.index) == days
    assert daily.to_numpy() == pytest.approx(history[2])


@pytest.mark.parametrize("point,low,high,expected", [
    (794, 446, 1208, "~795 pts expected range (10–90: 445–1210)"),
    (792, 442.5, 1207.5, "~790 pts expected range (10–90: 445–1210)"),
])
def test_rounding(point, low, high, expected):
    assert G.har_range_line({"range_points": point, "band80_low": low, "band80_high": high}) == "Volatility (HAR, NQ): " + expected


def synthetic_block():
    return {"date": "2026-11-17", "as_of": "2026-11-17T06:00:00-05:00",
            "event_gate": {}, "index_recap": {}, "macro_pulse": {}, "gex": {}, "finnhub": {}}


@pytest.mark.parametrize("flag", [None, "0", "true"])
def test_flag_off_render_is_byte_identical_to_original(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("MARKET_BRIEF_HAR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MARKET_BRIEF_HAR_ENABLED", flag)
    # Captured from the pre-change gather.py on the same synthetic input.
    expected_sha = "373e8e9e48e01baef1b1a797c66dc9fadbb9c75ee875b00d07ea955abd045d17"
    block = synthetic_block()
    assert hashlib.sha256(G.render_md(block).encode()).hexdigest() == expected_sha
    block["har_range"] = {"omitted": "stale"}  # retained enabled block is also hidden
    assert hashlib.sha256(G.render_md(block).encode()).hexdigest() == expected_sha


@pytest.mark.parametrize("reason", ["NQ parquet path is not configured", "NQ parquet is missing or unreadable (PermissionError)",
                                    "NQ parquet is stale (1 eligible sessions missing)",
                                    "fewer than 400 fit observations after 22-session warm-up"])
def test_gather_soft_failures_render_one_reason(monkeypatch, reason):
    monkeypatch.setenv("MARKET_BRIEF_HAR_ENABLED", "1")

    for name in ("event_gate", "index_recap", "macro_pulse", "sector_rotation", "gex_tradier", "finnhub_news", "source_crosscheck"):
        monkeypatch.setattr(G, name, lambda *a, **kw: {})
    monkeypatch.setattr(H, "forecast_range", Mock(side_effect=H.HarRangeUnavailable(reason)))
    block = G.gather(dt.date(2026, 11, 17))
    text = G.render_md(block)
    assert block["har_range"] == {"omitted": reason}
    assert text.count("Volatility (HAR, NQ): omitted — " + reason) == 1
    assert "pts expected range" not in text


def test_gather_off_does_not_read_parquet(monkeypatch):
    monkeypatch.delenv("MARKET_BRIEF_HAR_ENABLED", raising=False)
    for name in ("event_gate", "index_recap", "macro_pulse", "sector_rotation", "gex_tradier", "finnhub_news", "source_crosscheck"):
        monkeypatch.setattr(G, name, lambda *a, **kw: {})
    forecast = Mock(side_effect=AssertionError("flag-off must not open data"))
    monkeypatch.setattr(H, "forecast_range", forecast)
    assert "har_range" not in G.gather(dt.date(2026, 11, 17))
    forecast.assert_not_called()


def test_enabled_gather_retains_structure_and_configuration(monkeypatch, source):
    path, _, target = source
    monkeypatch.setenv("MARKET_BRIEF_HAR_ENABLED", "1")
    monkeypatch.setenv("MARKET_BRIEF_NQ_PARQUET", str(path))

    for name in ("event_gate", "index_recap", "macro_pulse", "sector_rotation", "gex_tradier", "finnhub_news", "source_crosscheck"):
        monkeypatch.setattr(G, name, lambda *a, **kw: {})
    block = G.gather(target)
    estimate = block["har_range"]
    assert estimate["sample_size"] == 400
    assert estimate["target_session"] == target
    assert isinstance(estimate["range_points"], float)
    assert estimate["extrapolated"] is False
    assert G.har_range_line(estimate) in G.render_md(block)


@pytest.mark.parametrize("old_override", ["1", "999", "not-a-number"])
def test_removed_age_override_cannot_publish_stale_data(monkeypatch, source, history, old_override):
    path, read, target = source
    bars, days, _, _ = history
    last_open = pd.Timestamp(dt.datetime.combine(days[-1] - dt.timedelta(days=1), dt.time(18)), tz=H.ET)
    read.return_value = bars.loc[bars.ts_event < last_open]
    monkeypatch.setenv("MARKET_BRIEF_HAR_ENABLED", "1")
    monkeypatch.setenv("MARKET_BRIEF_NQ_PARQUET", str(path))
    monkeypatch.setenv("MARKET_BRIEF_HAR_MAX_STALE_SESSIONS", old_override)
    for name in ("event_gate", "index_recap", "macro_pulse", "sector_rotation", "gex_tradier", "finnhub_news", "source_crosscheck"):
        monkeypatch.setattr(G, name, lambda *a, **kw: {})
    block = G.gather(target)
    assert block["har_range"] == {"omitted": "NQ parquet is stale (1 eligible sessions missing)"}
    assert "pts expected range" not in G.render_md(block)


def test_singular_fit_is_disclosed(source, monkeypatch):
    dates = [day.date() for day in pd.bdate_range(end="2026-11-16", periods=430)]
    monkeypatch.setattr(H, "_daily_ranges", lambda *args: pd.Series(500., index=dates))
    with pytest.raises(H.HarRangeUnavailable, match="singular"):
        H.forecast_range(source[0], dt.date(2026, 11, 17))


@pytest.mark.parametrize("target", [dt.date(2026, 11, 21), dt.date(2026, 11, 26)])
def test_non_session_target_is_omitted(target):
    with pytest.raises(H.HarRangeUnavailable, match="not an eligible"):
        H.forecast_range(None, target)


def test_flag_off_compose_preserves_exact_writer_bytes(composer):
    run, process, out, _ = composer
    expected = []
    def writer(cmd, **kwargs):
        candidate, claims, marker = outputs(cmd)
        content = (BODY + "\n" + marker).replace("\n", "\r\n").encode()
        expected.append(content)
        candidate.write_bytes(content)
        claims.write_bytes(b"[]")
        return Mock(returncode=0, stdout="", stderr="")
    process.side_effect = writer
    assert run()[0]
    assert out.read_bytes() == expected[0]


def test_flag_off_cannot_reemit_retained_har(composer):
    run, process, out, _ = composer
    line = "Volatility (HAR, NQ): ~795 pts expected range (10–90: 445–1210)"
    (out.parent / "data.md").write_text("- " + line + "\n", encoding="utf-8")
    def writer(cmd, **kwargs):
        candidate, claims, marker = outputs(cmd)
        snapshot = Path(cmd[3]).parent / "data.md"
        assert "Volatility (HAR, NQ)" not in snapshot.read_text(encoding="utf-8")
        candidate.write_text(BODY + "\n" + line + "\n" + marker, encoding="utf-8")
        claims.write_text("[]", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")
    process.side_effect = writer
    assert run()[0]
    assert "Volatility (HAR, NQ)" not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("line", ["Volatility (HAR, NQ): ~795 pts expected range (10–90: 445–1210)",
                                  "Volatility (HAR, NQ): ~1480 pts expected range (10–90: 1480–1485) — outside training range",
                                  "Volatility (HAR, NQ): omitted — NQ parquet is stale (1 eligible sessions missing)"])
def test_compose_inserts_exact_line_before_validation(composer, monkeypatch, line):
    run, process, out, _ = composer
    monkeypatch.setenv("MARKET_BRIEF_HAR_ENABLED", "1")
    data = out.parent / "data.md"
    data.write_text(f"# BRIEF DATA BLOCK — {DATE}\n- {line}\n", encoding="utf-8")
    def writer(cmd, **kwargs):
        candidate, claims, marker = outputs(cmd)
        candidate.write_text(BODY + "\n" + marker, encoding="utf-8")
        claims.write_text("[]", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")
    process.side_effect = writer
    ok, detail = run()
    assert ok, detail
    brief = out.read_text(encoding="utf-8")
    assert brief.count(line) == 1
    assert brief.index("## 📈") < brief.index(line) < brief.index("## 🚨")
    prompt_path = Path(process.call_args.args[0][3])
    assert "parent inserts it" in prompt_path.read_text(encoding="utf-8")


def test_percentile_is_causal_and_requires_100_prior_values(source, monkeypatch):
    rng = np.random.default_rng(9)
    values = np.exp(rng.normal(6, .2, 550))
    dates = [day.date() for day in pd.bdate_range(end="2026-11-16", periods=550)]
    target = dt.date(2026, 11, 17)
    monkeypatch.setattr(H, "_daily_ranges", lambda *args: pd.Series(values, index=dates))
    result = H.forecast_range(source[0], target)
    point = H._estimate(values, 550)[0]
    past = [H._estimate(values, i)[0] for i in range(422, 550)
            if dates[i] >= target - dt.timedelta(days=365)]
    assert result.percentile == 100 * sum(p <= point for p in past) / len(past)
    changed = values.copy()
    changed[549] = 1e20
    assert H._estimate(changed, 549) == H._estimate(values, 549)
