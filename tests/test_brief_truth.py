"""Offline truth gates: real validation/delivery, isolated payloads and fake transports."""
import datetime as dt
import json
import re
import sys
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from test_compose_env import BODY

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import deliver as D
import post_discord as P
import run_brief as R
import validate as V

DATE = "2026-11-19"
STAMP = "Thu Nov 19, 2026 8:35 AM ET"
ET = ZoneInfo("America/New_York")
DISPLAY = "Thu November 19, 2026"
BRIEF = BODY.replace("Sat November 14, 2026", DISPLAY).replace(
    "Sat Nov 14, 2026 8:00 AM ET", STAMP)


def healthy_gex():
    return {s: {v: {"regime": "positive", "data_warning": None}
                for v in ("near_term", "full_chain")} for s in ("SPY", "QQQ")}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "alert_failure", Mock())
    for name in ("DATA", "STATE", "BRIEFS", "LOGS", "PROJECT"):
        monkeypatch.setattr(config, name, tmp_path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "load_settings", lambda: {"discord": {"test_channel_id": "fake"}})
    monkeypatch.setattr(config, "hermes_bin", lambda: "fake-hermes")
    monkeypatch.setattr(D, "LEDGER", tmp_path / "delivery.jsonl")
    monkeypatch.setattr(P, "_now_et", lambda: dt.datetime(2026, 11, 19, 8, 36, tzinfo=ET))
    monkeypatch.setattr(P, "_resolve", lambda _: ["synthetic-destination"])
    post = Mock(return_value="MOCK ONLY")
    child = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(P, "post", post)
    monkeypatch.setattr(D.subprocess, "run", child)

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network")

    monkeypatch.setattr(P.requests.sessions.Session, "request", forbidden)
    payload = {"as_of_et": STAMP, "gex": healthy_gex(),
               "event_gate": {"today_events": [], "today_tier2": []}}

    def save(brief=BRIEF):
        (tmp_path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
        (tmp_path / f"{DATE}.md").write_text(brief, encoding="utf-8")

    save()
    return payload, save, post, child


def run(*flags):
    return R.main(["--date", DATE, "--deliver-only", "--ignore-window", *flags])


@pytest.mark.parametrize("stamp", [
    "Wed Nov 18, 2026 8:35 AM ET", None, "yesterday ET",
    "Thu Nov 19, 2026 8:36 AM ET", "Thu Nov 19, 2026 08:35 AM ET",
], ids=["yesterday", "missing", "unparsable", "one-minute-mismatch", "not-verbatim"])
def test_stamp_defect_exits_5_without_delivery(isolated, stamp):
    _, save, post, child = isolated
    replacement = "" if stamp is None else f"*Data pulled {stamp}*"
    save(re.sub(r"\*Data pulled .*?\*", replacement, BRIEF))
    assert run() == 5
    post.assert_not_called()
    child.assert_not_called()


@pytest.mark.parametrize("value", [None, "yesterday ET"])
def test_unreadable_block_stamp_exits_5(isolated, value):
    payload, save, post, _ = isolated
    payload["as_of_et"] = value
    save()
    assert run() == 5
    post.assert_not_called()


@pytest.mark.parametrize("test_channel", [False, True])
def test_matching_fresh_stamp_delivered(isolated, test_channel):
    _, _, post, child = isolated
    assert run(*(["--test"] if test_channel else [])) == 0
    assert (child if test_channel else post).call_count >= 1


@pytest.mark.parametrize("test_channel", [False, True])
@pytest.mark.parametrize("now", [(9, 0), (9, 30)])
def test_block_crossing_existing_release_threshold_labelled(isolated, monkeypatch, test_channel, now):
    _, _, post, child = isolated
    monkeypatch.setattr(P, "_now_et", lambda: dt.datetime(2026, 11, 19, *now, tzinfo=ET))
    assert run(*(["--test"] if test_channel else [])) == 0
    assert (child if test_channel else post).call_count >= 1
    assert "stale" in D.LEDGER.read_text(encoding="utf-8")


def test_calendar_specific_release_crossing_labelled(isolated):
    payload, save, post, child = isolated
    payload["event_gate"]["today_tier2"] = [{"time_et": "08:36", "name": "synthetic event"}]
    save()
    assert run() == 0
    assert "synthetic event" in post.call_args.args[0]
    child.assert_not_called()


def test_disclosed_missing_feed_delivered_without_claim(isolated):
    payload, save, post, _ = isolated
    payload["gex"]["SPY"] = {"error": "synthetic missing chain"}
    save(BRIEF.replace("## 🧭 Regime read (generalized)\n",
                       "## 🧭 Regime read (generalized)\nGamma read unavailable: synthetic missing chain.\n"))
    assert run() == 0
    post.assert_called_once()


@pytest.mark.parametrize("defect", [
    "missing-block", "bad-json", "calendar", "incomplete-calendar", "bad-event", "future", "prior-day",
])
@pytest.mark.parametrize("test_channel", [False, True])
def test_direct_delivery_unknown_or_stale_refused(isolated, defect, test_channel):
    payload, save, post, child = isolated
    brief = BRIEF
    path = config.DATA / f"{DATE}.json"
    if defect == "calendar":
        payload["event_gate"] = {"error": "synthetic calendar failure"}
    elif defect == "incomplete-calendar":
        payload["event_gate"] = {}
    elif defect == "bad-event":
        payload["event_gate"]["today_events"] = [{"time_et": "??:??"}]
    elif defect in ("future", "prior-day"):
        value = "Thu Nov 19, 2026 9:00 AM ET" if defect == "future" else "Wed Nov 18, 2026 8:35 AM ET"
        payload["as_of_et"] = value
        brief = BRIEF.replace(STAMP, value)
    save(brief)
    if defect == "missing-block":
        path.unlink()
    elif defect == "bad-json":
        path.write_text("[", encoding="utf-8")
    result = (D.deliver_test(brief, DATE, "fake", force=True) if test_channel else
              D.deliver_production(brief, DATE, force=True))
    assert result[0] is False
    assert ("stale block" if defect == "prior-day" else "could not determine freshness") in result[1]
    post.assert_not_called()
    child.assert_not_called()


@pytest.mark.parametrize("failure", [
    {"error": "synthetic symbol error"}, None, {},
    {"near_term": None, "full_chain": {"regime": "positive"}},
    {"near_term": {"error": "synthetic chain error"}, "full_chain": {"regime": "positive"}},
    {"near_term": {"data_warning": "synthetic incomplete"}, "full_chain": {"regime": "positive"}},
    {"data_warning": "synthetic symbol warning"},
])
def test_normalize_all_gex_failure_shapes(failure):
    payload = {"gex": healthy_gex()}
    payload["gex"]["SPY"] = failure
    warning = R._data_warning(payload)
    assert warning and "SPY" in warning and "unavailable" in warning


@pytest.mark.parametrize("gex", [None, {}, [], {"QQQ": healthy_gex()["QQQ"]}])
def test_missing_gex_symbols_are_unavailable(gex):
    assert R._data_warning({"gex": gex})


def historical():
    return (Path(__file__).resolve().parents[1] / "briefs" / f"{DATE}.md").read_text(
        encoding="utf-8").replace("Thu Nov 19, 2026\n", DISPLAY + "\n", 1)


def check(brief, warning):
    return V.check(brief, DATE, DISPLAY, 5200, warning)


def test_historical_unavailable_with_claim_is_error():
    errors, warnings = check(historical(), "synthetic GEX unavailable")
    assert any("forbidden gamma" in e for e in errors)
    assert any("does not say" in e for e in errors)
    assert warnings == []


def test_historical_disclosed_without_claim_passes():
    brief = historical()
    brief = brief[:brief.index("## 🧭")] + (
        "## 🧭 Regime read (generalized)\n"
        "Gamma read unavailable: synthetic chain failure. No positioning inference today.\n\n"
        "---\n*Not financial advice — situational awareness only.*")
    brief = brief.replace(
        ", and gamma is deeply negative on SPY and QQQ", "")
    assert check(brief, "synthetic GEX unavailable") == ([], [])


def test_historical_unaffected_brief_passes():
    assert check(historical(), None) == ([], [])
    assert R._data_warning({"gex": healthy_gex()}) is None


@pytest.mark.parametrize("claim", [
    "Positive gamma.", "Negative gamma.", "Gamma is negative.", "Gamma is deeply negative.",
    "Gamma regime is supportive.", "Flip level 100.",
    "Gamma read unavailable but positive gamma still applies.",
])
def test_disclosure_does_not_license_regime_claim(claim):
    brief = BRIEF.replace("## 🧭 Regime read (generalized)\n",
                          "## 🧭 Regime read (generalized)\nGamma read unavailable: chain failure.\n" + claim + "\n")
    assert any("forbidden gamma" in e for e in check(brief, "failure")[0])


def test_unrelated_unavailable_inside_regime_not_disclosure():
    brief = BRIEF.replace("## 🧭 Regime read (generalized)\n",
                         "## 🧭 Regime read (generalized)\nGamma cannot be inferred; yields unavailable.\n")
    assert any("does not say" in e for e in check(brief, "failure")[0])


def test_undisclosed_symbol_failure_stops_orchestrator(isolated):
    payload, save, post, child = isolated
    payload["gex"]["SPY"] = {"error": "synthetic symbol failure"}
    save(historical())
    assert run() == 5
    post.assert_not_called()
    child.assert_not_called()
