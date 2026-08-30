"""Regression tests for the pre-post stale-block check in `post_discord.stale_check`.

Run: .venv/Scripts/python.exe tests/test_stale_check.py     (no pytest dependency)

Why these exist. playbook.md step 4 requires checking the clock against today's release
times before posting, because writing the brief takes real time and a scheduled print can
land inside that gap — publishing an 🚨 Event gate that advertises as *upcoming* something
every reader already knows, over futures/yields that are pre-print.

That check was the last timestamp in the pipeline still done by hand, and on this machine
every shell route to ET is wrong: local is CDT, `date -u` is UTC, and `TZ=America/New_York
date` ignores the TZ and prints UTC labelled GMT. On 2026-08-25 that produced a reading of
12:42 PM ET at 8:44 AM ET, and a needless `gather.py` re-run against a block that was never
stale — with the correct tz-aware `as_of_et` (8:43 AM ET) sitting in the open data block.
Same root cause as the naive-LOCAL archive-name bug fixed 2026-08-04 (9:31 AM ET filed as
-0831).

The guard has the same two opposed failure modes as the GEX one: stay quiet through a
genuinely stale block, or nag on a fine one until it gets ignored. Cases 2 and 5 pin the
quiet side; 1, 3 and 4 pin the loud side. Case 6 pins the rule that it must never break a
post — it is a warning path, not a gate.
"""
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import post_discord as P  # noqa: E402

ET = ZoneInfo("America/New_York")
_passed = _failed = 0


def _run(now: datetime, stamp_text: str) -> tuple[bool, str]:
    """Call stale_check with `now` pinned, capturing what it printed."""
    real = P._now_et
    P._now_et = lambda: now
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fired = P.stale_check(f"# x\n*Data pulled {stamp_text}*\n\nbody\n")
    finally:
        P._now_et = real
    return fired, buf.getvalue()


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"[PASS] {label}")
    else:
        _failed += 1
        print(f"[FAIL] {label}")
    if detail:
        print(f"        {detail}")


# 1. A calendar release inside the gap must be named. 2026-07-14 was a CPI morning.
fired, out = _run(datetime(2026, 7, 14, 10, 5, tzinfo=ET), "Tue Jul 14, 2026 6:00 AM ET")
check("CPI at 08:30 inside the gap is named",
      fired and "08:30  CPI" in out and "calendar releases crossed" in out,
      f"fired={fired}")

# 2. The exact 2026-08-25 situation, done right: an 8:43 stamp read at 8:44 crosses
#    nothing. This is the case the shell clock got wrong by four hours.
fired, out = _run(datetime(2026, 8, 25, 8, 44, tzinfo=ET), "Tue Aug 25, 2026 8:43 AM ET")
check("fresh block one minute later stays silent", not fired, f"fired={fired}")

# 3. ...and the same block an hour later HAS crossed the 09:00 and 09:30 marks.
fired, out = _run(datetime(2026, 8, 25, 9, 40, tzinfo=ET), "Tue Aug 25, 2026 8:43 AM ET")
check("same block at 9:40 flags the 09:00 window and the open",
      fired and "09:00" in out and "MARKET OPEN" in out, f"fired={fired}")

# 4. Chronological order — the 09:30 open must not print after 10:00.
fired, out = _run(datetime(2026, 7, 14, 10, 5, tzinfo=ET), "Tue Jul 14, 2026 6:00 AM ET")
times = [ln.strip().split()[1] for ln in out.splitlines() if ln.strip().startswith("- ")]
check("crossed windows print in time order", times == sorted(times), f"{times}")

# 5. A brief for another date is a backfill or a re-post — never nag.
fired, _ = _run(datetime(2026, 8, 25, 15, 0, tzinfo=ET), "Mon Jul 13, 2026 6:00 AM ET")
check("a brief stamped another day stays silent", not fired, f"fired={fired}")

# 6. Never break a post: an unreadable stamp, a future stamp and a missing data file all
#    return cleanly rather than raising.
try:
    P._now_et, real = (lambda: datetime(2026, 8, 25, 9, 0, tzinfo=ET)), P._now_et
    with redirect_stdout(io.StringIO()):
        a = P.stale_check("# x\n\nno stamp at all\n")
        b = P.stale_check("# x\n*Data pulled Tue Aug 25, 2026 11:00 PM ET*\n\nbody\n")
        c = P._calendar_events_today(datetime(1999, 1, 1).date())
    P._now_et = real
    check("degraded inputs return quietly instead of raising",
          a is False and b is False and c == [], f"nostamp={a} future={b} missing={c}")
except Exception as e:  # pragma: no cover
    check("degraded inputs return quietly instead of raising", False, f"raised {e!r}")

print(f"\n{_passed}/{_passed + _failed} passed")
sys.exit(1 if _failed else 0)
