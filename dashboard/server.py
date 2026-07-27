"""Brief Live — the morning brief, as a dashboard you can leave open.

Reuses gather.py wholesale: every number here comes from the same functions that
build the daily brief, so the board and the brief can never disagree.

Design intent (deliberate, do not "improve" into a wall of numbers):
  - This is ORIENTATION, not signals. It answers "what's the weather and did
    anything break", never "what should I buy". A signals slot is reserved but
    intentionally empty until something earns it.
  - Calm by default. Loud only when a regime flips or a feed dies.

Data sources + why:
  - index/rotation/macro/events -> gather.py functions, polled on their own cadence.
  - GEX -> read from the Discount Bloomberg recorder's gex_log.csv, NOT live Tradier.
    gex_tradier() pulls ~35 expirations per symbol; far too heavy to poll. The recorder
    already writes spot/flip/regime/walls every 15 min during RTH for free. Reading it
    also means a dead recorder surfaces on the health panel instead of silently rotting
    (it went dark 7/14->7/16 and nothing noticed).

Run:  .venv/Scripts/python.exe dashboard/server.py   (then open http://localhost:8800)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # import gather.py from the repo root

import gather  # noqa: E402

STATIC = HERE / "static"
PORT = int(os.environ.get("BRIEF_DASH_PORT", "8800"))

# The DB recorder's GEX log. Override with BRIEF_DASH_GEX_LOG if the repo moves.
GEX_LOG = Path(os.environ.get(
    "BRIEF_DASH_GEX_LOG",
    r"C:\Users\chadm\OneDrive\Desktop\Project Analysis\Discount Bloomberg\state\gex\gex_log.csv",
))

ET = "America/New_York"

# section -> seconds between polls, tuned to how fast each SOURCE actually moves.
# Faster than the source changes = burnt API calls and false precision on screen.
CADENCE = {
    # Yahoo's CME feed is ~10 min delayed by license, but it is a CONTINUOUS stream —
    # polling often still surfaces the delayed bars sooner, so keep this tight-ish.
    # The panel stamps the real lag; do not let the refresh imply liveness.
    "index_recap": 90,
    # 5-day relative returns. Minutes of lag are immaterial and 14 ETF pulls are not
    # free — 2 min was wasteful for a number that barely moves within an hour.
    "sector_rotation": 300,
    # CNBC yields/VIX are genuinely LIVE (as_of == now), so this is the one place
    # where polling faster buys real freshness.
    "macro_pulse": 60,
    # Local file read, but the recorder only writes every 15 min — 30s was just
    # re-reading an unchanged file.
    "gex": 60,
    "event_gate": 1800,
    "finnhub": 600,     # free tier lags 20-60min anyway
    "brief": 300,       # a local file that changes at most a few times a day
}

_STATE: dict = {"sections": {}, "meta": {}}
_LOCK = threading.Lock()


def _now_et() -> pd.Timestamp:
    return pd.Timestamp.now(tz=ET)


def _in_rth(ts: pd.Timestamp | None = None) -> bool:
    ts = ts or _now_et()
    if ts.weekday() >= 5:
        return False
    return ts.replace(hour=9, minute=30, second=0) <= ts < ts.replace(hour=16, minute=0, second=0)


def read_gex() -> dict:
    """Latest QQQ/SPY snapshot from the recorder's log + how stale it is."""
    if not GEX_LOG.exists():
        return {"error": "gex_log.csv not found", "path": str(GEX_LOG)}
    df = pd.read_csv(GEX_LOG)
    df = df[df["snapshot_at_utc"] != "snapshot_at_utc"]  # guard against a re-written header
    df["ts"] = pd.to_datetime(df["snapshot_at_utc"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"])
    if df.empty:
        return {"error": "gex_log.csv is empty"}

    out: dict = {"symbols": {}}
    for sym in ("SPY", "QQQ"):
        s = df[df["symbol"] == sym]
        if s.empty:
            continue
        r = s.sort_values("ts").iloc[-1]
        spot, flip = float(r["spot"]), float(r["zero_gamma_level"])
        walls = []
        for i in range(1, 6):
            k, v = f"wall_{i}_strike", f"wall_{i}_gex"
            if k in r and pd.notna(r[k]):
                walls.append({"strike": float(r[k]), "gex": float(r[v])})
        out["symbols"][sym] = {
            "spot": spot,
            "flip": flip,
            "dist_to_flip": round(spot - flip, 2),
            "above_flip": bool(spot > flip),
            "regime": str(r["regime"]),
            "total_gex": float(r["total_gex"]),
            "walls": walls,
            "snapshot_et": r["ts"].tz_convert(ET).strftime("%H:%M:%S"),
        }
    age = (datetime.now(timezone.utc) - df["ts"].max().to_pydatetime()).total_seconds() / 60
    out["age_min"] = round(age, 1)
    out["last_snapshot_et"] = df["ts"].max().tz_convert(ET).strftime("%a %b %d %H:%M ET")
    # During RTH the recorder writes every 15 min; >20 means it is falling behind.
    out["stale"] = bool(_in_rth() and age > 20)
    return out


import re  # noqa: E402

BRIEFS_DIR = HERE.parent / "briefs"


def read_brief() -> dict:
    """Latest brief's title + freshness stamp + TL;DR, for the AI-analysis panel.

    This is the MORNING brief, written once in-session — not the continuously-updated
    intraday analysis the panel ultimately wants. Surfaced as-is with its own date so
    the panel can label it honestly ('this morning', not 'live'). Inline markdown is
    stripped so the board renders clean text, not asterisks.
    """
    files = sorted(BRIEFS_DIR.glob("[0-9]*.md"))
    files = [f for f in files if "update" not in f.stem]  # skip mid-day update stubs
    if not files:
        return {"error": "no brief files found", "path": str(BRIEFS_DIR)}
    f = files[-1]
    text = f.read_text(encoding="utf-8")

    def strip_md(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)   # bold
        s = re.sub(r"\*(.+?)\*", r"\1", s)        # italic
        return s.strip()

    title = tldr = stamp = ""
    for line in text.splitlines():
        ls = line.strip()
        if ls.startswith("# ") and not title:
            title = strip_md(ls.lstrip("# ").strip())
        elif ls.startswith("*Data pulled") and not stamp:
            stamp = ls.strip("*").strip()
        elif "TL;DR" in ls and not tldr:
            # Strip the whole "**TL;DR:**" label (asterisks included) BEFORE strip_md, else
            # the label's trailing ** orphans and mispairs with the first **bold** span.
            body = re.sub(r"\*{0,2}TL;DR:?\*{0,2}\s*", "", ls, count=1)
            tldr = strip_md(body)
    return {
        "title": title,
        "stamp": stamp,
        "tldr": tldr,
        "brief_date": f.stem,
    }


POLLERS = {
    "index_recap": gather.index_recap,
    "sector_rotation": gather.sector_rotation,
    "macro_pulse": gather.macro_pulse,
    "gex": read_gex,
    "event_gate": lambda: gather.event_gate(datetime.now().date()),
    "finnhub": lambda: gather.finnhub_news(gather._finnhub_key()),
    "brief": read_brief,
}


def _poll_once(name: str) -> None:
    try:
        data = POLLERS[name]()
        with _LOCK:
            _STATE["sections"][name] = data
            _STATE["meta"][name] = {"ok": True, "at": _now_et().isoformat(), "error": None}
    except Exception as e:
        traceback.print_exc()
        with _LOCK:
            _STATE["meta"][name] = {
                "ok": False, "at": _now_et().isoformat(), "error": f"{type(e).__name__}: {e}"
            }


def _poll_loop() -> None:
    last: dict[str, float] = {}
    while True:
        now = time.monotonic()
        for name, every in CADENCE.items():
            if now - last.get(name, -1e9) >= every:
                _poll_once(name)
                last[name] = time.monotonic()
        with _LOCK:
            _STATE["as_of_et"] = _now_et().strftime("%a %b %d, %Y %I:%M:%S %p ET").replace(" 0", " ")
            _STATE["in_rth"] = _in_rth()
        time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/state":
            with _LOCK:
                body = json.dumps(_STATE, default=str).encode()
            return self._send(200, body, "application/json")

        rel = "index.html" if path == "/" else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}.get(f.suffix, "text/plain")
        return self._send(200, f.read_bytes(), f"{ctype}; charset=utf-8")


def main() -> None:
    print(f"Brief Live — priming sections (first pull takes ~30s)...")
    for name in ("brief", "gex", "index_recap", "macro_pulse", "sector_rotation", "event_gate"):
        _poll_once(name)
        ok = _STATE["meta"].get(name, {}).get("ok")
        print(f"  {name:16s} {'ok' if ok else 'FAILED — ' + str(_STATE['meta'].get(name, {}).get('error'))}")
    threading.Thread(target=_poll_loop, daemon=True).start()
    print(f"\n  ->  http://localhost:{PORT}\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
