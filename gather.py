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
import pandas as pd
import yaml
import yfinance as yf

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

# Treasury yields: source from the US Treasury's own Daily Par Yield Curve (the
# authoritative source — these ARE the constant-maturity rates FRED republishes a
# day later), with FRED then yfinance as fallbacks. Tradier does NOT quote the
# Treasury yield indices, so it is not in the chain.
YIELD_TENORS = ["2Y", "5Y", "10Y", "30Y"]
_TSY_COL = {"2Y": "2 Yr", "5Y": "5 Yr", "10Y": "10 Yr", "30Y": "30 Yr"}
_FRED_SID = {"2Y": "DGS2", "5Y": "DGS5", "10Y": "DGS10", "30Y": "DGS30"}
_YF_YIELD = {"5Y": "^FVX", "10Y": "^TNX", "30Y": "^TYX"}  # no 2Y proxy on yfinance

# Vol stays on yfinance/CNBC (live, reliable): 30d + 9d VIX for term structure.
VOL_TICKERS = {"^VIX": "VIX (30d)", "^VIX9D": "VIX9D (9d)"}

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
def _close_series(h: pd.DataFrame) -> pd.Series:
    c = h["Close"]
    c = c.iloc[:, 0] if hasattr(c, "columns") else c
    return c.dropna()


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
        return {
            "last": round(last, 3),
            "prev_close": round(prev, 3) if prev else None,
            "chg": round(last - prev, 3) if prev else None,
            "chg_pct": round(100 * (last - prev) / prev, 2) if prev else None,
            "last_date": str(c.index[-1].date()),
            "last_range": [round(float(lo.iloc[-1]), 3), round(float(hi.iloc[-1]), 3)],
            "recent_closes": [round(float(x), 3) for x in c.iloc[-6:]],
        }
    except Exception as e:
        return {"error": repr(e)}


def pull_intraday_nq() -> dict:
    """Overnight/this-morning read for NQ: latest price, gap vs prior close,
    and overnight range. Best-effort; flags staleness if the market is shut."""
    try:
        h = yf.download("NQ=F", period="2d", interval="1m", progress=False, auto_adjust=False)
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
        prior_close = None
        if len(dates) >= 2:
            prior = c[(c.index.date == dates[-2]) & (c.index.time <= dt.time(16, 0))]
            prior_close = float(prior.iloc[-1]) if len(prior) else None
        on = h[h.index.date == dates[-1]]
        on_hi = float(on["High"].max().iloc[0]) if hasattr(on["High"].max(), "iloc") else float(on["High"].max())
        on_lo = float(on["Low"].min().iloc[0]) if hasattr(on["Low"].min(), "iloc") else float(on["Low"].min())
        return {
            "last_px": round(last_px, 2),
            "last_ts_et": last_ts.isoformat(),
            "prior_rth_close": round(prior_close, 2) if prior_close else None,
            "gap": round(last_px - prior_close, 2) if prior_close else None,
            "overnight_range": [round(on_lo, 2), round(on_hi, 2)],
        }
    except Exception as e:
        return {"error": repr(e)}


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
    Yield Curve CSV. [] on any failure (caller falls back to FRED)."""
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
            except Exception:
                continue
            vals = {}
            for ten, col in _TSY_COL.items():
                raw = (rec.get(col) or "").strip()
                if raw not in ("", "N/A"):
                    try:
                        vals[ten] = float(raw)
                    except ValueError:
                        pass
            if vals:
                rows.append((d, vals))
        rows.sort(key=lambda x: x[0], reverse=True)
        return rows
    except Exception:
        return []


def _fred_yields() -> dict:
    """{tenor: [(date, value), ...newest-first]} from FRED. {} on failure."""
    key = _fred_key()
    if not key:
        return {}
    out = {}
    for ten, sid in _FRED_SID.items():
        try:
            r = httpx.get("https://api.stlouisfed.org/fred/series/observations",
                          params={"series_id": sid, "api_key": key, "file_type": "json",
                                  "sort_order": "desc", "limit": 5}, timeout=20)
            obs = [(o["date"], float(o["value"])) for o in r.json().get("observations", [])
                   if o.get("value") not in (".", "", None)]
            if obs:
                out[ten] = obs
        except Exception:
            pass
    return out


def treasury_yields(today: dt.date) -> dict:
    """Most-current EOD Treasury yields with a source chain:
    US Treasury par-yield API -> FRED -> yfinance. Always carries the as-of date,
    source, and a staleness flag."""
    tenors, source, last_date = {}, None, None

    rows = _treasury_curve_rows(today.year)
    if len(rows) < 2 and today.month == 1:
        rows = (rows + _treasury_curve_rows(today.year - 1))
        rows.sort(key=lambda x: x[0], reverse=True)
    if rows:
        last_date, last_vals = rows[0]
        prev_vals = rows[1][1] if len(rows) > 1 else {}
        for ten in YIELD_TENORS:
            if ten in last_vals:
                p = prev_vals.get(ten)
                tenors[ten] = {"last": round(last_vals[ten], 3),
                               "prev": round(p, 3) if p is not None else None,
                               "chg": round(last_vals[ten] - p, 3) if p is not None else None}
        source = "US Treasury par yield curve"

    if not tenors:
        fred = _fred_yields()
        for ten, obs in fred.items():
            last = obs[0]
            prev = obs[1] if len(obs) > 1 else None
            tenors[ten] = {"last": round(last[1], 3),
                           "prev": round(prev[1], 3) if prev else None,
                           "chg": round(last[1] - prev[1], 3) if prev else None}
            d = dt.datetime.strptime(last[0], "%Y-%m-%d").date()
            last_date = max(last_date, d) if last_date else d
        if tenors:
            source = "FRED (DGS series)"

    if not tenors:
        for ten, sym in _YF_YIELD.items():
            d = pull_daily(sym)
            if "error" not in d:
                tenors[ten] = {"last": d.get("last"), "prev": d.get("prev_close"),
                               "chg": d.get("chg")}
                ld = dt.date.fromisoformat(d["last_date"])
                last_date = max(last_date, ld) if last_date else ld
        if tenors:
            source = "yfinance (^TNX/^TYX/^FVX proxy)"

    behind = _weekdays_behind(last_date, today) if last_date else None
    return {
        "mode": "EOD",
        "tenors": tenors,
        "source": source or "UNAVAILABLE",
        "last_date": str(last_date) if last_date else None,
        "as_of": str(last_date) if last_date else None,
        "weekdays_behind": behind,
        "stale": bool(behind and behind >= 1),
    }


def _cnbc_yields() -> dict:
    """LIVE benchmark UST yields from CNBC's quote webservice (the actual cash yield,
    not the CBOE index). {tenor: {last, chg, time, status}} or {} on failure."""
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
                continue
            try:
                chg = round(float(d.get("change")), 3)
            except (TypeError, ValueError):
                chg = None
            out[ten] = {"last": round(last, 3), "chg": chg,
                        "time": d.get("last_time"), "status": d.get("curmktstatus")}
        return out
    except Exception:
        return {}


def _yahoo_chart_yields() -> dict:
    """LIVE-ish fallback via Yahoo's v8 chart endpoint for the CBOE yield indices.
    No 2Y. {tenor: {last, chg}} or {} on failure."""
    out = {}
    for sym, ten in [("%5EFVX", "5Y"), ("%5ETNX", "10Y"), ("%5ETYX", "30Y")]:
        try:
            r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                          params={"range": "1d", "interval": "1m", "includePrePost": "true"},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            m = r.json()["chart"]["result"][0]["meta"]
            px = m.get("regularMarketPrice")
            pc = m.get("chartPreviousClose") or m.get("previousClose")
            if px is not None:
                out[ten] = {"last": round(px, 3),
                            "chg": round(px - pc, 3) if pc else None}
        except Exception:
            pass
    return out


def live_yields(today: dt.date) -> dict:
    """Most-current LIVE yields: CNBC benchmark UST -> Yahoo intraday index. {} if
    both fail (caller falls back to the EOD par-curve chain). `chg` = move vs prior close."""
    c = _cnbc_yields()
    if c:
        times = [v["time"] for v in c.values() if v.get("time")]
        as_of = max(times) if times else None
        as_of_date = as_of[:10] if as_of else None
        status = next((v.get("status") for v in c.values() if v.get("status")), None)
        return {
            "mode": "LIVE",
            "source": "CNBC (UST benchmark, intraday)",
            "tenors": {t: {"last": v["last"], "chg": v["chg"]} for t, v in c.items()},
            "as_of": as_of, "last_date": as_of_date, "market_status": status,
            "stale": bool(as_of_date and as_of_date < str(today)),
        }
    y = _yahoo_chart_yields()
    if y:
        return {
            "mode": "LIVE",
            "source": "Yahoo ^TNX/^TYX/^FVX (intraday)",
            "tenors": y, "as_of": None, "last_date": str(today),
            "market_status": None, "stale": False,
        }
    return {}


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
                continue
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
    except Exception:
        return {}


def _yahoo_vol() -> dict:
    """Fallback live-ish vol via Yahoo v8 chart. {'^VIX': {...}, '^VIX9D': {...}} or {}."""
    out = {}
    for sym, key in [("%5EVIX", "^VIX"), ("%5EVIX9D", "^VIX9D")]:
        try:
            r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                          params={"range": "5d", "interval": "1d", "includePrePost": "true"},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            res = r.json()["chart"]["result"][0]
            px = res["meta"].get("regularMarketPrice")
            closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                      if c is not None]
            prev = closes[-2] if len(closes) >= 2 else None
            if px is not None:
                out[key] = {"last": round(px, 2), "chg": round(px - prev, 2) if prev else None,
                            "last_date": None, "source": "Yahoo(intraday)", "label": VOL_TICKERS[key]}
        except Exception:
            pass
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
    cy, yy = _cnbc_yields(), _yahoo_chart_yields()
    cv, yv = _cnbc_vol(), _yahoo_vol()
    return {
        "yields": _compare_sources({k: v["last"] for k, v in cy.items()},
                                   {k: v["last"] for k, v in yy.items()}, tol=0.03),
        "vol": _compare_sources({k: v["last"] for k, v in cv.items()},
                                {k: v["last"] for k, v in yv.items()}, tol=0.30),
    }


def macro_pulse(today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now().date()
    live = live_yields(today)
    eod = treasury_yields(today)
    out = {"yields": live if live.get("tenors") else eod, "yields_eod_close": eod}
    vol = _cnbc_vol() or _yahoo_vol()
    for t, lbl in VOL_TICKERS.items():
        d = vol.get(t)
        if not d or d.get("last") is None:
            d = pull_daily(t)
            d["label"] = lbl
            d["source"] = "yfinance(daily)"
        out[t] = d
    try:
        v, v9 = out["^VIX"]["last"], out["^VIX9D"]["last"]
        out["term_structure"] = {
            "vix": v, "vix9d": v9, "spread_9d_minus_30d": round(v9 - v, 2),
            "state": ("BACKWARDATION (9d > 30d — acute near-term fear)" if v9 > v
                      else "CONTANGO (9d < 30d — normal/calm near-term)"),
        }
    except Exception:
        pass
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
    rows = []
    for t, lbl in {**SECTOR_ETFS, **THEME_ETFS}.items():
        d = pull_daily(t)
        if "error" in d or d.get("chg_pct") is None:
            continue
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


def _chains_parallel(symbol: str, expirations, workers: int = 8) -> dict:
    """Fetch every expiration's chain once, concurrently. {expiration: [options]}."""
    out: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_chain, symbol, e): e for e in expirations}
        for fut, e in futs.items():
            try:
                out[e] = fut.result()
            except Exception:
                out[e] = []
    return out


def _zero_gamma_level(strike_totals: dict, significance_threshold_pct: float = 0.01):
    """Zero-gamma (gamma-flip) level: the first per-strike net-gamma sign flip among
    SIGNIFICANT strikes (deep-OTM noise below 1% of the peak is ignored), linearly
    interpolated between the two bracketing strikes. This is the method the recorded
    (Discount Bloomberg) GEX used — kept identical so the level stays continuous with
    that history, and (unlike a cumulative-sum cross) it returns a real level in a
    one-sided negative-gamma tape instead of None."""
    if not strike_totals:
        return None
    max_abs = max(abs(v) for v in strike_totals.values())
    if max_abs == 0.0:
        return None
    threshold = max_abs * significance_threshold_pct
    significant = {k: v for k, v in strike_totals.items() if abs(v) >= threshold}
    if len(significant) < 2:
        return None
    prev_strike, prev_net = None, 0.0
    for k in sorted(significant):
        net = significant[k]
        if prev_strike is not None and ((prev_net < 0.0 <= net) or (prev_net > 0.0 >= net)):
            if net == prev_net:
                return k
            ratio = max(0.0, min(1.0, -prev_net / (net - prev_net)))
            return prev_strike + ratio * (k - prev_strike)
        prev_strike, prev_net = k, net
    return None


def _gex_from_options(options, spot: float) -> dict:
    """Aggregate net dealer GEX by strike over a flat list of option rows."""
    by_strike: dict[float, float] = {}
    call_total = put_total = 0.0
    n = 0
    for o in options:
        g = (o.get("greeks") or {}).get("gamma")
        oi, strike, otype = o.get("open_interest"), o.get("strike"), o.get("option_type")
        if g is None or oi is None or strike is None or otype not in ("call", "put"):
            continue
        gex = float(g) * float(oi) * 100.0 * (spot ** 2) * 0.01
        by_strike[strike] = by_strike.get(strike, 0.0) + (gex if otype == "call" else -gex)
        if otype == "call":
            call_total += gex
        else:
            put_total += gex
        n += 1
    total = sum(by_strike.values())
    flip = _zero_gamma_level(by_strike)
    top = sorted(by_strike.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
    return {
        "total_gex": total,
        "call_gex": call_total,
        "put_gex": -put_total,
        "regime": "positive" if total > 0 else "negative",
        "flip": round(flip, 2) if flip is not None else None,
        "call_wall": max(by_strike, key=by_strike.get) if by_strike else None,
        "put_wall": min(by_strike, key=by_strike.get) if by_strike else None,
        "top_walls": [round(float(k), 1) for k, _ in top],
        "n_contracts": n,
    }


def gex_tradier(today: dt.date) -> dict:
    """Front-dated + full-chain GEX for SPY+QQQ from the live Tradier chain.
    NOTE: pre-market this reflects the PRIOR session's IV/greeks (options don't
    trade overnight) — the brief labels it as such."""
    out = {}
    for sym in GEX_SYMBOLS:
        try:
            q = _quote(sym)
            spot = float(q.get("last") or q.get("close"))
            exps = sorted(_expirations(sym))
            if not exps:
                out[sym] = {"error": "no expirations"}
                continue
            chains = _chains_parallel(sym, exps)             # fetch once, reuse for both views
            near_exps = exps[:GEX_NEAR_EXPIRATIONS]
            near_opts = [o for e in near_exps for o in chains.get(e, [])]
            full_opts = [o for e in exps for o in chains.get(e, [])]
            res = {
                "spot": round(spot, 2),
                "near_term": _gex_from_options(near_opts, spot)
                             | {"expirations": near_exps, "n_expirations": len(near_exps)},
                "full_chain": _gex_from_options(full_opts, spot)
                              | {"n_expirations": len(exps), "last_expiration": exps[-1]},
            }
            out[sym] = res
        except Exception as e:
            out[sym] = {"error": repr(e)}
    return out


def event_gate(today: dt.date, horizon_days: int = 21) -> dict:
    if not T1_CALENDAR.exists():
        return {"error": "no t1 calendar"}
    cal = yaml.safe_load(T1_CALENDAR.read_text(encoding="utf-8"))
    events = []
    for e in cal.get("events", []):
        d = e["date"] if isinstance(e["date"], dt.date) else dt.date.fromisoformat(str(e["date"]))
        delta = (d - today).days
        if 0 <= delta <= horizon_days:
            events.append({"name": e["name"], "date": str(d), "time_et": e.get("time_et"),
                           "days_away": delta, "verified": e.get("verified")})
    events.sort(key=lambda x: x["days_away"])
    return {
        "today_is_t1": any(e["days_away"] == 0 for e in events),
        "today_events": [e for e in events if e["days_away"] == 0],
        "upcoming": events,
    }


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
                "dt": (dt.datetime.fromtimestamp(x["datetime"], dt.timezone.utc).isoformat()
                       if x.get("datetime") else None),
                "url": x.get("url", ""),
                "trusted": (x.get("source", "").lower() in TRUSTED_SOURCES),
            })
        return rows

    try:
        r = httpx.get("https://finnhub.io/api/v1/news",
                      params={"category": "general", "token": key}, timeout=25)
        out["status"] = r.status_code
        items = clean(r.json()) if r.status_code == 200 else []
        items.sort(key=lambda x: x["dt"] or "", reverse=True)
        items.sort(key=lambda x: not x["trusted"])  # stable: trusted block first
        out["general"] = items[:14]
        themed = [x for x in items if any(k in x["headline"].lower() for k in THEME_KEYWORDS)]
        out["themed"] = themed[:12]
    except Exception as e:
        out["error"] = repr(e)
    return out


# --------------------------------------------------------------------------- #
# Assemble + render
# --------------------------------------------------------------------------- #
def gather(today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now().date()
    _et = pd.Timestamp.now(tz="America/New_York")
    return {
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
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


def _bn(x) -> str:
    """Human-scale a raw GEX figure (they run to the billions) with a sign."""
    try:
        return f"{x / 1e9:+.2f}bn"
    except Exception:
        return "na"


def render_md(d: dict) -> str:
    """A compact, human-readable dump of the data block (what Claude reads)."""
    L = [f"# BRIEF DATA BLOCK — {d['date']}",
         f"Data pulled: **{d.get('as_of_et', d['as_of'])}**  "
         f"(put this freshness stamp in the brief so readers know it's live)", ""]

    eg = d["event_gate"]
    L.append("## Event gate")
    L.append(f"- today_is_t1: **{eg.get('today_is_t1')}**")
    for e in eg.get("upcoming", [])[:6]:
        L.append(f"  - +{e['days_away']}d  {e['date']}  {e['name']} {e.get('time_et') or ''}"
                 f"{'' if e.get('verified') else '  (unverified)'}")

    L.append("\n## Index recap (daily close-to-close)")
    ir = d["index_recap"]
    for k in ["ES", "YM", "NQ", "RTY", "CL=F"]:
        v = ir.get(k, {})
        if "error" in v:
            L.append(f"- {k}: ERROR {v['error']}"); continue
        L.append(f"- {v.get('label', k)} ({k}): {v.get('last')}  "
                 f"{v.get('chg_pct')}%  range {v.get('last_range')}  [{v.get('last_date')}]")
    L.append(f"- divergence %: {ir.get('divergence_pct')}")
    ni = ir.get("nq_intraday", {})
    if "error" not in ni:
        L.append(f"- NQ intraday: last {ni.get('last_px')} @ {ni.get('last_ts_et')}; "
                 f"gap vs prior close {ni.get('gap')}; overnight range {ni.get('overnight_range')}")

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
            L.append(f"  (prior official close [{eod.get('last_date')}] via "
                     f"{eod.get('source')}: 10Y {e10.get('last')}%)")
    else:
        L.append(f"- Treasury yields: UNAVAILABLE (source chain failed: {y.get('source')})")
    for t in ["^VIX", "^VIX9D"]:
        v = mp.get(t, {})
        if "error" not in v:
            L.append(f"- {v.get('label')} ({t}): {v.get('last')}  chg {v.get('chg')}  "
                     f"[{v.get('last_date')}] ({v.get('source', '?')})")
    ts = mp.get("term_structure")
    if ts:
        L.append(f"- TERM STRUCTURE: VIX9D {ts['vix9d']} vs VIX {ts['vix']} "
                 f"(spread {ts['spread_9d_minus_30d']}) -> {ts['state']}")

    cc = d.get("source_crosscheck", {})
    if cc:
        L.append("\n## Source cross-check (CNBC primary vs Yahoo v8 fallback)")
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
        L.append(f"- {sym}: spot {v['spot']}")
        L.append(f"    near-term ({nt['n_expirations']} exp {nt.get('expirations')}): "
                 f"regime **{nt['regime']}**, total {_bn(nt['total_gex'])}, "
                 f"zero-gamma flip {nt['flip']}, call wall {nt['call_wall']}, put wall {nt['put_wall']}, "
                 f"walls {nt['top_walls']}")
        L.append(f"    full-chain ({fc['n_expirations']} exp, thru {fc.get('last_expiration')}): "
                 f"regime **{fc['regime']}**, total {_bn(fc['total_gex'])}, "
                 f"zero-gamma flip {fc['flip']}, call wall {fc['call_wall']}, put wall {fc['put_wall']}")

    L.append("\n## Finnhub headlines")
    fh = d["finnhub"]
    L.append(f"- status: {fh.get('status')}")
    L.append("- GENERAL (trusted-first):")
    for x in fh.get("general", [])[:12]:
        tag = "★" if x["trusted"] else " "
        L.append(f"  {tag} [{x['source']}] {x['headline']}  ({x['dt']})")
    L.append("- THEMED:")
    for x in fh.get("themed", [])[:10]:
        L.append(f"  • [{x['source']}] {x['headline']}")
    return "\n".join(L)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    d = gather()
    (DATA_DIR / f"{d['date']}.json").write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
    md = render_md(d)
    (DATA_DIR / f"{d['date']}.data.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n[gather] wrote data/{d['date']}.json + data/{d['date']}.data.md ; "
          f"the finished brief goes to briefs/{d['date']}.md")


if __name__ == "__main__":
    main()
