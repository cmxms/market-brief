"""market-brief data gatherer — the deterministic half of the daily market brief.

Pulls every *hard number* from reliable, cloud-reachable sources so the brief is
never built on the model's memory. It does NOT write the brief — it emits a
structured data block (JSON + a readable markdown dump). Claude then reads that
block, runs themed web searches for the narrative/catalysts, and writes the brief.

Division of labor (see playbook.md):
  gather.py  -> hard numbers + Finnhub headlines + event gate
  Claude     -> themed web search, "so what" synthesis, final brief + save

Sources, by freshness need:
  - Live (yfinance): index futures (ES/YM/NQ/RTY), oil (CL=F), and the macro
    pulse — yields (^TNX/^TYX/^FVX) + vol (^VIX/^VIX9D).
  - Live (Tradier chain): dealer gamma (GEX) for SPY + QQQ — we own the math.
  - Local: T1 event calendar (t1_calendar.yaml).
  - News (Finnhub): general wire + theme-filtered, ranked by source quality.

SELF-CONTAINED: no imports from sibling projects, no local database. Credentials
come from environment variables or a local .env file in this folder (see .env.example).

Run:
    .venv/Scripts/python.exe gather.py     # Windows
    .venv/bin/python gather.py             # macOS/Linux
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import yaml
import yfinance as yf
from curl_cffi.requests.exceptions import RequestException as YahooRequestError
from yfinance.exceptions import YFDataException, YFRateLimitError, YFTickerMissingError

# Windows consoles default to cp1252, which can't encode the ★/→/— glyphs in the
# rendered block -> print() would crash AFTER the files are already written.
# Force UTF-8 on the streams so the dump prints cleanly on any console.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --------------------------------------------------------------------------- #
# Paths (resolved relative to this file, so CWD doesn't matter)
# --------------------------------------------------------------------------- #
HERE = Path(__file__).resolve().parent
T1_CALENDAR = HERE / "t1_calendar.yaml"
DATA_DIR = HERE / "data"           # gather output (json + readable md)
BRIEFS_DIR = HERE / "briefs"       # where Claude saves the final brief


# --------------------------------------------------------------------------- #
# Env: local .env (for local runs) with real env vars (CI / a friend's box) winning
# --------------------------------------------------------------------------- #
def _load_local_env() -> None:
    f = HERE / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and not os.environ.get(k):
            os.environ[k] = v


_load_local_env()


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def _finnhub_key() -> str:
    return _env("FINNHUB_API_KEY")


def _fred_key() -> str:
    return _env("FRED_API_KEY")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
INDEX_FUTURES = {"ES": "ES=F", "YM": "YM=F", "NQ": "NQ=F", "RTY": "RTY=F"}
INDEX_LABEL = {"ES": "S&P 500", "YM": "Dow", "NQ": "Nasdaq-100", "RTY": "Russell 2000"}
OIL = ("CL=F", "WTI crude")

# GEX: dealer gamma for the two most-hedged index proxies.
GEX_SYMBOLS = ("SPY", "QQQ")
GEX_NEAR_EXPIRATIONS = 4   # nearest N expirations for the intraday regime read (matches the recorded baseline)
GEX_FLIP_SCAN_PCT = 0.10   # zero-gamma search window: re-price total GEX over spot ±10%
GEX_FLIP_GRID = 801        # grid points across that window (~0.025% of spot apart)
# A contract with no gamma or no open interest can't be used and gets skipped. That is
# normally fine — but if Tradier's greeks degrade, GEX shrinks toward zero and every field
# still looks plausible, which is what a real regime flip looks like. So: measure the skips
# and speak up past this share.
#
# ⚠️ This threshold applies to `dropped_oi_pct` — the share of OPEN INTEREST excluded — and
# never to the raw contract count. See `_gex_from_options` for the measurement that settled
# it: newly-listed strikes come back with no greeks and OI == 0, contribute exactly zero
# GEX, and pushed the count to 5.42% on 2026-08-06 while the OI measure sat at 0.0%. Gating
# on the count cost three straight mornings of gamma reads (08-04, 08-05, 08-06).
GEX_DROP_WARN_PCT = 2.0
#: The flip is only reported when the re-priced contracts carry at least this share of
#: the usable open interest. Below it the profile is describing a different book than
#: the total, and a flip drawn from a minority of the OI is worse than no flip.
GEX_FLIP_MIN_OI_SHARE = 0.98

# Treasury yields: source from the US Treasury's own Daily Par Yield Curve (the
# authoritative source — these ARE the constant-maturity rates FRED republishes a
# day later), with FRED then yfinance as fallbacks. Tradier does NOT quote the
# Treasury yield indices, so it is not in the chain.
YIELD_TENORS = ["2Y", "5Y", "10Y", "30Y"]
_TSY_COL = {"2Y": "2 Yr", "5Y": "5 Yr", "10Y": "10 Yr", "30Y": "30 Yr"}
_FRED_SID = {"2Y": "DGS2", "5Y": "DGS5", "10Y": "DGS10", "30Y": "DGS30"}
_YF_YIELD = {"5Y": "^FVX", "10Y": "^TNX", "30Y": "^TYX"}  # no 2Y proxy on yfinance

# Vol has TWO legs, and the split matters:
#   - CNBC/Yahoo for the LIVE pre-open tick. Cboe cannot do this (see below).
#   - Cboe's own CSVs for the OFFICIAL SETTLED close and the term structure.
# CNBC and Yahoo both redistribute Cboe's calculation, so cross-checking them against each
# other only confirms that two relayers agree. This is the same source chain already used for
# yields — issuer first (US Treasury par curve), live relay second.
VOL_TICKERS = {"^VIX": "VIX (30d)", "^VIX9D": "VIX9D (9d)"}

# Cboe publishes each index's full daily history as a keyless CSV on its own CDN.
# 🔴 These publish AFTER THE CLOSE, so pre-open the newest row is the PRIOR session. That is
# why the CNBC leg stays: Cboe is the official number, CNBC is the live one.
CBOE_INDEX_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv"
# Ordered by horizon — reading them together IS the term structure, no interpolation needed.
CBOE_INDICES = {"VIX9D": "VIX9D (9d)", "VIX": "VIX (30d)",
                "VIX3M": "VIX3M (3mo)", "VIX6M": "VIX6M (6mo)"}
# Sessions the VIX/VIX3M percentile is measured over. ~2 years: long enough for a full regime
# cycle, short enough that 2011's vol environment is not setting today's reference.
CBOE_LOOKBACK = 504

# Sector rotation: the 11 SPDR sector ETFs, ranked by performance RELATIVE to SPY
# (relative isolates rotation from the broad market move). THEME_ETFS add within-tech
# granularity (semis/software) + a growth/speculative-appetite gauge.
SECTOR_ETFS = {
    "XLK": "Technology", "XLC": "Communications", "XLY": "Consumer Disc",
    "XLP": "Consumer Staples", "XLE": "Energy", "XLF": "Financials",
    "XLV": "Health Care", "XLI": "Industrials", "XLB": "Materials",
    "XLRE": "Real Estate", "XLU": "Utilities",
}
THEME_ETFS = {"SMH": "Semis", "IGV": "Software", "ARKK": "Growth/spec"}
ROTATION_BENCH = "SPY"

# Finnhub: sources we trust for signal; everything else is deprioritized.
TRUSTED_SOURCES = {
    "reuters", "bloomberg", "cnbc", "the wall street journal", "wsj",
    "financial times", "ft", "associated press", "ap", "barrons", "barron's",
    "marketwatch", "the new york times", "nyt", "axios", "the washington post",
    "the economist", "politico",
}
# Themes the brief cares about — used to tag/filter headlines (macro searches
# alone miss sector catalysts).
THEME_KEYWORDS = [
    "ai", "artificial intelligence", "nvidia", "nvda", "chip", "semiconductor",
    "broadcom", "avgo", "fed", "rate", "cpi", "inflation", "powell", "jobs",
    "payroll", "oil", "crude", "iran", "hormuz", "tariff", "tech", "nasdaq",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class SourceUnavailable(RuntimeError):
    """An expected source failure, safe to report and eligible for fallback."""


def _failure_reason(exc: Exception) -> str:
    # Never stringify transport exceptions: URLs can contain credentials.
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout ({type(exc).__name__})"
    if isinstance(exc, SourceUnavailable):
        return str(exc)
    return type(exc).__name__


def _attempt(source: str, fetch, failures: list):
    """Retain expected failures without hiding unrelated RuntimeErrors or assertions."""
    try:
        value = fetch()
    except (SourceUnavailable, httpx.HTTPError, YahooRequestError, YFDataException,
            YFRateLimitError, YFTickerMissingError, ValueError, KeyError, TypeError,
            IndexError, OverflowError, csv.Error) as exc:
        failures.append((source, _failure_reason(exc)))
        return {}
    if not value or (isinstance(value, dict) and value.get("error")):
        failures.append((source, value.get("error") if value else "empty"))
        return {}
    return value


def _unavailable(section: str, failures: list) -> str:
    return f"{section} unavailable: " + ", ".join(f"{s} ({r})" for s, r in failures)


def _yahoo_time(meta: dict) -> str:
    stamp = meta["regularMarketTime"]
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not np.isfinite(stamp):
        raise ValueError("Yahoo quote timestamp missing or invalid")
    return pd.Timestamp(stamp, unit="s", tz="UTC").tz_convert("America/New_York").isoformat()


def _close_series(h: pd.DataFrame) -> pd.Series:
    c = h["Close"]
    c = c.iloc[:, 0] if hasattr(c, "columns") else c
    return c.dropna()


# A continuous-contract move that is really a contract change. yfinance's ES=F/NQ=F
# swap front contracts silently: on 2026-09-15 the daily bar's prev_close was the
# September settle and its last was December, so ES printed +0.73% / NQ +0.9% on
# a session where the same contract moved -0.12% / -0.07%. Sep->Dec carry at 5%
# rates is ~1% -- an order of magnitude above a normal 16:00->17:00 settle drift,
# so a 0.4% disagreement between the two prior closes is unambiguous.
ROLL_TOL_PCT = 0.4


def _intraday_prior_close(ticker: str) -> tuple[float | None, float | None]:
    """(prior session's <=16:00 ET close, latest 1m close) from the 1-minute series,
    which yfinance serves from ONE contract across the window. Errors stop the run."""
    try:
        h = yf.download(ticker, period="5d", interval="1m", progress=False, auto_adjust=False)
        if h.empty:
            return None, None
        idx = h.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        h.index = idx.tz_convert("US/Eastern")
        c = _close_series(h)
        dates = sorted(set(c.index.date))
        for _d in reversed(dates[:-1]):
            prior = c[(c.index.date == _d) & (c.index.time <= dt.time(16, 0))]
            if len(prior):
                return float(prior.iloc[-1]), float(c.iloc[-1])
        return None, float(c.iloc[-1])
    except (ValueError, KeyError, IndexError) as exc:
        raise RuntimeError(f"Contract-roll verification unavailable: {type(exc).__name__}") from None


def roll_guard(last: float, prev_daily: float, prev_1m: float | None, last_1m: float | None,
               tol_pct: float = ROLL_TOL_PCT) -> dict | None:
    """Decide whether the daily close-to-close straddles a contract roll.

    Returns None when the daily numbers stand, else {"prev_close": <same-contract prior
    close>, "prev_close_daily": ..., "gap_pct": ...}. Requires the 1m series to be on the
    same contract as today's daily bar (last vs last_1m agree within tol) -- otherwise
    there is nothing trustworthy to substitute and the daily number is left alone."""
    if prev_1m is None or last_1m is None or not prev_daily or not last:
        return None
    if abs(last_1m - last) / last * 100 > tol_pct:
        return None
    gap_pct = abs(prev_1m - prev_daily) / prev_daily * 100
    if gap_pct <= tol_pct:
        return None
    return {"prev_close": prev_1m, "prev_close_daily": prev_daily, "gap_pct": round(gap_pct, 2)}


def pull_daily(ticker: str) -> dict:
    """Daily close + 1-day change + recent + the latest day's range."""
    try:
        h = yf.download(ticker, period="7d", interval="1d", progress=False, auto_adjust=False)
        if h.empty:
            return {"error": "no data"}
        c = _close_series(h)
        hi = h["High"]; lo = h["Low"]
        hi = hi.iloc[:, 0] if hasattr(hi, "columns") else hi
        lo = lo.iloc[:, 0] if hasattr(lo, "columns") else lo
        last, prev = float(c.iloc[-1]), (float(c.iloc[-2]) if len(c) > 1 else None)
        roll = None
        if prev and ticker.endswith("=F") and ticker in INDEX_FUTURES.values():
            prev_1m, last_1m = _intraday_prior_close(ticker)
            roll = roll_guard(last, prev, prev_1m, last_1m)
            if roll:
                prev = roll["prev_close"]
        out = {
            "last": round(last, 3),
            "prev_close": round(prev, 3) if prev else None,
            "chg": round(last - prev, 3) if prev else None,
            "chg_pct": round(100 * (last - prev) / prev, 2) if prev else None,
            "last_date": str(c.index[-1].date()),
            "last_range": [round(float(lo.iloc[-1]), 3), round(float(hi.iloc[-1]), 3)],
            "recent_closes": [round(float(x), 3) for x in c.iloc[-6:]],
        }
        if roll:
            out["roll_adjusted"] = {"prev_close_daily": round(roll["prev_close_daily"], 3),
                                    "gap_pct": roll["gap_pct"]}
        return out
    except (ValueError, KeyError, IndexError) as e:
        return {"error": f"daily prices unavailable: {type(e).__name__}"}


def pull_intraday_nq() -> dict:
    """Overnight/this-morning read for NQ: latest price, gap vs prior close,
    and overnight range. Best-effort; flags staleness if the market is shut.

    period="5d", not "2d": a 2-CALENDAR-day window collapses to a single session
    across a weekend (measured live on a Sunday — one date returned, not two), and
    one session means there is no prior close to gap against.
    """
    try:
        h = yf.download("NQ=F", period="5d", interval="1m", progress=False, auto_adjust=False)
        if h.empty:
            return {"error": "no data"}
        idx = h.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        h.index = idx.tz_convert("US/Eastern")
        c = _close_series(h)
        hi = h["High"]; lo = h["Low"]
        hi = hi.iloc[:, 0] if hasattr(hi, "columns") else hi
        lo = lo.iloc[:, 0] if hasattr(lo, "columns") else lo
        last_ts = c.index[-1]
        last_px = float(c.iloc[-1])
        dates = sorted(set(c.index.date))
        # Walk BACK to the most recent EARLIER date that actually has bars at/before
        # the 16:00 ET settle — do not assume dates[-2].
        # yfinance labels futures bars by calendar date and a date runs 00:09–23:58 ET,
        # so a SUNDAY contains only the 18:00 evening open. On a Monday dates[-2] IS
        # Sunday, a `time <= 16:00` filter there matches zero bars, and the gap came
        # back None on every Monday in the archive (3 of 3, while 14 of 14 other
        # weekdays had a number). Walking back lands on Friday's settle instead —
        # which is what a Monday gap means — and skips holidays for free.
        prior_close, prior_date = None, None
        for _d in reversed(dates[:-1]):
            prior = c[(c.index.date == _d) & (c.index.time <= dt.time(16, 0))]
            if len(prior):
                prior_close, prior_date = float(prior.iloc[-1]), _d
                break
        on = h[h.index.date == dates[-1]]
        on_hi = float(on["High"].max().iloc[0]) if hasattr(on["High"].max(), "iloc") else float(on["High"].max())
        on_lo = float(on["Low"].min().iloc[0]) if hasattr(on["Low"].min(), "iloc") else float(on["Low"].min())
        return {
            "last_px": round(last_px, 2),
            "last_ts_et": last_ts.isoformat(),
            "prior_rth_close": round(prior_close, 2) if prior_close else None,
            "prior_close_date": str(prior_date) if prior_date else None,
            "gap": round(last_px - prior_close, 2) if prior_close else None,
            "overnight_range": [round(on_lo, 2), round(on_hi, 2)],
        }
    except (ValueError, KeyError, IndexError) as e:
        return {"error": f"NQ intraday unavailable: {type(e).__name__}"}


def _weekdays_behind(last_date: dt.date, today: dt.date) -> int:
    """Weekdays strictly between last_date and today. 0 = fresh (has the prior
    session), >=1 = at least one full session missing = stale. Holiday-blind."""
    n, d = 0, last_date + dt.timedelta(days=1)
    while d < today:
        if d.weekday() < 5:
            n += 1
        d += dt.timedelta(days=1)
    return n


def _treasury_curve_rows(year: int) -> list:
    """[(date, {tenor: float}), ...] newest-first from the US Treasury Daily Par
    Yield Curve CSV. Empty data and expected failures permit fallback."""
    url = ("https://home.treasury.gov/resource-center/data-chart-center/"
           f"interest-rates/daily-treasury-rates.csv/{year}/all")
    try:
        r = httpx.get(url, params={"type": "daily_treasury_yield_curve",
                                   "field_tdr_date_value": str(year)},
                      timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rows = []
        for rec in csv.DictReader(io.StringIO(r.text)):
            try:
                d = dt.datetime.strptime(rec["Date"].strip(), "%m/%d/%Y").date()
            except (KeyError, ValueError) as exc:
                raise ValueError(f"Treasury curve date unreadable: {type(exc).__name__}") from None
            vals = {}
            for ten, col in _TSY_COL.items():
                raw = (rec.get(col) or "").strip()
                if raw not in ("", "N/A"):
                    try:
                        vals[ten] = float(raw)
                    except ValueError:
                        raise ValueError(f"Treasury curve {ten} value unreadable") from None
            if vals:
                rows.append((d, vals))
        rows.sort(key=lambda x: x[0], reverse=True)
        return rows
    except (httpx.HTTPError, ValueError, csv.Error) as exc:
        raise SourceUnavailable(f"Treasury curve unavailable: {_failure_reason(exc)}") from None


def _fred_yields() -> dict:
    """{tenor: [(date, value), ...newest-first]} from FRED; expected errors permit fallback."""
    key = _fred_key()
    if not key:
        raise SourceUnavailable("FRED unavailable: no FRED_API_KEY")
    out = {}
    for ten, sid in _FRED_SID.items():
        try:
            r = httpx.get("https://api.stlouisfed.org/fred/series/observations",
                          params={"series_id": sid, "api_key": key, "file_type": "json",
                                  "sort_order": "desc", "limit": 5}, timeout=20)
            r.raise_for_status()
            obs = [(o["date"], float(o["value"])) for o in r.json().get("observations", [])
                   if o.get("value") not in (".", "", None)]
            if obs:
                out[ten] = obs
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise SourceUnavailable(f"FRED {ten} unavailable: {_failure_reason(exc)}") from None
    return out


def treasury_yields(today: dt.date) -> dict:
    """Most-current EOD Treasury yields with a source chain:
    US Treasury par-yield API -> FRED -> yfinance. Always carries the as-of date,
    source, and a staleness flag."""
    failures = []

    def treasury():
        rows = _treasury_curve_rows(today.year)
        if len(rows) < 2 and today.month == 1:
            rows += (_attempt("Treasury prior year",
                              lambda: _treasury_curve_rows(today.year - 1), failures) or [])
            rows.sort(key=lambda x: x[0], reverse=True)
        out = {}
        if rows:
            date, vals = rows[0]
            prev = rows[1][1] if len(rows) > 1 else {}
            for ten in YIELD_TENORS:
                if ten in vals:
                    p = prev.get(ten)
                    out[ten] = {"last": round(vals[ten], 3), "prev": p,
                                "chg": round(vals[ten] - p, 3) if p is not None else None,
                                "last_date": date.isoformat()}
        return out

    def fred():
        out = {}
        for ten, obs in _fred_yields().items():
            last = obs[0]
            date = dt.date.fromisoformat(last[0])
            prev = obs[1][1] if len(obs) > 1 else None
            out[ten] = {"last": round(last[1], 3), "prev": prev,
                        "chg": round(last[1] - prev, 3) if prev is not None else None,
                        "last_date": date.isoformat()}
        return out

    def yahoo():
        out = {}
        for ten, sym in _YF_YIELD.items():
            def fetch(sym=sym):
                d = pull_daily(sym)
                if "error" in d:
                    return d
                return {"last": d["last"], "prev": d.get("prev_close"),
                        "chg": d.get("chg"),
                        "last_date": dt.date.fromisoformat(d["last_date"]).isoformat()}
            d = _attempt(f"yfinance {sym}", fetch, failures)
            if d:
                out[ten] = d
        return out

    tenors, source = {}, None
    for name, fetch in [("US Treasury par yield curve", treasury),
                        ("FRED (DGS series)", fred),
                        ("yfinance (^TNX/^TYX/^FVX proxy)", yahoo)]:
        tenors = _attempt(name, fetch, failures)
        if tenors:
            source = name
            break
    # Use the oldest leg as the aggregate freshness stamp; each tenor retains its date.
    last_date = min((dt.date.fromisoformat(v["last_date"]) for v in tenors.values()), default=None)

    behind = _weekdays_behind(last_date, today) if last_date else None
    return {
        "mode": "EOD",
        "tenors": tenors,
        "source": source or "UNAVAILABLE",
        "last_date": str(last_date) if last_date else None,
        "as_of": str(last_date) if last_date else None,
        "weekdays_behind": behind,
        "stale": bool(behind and behind >= 1),
        "failures": failures,
        "error": _unavailable("Yields", failures) if not tenors else None,
    }


def _cnbc_yields() -> dict:
    """LIVE benchmark UST yields from CNBC's quote webservice (the actual cash yield,
    not the CBOE index). Expected request/parse failures permit fallback."""
    sym_map = {"US2Y": "2Y", "US5Y": "5Y", "US10Y": "10Y", "US30Y": "30Y"}
    try:
        r = httpx.get("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol",
                      params={"symbols": "|".join(sym_map), "requestMethod": "itv", "noform": "1",
                              "fund": "1", "exthrs": "1", "output": "json"},
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        out = {}
        for d in r.json()["FormattedQuoteResult"]["FormattedQuote"]:
            ten = sym_map.get(d.get("symbol"))
            if not ten:
                continue
            try:
                last = float(str(d.get("last", "")).rstrip("%"))
            except ValueError:
                raise ValueError(f"CNBC {ten} yield unreadable") from None
            try:
                chg = round(float(d.get("change")), 3)
            except (TypeError, ValueError):
                chg = None
            out[ten] = {"last": round(last, 3), "chg": chg,
                        "time": d.get("last_time"), "status": d.get("curmktstatus")}
        return out
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise SourceUnavailable(f"CNBC yields unavailable: {_failure_reason(exc)}") from None


def _yahoo_chart_yields() -> dict:
    """LIVE-ish fallback via Yahoo's v8 chart endpoint for the CBOE yield indices.
    No 2Y. Source timestamps are retained; expected errors permit fallback."""
    out = {}
    for sym, ten in [("%5EFVX", "5Y"), ("%5ETNX", "10Y"), ("%5ETYX", "30Y")]:
        try:
            r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                          params={"range": "1d", "interval": "1m", "includePrePost": "true"},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
            m = r.json()["chart"]["result"][0]["meta"]
            px = m.get("regularMarketPrice")
            pc = m.get("chartPreviousClose") or m.get("previousClose")
            if px is not None:
                out[ten] = {"last": round(px, 3),
                            "chg": round(px - pc, 3) if pc else None,
                            "as_of": _yahoo_time(m)}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            raise SourceUnavailable(f"Yahoo {ten} yield unavailable: {_failure_reason(exc)}") from None
    return out


def live_yields(today: dt.date) -> dict:
    """CNBC -> Yahoo intraday; caller can use an explicitly labelled EOD fallback."""
    failures = []
    c = _attempt("CNBC yields", _cnbc_yields, failures)
    if c:
        times = [v["time"] for v in c.values() if v.get("time")]
        as_of = min(times) if len(times) == len(c) else None
        as_of_date = as_of[:10] if as_of else None
        status = next((v.get("status") for v in c.values() if v.get("status")), None)
        return {
            "mode": "LIVE",
            "source": "CNBC (UST benchmark, intraday)",
            "tenors": {t: {"last": v["last"], "chg": v["chg"]} for t, v in c.items()},
            "as_of": as_of, "last_date": as_of_date, "market_status": status,
            "stale": not as_of_date or as_of_date < str(today), "failures": failures,
        }
    y = _attempt("Yahoo yields", _yahoo_chart_yields, failures)
    if y:
        times = [v.get("as_of") for v in y.values() if v.get("as_of")]
        as_of = min(times) if times else None
        return {
            "mode": "LIVE",
            "source": "Yahoo ^TNX/^TYX/^FVX (intraday)",
            "tenors": y, "as_of": as_of, "last_date": as_of[:10] if as_of else None,
            "market_status": None, "stale": not as_of or as_of[:10] < str(today),
            "failures": failures,
        }
    return {"failures": failures, "error": _unavailable("Live yields", failures)}


def _cnbc_vol() -> dict:
    """LIVE CBOE vol indices from CNBC. VIX trades global-hours (live pre-open);
    VIX9D only recomputes in RTH, so pre-open its 'last' is the prior session's close."""
    smap = {"VIX": "^VIX", "VIX9D": "^VIX9D"}
    try:
        r = httpx.get("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol",
                      params={"symbols": "|".join(smap), "requestMethod": "itv", "noform": "1",
                              "fund": "1", "exthrs": "1", "output": "json"},
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        out = {}
        for d in r.json()["FormattedQuoteResult"]["FormattedQuote"]:
            key = smap.get(d.get("symbol"))
            if not key:
                continue
            try:
                last = float(str(d.get("last", "")).rstrip("%"))
            except ValueError:
                raise ValueError(f"CNBC {key} volatility unreadable") from None
            ch = str(d.get("change", "")).strip().upper()
            if ch in ("UNCH", "", "N/A"):
                chg = 0.0 if ch == "UNCH" else None
            else:
                try:
                    chg = round(float(d.get("change")), 2)
                except (TypeError, ValueError):
                    chg = None
            lt = d.get("last_time") or ""
            out[key] = {"last": round(last, 2), "chg": chg, "last_date": (lt[:10] or None),
                        "status": d.get("curmktstatus"), "source": "CNBC", "label": VOL_TICKERS[key]}
        return out
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise SourceUnavailable(f"CNBC volatility unavailable: {_failure_reason(exc)}") from None


def _yahoo_vol() -> dict:
    """Fallback live-ish vol via Yahoo v8 chart, retaining the source date."""
    out = {}
    for sym, key in [("%5EVIX", "^VIX"), ("%5EVIX9D", "^VIX9D")]:
        try:
            r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                          params={"range": "5d", "interval": "1d", "includePrePost": "true"},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            px = res["meta"].get("regularMarketPrice")
            closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                      if c is not None]
            prev = closes[-2] if len(closes) >= 2 else None
            if px is not None:
                out[key] = {"last": round(px, 2), "chg": round(px - prev, 2) if prev else None,
                            "last_date": _yahoo_time(res["meta"])[:10],
                            "source": "Yahoo(intraday)", "label": VOL_TICKERS[key]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            raise SourceUnavailable(f"Yahoo {key} volatility unavailable: {_failure_reason(exc)}") from None
    return out


def _cboe_index(symbol: str, tail: int = CBOE_LOOKBACK + 5) -> list[tuple[str, float]]:
    """(date, close) rows for one Cboe vol index, oldest first. Request errors stop the run.

    Dates arrive MM/DD/YYYY and leave ISO. Malformed lines stop the run rather than being guessed.
    """
    try:
        r = httpx.get(CBOE_INDEX_CSV.format(symbol=symbol),
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceUnavailable(f"Cboe {symbol} unavailable: {_failure_reason(exc)}") from None
    rows = []
    rd = csv.reader(io.StringIO(r.text))
    try:
        header = [h.strip().upper() for h in next(rd)]
    except StopIteration:
        return []
    # VIX/VIX9D/VIX3M/VIX6M serve DATE,OPEN,HIGH,LOW,CLOSE; VVIX serves DATE,VVIX.
    ci = 4 if (len(header) >= 5 and "OPEN" in header) else 1
    for rec in rd:
        try:
            d = dt.datetime.strptime(rec[0].strip(), "%m/%d/%Y").date().isoformat()
            rows.append((d, float(rec[ci])))
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"Cboe {symbol} history unreadable: {type(exc).__name__}") from None
    rows.sort()
    return rows[-tail:] if tail else rows


def cboe_vol_official() -> dict:
    """The OFFICIAL settled VIX curve from Cboe, plus where the regime ratio sits historically.

    This is the issuer publishing its own calculation — the vol equivalent of the US Treasury
    par-yield leg already used for yields. CNBC and Yahoo both redistribute this number.

    🔑 **All four legs come from the same CSV family and carry their own date**, so the
    VIX9D-vs-VIX alignment problem that `_vol_term_structure` has to work around cannot occur
    here: `aligned` is a direct fact, not a reconstruction. Reports an error if VIX itself
    fails; a curve without its anchor is not worth half-reporting.
    """
    failures = []
    series = {s: _attempt(f"Cboe {s}", lambda s=s: _cboe_index(s), failures)
              for s in CBOE_INDICES}
    if not series.get("VIX"):
        return {"error": _unavailable("Cboe curve", failures), "failures": failures}

    levels, dates = {}, []
    for sym, rows in series.items():
        if not rows:
            continue
        d, c = rows[-1]
        prev = rows[-2][1] if len(rows) > 1 else None
        levels[sym] = {"last": round(c, 2), "date": d, "label": CBOE_INDICES[sym],
                       "chg": round(c - prev, 2) if prev is not None else None}
        dates.append(d)

    out = {
        "source": "Cboe official close (cdn.cboe.com daily index CSVs)",
        "as_of": levels["VIX"]["date"],
        "levels": levels,
        "aligned": len(set(dates)) == 1,
        "missing": [s for s in CBOE_INDICES if s not in levels],
        "failures": failures,
    }

    vix = levels["VIX"]["last"]
    v9 = levels.get("VIX9D", {}).get("last")
    v3 = levels.get("VIX3M", {}).get("last")

    if v9:
        out["spread_9d_minus_30d"] = round(v9 - vix, 2)
    if v3:
        ratio = round(vix / v3, 4)
        out["vix_vix3m"] = ratio
        # Definitional, not a judgment: below 1.0 the next month is priced calmer than the
        # next quarter. The percentile beside it is CONTEXT for a regime read, never a gate.
        out["state"] = ("CONTANGO (30d < 3mo — normal)" if ratio < 1.0
                        else "BACKWARDATION (30d > 3mo — near-term stress)")

        # Percentile joins only sessions where BOTH legs exist. VIX starts 1990 and VIX3M
        # 2009; an unaligned join would score today against days VIX3M never had.
        v3map = dict(series["VIX3M"])
        hist = [c / v3map[d] for d, c in series["VIX"] if v3map.get(d)]
        window = hist[-CBOE_LOOKBACK:]
        if window:
            out["vix_vix3m_pct"] = round(
                100.0 * sum(1 for x in window if x <= ratio) / len(window), 1)
            out["backwardation_share_pct"] = round(
                100.0 * sum(1 for x in window if x >= 1.0) / len(window), 1)
            out["lookback_sessions"] = len(window)
    return out


def _compare_sources(primary: dict, fallback: dict, tol: float) -> dict:
    """Agreement check between two {key: last} maps over their overlapping keys."""
    rows, all_match = {}, True
    for k in sorted(set(primary) & set(fallback)):
        diff = round(abs(primary[k] - fallback[k]), 4)
        match = diff <= tol
        all_match = all_match and match
        rows[k] = {"primary": primary[k], "fallback": fallback[k], "diff": diff, "match": match}
    return {"rows": rows, "all_match": all_match if rows else None, "n": len(rows), "tol": tol}


def source_crosscheck() -> dict:
    """Each morning, confirm CNBC (primary) and Yahoo v8 (fallback) AGREE on the live
    numbers — a mismatch flags a bad feed before it reaches the brief."""
    failures = []
    cy = _attempt("CNBC yields", _cnbc_yields, failures)
    yy = _attempt("Yahoo yields", _yahoo_chart_yields, failures)
    cv = _attempt("CNBC volatility", _cnbc_vol, failures)
    yv = _attempt("Yahoo volatility", _yahoo_vol, failures)
    return {
        "failures": failures,
        "yields": _compare_sources({k: v["last"] for k, v in cy.items()},
                                   {k: v["last"] for k, v in yy.items()}, tol=0.03),
        "vol": _compare_sources({k: v["last"] for k, v in cv.items()},
                                {k: v["last"] for k, v in yv.items()}, tol=0.30),
    }


def _vol_term_structure(vix: dict, vix9d: dict) -> dict | None:
    """VIX9D vs VIX — but only ever as a SAME-MOMENT comparison.

    VIX publishes through the pre-open; VIX9D only recomputes in RTH. Measured over
    20 July data blocks, VIX9D's last_date was the PRIOR session on 17 of them — so
    the naive live-minus-stale subtraction compares two different days and then
    labels the result with confidence.

    That is not a rounding problem, it flips signs. On 2026-07-30 it produced the
    archive's ONLY backwardation reading — "acute near-term fear" — from VIX 18.91
    (7/30) minus VIX9D 20.38 (7/29). Aligned at the 7/29 close, VIX was 20.66 and
    the spread is -0.28: contango.

    Report a spread only when both legs share a known source date. A change-on-day
    does not identify the previous close's date; it cannot align a stale leg safely.
    """
    v, v9 = vix.get("last"), vix9d.get("last")
    if v is None or v9 is None:
        return None

    def _state(spread: float) -> str:
        return ("BACKWARDATION (9d > 30d — acute near-term fear)" if spread > 0
                else "CONTANGO (9d < 30d — normal/calm near-term)")

    vd, v9d = vix.get("last_date"), vix9d.get("last_date")
    ts = {"vix": v, "vix9d": v9, "vix_date": vd, "vix9d_date": v9d,
          "mixed_dates": bool(vd and v9d and vd != v9d)}

    if not vd or not v9d:
        return {**ts, "aligned": False, "mixed_dates": True,
                "note": "Source dates missing; no same-moment term structure available."}

    if not ts["mixed_dates"]:
        ts |= {"as_of": vd, "aligned": True, "spread_9d_minus_30d": round(v9 - v, 2),
               "state": _state(v9 - v)}
        return ts

    ts |= {"as_of": None, "aligned": False, "spread_9d_minus_30d": None, "state": None,
           "note": f"VIX is {vd} and VIX9D is {v9d}; no verified same-session close pair."}
    return ts


def macro_pulse(today: dt.date | None = None) -> dict:
    today = today or pd.Timestamp.now(tz="America/New_York").date()
    live = live_yields(today)
    eod = treasury_yields(today)
    out = {"yields": live if live.get("tenors") else eod, "yields_eod_close": eod}
    out["failures"] = live.get("failures", []) + eod.get("failures", [])
    if not live.get("tenors") and not eod.get("tenors"):
        out["yields"] = {**eod, "error": _unavailable("Yields", out["failures"])}
    vol_failures = []
    cnbc = _attempt("CNBC volatility", _cnbc_vol, vol_failures)
    need_yahoo = any(not cnbc.get(t) for t in VOL_TICKERS)
    yahoo = _attempt("Yahoo volatility", _yahoo_vol, vol_failures) if need_yahoo else {}
    official = cboe_vol_official()
    out["vol_official"] = official
    for t, lbl in VOL_TICKERS.items():
        failures = list(vol_failures)
        d = cnbc.get(t) or yahoo.get(t)
        for name, values in [("CNBC", cnbc), ("Yahoo", yahoo)]:
            if values and not values.get(t):
                failures.append((f"{name} {t}", "empty"))
        if not d or d.get("last") is None:
            d = _attempt(f"yfinance {t}", lambda t=t: pull_daily(t), failures)
            if d:
                d = {**d, "label": lbl, "source": "yfinance(daily)", "mode": "EOD"}
        if not d:
            level = official.get("levels", {}).get(t.lstrip("^"))
            if level:
                d = {**level, "last_date": level["date"], "source": official["source"],
                     "mode": "EOD"}
            else:
                failures.extend(official.get("failures", []))
                failures.append((f"Cboe {t}", "no settled level"))
        if d:
            d = {**d, "stale": not d.get("last_date") or d["last_date"] < str(today)}
        out[t] = d or {"error": _unavailable(t, failures)}
        out["failures"].extend(failures)

    # The term structure comes from Cboe's OWN closes when they are available: all legs are
    # from one publisher and one session, so the VIX9D-vs-VIX alignment problem below cannot
    # arise. The CNBC/Yahoo legs stay because Cboe publishes after the close and this brief is
    # pre-open — they carry the live move, which Cboe cannot.
    live_ts = _vol_term_structure(out.get("^VIX") or {}, out.get("^VIX9D") or {})

    if official and official.get("aligned") and official.get("spread_9d_minus_30d") is not None:
        vix_live = (out.get("^VIX") or {}).get("last")
        vix_close = official["levels"]["VIX"]["last"]
        ts = {
            "source": official["source"],
            "as_of": official["as_of"],
            "aligned": True,
            "vix": vix_close,
            "vix9d": official["levels"]["VIX9D"]["last"],
            "vix_date": official["as_of"],
            "vix9d_date": official["levels"]["VIX9D"]["date"],
            "mixed_dates": False,
            "spread_9d_minus_30d": official["spread_9d_minus_30d"],
            "state": ("BACKWARDATION (9d > 30d — acute near-term fear)"
                      if official["spread_9d_minus_30d"] > 0
                      else "CONTANGO (9d < 30d — normal/calm near-term)"),
            "vix3m": official.get("levels", {}).get("VIX3M", {}).get("last"),
            "vix6m": official.get("levels", {}).get("VIX6M", {}).get("last"),
            "vix_vix3m": official.get("vix_vix3m"),
            "vix_vix3m_pct": official.get("vix_vix3m_pct"),
            "backwardation_share_pct": official.get("backwardation_share_pct"),
            "lookback_sessions": official.get("lookback_sessions"),
        }
        if (vix_live is not None and vix_live != vix_close
                and out.get("^VIX", {}).get("mode") != "EOD"
                and not out.get("^VIX", {}).get("stale")):
            ts["vix_live"] = vix_live
            ts["vix_live_chg"] = (out.get("^VIX") or {}).get("chg")
            ts["note"] = (f"Term structure is Cboe's OFFICIAL {official['as_of']} close, all "
                          f"legs same session. VIX has since traded to {vix_live} live "
                          f"(CNBC/Yahoo) — quote that as the live move, not as a curve leg.")
        # Kept for transparency: if the relayed feed disagrees with the issuer, that is worth
        # seeing rather than silently discarding.
        if live_ts:
            ts["relayed_reading"] = live_ts
        out["term_structure"] = ts
    elif live_ts:
        # Cboe unreachable or unaligned: only dated, aligned relay/daily pairs are quotable.
        live_ts["source"] = "Dated relayed/daily quotes — Cboe official unavailable or unaligned"
        out["term_structure"] = live_ts
    else:
        out["term_structure"] = {"aligned": False, "mixed_dates": True,
                                 "note": _unavailable("Term structure", out["failures"])}
    return out


def sector_rotation() -> dict:
    """Sector + thematic rotation via ETF performance RELATIVE to SPY. Rank each SPDR
    sector ETF (+ semis/software/growth proxies) by return vs SPY over 1d and 5d.
    The 5d-relative is the primary sort (rotation is a multi-day tell). Live yfinance."""
    def ret5(d: dict):
        cl = d.get("recent_closes") or []
        return round(100 * (cl[-1] / cl[0] - 1), 2) if len(cl) >= 2 and cl[0] else None

    bench = pull_daily(ROTATION_BENCH)
    b1, b5 = bench.get("chg_pct"), ret5(bench)
    if "error" in bench or b1 is None or b5 is None:
        return {"error": f"SPY benchmark unavailable: {bench.get('error') or 'insufficient closes'}"}
    rows = []
    for t, lbl in {**SECTOR_ETFS, **THEME_ETFS}.items():
        d = pull_daily(t)
        if "error" in d or d.get("chg_pct") is None:
            return {"error": f"{t} unavailable: {d.get('error') or 'missing daily change'}"}
        r1, r5 = d["chg_pct"], ret5(d)
        rows.append({
            "etf": t, "label": lbl, "chg_1d": r1, "chg_5d": r5,
            "rel_1d": round(r1 - b1, 2) if b1 is not None else None,
            "rel_5d": round(r5 - b5, 2) if (r5 is not None and b5 is not None) else None,
            "is_theme": t in THEME_ETFS,
        })
    rows.sort(key=lambda x: (x["rel_5d"] if x["rel_5d"] is not None
                             else (x["rel_1d"] or 0)), reverse=True)
    return {"bench": {"etf": ROTATION_BENCH, "chg_1d": b1, "chg_5d": b5}, "ranked": rows}


def index_recap() -> dict:
    idx = {k: {**pull_daily(v), "label": INDEX_LABEL[k]} for k, v in INDEX_FUTURES.items()}
    idx[OIL[0]] = {**pull_daily(OIL[0]), "label": OIL[1]}
    idx["divergence_pct"] = {k: idx[k].get("chg_pct") for k in INDEX_FUTURES}
    idx["nq_intraday"] = pull_intraday_nq()
    return idx


# --------------------------------------------------------------------------- #
# GEX (dealer gamma) — Tradier live chain; we own the math.
#   $GEX per contract = sign * gamma * spot^2 * 0.01 * 100 * open_interest
#   sign = +1 calls, -1 puts (dealers net short calls / long puts).
#   Positive total GEX -> dealers long gamma -> vol-suppress / mean-revert.
#   Negative total GEX -> dealers short gamma -> vol-amplify / momentum.
# This is the identical formula the recorded (Discount Bloomberg) GEX used, on the
# same Tradier-supplied greeks — so the numbers stay continuous with that history.
# We report TWO views from one chain fetch:
#   - near_term: nearest GEX_NEAR_EXPIRATIONS expirations (the intraday regime read),
#   - full_chain: every listed expiration (the structural/textbook zero-gamma flip).
# --------------------------------------------------------------------------- #
def _tradier_get(endpoint: str, params: dict):
    tok = _env("TRADIER_API_KEY")
    if not tok:
        raise RuntimeError("no TRADIER_API_KEY")
    r = httpx.get(f"https://api.tradier.com/v1{endpoint}",
                  headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
                  params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _quote(symbol: str):
    q = (_tradier_get("/markets/quotes", {"symbols": symbol}).get("quotes") or {}).get("quote")
    return q[0] if isinstance(q, list) else q


def _expirations(symbol: str):
    e = (_tradier_get("/markets/options/expirations", {"symbol": symbol}).get("expirations") or {}).get("date", [])
    return [e] if isinstance(e, str) else (e or [])


def _chain(symbol: str, expiration: str):
    o = (_tradier_get("/markets/options/chains",
                      {"symbol": symbol, "expiration": expiration, "greeks": "true"}).get("options") or {}).get("option", [])
    return [o] if isinstance(o, dict) else (o or [])


def _chains_parallel(symbol: str, expirations, workers: int = 8) -> tuple[dict, list]:
    """Fetch every expiration's chain once, concurrently.

    Returns `(chains, failed)`. A failed request is REPORTED, never replaced by an empty
    list: "this expiration has no contracts" and "this request failed" are different
    facts, and the second one silently understates GEX. An empty chain counts as a
    failure too — a real listed expiration always has rows.
    """
    out: dict[str, list] = {}
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_chain, symbol, e): e for e in expirations}
        for fut, e in futs.items():
            try:
                rows = fut.result()
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                failed.append(e)
                continue
            if rows:
                out[e] = rows
            else:
                failed.append(e)
    return out, failed


def _zero_gamma_level(contracts, spot: float, now=None):
    """Zero-gamma (gamma-flip) level: the hypothetical spot at which total dealer GEX
    crosses zero, found by RE-PRICING every contract's gamma (Black-Scholes, r=q=0, its
    own IV and time to expiry) across spot ±GEX_FLIP_SCAN_PCT. Returns
    (flip, crossings): the crossing nearest spot, and every crossing in the window.

    Why not the per-strike method this replaced. That took the FIRST sign change in the
    net-gamma-by-strike table, scanning up from the lowest strike. Per-strike net gamma
    flickers sign all through the put-heavy zone, so "first" was usually a noise strike
    well below spot. 2026-09-29 QQQ: five per-strike sign changes between 690 and 765;
    it reported 720.8 (a lone +77M strike amid negatives) with spot 736.5 and total gamma
    NEGATIVE, a flip the regime itself contradicted. The re-priced profile has one
    crossing, ~739.3, just above spot. SPY that morning had a single per-strike crossing
    and the two methods agreed (765.8 vs 766.7), which is how the bug hid.

    `contracts` is [(strike, sign, open_interest, iv, expiration 'YYYY-MM-DD')]. None
    means no crossing inside the window (a genuinely one-sided tape), not an error."""
    if not contracts or not spot:
        return None, []
    now = now if now is not None else pd.Timestamp.now(tz="America/New_York")
    K, sign, oi, iv, t = [], [], [], [], []
    for strike, sgn, o, v, exp in contracts:
        close = pd.Timestamp(f"{exp} 16:00", tz="America/New_York")
        # Floor at 15 minutes: a contract minutes from expiry still carries gamma, and a
        # zero or negative T would divide by zero below.
        years = max((close - now).total_seconds(), 900.0) / (365.0 * 86400.0)
        K.append(strike); sign.append(sgn); oi.append(o); iv.append(v); t.append(years)
    K, sign, oi = np.array(K), np.array(sign), np.array(oi)
    sd = np.array(iv) * np.sqrt(np.array(t))
    weight = sign * oi / (np.sqrt(2.0 * np.pi) * sd)
    grid = np.linspace(spot * (1 - GEX_FLIP_SCAN_PCT), spot * (1 + GEX_FLIP_SCAN_PCT), GEX_FLIP_GRID)
    profile = np.empty_like(grid)
    for i in range(0, len(grid), 50):          # chunked: a full chain is ~25k contracts
        s = grid[i:i + 50, None]
        d1 = (np.log(s / K) + 0.5 * sd ** 2) / sd
        # gamma = pdf(d1) / (S·σ√T); times S²·0.01·100 per contract = pdf(d1)·S / σ√T
        profile[i:i + 50] = (np.exp(-0.5 * d1 ** 2) * weight).sum(axis=1) * s[:, 0]
    idx = np.nonzero(np.sign(profile[:-1]) * np.sign(profile[1:]) < 0)[0]
    crossings = [float(grid[i] + (grid[i + 1] - grid[i]) * profile[i] / (profile[i] - profile[i + 1]))
                 for i in idx]
    if not crossings:
        return None, []
    return min(crossings, key=lambda x: abs(x - spot)), crossings


def _gex_from_options(options, spot: float, now=None, *, spot_source=None, spot_time=None,
                      expirations_requested=None, expirations_failed=None) -> dict:
    """Aggregate net dealer GEX by strike over a flat list of option rows.

    Contracts missing gamma or open interest are unusable and get skipped. We COUNT the
    skips rather than dropping them in silence: a degraded greeks feed shrinks GEX toward
    zero while every field stays plausible, and a shrinking GEX is exactly what a genuine
    regime flip looks like.

    🔑 The alarm fires on `dropped_oi_pct`, NEVER on the raw count. Open interest is the
    multiplier in the GEX formula, so a contract with OI == 0 contributes EXACTLY zero —
    its absence cannot understate anything no matter how many of them there are, and a
    count cannot tell that apart from a real degradation. Measured across every run from
    2026-08-03 to 08-06: 100% of drops were newly-listed strikes that Tradier returns with
    no `greeks` block at all and OI == 0 (08-06: SPY 656 of 656, QQQ 700 of 700), and
    `dropped_oi_pct` was 0.0% in every sample ever recorded while `dropped_pct` ran as high
    as 5.42%. The old `max(count, oi)` gate therefore suppressed a perfectly good gamma
    read on three consecutive mornings. New listings arrive in bursts, so the count will
    keep spiking — it stays in the JSON as chain-composition context, not as an alarm.

    `n_dropped_oi_unknown` is the one case the OI measure genuinely cannot see: if open
    interest itself is missing, the loss is unbounded rather than zero, so it alarms on
    sight. Never fold that into `dropped_oi` as a 0 — that reports an unknown as harmless.
    """
    by_strike: dict[float, float] = {}
    profile_rows = []    # (strike, sign, oi, iv, expiration) for the re-priced flip
    call_total = put_total = 0.0
    n = n_dropped = n_oi_unknown = 0
    used_oi = dropped_oi = 0.0
    for o in options:
        g = (o.get("greeks") or {}).get("gamma")
        oi, strike, otype = o.get("open_interest"), o.get("strike"), o.get("option_type")
        if g is None or oi is None or strike is None or otype not in ("call", "put"):
            n_dropped += 1
            try:
                dropped_oi += float(oi)
            except (TypeError, ValueError):
                n_oi_unknown += 1
            continue
        gex = float(g) * float(oi) * 100.0 * (spot ** 2) * 0.01
        by_strike[strike] = by_strike.get(strike, 0.0) + (gex if otype == "call" else -gex)
        if otype == "call":
            call_total += gex
        else:
            put_total += gex
        used_oi += float(oi)
        n += 1
        # smv_vol is Tradier's smoothed surface; mid_iv is raw and junk on deep-ITM
        # strikes (0.43 vs 0.26 on a 709 QQQ call, 2026-09-29), so it is only a fallback.
        iv = o["greeks"].get("smv_vol") or o["greeks"].get("mid_iv")
        if float(oi) > 0 and iv and o.get("expiration_date"):
            profile_rows.append((float(strike), 1 if otype == "call" else -1,
                                 float(oi), float(iv), o["expiration_date"]))
    seen = n + n_dropped
    drop_pct = round(100.0 * n_dropped / seen, 2) if seen else None
    oi_seen = used_oi + dropped_oi
    drop_oi_pct = round(100.0 * dropped_oi / oi_seen, 2) if oi_seen else None
    warn = None
    if (drop_oi_pct or 0.0) >= GEX_DROP_WARN_PCT:
        warn = (f"{drop_oi_pct}% of open interest ({n_dropped} of {seen} contracts) came back "
                f"without usable greeks and was excluded. Open interest is the multiplier in "
                f"the GEX formula, so that much missing OI means GEX is materially "
                f"UNDERSTATED; treat the regime/flip/walls as unreliable rather than as a "
                f"change in positioning.")
    elif n_oi_unknown:
        warn = (f"{n_oi_unknown} of {seen} contracts reported NO open interest at all, so how "
                f"much GEX went missing cannot be measured. Treat the regime/flip/walls as "
                f"unverified.")
    # The flip is repriced from contracts carrying an IV; the total uses whatever gamma
    # the provider sent. Those are different sets, so measure the overlap and refuse a
    # flip drawn from a minority of the book.
    flip_oi = sum(row[2] for row in profile_rows)
    flip_share = round(flip_oi / used_oi, 4) if used_oi else None
    flip, crossings = _zero_gamma_level(profile_rows, spot, now)
    flip_note = None
    if flip is not None and flip_share is not None and flip_share < GEX_FLIP_MIN_OI_SHARE:
        flip, crossings = None, []
        flip_note = (f"the re-priced set covers {flip_share:.1%} of open interest, below the "
                     f"{GEX_FLIP_MIN_OI_SHARE:.0%} floor, so no flip is reported")

    failed = list(expirations_failed or [])
    degraded_reason = None
    if failed:
        degraded_reason = (
            f"{len(failed)} of {expirations_requested or '?'} expiration(s) returned no chain "
            f"({', '.join(failed)}). A missing chain never enters the open-interest denominator, "
            f"so the drop alarm cannot see it; gamma is withheld rather than understated."
        )
        warn = degraded_reason

    total = sum(by_strike.values())
    top = sorted(by_strike.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
    out = {
        "status": "degraded" if degraded_reason else "ok",
        "degraded_reason": degraded_reason,
        "total_gex": total,
        "call_gex": call_total,
        "put_gex": -put_total,
        "regime": "positive" if total > 0 else "negative",
        "flip": round(flip, 2) if flip is not None else None,
        "flip_note": flip_note,
        # Every zero crossing inside spot ±GEX_FLIP_SCAN_PCT; `flip` is the nearest one.
        # More than one means gamma changes sign more than once in the window; say so
        # rather than trusting the nearest blindly.
        "flip_crossings": [round(c, 2) for c in crossings],
        "call_wall": max(by_strike, key=by_strike.get) if by_strike else None,
        "put_wall": min(by_strike, key=by_strike.get) if by_strike else None,
        "top_walls": [round(float(k), 1) for k, _ in top],
        "n_contracts": n,
        # Recorded every run, healthy or not, so the warn threshold can be retuned from
        # real data instead of one sample. Only `data_warning` is ever surfaced.
        "n_dropped": n_dropped,
        "dropped_pct": drop_pct,          # chain-composition context only — NOT the alarm
        "dropped_oi_pct": drop_oi_pct,    # the alarm: OI is the multiplier in the formula
        "n_dropped_oi_unknown": n_oi_unknown,
        "data_warning": warn,
        "coverage": {
            "expirations_requested": expirations_requested,
            "expirations_failed": failed,
            "contracts": n,
            "contracts_dropped": n_dropped,
            "oi_used": round(used_oi, 0),
            "oi_dropped": round(dropped_oi, 0),
            "flip_oi_share": flip_share,
        },
        "spot_source": spot_source,
        "spot_time": spot_time,
        "greeks_age": "unknown: the provider supplies no greeks timestamp",
        "assumptions": [
            ("call gamma counted positive and put gamma negative: dealers assumed long "
             "customer calls and short customer puts (inferred, never reported)"),
            "open interest is end-of-day, so walls describe the prior session's positioning",
            ("the flip re-prices gamma with Black-Scholes at r=q=0, each contract's own IV, "
             "a 16:00 ET expiry and a 15-minute floor"),
        ],
    }
    if degraded_reason:
        out.update({"regime": None, "flip": None, "flip_crossings": [], "call_wall": None,
                    "put_wall": None, "top_walls": []})
    return out


def _ms_to_et(ms):
    """Tradier's epoch-ms timestamp -> ET ISO, or None. Never guessed."""
    if not ms:
        return None
    try:
        return pd.Timestamp(int(ms), unit="ms", tz="UTC").tz_convert("America/New_York").isoformat()
    except (TypeError, ValueError):
        return None


def gex_tradier(today: dt.date) -> dict:
    """Front-dated + full-chain GEX for SPY+QQQ from the live Tradier chain.
    NOTE: pre-market this reflects the PRIOR session's IV/greeks (options don't
    trade overnight) — the brief labels it as such. The provider supplies no greeks
    timestamp, so its age is reported as unknown rather than estimated.

    A failed expiration makes that view DEGRADED: regime, flip and walls are withheld.
    An understated GEX looks exactly like a genuine shift to negative gamma, so a
    partial chain has no safe number to publish."""
    out = {}
    for sym in GEX_SYMBOLS:
        try:
            q = _quote(sym)
            spot_source = "last" if q.get("last") else "close"
            spot = float(q.get("last") or q.get("close"))
            spot_time = _ms_to_et(q.get("trade_date"))
            exps = sorted(_expirations(sym))
            if not exps:
                out[sym] = {"error": "no expirations"}
                continue
            chains, failed = _chains_parallel(sym, exps)     # fetch once, reuse for both views
            near_exps = exps[:GEX_NEAR_EXPIRATIONS]
            near_failed = [e for e in near_exps if e in failed]
            near_opts = [o for e in near_exps for o in chains.get(e, [])]
            full_opts = [o for e in exps for o in chains.get(e, [])]
            ctx = {"spot_source": spot_source, "spot_time": spot_time}
            res = {
                "spot": round(spot, 2),
                "spot_source": spot_source,
                "spot_time": spot_time,
                "near_term": _gex_from_options(
                    near_opts, spot, expirations_requested=len(near_exps),
                    expirations_failed=near_failed, **ctx)
                    | {"expirations": near_exps, "n_expirations": len(near_exps)},
                "full_chain": _gex_from_options(
                    full_opts, spot, expirations_requested=len(exps),
                    expirations_failed=failed, **ctx)
                    | {"n_expirations": len(exps), "last_expiration": exps[-1]},
            }
            out[sym] = res
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as e:
            out[sym] = {"error": f"gamma read unavailable: {type(e).__name__}"}
    return out


def event_gate(today: dt.date, horizon_days: int = 21) -> dict:
    """Read both calendar tiers.

    The two tiers answer different questions and are deliberately NOT merged:
      - `events` (T1) is MFFU's restricted-trading list -> drives `today_is_t1`.
      - `tier2`  is market-moving-but-not-T1 (PCE, GDP, PPI, retail, claims).

    Before tier2 existed the brief's event section was T1-only, and on 2026-07-30
    it reported "no events today" on a morning with GDP + PCE + claims all at
    08:30. `today_is_t1` still means ONLY what it always meant -- don't fold
    tier2 into it, or the prop-firm gate silently changes meaning.
    """
    if not T1_CALENDAR.exists():
        return {"error": "no t1 calendar"}
    try:
        return _event_gate(today, horizon_days)
    except (OSError, yaml.YAMLError, ValueError, TypeError) as e:
        # Outer net. This section is the only one that could ever take the WHOLE run
        # down: gather() raises before writing anything, so a single typo in the YAML
        # cost the entire morning — no data block, no brief, and the futures/GEX/news
        # calls never even ran. Measured: 5 of 7 realistic typos did exactly that
        # (13th month, day 32, unpadded date, a one-space indent slip, a stray tab).
        # Expected calendar failures carry an explicit error; programming errors propagate.
        return {"error": f"event calendar unreadable: {e!r}"}


def _event_gate(today: dt.date, horizon_days: int) -> dict:
    cal = yaml.safe_load(T1_CALENDAR.read_text(encoding="utf-8"))
    if not isinstance(cal, dict):
        raise TypeError("event calendar must be a mapping")
    malformed: list[str] = []

    def _window(key: str, default_verified) -> list:
        rows = []
        if not isinstance(cal.get(key, []), list):
            raise TypeError(f"{key} must be a list")
        for i, e in enumerate(cal.get(key, []) or []):
            # Per-entry guard: one bad row costs that ONE event, not the calendar.
            try:
                if not isinstance(e, dict):
                    raise TypeError(f"expected a mapping, got {type(e).__name__}")
                raw = e["date"]
                if isinstance(raw, dt.datetime):        # check BEFORE date — datetime subclasses it
                    d = raw.date()
                elif isinstance(raw, dt.date):
                    d = raw
                else:
                    d = dt.date.fromisoformat(str(raw).strip())
                # Read `name` for EVERY entry, not just in-window ones. It used to be read
                # only inside the horizon test, which made a missing `name:` a time bomb:
                # harmless when you edit and verify the file, fatal every morning from the
                # day the event drifts inside 21 days. Measured on a 2026-08-26 entry —
                # fine on 08-04, dead from 08-05.
                name = e["name"]
            except (KeyError, TypeError, ValueError) as ex:
                malformed.append(f"{key}[{i}]: {ex!r}")
                continue
            delta = (d - today).days
            if 0 <= delta <= horizon_days:
                rows.append({"name": name, "date": str(d), "time_et": e.get("time_et"),
                             "days_away": delta,
                             "verified": e.get("verified", default_verified)})
        rows.sort(key=lambda x: x["days_away"])
        return rows

    events = _window("events", None)
    # tier2 is FRED-sourced and verified by construction, so it has no per-entry flag.
    tier2 = _window("tier2", True)

    out = {
        "today_is_t1": any(e["days_away"] == 0 for e in events),
        "today_events": [e for e in events if e["days_away"] == 0],
        "upcoming": events,
        "today_tier2": [e for e in tier2 if e["days_away"] == 0],
        "upcoming_tier2": tier2,
    }
    if malformed:
        out["malformed"] = malformed
    return out


def finnhub_news(key: str) -> dict:
    if not key:
        return {"error": "no FINNHUB_API_KEY"}
    out = {"general": [], "themed": []}

    def clean(items):
        rows, seen = [], set()
        for x in items:
            hl = (x.get("headline") or "").strip()
            if not hl or hl in seen:
                continue
            seen.add(hl)
            rows.append({
                "headline": hl, "source": x.get("source", ""),
                "dt": (dt.datetime.fromtimestamp(x["datetime"], dt.UTC).isoformat()
                       if x.get("datetime") else None),
                "url": x.get("url", ""),
                "trusted": (x.get("source", "").lower() in TRUSTED_SOURCES),
            })
        return rows

    try:
        r = httpx.get("https://finnhub.io/api/v1/news",
                      params={"category": "general", "token": key}, timeout=25)
        out["status"] = r.status_code
        r.raise_for_status()
        items = clean(r.json()) if r.status_code == 200 else []
        items.sort(key=lambda x: x["dt"] or "", reverse=True)
        items.sort(key=lambda x: not x["trusted"])  # stable: trusted block first
        out["general"] = items[:14]
        themed = [x for x in items if any(k in x["headline"].lower() for k in THEME_KEYWORDS)]
        out["themed"] = themed[:12]
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as e:
        out["error"] = f"headlines unavailable: {type(e).__name__}"
    return out


# --------------------------------------------------------------------------- #
# Assemble + render
# --------------------------------------------------------------------------- #
def gather(today: dt.date | None = None) -> dict:
    _et = pd.Timestamp.now(tz="America/New_York")
    today = today or _et.date()
    return {
        # ET, not local. This field names the data/runs/ archive (_archive_previous_run),
        # and a naive local clock silently offset every archived filename from the brief's
        # own ET stamp — on a UTC-1 machine the 9:31 AM ET run archived as `-0831`, so the
        # "a stamped brief resolves to exactly one block" contract failed quietly. Keeping
        # it tz-aware also survives the DST changeover, which a fixed offset would not.
        "as_of": _et.isoformat(timespec="seconds"),
        "as_of_et": _et.strftime("%a %b %d, %Y ") + _et.strftime("%I:%M %p").lstrip("0") + " ET",
        "date": str(today),
        "event_gate": event_gate(today),
        "index_recap": index_recap(),
        "macro_pulse": macro_pulse(today),
        "source_crosscheck": source_crosscheck(),
        "sector_rotation": sector_rotation(),
        "gex": gex_tradier(today),
        "finnhub": finnhub_news(_finnhub_key()),
    }


def _flip_text(view: dict) -> str:
    """The zero-gamma level as the block prints it. No crossing and several crossings
    both have to read differently from a clean single level."""
    pct = f"±{GEX_FLIP_SCAN_PCT:.0%}"
    if view.get("flip") is None:
        return f"none within {pct} of spot"
    others = [c for c in view.get("flip_crossings", []) if c != view["flip"]]
    if others:
        return f"{view['flip']} (nearest of {len(others) + 1} crossings in {pct}: {view['flip_crossings']})"
    return str(view["flip"])


def _bn(x) -> str:
    """Human-scale a raw GEX figure (they run to the billions) with a sign."""
    try:
        return f"{x / 1e9:+.2f}bn"
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Gamma total unreadable: {type(exc).__name__}") from None


def render_md(d: dict) -> str:
    """A compact, human-readable dump of the data block (what Claude reads)."""
    L = [f"# BRIEF DATA BLOCK — {d['date']}",
         (f"Data pulled: **{d.get('as_of_et', d['as_of'])}**  "
          f"(put this freshness stamp in the brief so readers know it's live)"), ""]

    eg = d["event_gate"]
    L.append("## Event gate")
    if eg.get("error"):
        # A blind gate must never be read as a quiet calendar — that is the 2026-07-30
        # mistake in its worst form. The rest of the block is still good; this section
        # is simply absent and has to be replaced by the step-2 web sweep.
        L.append(f"- 🔴 EVENT CALENDAR UNAVAILABLE — {eg['error']}")
        L.append("  **The gate is BLIND today. Do NOT write 'no events today' or infer a "
                 "quiet calendar from this.** Cover the whole calendar from the step-2 web "
                 "sweep instead, and fix t1_calendar.yaml after the brief goes out.")
    else:
        if eg.get("malformed"):
            L.append(f"- ⚠️ {len(eg['malformed'])} calendar entr"
                     f"{'y was' if len(eg['malformed']) == 1 else 'ies were'} skipped as "
                     f"malformed — the rest of the calendar below is fine, but treat it as "
                     f"possibly missing an event and fix the file:")
            for m in eg["malformed"][:6]:
                L.append(f"    - {m}")
        L.append(f"- today_is_t1: **{eg.get('today_is_t1')}**   "
                 f"(MFFU restricted-trading list ONLY — not 'nothing happens today')")

        # Today's tier-2 releases go FIRST and loud. This is the line whose absence
        # nearly cost the 2026-07-30 brief its two biggest numbers.
        t2_today = eg.get("today_tier2") or []
        if t2_today:
            L.append("- 🔴 **TODAY (tier 2 — market-moving, not T1):**")
            for e in t2_today:
                L.append(f"    - {e.get('time_et') or ''} ET  **{e['name']}**")

        up = eg.get("upcoming", [])
        if up:
            L.append("- T1 ahead:")
            for e in up[:6]:
                L.append(f"  - +{e['days_away']}d  {e['date']}  {e['name']} {e.get('time_et') or ''}"
                         f"{'' if e.get('verified') else '  (unverified)'}")

        up2 = [e for e in eg.get("upcoming_tier2", []) if e["days_away"] > 0]
        if up2:
            L.append("- Tier 2 ahead:")
            for e in up2[:8]:
                L.append(f"  - +{e['days_away']}d  {e['date']}  {e['name']} {e.get('time_et') or ''}")
        L.append("- (ISM is in NEITHER tier — FRED doesn't carry it. Catch it in the step-2 web "
                 "sweep, along with Fed speakers.)")

    L.append("\n## Index recap (daily close-to-close)")
    ir = d["index_recap"]
    for k in ["ES", "YM", "NQ", "RTY", "CL=F"]:
        v = ir.get(k, {})
        if "error" in v:
            L.append(f"- {k}: ERROR {v['error']}"); continue
        L.append(f"- {v.get('label', k)} ({k}): {v.get('last')}  "
                 f"{v.get('chg_pct')}%  range {v.get('last_range')}  [{v.get('last_date')}]")
        ra = v.get("roll_adjusted")
        if ra:
            L.append(f"    ⚠️ CONTRACT ROLL: daily prev_close {ra['prev_close_daily']} was the old "
                     f"contract ({ra['gap_pct']}% apart); chg is vs the same-contract prior close")
    L.append(f"- divergence %: {ir.get('divergence_pct')}")
    ni = ir.get("nq_intraday", {})
    if "error" not in ni:
        L.append(f"- NQ intraday: last {ni.get('last_px')} @ {ni.get('last_ts_et')}; "
                 f"gap vs prior close {ni.get('gap')}; overnight range {ni.get('overnight_range')}")
    else:
        L.append(f"- NQ intraday: UNAVAILABLE — {ni['error']}")

    L.append("\n## Macro pulse (moves, not levels)")
    mp = d["macro_pulse"]
    y = mp.get("yields", {})
    if y.get("tenors"):
        mode = y.get("mode", "EOD")
        stale_tag = ("  ⚠️ STALE (data is behind — flag in the brief, do NOT present "
                     "as live)" if y.get("stale") else "")
        mstat = f"  [{y.get('market_status')}]" if y.get("market_status") else ""
        L.append(f"- Treasury yields ({mode}) — source: {y.get('source')}  "
                 f"as-of [{y.get('as_of') or y.get('last_date')}]{mstat}{stale_tag}")
        L.append("  (chg = move vs prior close)")
        for ten in YIELD_TENORS:
            v = y["tenors"].get(ten)
            if v:
                L.append(f"    {ten:>3}: {v.get('last')}%  chg {v.get('chg')}")
        eod = mp.get("yields_eod_close") or {}
        if mode == "LIVE" and eod.get("tenors"):
            e10 = eod["tenors"].get("10Y", {})
            L.append(f"  (EOD reference [{eod.get('last_date')}] via "
                     f"{eod.get('source')}: 10Y {e10.get('last')}%)")
    else:
        L.append(f"- Treasury yields: UNAVAILABLE — {y.get('error') or y.get('source')}")
    for source, reason in mp.get("failures", []):
        L.append(f"  Source attempt failed: {source} ({reason})")
    for t in ["^VIX", "^VIX9D"]:
        v = mp.get(t, {})
        if "error" not in v:
            L.append(f"- {v.get('label')} ({t}): {v.get('last')}  chg {v.get('chg')}  "
                     f"[{v.get('last_date')}] ({v.get('source', '?')}; "
                     f"{v.get('mode', 'relayed quote')})"
                     f"{' STALE / not current' if v.get('stale') else ''}")
        else:
            L.append(f"- {t}: UNAVAILABLE — {v['error']}")
    off = mp.get("vol_official")
    if off and off.get("error"):
        L.append(f"- VOL CURVE: UNAVAILABLE — {off['error']}")
    if off and off.get("levels"):
        lv = off.get("levels", {})
        curve = "  ".join(f"{s} {lv[s]['last']}" for s in CBOE_INDICES if s in lv)
        L.append(f"- VOL CURVE (Cboe OFFICIAL close [{off.get('as_of')}], "
                 f"{'all legs same session' if off.get('aligned') else '⚠️ MIXED SESSIONS'}): "
                 f"{curve}")
        if off.get("vix_vix3m") is not None:
            pct = off.get("vix_vix3m_pct")
            ctx = (f" — percentile {pct} of the last {off.get('lookback_sessions')} sessions"
                   if pct is not None else "")
            L.append(f"    VIX/VIX3M {off['vix_vix3m']} -> {off['state']}{ctx}. "
                     f"Backwardation occurs in {off.get('backwardation_share_pct')}% of them.")
            L.append("    (percentile is CONTEXT for the regime read — not a gate, not a signal)")
        if off.get("missing"):
            L.append(f"    ⚠️ legs unavailable: {', '.join(off['missing'])}")
        for source, reason in off.get("failures", []):
            L.append(f"    {source} ({reason})")

    ts = mp.get("term_structure")
    if ts and not ts.get("mixed_dates"):
        src = ts.get("source", "")
        L.append(f"- TERM STRUCTURE [both legs {ts.get('as_of')}, same-moment"
                 f"{'; ' + src if src else ''}]: "
                 f"VIX9D {ts['vix9d']} vs VIX {ts['vix']} "
                 f"(spread {ts['spread_9d_minus_30d']}) -> {ts['state']}")
        if ts.get("vix_live") is not None:
            L.append(f"    VIX has since traded to {ts['vix_live']} live — that is the intraday "
                     f"move, NOT a curve leg. The curve above is the official close.")
    elif ts and ts.get("aligned"):
        L.append(f"- TERM STRUCTURE ⚠️ legs are from DIFFERENT sessions — VIX9D only "
                 f"recomputes in RTH, so it is the {ts['vix9d_date']} close while VIX is "
                 f"live {ts['vix_date']}.")
        L.append(f"    QUOTE THIS (aligned at the {ts['as_of']} close): "
                 f"VIX9D {ts['vix9d']} vs VIX {ts['vix_at_that_close']} "
                 f"(spread {ts['spread_9d_minus_30d']}) -> {ts['state']}")
        L.append(f"    VIX has since moved to {ts['vix_live']} ({ts['vix_live_chg']:+}). "
                 f"The raw live-minus-stale spread would read "
                 f"{ts['spread_raw_unaligned']:+} — do NOT quote that.")
    elif ts:
        L.append(f"- TERM STRUCTURE: ⚠️ UNAVAILABLE — {ts.get('note')}")

    cc = d.get("source_crosscheck", {})
    if cc:
        L.append("\n## Source cross-check (CNBC primary vs Yahoo v8 fallback)")
        for source, reason in cc.get("failures", []):
            L.append(f"- Cross-check unavailable: {source} ({reason})")
        for grp, lbl, unit in [("yields", "Yields", "bp"), ("vol", "Vol", "pt")]:
            c = cc.get(grp, {})
            if not c.get("n"):
                L.append(f"- {lbl}: only one source available — NOT cross-checked"); continue
            scale = 100 if unit == "bp" else 1
            maxd = max((r["diff"] for r in c["rows"].values()), default=0) * scale
            if c["all_match"]:
                L.append(f"- {lbl}: ✓ all {c['n']} match (max Δ {maxd:.1f}{unit}, "
                         f"tol {c['tol']*scale:g}{unit})")
            else:
                bad = [f"{k}: CNBC {r['primary']} vs Yahoo {r['fallback']} (Δ{r['diff']*scale:.1f}{unit})"
                       for k, r in c["rows"].items() if not r["match"]]
                L.append(f"- {lbl}: ⚠️ MISMATCH — VERIFY BEFORE PUBLISHING: " + "; ".join(bad))

    sr = d.get("sector_rotation", {})
    if "error" in sr or not sr.get("ranked"):
        L.append("\n## Sector rotation (ETF rel-perf vs SPY — LIVE yfinance)")
        L.append(f"- Sector rotation: UNAVAILABLE — {sr.get('error') or 'no usable ETF returns'}")
    if sr and "error" not in sr and sr.get("ranked"):
        f = lambda x: f"{x:+.2f}" if isinstance(x, (int, float)) else " na "
        b = sr["bench"]
        L.append("\n## Sector rotation (ETF rel-perf vs SPY — LIVE yfinance)")
        L.append(f"- bench {b['etf']}: 1d {f(b['chg_1d'])}%  5d {f(b['chg_5d'])}%  "
                 f"(rotation = each sector's return REL to this)")
        rk = sr["ranked"]
        L.append("- LEADERS (rel 5d, + = rotating IN):")
        for r in rk[:4]:
            th = " [theme]" if r["is_theme"] else ""
            L.append(f"    {r['etf']:4s} {r['label']:15s} 1d {f(r['chg_1d'])}% (rel {f(r['rel_1d'])})  "
                     f"5d {f(r['chg_5d'])}% (rel {f(r['rel_5d'])}){th}")
        L.append("- LAGGARDS (rel 5d, - = rotating OUT):")
        for r in rk[-4:]:
            th = " [theme]" if r["is_theme"] else ""
            L.append(f"    {r['etf']:4s} {r['label']:15s} 1d {f(r['chg_1d'])}% (rel {f(r['rel_1d'])})  "
                     f"5d {f(r['chg_5d'])}% (rel {f(r['rel_5d'])}){th}")
        themes = [r for r in rk if r["is_theme"]]
        if themes:
            L.append("- THEME proxies (within-tech tilt): "
                     + " · ".join(f"{r['etf']} rel5d {f(r['rel_5d'])}" for r in themes))

    L.append("\n## GEX (dealer gamma — Tradier live chain; pre-market = prior-session greeks)")
    L.append(f"  near-term = nearest {GEX_NEAR_EXPIRATIONS} expirations (intraday regime); "
             f"full-chain = every listed expiration (structural flip)")
    for sym in GEX_SYMBOLS:
        v = d["gex"].get(sym, {})
        if "error" in v:
            L.append(f"- {sym}: ERROR {v['error']}"); continue
        nt, fc = v["near_term"], v["full_chain"]
        # A degraded view has no regime, flip or walls — the fields are nulled upstream.
        # Printing the partial totals here would invite a regime call from a chain that
        # is missing an expiration, which is the one failure that looks like a market move.
        if nt.get("status") == "degraded" or fc.get("status") == "degraded":
            reason = fc.get("degraded_reason") or nt.get("degraded_reason")
            L.append(f"- {sym}: 🔴 GAMMA READ UNAVAILABLE — {reason}")
            L.append("    do not write a regime call, a flip, or walls for this symbol")
            continue
        L.append(f"- {sym}: spot {v['spot']} ({v.get('spot_source') or 'source unknown'} "
                 f"@ {v.get('spot_time') or 'time unknown'})")
        L.append(f"    near-term ({nt['n_expirations']} exp {nt.get('expirations')}): "
                 f"regime **{nt['regime']}**, total {_bn(nt['total_gex'])}, "
                 f"zero-gamma flip {_flip_text(nt)}, call wall {nt['call_wall']}, put wall {nt['put_wall']}, "
                 f"walls {nt['top_walls']}")
        L.append(f"    full-chain ({fc['n_expirations']} exp, thru {fc.get('last_expiration')}): "
                 f"regime **{fc['regime']}**, total {_bn(fc['total_gex'])}, "
                 f"zero-gamma flip {_flip_text(fc)}, call wall {fc['call_wall']}, put wall {fc['put_wall']}")
        # Silent when the chain is healthy. "Healthy" means no OPEN INTEREST went missing —
        # a nonzero `dropped_pct` alone is normal (newly-listed strikes carry no greeks and
        # no OI, and contribute exactly zero GEX). This only appears when the feed is truly
        # degraded, and it is a note to the WRITER, not brief copy: see playbook.md's
        # "never publish process notes".
        warn = fc.get("data_warning") or nt.get("data_warning")
        if warn:
            L.append(f"    🔴 CHAIN INCOMPLETE — {warn}")
        cov = fc.get("coverage") or {}
        L.append(f"    coverage: {cov.get('contracts', 'n/a')} contracts, "
                 f"OI used {cov.get('oi_used', 'n/a')}, "
                 f"flip OI share {cov.get('flip_oi_share', 'n/a')}, "
                 f"expirations failed {len(cov.get('expirations_failed') or [])}")
        L.append(f"    greeks: {fc.get('greeks_age') or 'age unknown'}")

    L.append("\n## Finnhub headlines")
    fh = d["finnhub"]
    if fh.get("error"):
        L.append(f"- Finnhub headlines: UNAVAILABLE — {fh['error']}")
    L.append(f"- status: {fh.get('status')}")
    L.append("- GENERAL (trusted-first):")
    for x in fh.get("general", [])[:12]:
        tag = "★" if x["trusted"] else " "
        L.append(f"  {tag} [{x['source']}] {x['headline']}  ({x['dt']})")
    L.append("- THEMED:")
    for x in fh.get("themed", [])[:10]:
        L.append(f"  • [{x['source']}] {x['headline']}")
    return "\n".join(L)


def _archive_previous_run(date: str) -> str | None:
    """Move an existing same-day data block into data/runs/ before it gets overwritten.

    A published brief's only link back to the numbers it was written from is its
    freshness stamp (the gatherer's `as_of_et`). A second run of the day overwrote
    data/{date}.json in place, so that link broke silently — measured on 3 of 19 July
    days (7/16 by 4 min, 7/30 by 41 min, 7/27 by 91 min), and the published error
    tracked the gap: 7/27's NQ change was 1.39pp away from the stored block. `data/`
    is gitignored, so the original run was simply gone.

    Deliberately archive rather than rename the live file: data/{date}.json stays
    "the latest run", so playbook.md and anything else reading that path are untouched.
    (`brief-live`, which used to import these functions live, was retired with CMTMS.) The archive is keyed by the SAME HH:MM the brief's stamp shows, so
    any past brief resolves to exactly one file with no extra bookkeeping.

    That key is **ET**, from the block's own tz-aware `as_of` — the same clock as the
    stamp. It read a naive LOCAL `as_of` until 2026-08-04, which silently offset every
    archived name from the stamp by the machine's UTC offset (the 9:31 AM ET run landed
    as `-0831`), quietly breaking the one-file guarantee this docstring promises.

    Returns the archived run's HHMM, or None if there was nothing to archive.
    """
    j = DATA_DIR / f"{date}.json"
    if not j.exists():
        return None
    try:
        stamp = dt.datetime.fromisoformat(
            json.loads(j.read_text(encoding="utf-8"))["as_of"]).strftime("%H%M")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Previous data block cannot be archived safely: {type(exc).__name__}") from None
    runs = DATA_DIR / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    j.replace(runs / f"{date}-{stamp}.json")
    md = DATA_DIR / f"{date}.data.md"
    if md.exists():
        md.replace(runs / f"{date}-{stamp}.data.md")
    return stamp


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    d = gather()
    archived = _archive_previous_run(d["date"])   # only after gather() succeeds
    (DATA_DIR / f"{d['date']}.json").write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
    md = render_md(d)
    (DATA_DIR / f"{d['date']}.data.md").write_text(md, encoding="utf-8")
    print(md)
    if archived:
        print(f"\n[gather] NOTE: an earlier run from {archived[:2]}:{archived[2:]} ET already "
              f"existed for {d['date']} — archived to data/runs/{d['date']}-{archived}.*  "
              f"A brief stamped with that time was written from THAT block, not this one.")
    print(f"\n[gather] wrote data/{d['date']}.json + data/{d['date']}.data.md ; "
          f"the finished brief goes to briefs/{d['date']}.md")


if __name__ == "__main__":
    main()
