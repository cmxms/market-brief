"""D2 offline guards; D2_BASELINE selects an extracted, unmodified revision."""
import datetime as dt
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("D2_BASELINE", str(ROOT)))
import compose as C
import config
import deliver as D
import gather as G
import post_discord as P
import run_brief as R
import validate as V

DATE = "2026-11-20"
STAMP = "Fri Nov 20, 2026 8:35 AM ET"
# Committed brief, not runtime output. Remove the out-of-section gamma/VIX
# claims to exercise section-only repair independently of the copied-claim guard.
BRIEF = (ROOT / "briefs" / f"{DATE}.md").read_text(encoding="utf-8").replace(
    ", and dealers are short gamma far below the flip", "").replace("a 31 VIX plus ", "")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in ("PROJECT", "DATA", "STATE", "BRIEFS", "LOGS"):
        monkeypatch.setattr(config, name, tmp_path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "load_settings", lambda: {"brief": {"max_chars": 6000},
                                                       "discord": {"test_channel_id": "fake"}})
    monkeypatch.setattr(D, "LEDGER", tmp_path / "delivery.jsonl")
    monkeypatch.setattr(P, "_now_et", lambda: dt.datetime(2026, 11, 20, 8, 36,
                                                        tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(P, "_resolve", lambda _: ["https://example.invalid/d2"])
    monkeypatch.setattr(P, "alert_failure", Mock())

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network or model call")

    monkeypatch.setattr(G.httpx, "get", forbidden)
    monkeypatch.setattr(G.yf, "download", forbidden)
    monkeypatch.setattr(P.requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(C, "run", forbidden)
    monkeypatch.setattr(D.subprocess, "run", forbidden)
    send = Mock()
    monkeypatch.setattr(P, "_send", send)
    payload = {"date": DATE, "as_of": "2026-11-20T08:35:00-05:00", "as_of_et": STAMP,
               "event_gate": {"today_events": [], "today_tier2": []},
               "gex": {s: {v: {"regime": "positive"} for v in ("near_term", "full_chain")}
                       for s in ("SPY", "QQQ")}}
    return tmp_path, payload, send


def save(fixture, brief=BRIEF):
    path, payload, _ = fixture
    (path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
    (path / f"{DATE}.md").write_text(brief, encoding="utf-8")


def run(*args):
    return R.main(["--date", DATE, "--deliver-only", "--ignore-window", *args])


def mismatch(payload):
    payload["source_crosscheck"] = {"vol": {"rows": {"^VIX": {
        "primary": 31.1, "fallback": 31.6, "diff": 0.5, "match": False}},
        "all_match": False, "n": 1, "tol": 0.1}}


@pytest.mark.parametrize("guard,section", [("crosscheck", "## 📈"), ("gex", "## 🧭"),
                                         ("source", "## 🔁")])
@pytest.mark.parametrize("route", ["production", "test", "dry"])
def test_data_error_posts_with_claims_quarantined(isolated, monkeypatch, guard, section, route):
    path, payload, send = isolated
    if guard == "crosscheck":
        mismatch(payload)
    elif guard == "gex":
        payload["gex"]["SPY"] = {"error": "missing chains"}
    else:
        payload["sector_rotation"] = {"error": "vendor timeout"}
    if route == "test":
        def child(args, **kwargs):
            send("fake", Path(args[-1]).read_text(encoding="utf-8"))
            return Mock(returncode=0)
        monkeypatch.setattr(D.subprocess, "run", child)
    save(isolated)
    assert run(*({"production": [], "test": ["--test"], "dry": ["--dry"]}[route])) == 0
    log = (path / f"{DATE}.log").read_text(encoding="utf-8")
    assert "QUARANTINE " in log
    records = [json.loads(line.split("QUARANTINE ", 1)[1]) for line in log.splitlines()
               if "QUARANTINE " in line]
    record = next(r for r in records if r["section"] == section)
    assert record["action"] == "section_replacement"
    start, end = V.section_bodies(BRIEF)[section]
    assert record["removed"] == BRIEF[start:end]
    expected = BRIEF
    for edit in reversed(records):
        assert expected[edit["start"]:edit["end"]] == edit["removed"]
        expected = expected[:edit["start"]] + edit["replacement"] + expected[edit["end"]:]
    assert (path / f"{DATE}.md").read_text(encoding="utf-8") == BRIEF
    assert V.availability_errors(expected, V.data_issues(payload, R._data_warning(payload))) == {}
    if route == "dry":
        send.assert_not_called()
        assert not D.LEDGER.exists()
    else:
        wire = "\n".join(c.args[1] for c in send.call_args_list)
        assert record["replacement"].strip() in wire
        assert record["removed"].strip() not in wire
        rows = [json.loads(line) for line in D.LEDGER.read_text(encoding="utf-8").splitlines()]
        quarantine = next(row for row in rows if row["status"] == "quarantined")
        assert quarantine["sections"] == records
        assert quarantine["brief_id"] == D._identity(expected, D._chunks(expected))
        assert rows[-1]["status"] == "ok"
        send.reset_mock()
        assert run(*(["--test"] if route == "test" else [])) == 0
        send.assert_not_called()


def render_block(payload):
    payload.update(index_recap={}, macro_pulse={}, finnhub={}, sector_rotation={})
    payload["gex"] = {s: {"error": "missing chains"} for s in G.GEX_SYMBOLS}
    return payload


def test_mismatched_value_is_not_plain_publishable_data(isolated):
    _, payload, _ = isolated
    render_block(payload)
    mismatch(payload)
    payload["macro_pulse"]["^VIX"] = {"last": 31.1, "chg": 3.0, "label": "VIX"}
    text = G.render_md(payload)
    assert "(^VIX): 31.1" not in text
    assert "^VIX: UNVERIFIED / UNAVAILABLE — DO NOT PUBLISH — CNBC vs Yahoo disagreement" in text


def test_source_error_is_explicitly_unpublishable(isolated):
    _, payload, _ = isolated
    render_block(payload)
    payload["macro_pulse"]["^VIX"] = {"error": "vendor timeout", "last": 31.1}
    text = G.render_md(payload)
    line = next(line for line in text.splitlines() if line.startswith("- ^VIX:"))
    assert "DO NOT PUBLISH" in line and "vendor timeout" in line
    assert "31.1" not in text


def test_prompt_forbids_publishing_flagged_value():
    assert "Never state a value marked DO NOT PUBLISH" in C.PROMPT


@pytest.mark.parametrize("claim", ["VIX 31.1", "VIX around 31", "VIX elevated", "VIX 31.6"])
def test_disclosure_does_not_license_flagged_claim(isolated, claim):
    _, payload, _ = isolated
    mismatch(payload)
    issues = V.data_issues(payload)
    start, end = V.section_bodies(BRIEF)["## 📈"]
    text = BRIEF[:start] + "^VIX unavailable: CNBC vs Yahoo disagreement. " + claim + "\n\n" + BRIEF[end:]
    assert "## 📈" in V.availability_errors(text, issues)


@pytest.mark.parametrize("defect", ["stamp", "title", "footer", "order", "url", "empty", "calendar", "future", "prior"])
def test_structural_and_provenance_failures_not_repaired(isolated, defect):
    _, payload, send = isolated
    mismatch(payload)
    text = BRIEF
    expected = 5
    if defect == "stamp":
        text = text.replace(STAMP, "Fri Nov 20, 2026 8:34 AM ET")
    elif defect == "title":
        text = text.replace("# 📊 Market Brief", "Wrong title", 1)
    elif defect == "footer":
        text = text.replace("Not financial advice", "Wrong footer")
    elif defect == "order":
        text = text.replace("## 📈 Macro", "## 🚨 Macro").replace("## 🚨 Event", "## 📈 Event")
    elif defect == "url":
        text += "\nhttps://example.invalid"
    elif defect == "empty":
        start, end = V.section_bodies(text)["## 📈"]
        text = text[:start] + "\n" + text[end:]
    elif defect == "calendar":
        payload["event_gate"] = {"error": "calendar unavailable"}
        expected = 7
    else:
        stamp = "Fri Nov 20, 2026 8:37 AM ET" if defect == "future" else "Thu Nov 19, 2026 8:35 AM ET"
        payload["as_of_et"] = stamp
        text = text.replace(STAMP, stamp)
        expected = 7
    save(isolated, text)
    assert run() == expected
    send.assert_not_called()
    assert not any(row.get("status") == "ok" for row in D._rows())


def test_multiple_sections_replaced_without_touching_other_bytes(isolated):
    _, payload, _ = isolated
    mismatch(payload)
    payload["sector_rotation"] = {"error": "vendor timeout"}
    errors = V.availability_errors(BRIEF, V.data_issues(payload))
    output, records = V.quarantine(BRIEF, errors)
    restored = output
    for record in records:
        restored = restored.replace(record["replacement"], record["removed"], 1)
    assert restored == BRIEF
    assert {r["section"] for r in records} == {"## 📈", "## 🔁"}


def test_out_of_section_gamma_claim_is_removed(isolated):
    _, payload, send = isolated
    payload["gex"]["SPY"] = {"error": "missing chains"}
    save(isolated, BRIEF.replace("**TL;DR:**", "**TL;DR:** Short gamma."))
    assert run() == 0
    assert send.call_count


@pytest.mark.parametrize("section", ["## 📈", "## 🧭"])
def test_correct_disclosure_is_preserved_without_quarantine(isolated, section):
    path, payload, send = isolated
    if section == "## 📈":
        mismatch(payload)
    else:
        payload["gex"]["SPY"] = {"error": "missing chains"}
    issues = V.data_issues(payload, R._data_warning(payload))
    start, end = V.section_bodies(BRIEF)[section]
    text = BRIEF[:start] + V.unavailable_line(issues) + "\n\n" + BRIEF[end:]
    save(isolated, text)
    assert run() == 0
    assert send.call_count
    assert "QUARANTINE " not in (path / f"{DATE}.log").read_text(encoding="utf-8")


def test_recovered_source_attempt_does_not_quarantine(isolated):
    path, payload, send = isolated
    payload["macro_pulse"] = {"failures": [["CNBC", "timeout"]],
                              "^VIX": {"last": 31, "source": "Yahoo"}}
    save(isolated)
    assert run() == 0
    assert send.call_count
    assert "QUARANTINE " not in (path / f"{DATE}.log").read_text(encoding="utf-8")


def test_yield_mismatch_withholds_move_as_well_as_level(isolated):
    _, payload, _ = isolated
    render_block(payload)
    payload["source_crosscheck"] = {"yields": {"rows": {"10Y": {
        "primary": 4.1, "fallback": 4.4, "diff": 0.3, "match": False}},
        "all_match": False, "n": 1, "tol": 0.02}}
    payload["macro_pulse"]["yields"] = {"tenors": {"10Y": {"last": 4.1, "chg": 0.17}}}
    text = G.render_md(payload)
    assert "10Y: UNVERIFIED / UNAVAILABLE — DO NOT PUBLISH" in text
    assert "chg 0.17" not in text and "10Y: 4.1%" not in text


def test_gex_warning_never_emits_the_rejected_view(isolated):
    _, payload, _ = isolated
    render_block(payload)
    bad = {"data_warning": "missing open interest", "regime": "negative", "total_gex": -1e9}
    payload["gex"]["SPY"] = {"near_term": bad, "full_chain": bad}
    text = G.render_md(payload)
    assert "missing open interest" in text and "DO NOT PUBLISH" in text
    assert "regime **negative**" not in text and "total -1.00bn" not in text


def test_leading_whitespace_and_completion_marker_survive_exactly(isolated):
    _, payload, _ = isolated
    mismatch(payload)
    source = "\n " + BRIEF + "\n<!-- market-brief complete: attempt-d2 -->\n"
    output, records = V.quarantine(source, V.availability_errors(source, V.data_issues(payload)))
    record = records[0]
    assert output.replace(record["replacement"], record["removed"], 1) == source


def test_degraded_status_without_warning_is_not_healthy(isolated):
    _, payload, _ = isolated
    payload["gex"]["SPY"]["near_term"] = {"status": "degraded", "degraded_reason": "missing expiry"}
    warning = R._data_warning(payload)
    assert warning is not None and "missing expiry" in warning


def test_copied_mismatched_metric_cannot_escape_section_repair(isolated):
    _, payload, send = isolated
    mismatch(payload)
    save(isolated, BRIEF.replace("**TL;DR:**", "**TL;DR:** VIX around 31."))
    assert run() == 0
    assert send.call_count
