"""Run provenance regressions: isolated files, fake writer, no network or posts."""
import datetime as dt
import json
import re
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import test_compose_env as environment
from test_brief_metadata import sized_brief
from test_compose_env import BODY, DATE, outputs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compose as C
import config
import deliver as D
import gather as G
import post_discord as P
import run_brief as R
import run_identity as I
import validate as V

composer = environment.composer
ID = "a" * 32


def block():
    return {"date": DATE, "as_of": "2026-11-14T08:00:00-05:00",
            "as_of_et": "Sat Nov 14, 2026 8:00 AM ET", "event_gate": {},
            "index_recap": {}, "macro_pulse": {}, "sector_rotation": {},
            "finnhub": {}, "gex": {s: {"error": "fixture missing chain"} for s in ("SPY", "QQQ")}}


@pytest.mark.parametrize("bad", [None, "../escape", "A" * 32, "x" * 32, 123, "a" * 31])
def test_identifiers_are_bounded_and_path_safe(bad):
    with pytest.raises(ValueError):
        I.require(bad)


def test_new_ids_are_distinct_valid_uuid4():
    import uuid
    ids = [I.new() for _ in range(20)]
    assert len(set(ids)) == len(ids)
    assert all(I.require(value) == value and uuid.UUID(hex=value).version == 4 for value in ids)


@pytest.mark.parametrize("extra", [0, 1])
def test_trace_counts_at_real_compose_ceiling(composer, extra):
    _, process, out, log = composer
    data = config.STATE / "data.md"
    data.write_text(I.line(ID), encoding="utf-8")
    trace_room = len(I.line(ID)) + 1
    draft = sized_brief(5200 + extra - trace_room)

    def child(cmd, **kwargs):
        candidate, claims, end = outputs(cmd)
        candidate.write_bytes((draft + "\n" + end).encode())
        claims.write_bytes(b"[]")
        return Mock(returncode=0, stdout="", stderr="")

    process.side_effect = child
    ok, detail = C.run(DATE, data, out, log, run_id=ID)
    assert ok is (extra == 0), detail
    if ok:
        result = out.read_text(encoding="utf-8")
        assert len(V.visible_text(result)) == 5200
        assert I.errors(result, ID) == []
        assert I.line(ID) in "\n".join(P.chunk(result))
        record_path = next((config.STATE / "compose-attempts").glob("*/complete.json"))
        record = json.loads(record_path.read_text())
        assert record["run_id"] == ID
        assert out.read_bytes() == (record_path.parent / "brief.md").read_bytes()
    else:
        assert "length 5201 exceeds ceiling 5200" in detail
        assert not out.exists()


def test_compose_mismatched_data_id_never_launches(composer):
    _, process, out, log = composer
    data = config.STATE / "data.md"
    data.write_text(I.line("b" * 32), encoding="utf-8")
    assert not C.run(DATE, data, out, log, run_id=ID)[0]
    process.assert_not_called()


@pytest.mark.parametrize("trace", ["", I.line("b" * 32), I.line(ID) + "\n" + I.line(ID)])
def test_validation_rejects_missing_wrong_duplicate_trace(trace):
    errors, _ = V.check(BODY + "\n" + trace, DATE, "Sat November 14, 2026", 5200, None, run_id=ID)
    assert "Run trace does not exactly match gathered block run_id" in errors


def test_trace_is_not_part_of_regime_body_or_quarantine():
    source = BODY + "\n" + I.line(ID)
    a, b = V.section_bodies(source)["## 🧭"]
    assert I.line(ID) not in source[a:b]
    issue = V.DataIssue("## 🧭", "Gamma read", "fixture missing chain")
    repaired, _ = V.quarantine(source, {"## 🧭": [issue]}, [issue])
    assert I.errors(repaired, ID) == []
    assert V.check(repaired, DATE, "Sat November 14, 2026", 5200, None, run_id=ID) == ([], [])


@pytest.mark.parametrize("route", ["production", "test"])
def test_delivery_rejects_same_minute_wrong_block(monkeypatch, tmp_path, route):
    monkeypatch.setattr(config, "DATA", tmp_path)
    payload = block() | {"run_id": "b" * 32}
    (tmp_path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
    transport = Mock(side_effect=AssertionError("must not post"))
    monkeypatch.setattr(P, "post", transport)
    monkeypatch.setattr(D.subprocess, "run", transport)
    brief = BODY + "\n" + I.line(ID)
    ok, detail = (D.deliver_production(brief, DATE) if route == "production" else
                  D.deliver_test(brief, DATE, "fake"))
    assert not ok and "run_id" in detail
    transport.assert_not_called()


def test_legacy_skip_then_recompose_and_deliver_only_keep_identity(composer, monkeypatch):
    _, process, _, _ = composer
    monkeypatch.setattr(config, "DATA", config.STATE)
    monkeypatch.setattr(config, "LOGS", config.STATE)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(P, "alert_failure", Mock(side_effect=AssertionError("no failure expected")))
    payload = block()
    data = config.DATA / f"{DATE}.json"
    data.write_text(json.dumps(payload), encoding="utf-8")
    md = config.DATA / f"{DATE}.data.md"
    md.write_text(G.render_md(payload), encoding="utf-8")
    original = data.read_bytes(), md.read_bytes()
    generated = Mock(return_value=ID)
    monkeypatch.setattr(I, "new", generated)
    monkeypatch.setattr(G, "main", Mock(side_effect=AssertionError("must reuse")))

    def child(cmd, **kwargs):
        candidate, claims, end = outputs(cmd)
        snapshot = candidate.parent / "data.md"
        assert I.errors(snapshot.read_text(encoding="utf-8"), ID) == []
        # A replacement while the model writes cannot change its input snapshot.
        before = snapshot.read_bytes()
        md.write_text("changed live path", encoding="utf-8")
        assert snapshot.read_bytes() == before
        md.write_bytes(before)
        candidate.write_bytes((BODY + "\n" + end).encode())
        claims.write_bytes(b"[]")
        return Mock(returncode=0, stdout="", stderr="")

    process.side_effect = child
    monkeypatch.setattr(D, "deliver_production", Mock(return_value=(True, "stubbed")))
    monkeypatch.setattr(D, "record_quarantine", Mock())
    flags = ["--date", DATE, "--ignore-window", "--force-weekend"]
    assert R.main(flags + ["--skip-gather"]) == 0
    assert (config.DATA / "runs" / f"{DATE}-0800.json").read_bytes() == original[0]
    assert (config.DATA / "runs" / f"{DATE}-0800.data.md").read_bytes() == original[1]
    assert json.loads(data.read_text())["run_id"] == ID
    assert R.main(flags + ["--skip-gather"]) == 0
    out = config.BRIEFS / f"{DATE}.md"
    assert out.exists()
    assert R.main(flags + ["--deliver-only"]) == 0
    generated.assert_called_once()
    result = (config.BRIEFS / f"{DATE}.md").read_text(encoding="utf-8")
    assert I.errors(result, ID) == I.errors(md.read_text(encoding="utf-8"), ID) == []
    log = (config.LOGS / f"{DATE}.log").read_text(encoding="utf-8")
    assert all(f"run_id={ID}" in line for line in log.splitlines() if "run start " in line or "compose ok:" in line)
    executions = [re.findall(r"execution_id=([0-9a-f]{32})", line)[0]
                  for line in log.splitlines() if "run start " in line]
    assert len(set(executions)) == 3  # Two skip-gather attempts, then deliver-only.


def test_deliver_only_does_not_relabel_legacy_brief(composer, monkeypatch):
    monkeypatch.setattr(config, "DATA", config.STATE)
    monkeypatch.setattr(config, "LOGS", config.STATE)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(P, "alert_failure", Mock())
    (config.DATA / f"{DATE}.json").write_text(json.dumps(block()), encoding="utf-8")
    out = config.BRIEFS / f"{DATE}.md"
    out.write_text(BODY, encoding="utf-8")
    before = out.read_bytes()
    assert R.main(["--date", DATE, "--deliver-only", "--dry", "--ignore-window", "--force-weekend"]) == 5
    assert out.read_bytes() == before


def test_new_run_identity_exists_before_real_gather_entry(composer, monkeypatch):
    _, process, _, _ = composer
    monkeypatch.setattr(config, "DATA", config.STATE)
    monkeypatch.setattr(config, "LOGS", config.STATE)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(P, "alert_failure", Mock(side_effect=AssertionError("no failure expected")))
    generated = Mock(return_value=ID)
    monkeypatch.setattr(I, "new", generated)
    today = dt.datetime.now(R.ET).date()
    date = today.isoformat()
    payload = block() | {"date": date}

    def fetch(day):
        generated.assert_called_once()
        assert day == today
        log = (config.LOGS / f"{date}.log").read_text(encoding="utf-8")
        assert f"run start date={date}" in log and f"run_id={ID}" in log
        return payload

    monkeypatch.setattr(G, "gather", fetch)

    def child(cmd, **kwargs):
        candidate, claims, end = outputs(cmd)
        draft = BODY.replace("Sat November 14, 2026", V.display_date(today))
        candidate.write_bytes((draft + "\n" + end).encode())
        claims.write_bytes(b"[]")
        return Mock(returncode=0, stdout="", stderr="")

    process.side_effect = child
    assert R.main(["--date", date, "--dry", "--ignore-window", "--force-weekend", "--force-holiday"]) == 0
    generated.assert_called_once()
    assert not (config.DATA / f"{date}.json").exists()
    preview = next((config.STATE / "compose-attempts").glob("preview-*"))
    assert json.loads((preview / f"{date}.json").read_text())["run_id"] == ID
    assert I.errors((preview / f"{date}.data.md").read_text(encoding="utf-8"), ID) == []
