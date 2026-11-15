"""Regression tests for the zero-gamma level in `_zero_gamma_level`.

Run: .venv/Scripts/python.exe tests/test_gamma_flip.py     (no pytest dependency)

Why these exist. Until 2026-09-29 the flip was the FIRST sign change in the net-gamma-
by-strike table, scanning up from the lowest strike. Per-strike net gamma flickers sign
all through the put-heavy zone, so "first" was usually a noise strike. That morning's
QQQ block printed a flip of 720.8 with spot 736.5 and total gamma NEGATIVE: a flip 16
points below spot in a short-gamma tape, which the regime itself contradicts. The flip
is now the zero crossing of total GEX re-priced across hypothetical spots.

The fixture is that morning's real QQQ near-term chain (4 expirations), trimmed to
the fields the GEX code reads.
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gather as G  # noqa: E402

NOW = pd.Timestamp("2026-09-29 08:36", tz="America/New_York")
EXP = "2026-10-02"

failures = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        failures.append(name)


def contract(strike, otype, oi, iv=0.20, gamma=0.01, exp=EXP):
    return {"strike": strike, "option_type": otype, "open_interest": oi,
            "expiration_date": exp, "greeks": {"gamma": gamma, "smv_vol": iv}}


# 1. The real chain that exposed the bug. The flip must sit ABOVE spot (short gamma at
#    spot means the zero crossing is overhead) and near it, not at the 720.8 noise strike.
fx = json.loads((Path(__file__).parent / "fixtures" / "qqq-2026-09-29-near.json").read_text())
r = G._gex_from_options(fx["options"], fx["spot"], NOW)
check("QQQ 2026-09-29: negative regime, flip above spot and within 1%",
      r["regime"] == "negative" and r["flip"] is not None
      and fx["spot"] < r["flip"] < fx["spot"] * 1.01,
      f"spot {fx['spot']}  regime {r['regime']}  flip {r['flip']}  crossings {r['flip_crossings']}")
check("QQQ 2026-09-29: flip is nowhere near the old 720.8",
      r["flip"] is not None and abs(r["flip"] - 720.81) > 10, f"flip {r['flip']}")

# 2. The trap in miniature: put wall under spot, call wall over it, and one stray
#    call-heavy strike far below. The old method returned the stray strike's crossing.
SPOT = 700.0
trap = ([contract(640, "call", 5_000)]                      # the noise strike
        + [contract(k, "put", 20_000) for k in (650, 670, 690)]
        + [contract(k, "call", 20_000) for k in (710, 730, 750)])
flip, crossings = G._zero_gamma_level(
    [(o["strike"], 1 if o["option_type"] == "call" else -1, o["open_interest"],
      o["greeks"]["smv_vol"], o["expiration_date"]) for o in trap], SPOT, NOW)
check("stray low strike does not capture the flip; it lands between the walls",
      flip is not None and 690 < flip < 710, f"flip {flip}  crossings {crossings}")

# 3. Symmetric book: equal puts and calls mirrored around spot put the flip at spot.
sym = [contract(SPOT - d, "put", 10_000) for d in (10, 20)] + \
      [contract(SPOT + d, "call", 10_000) for d in (10, 20)]
r = G._gex_from_options(sym, SPOT, NOW)
check("mirrored book puts the flip at spot (±0.5%)",
      r["flip"] is not None and abs(r["flip"] - SPOT) < SPOT * 0.005, f"flip {r['flip']}")

# 4. One-sided tape: puts only. Gamma is negative everywhere, so there is no flip in the
#    window, and that must come back as None, not as a made-up level.
r = G._gex_from_options([contract(k, "put", 10_000) for k in (680, 690, 700, 710)], SPOT, NOW)
check("puts-only book reports no flip rather than inventing one",
      r["flip"] is None and r["flip_crossings"] == [] and r["regime"] == "negative",
      f"flip {r['flip']}  text '{G._flip_text(r)}'")

# 5. An expired-today contract after the close must not divide by zero.
late = pd.Timestamp("2026-10-02 17:30", tz="America/New_York")
try:
    G._zero_gamma_level([(700.0, 1, 1000.0, 0.2, EXP), (690.0, -1, 1000.0, 0.2, EXP)], SPOT, late)
    check("contract past expiry is floored, not a crash", True)
except Exception as e:
    check("contract past expiry is floored, not a crash", False, repr(e))

print(f"\n{6 - len(failures)}/6 passed")
if failures:
    for f in failures:
        print(f"  FAILED: {f}")
    sys.exit(1)
