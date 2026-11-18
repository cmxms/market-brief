"""Regression tests for the incomplete-chain guard in `_gex_from_options`.

Run: .venv/Scripts/python.exe tests/test_gex_guard.py     (no pytest dependency)

Why these exist. The guard is an alarm that gates whether the brief may publish a
dealer-gamma regime call, so it has two ways to fail and they pull in opposite
directions: stay quiet through a real degradation, or cry wolf and suppress a good
read. It did the second — from 2026-08-04 to 08-06 it blocked the gamma read on
three consecutive mornings because the trigger was `max(dropped_pct, dropped_oi_pct)`
and the *count* tripped on newly-listed strikes.

The fact that settles it: Tradier omits the `greeks` block entirely for freshly
listed strikes, and those strikes also carry ZERO open interest. Open interest is
the multiplier in the GEX formula, so they contribute exactly 0.0 — dropping them
cannot understate anything. Case 5 asserts that directly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gather as G

SPOT = 700.0


def contract(strike, otype="call", gamma=0.01, oi=1000):
    """A Tradier-shaped option row. gamma=None omits the greeks block, matching how
    Tradier actually returns a never-traded strike (not a null gamma inside it)."""
    o = {"strike": strike, "option_type": otype, "open_interest": oi}
    if gamma is not None:
        o["greeks"] = {"gamma": gamma}
    return o


HEALTHY = [contract(690 + i, oi=1000) for i in range(40)]
FRESH_LISTINGS = [contract(600 + i, gamma=None, oi=0) for i in range(27)]

failures = []


def check(name, opts, expect_warn):
    r = G._gex_from_options(opts, SPOT)
    warned = r["data_warning"] is not None
    ok = warned == expect_warn
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"        count {r['dropped_pct']}%  OI-weighted {r['dropped_oi_pct']}%  "
          f"oi_unknown {r['n_dropped_oi_unknown']}  warned={warned}")
    if not ok:
        failures.append(name)
    return r


# 1. The false positive that caused the incident: a burst of newly-listed strikes.
#    40% of contracts dropped, 0% of open interest. Must stay SILENT.
check("newly-listed strikes (no greeks, OI==0) do not trip the alarm",
      HEALTHY + FRESH_LISTINGS, expect_warn=False)

# 2. The failure the guard exists to catch: greeks degrade on contracts that hold
#    real open interest, so GEX really is understated. Must FIRE.
check("greeks lost on contracts holding real OI does trip the alarm",
      HEALTHY + [contract(700 + i, gamma=None, oi=1000) for i in range(12)],
      expect_warn=True)

# 3. Open interest absent entirely -> the loss is UNBOUNDED, not zero. The old code
#    did float(oi or 0) and booked it as harmless. Must FIRE despite dropped_oi_pct 0.0.
check("absent open interest trips the alarm even though dropped_oi_pct reads 0.0%",
      HEALTHY + [contract(700 + i, gamma=None, oi=None) for i in range(3)],
      expect_warn=True)

# 4. Clean chain stays silent.
check("clean chain stays silent", HEALTHY, expect_warn=False)

# 5. The claim underneath the whole retune: zero-OI drops cannot move the number.
with_drops = G._gex_from_options(HEALTHY + FRESH_LISTINGS, SPOT)
without = G._gex_from_options(HEALTHY, SPOT)
identical = (with_drops["total_gex"] == without["total_gex"]
             and with_drops["flip"] == without["flip"]
             and with_drops["call_wall"] == without["call_wall"]
             and with_drops["put_wall"] == without["put_wall"])
print(f"[{'PASS' if identical else 'FAIL'}] zero-OI drops leave GEX/flip/walls bit-identical")
print(f"        with drops {with_drops['total_gex']:.6e} | without {without['total_gex']:.6e}")
if not identical:
    failures.append("zero-OI drops leave GEX/flip/walls bit-identical")

print(f"\n{5 - len(failures)}/5 passed")
if failures:
    for f in failures:
        print(f"  FAILED: {f}")
    if __name__ == "__main__":
        sys.exit(1)
