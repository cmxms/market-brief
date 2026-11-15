"""roll_guard: a continuous-contract move that is really a contract change."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from gather import roll_guard


def test_2026_09_15_nq_roll_is_caught():
    # daily: prev 29152.25 (Sep), last 29413.75 (Dec). 1m: prior 29456.25, last 29414.5 (Dec).
    r = roll_guard(29413.75, 29152.25, 29456.25, 29414.5)
    assert r and r["prev_close"] == 29456.25 and r["gap_pct"] > 0.9


def test_normal_settle_drift_is_left_alone():
    # 4pm->5pm drift of a few points is not a roll
    assert roll_guard(7680.0, 7625.0, 7627.5, 7680.25) is None


def test_no_intraday_means_no_adjustment():
    assert roll_guard(7680.0, 7625.0, None, None) is None


def test_intraday_on_a_different_contract_is_not_trusted():
    # 1m series disagrees with the daily last -> nothing trustworthy to substitute
    assert roll_guard(29413.75, 29152.25, 29200.0, 29130.0) is None
