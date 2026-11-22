"""Read-only, causal NQ session-range estimate. No network or persisted model.

Prices are continuous NQ/CME index points (USD quoted), not dollars of P&L.
Expected input: start-stamped, timezone-aware one-minute OHLC bars with symbol,
source and additive adj_offset provenance. Optional parquet engine is lazy-loaded.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

ET = "America/New_York"
WINDOW = 400


class HarRangeUnavailable(ValueError):
    """Expected input/fit failure with a public-safe, one-line reason."""


@dataclass(frozen=True)
class HarRangeForecast:
    as_of: dt.date
    target_session: dt.date
    range_points: float
    band80_low: float
    band80_high: float
    percentile: float | None
    sample_size: int
    extrapolated: bool


def _excluded_dates(year: int) -> set[dt.date]:
    # Shared calendar, plus conservative short-session exclusions used in research.
    # Import lazily: gather imports this module only on the enabled path.
    from gather import _market_holidays

    holidays = _market_holidays(year)
    if year < 2022:
        holidays.discard(dt.date(year, 6, 19))
        observed = dt.date(year, 6, 19)
        if observed.weekday() == 5:
            holidays.discard(observed - dt.timedelta(days=1))
        elif observed.weekday() == 6:
            holidays.discard(observed + dt.timedelta(days=1))
    thanksgiving = next(d for d in holidays if d.month == 11)
    return holidays | {thanksgiving + dt.timedelta(days=1),
                       dt.date(year, 7, 3), dt.date(year, 12, 24)}


def _eligible(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in _excluded_dates(day.year)


def _daily_ranges(bars: pd.DataFrame, target: dt.date) -> pd.Series:
    cutoff = pd.Timestamp(dt.datetime.combine(target - dt.timedelta(days=1), dt.time(18)), tz=ET)
    if "ts_event" not in bars:
        raise HarRangeUnavailable("parquet timestamp column missing")
    stamps = pd.DatetimeIndex(bars["ts_event"])
    if stamps.tz is None or stamps.hasnans:
        raise HarRangeUnavailable("timestamps must be timezone-aware and nonmissing")
    # Apply the causal boundary BEFORE examining any prices, identity or ordering.
    bars = bars.loc[stamps < cutoff].copy()
    stamps = stamps[stamps < cutoff].tz_convert(ET)
    required = ["open", "high", "low", "close", "symbol", "source", "adj_offset"]
    if any(c not in bars for c in required):
        raise HarRangeUnavailable("parquet price or provenance columns missing")
    if not stamps.is_monotonic_increasing or stamps.has_duplicates or any(stamps.second) or any(stamps.microsecond) or any(stamps.nanosecond):
        raise HarRangeUnavailable("timestamps must be ordered unique whole minutes")
    if bars.empty:
        raise HarRangeUnavailable("no completed sessions before target open")
    bars["session"] = (stamps.tz_localize(None).normalize()
                       + pd.to_timedelta((stamps.hour >= 18).astype(int), unit="D")).date
    bars["minute"] = (stamps.hour * 60 + stamps.minute - 1080) % 1440
    prices = bars[["open", "high", "low", "close", "adj_offset"]].astype(float)
    valid = np.isfinite(prices).all(axis=1)
    valid &= (prices.low > 0) & (prices.low - prices.adj_offset > 0)
    valid &= (prices.low <= prices[["open", "close"]].min(axis=1))
    valid &= (prices.high >= prices[["open", "close"]].max(axis=1))
    valid &= bars.symbol.notna() & bars.source.notna()
    valid &= bars.symbol.astype(str).str.match(r"^NQ(?:=F|[HMUZ]\d{1,4})$")
    bars["valid"] = valid
    # The same Yahoo instrument can arrive through vendor and recorder storage.
    source = bars.source.mask((bars.symbol == "NQ=F") & (prices.adj_offset == 0)
                              & bars.source.isin(["vendor", "state"]), "vendor_or_state")
    bars["identity"] = list(zip(bars.symbol, source, prices.adj_offset))
    rows = {}
    for day, group in bars.groupby("session", sort=True):
        if not _eligible(day) or day >= target:
            continue
        aligned = group.loc[group.minute % 5 == 4, "minute"].nunique()
        if (aligned < math.ceil(0.95 * 276) or (group.minute >= 1380).any()
                or not group.valid.all() or group.identity.nunique() != 1):
            continue
        span = float(group.high.max() - group.low.min())
        if math.isfinite(span) and span > 0:
            rows[day] = span
    return pd.Series(rows, dtype=float)


def _estimate(values: np.ndarray, end: int) -> tuple[float, float, float]:
    # Observation i predicts range i from strictly earlier eligible sessions.
    def features(i):
        return [1.0, np.log(values[i - 1]), np.log(values[i - 5:i].mean()),
                np.log(values[i - 22:i].mean())]

    x = np.array([features(i) for i in range(end - WINDOW, end)])
    y = np.log(values[end - WINDOW:end])
    beta, _, rank, _ = np.linalg.lstsq(x, y, rcond=None)
    if rank != 4:
        raise HarRangeUnavailable("range fit is singular")
    residuals = y - x @ beta
    variance = float(residuals @ residuals / (WINDOW - 4))
    mu = float(np.dot(features(end), beta))
    with np.errstate(over="raise", invalid="raise"):
        point = float(np.exp(mu + variance / 2))
        low, high = np.exp(mu + np.quantile(residuals, [0.1, 0.9]))
    if not (0 < low < high and np.isfinite([point, low, high]).all()):
        raise HarRangeUnavailable("range fit produced invalid bounds")
    return point, float(low), float(high)


def forecast_range(path: Path | str | None, target_session: dt.date) -> HarRangeForecast:
    """Return a corrected 400-observation HAR estimate or raise HarRangeUnavailable.

    No missing eligible sessions are allowed between as_of and target.
    No fallback value; the caller decides how to disclose omission. Percentile is
    against causal prior forecasts in 365 calendar days, with at least 100 needed.
    """

    if not _eligible(target_session):
        raise HarRangeUnavailable("target is not an eligible full session")
    if not path:
        raise HarRangeUnavailable("NQ parquet path is not configured")
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise HarRangeUnavailable("NQ parquet path must be absolute")
    try:
        # No installation is attempted. The default-off brief needs no engine.
        bars = pd.read_parquet(path)
    except ImportError:
        raise HarRangeUnavailable("parquet engine is not installed") from None
    except (OSError, ValueError) as exc:
        raise HarRangeUnavailable(f"NQ parquet is missing or unreadable ({type(exc).__name__})") from None
    try:
        ranges = _daily_ranges(bars, target_session)
        if ranges.empty:
            raise HarRangeUnavailable("no usable completed sessions")
        as_of = cast(dt.date, ranges.index[-1])
        day, behind = as_of + dt.timedelta(days=1), 0
        while day < target_session:
            behind += _eligible(day)
            day += dt.timedelta(days=1)
        if behind:
            raise HarRangeUnavailable(f"NQ parquet is stale ({behind} eligible sessions missing)")
        if len(ranges) < WINDOW + 22:
            raise HarRangeUnavailable("fewer than 400 fit observations after 22-session warm-up")
        values = ranges.to_numpy()
        point, low, high = _estimate(values, len(values))
        start = target_session - dt.timedelta(days=365)
        history = [_estimate(values, i)[0] for i in range(WINDOW + 22, len(values))
                   if ranges.index[i] >= start]
        percentile = (100.0 * sum(p <= point for p in history) / len(history)
                      if len(history) >= 100 else None)
        training = values[-WINDOW:]
        extrapolated = bool(point < training.min() or point > training.max())
        return HarRangeForecast(as_of, target_session, point, low, high,
                                percentile, WINDOW, extrapolated)
    except HarRangeUnavailable:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, FloatingPointError, np.linalg.LinAlgError):
        raise HarRangeUnavailable("NQ parquet data or range fit is invalid") from None
