"""Completion contract regressions. All artifacts and delivery are isolated."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_compose_env as environment
from test_compose_env import BODY, DATE, FOOTER, HEADERS, PARAGRAPH, outputs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compose as C
import config
import post_discord as P
import run_brief as R
import validate as V

composer = environment.composer


def writer(*, brief=BODY, ledger="[]", exit_code=7, marker=True, suffix=""):
    def child(cmd, **kwargs):
        candidate, claims, end = outputs(cmd)
        if brief is not None:
            candidate.write_text(brief + ("\n" + end if marker else "") + suffix, encoding="utf-8")
        if ledger is not None:
            claims.write_text(ledger, encoding="utf-8")
        return Mock(returncode=exit_code, stdout="", stderr="synthetic provider failure" if exit_code else "")
    return child


def records():
    return list((config.STATE / "compose-attempts").glob("*/complete.json"))


@pytest.mark.parametrize("age", [-4, 3600, -3600])
def test_earlier_attempt_cannot_satisfy_current(composer, monkeypatch, age):
    run, process, out, _ = composer
    ledger = config.STATE / f"claims-{DATE}.json"
    out.write_text(BODY, encoding="utf-8")
    ledger.write_text("[]", encoding="utf-8")
    stamp = out.stat().st_mtime
    os.utime(out, (stamp + age, stamp + age))
    os.utime(ledger, (stamp + age, stamp + age))
    # Include a backwards wall clock, not just future-dated/restored mtimes.
    monkeypatch.setattr(C, "time", SimpleNamespace(time=lambda: stamp - 7200), raising=False)
    process.return_value.returncode = 7  # child writes nothing
    assert run()[0] is False
    assert out.read_text(encoding="utf-8") == BODY
    assert not records()


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("ledger", ["[", "{}", "[1]", '[{"claim":"x"}]',
                                    '[{"claim":"x","source":1,"retrieved":"today"}]',
                                    '[{"claim":"","source":"x","retrieved":"today"}]',
                                    '[{"claim":"x","source":"not a URL","retrieved":"2026-11-14"}]',
                                    '[{"claim":"x","source":"https://example.invalid","retrieved":"today"}]',
                                    '[{"claim":"x","source":"https://example.invalid","retrieved":"20261114"}]', None])
def test_ledger_must_parse_and_match_schema(composer, ledger, exit_code):
    run, process, out, _ = composer
    process.side_effect = writer(ledger=ledger, exit_code=exit_code)
    assert run()[0] is False
    assert not out.exists()
    assert not records()


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("marker,suffix", [(False, ""), (True, "\nunfinished after marker")])
def test_end_marker_is_required_and_last(composer, exit_code, marker, suffix):
    run, process, out, _ = composer
    process.side_effect = writer(exit_code=exit_code, marker=marker, suffix=suffix)
    assert run()[0] is False
    assert not out.exists()
    assert not records()


@pytest.mark.parametrize("header", HEADERS)
def test_each_section_requires_body(header):
    brief = BODY.replace(header + "\n" + PARAGRAPH, header + "\n\n<!-- not a body -->\n---")
    errors, _ = V.check(brief, DATE, "Sat November 14, 2026", 5200, None)
    assert any(e.startswith("empty section body") for e in errors)


@pytest.mark.parametrize("footer", ["", "---", "*Not financial advice — situational awareness only.*"])
def test_footer_requires_rule_and_disclaimer(footer):
    errors, _ = V.check(BODY.replace(FOOTER, footer), DATE, "Sat November 14, 2026", 5200, None)
    assert "missing footer/disclaimer" in errors


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("defect", ["empty", "footer", "title", "url", "length"])
def test_validation_precedes_promotion(composer, exit_code, defect):
    run, process, out, _ = composer
    brief = {
        "empty": BODY.replace(HEADERS[-1] + "\n" + PARAGRAPH, HEADERS[-1]),
        "footer": BODY.replace(FOOTER, ""),
        "title": BODY.replace(DATE, "wrong").replace("November 14", "November 15"),
        "url": BODY + "\nhttps://example.invalid",
        "length": BODY + "x" * 5200,
    }[defect]
    out.write_text("previous preserved brief", encoding="utf-8")
    canonical_claims = config.STATE / f"claims-{DATE}.json"
    canonical_claims.write_text('[{"previous":true}]', encoding="utf-8")
    process.side_effect = writer(brief=brief, exit_code=exit_code)
    assert run()[0] is False
    assert out.read_text(encoding="utf-8") == "previous preserved brief"
    assert canonical_claims.read_text(encoding="utf-8") == '[{"previous":true}]'
    assert not records()


@pytest.mark.parametrize("exit_code", [0, 1, 7])
def test_complete_attempt_has_record_and_honest_status(composer, exit_code):
    run, process, out, log = composer
    ledger = '[{"claim":"synthetic","source":"https://example.invalid","retrieved":"2026-11-14"}]'
    process.side_effect = writer(ledger=ledger, exit_code=exit_code)
    ok, detail = run()
    assert ok is True
    assert len(records()) == 1
    path = records()[0]
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["attempt"] == path.parent.name
    assert record["child_exit"] == exit_code
    assert record["date"] == DATE
    assert record["brief"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert record["claims"]["sha256"] == hashlib.sha256(ledger.encode()).hexdigest()
    assert out.read_bytes() == (path.parent / record["brief"]["path"]).read_bytes()
    assert (config.STATE / f"claims-{DATE}.json").read_text(encoding="utf-8") == ledger
    expected = "hermes exit 0" if exit_code == 0 else "recovered after a failed child (complete attempt)"
    assert detail.startswith(expected)
    assert detail in log.read_text(encoding="utf-8")


def test_attempt_paths_are_never_reused(composer):
    run, process, out, _ = composer
    process.side_effect = writer(exit_code=0)
    assert run()[0] is True
    first = process.call_args.args[0][3]
    before = out.read_bytes()
    process.side_effect = None
    process.return_value.returncode = 7
    assert run()[0] is False
    assert process.call_args.args[0][3] != first
    assert out.read_bytes() == before


def test_linked_artifacts_cannot_complete(composer, monkeypatch):
    run, process, _, _ = composer
    process.side_effect = writer()
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "brief.md")
    assert run()[0] is False
    assert not records()


def test_completion_record_write_failure_prevents_promotion(composer, monkeypatch):
    run, process, out, _ = composer
    process.side_effect = writer()
    original = Path.write_text

    def write(self, *args, **kwargs):
        if self.name == "complete.json":
            raise OSError("synthetic record failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    assert run()[0] is False
    assert not out.exists()


def test_recovery_detail_does_not_overstate_the_checks(composer):
    run, process, _, log = composer
    process.side_effect = writer()
    ok, detail = run()
    assert ok is True
    assert detail.startswith("recovered after a failed child (complete attempt)")
    assert detail in log.read_text(encoding="utf-8")


def test_prompt_supplies_attempt_specific_end_marker(composer):
    run, process, _, _ = composer
    run()
    prompt_file = Path(process.call_args.args[0][3])
    prompt = prompt_file.read_text(encoding="utf-8")
    assert f"<!-- market-brief complete: {prompt_file.parent.name} -->" in prompt
    assert "last thing in the brief file" in prompt


def test_timeout_remains_fail_closed_even_with_artifacts(composer):
    run, process, out, log = composer

    def timeout(cmd, **kwargs):
        writer()(cmd)
        raise subprocess.TimeoutExpired(cmd, 1020)

    process.side_effect = timeout
    assert run()[0] is False
    assert not out.exists()
    assert not records()
    assert "failed: compose timed out" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("scenario", ["unfinished", "malformed", "stale", "complete"])
def test_audit_scenarios_at_delivery_boundary(composer, monkeypatch, scenario):
    monkeypatch.setattr(P, "alert_failure", Mock())
    _, process, _, _ = composer
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "DATA", config.STATE)
    monkeypatch.setattr(config, "LOGS", config.STATE)
    (config.DATA / f"{DATE}.json").write_text(json.dumps({
        "as_of_et": "Sat Nov 14, 2026 8:00 AM ET",
        "gex": {s: {v: {"regime": "positive"} for v in ("near_term", "full_chain")}
                for s in ("SPY", "QQQ")},
    }), encoding="utf-8")
    (config.DATA / f"{DATE}.data.md").write_text("synthetic", encoding="utf-8")
    out = config.BRIEFS / f"{DATE}.md"
    ledger = config.STATE / f"claims-{DATE}.json"
    if scenario == "stale":
        out.write_text(BODY, encoding="utf-8")
        ledger.write_text("[]", encoding="utf-8")
        stamp = out.stat().st_mtime - 4
        os.utime(out, (stamp, stamp))
        os.utime(ledger, (stamp, stamp))
        process.return_value.returncode = 7
    else:
        brief = BODY.split(HEADERS[-1])[0] + HEADERS[-1] if scenario == "unfinished" else BODY
        process.side_effect = writer(
            brief=brief, ledger="[" if scenario in ("unfinished", "malformed") else "[]",
            marker=scenario != "unfinished",
        )
    import deliver
    sender = Mock(return_value=(True, "MOCK ONLY: no post"))
    monkeypatch.setattr(deliver, "deliver_production", sender)
    code = R.main(["--date", DATE, "--skip-gather", "--ignore-window", "--force-weekend"])
    print(f"SCENARIO {scenario}: exit={code}, delivery_calls={sender.call_count}")
    assert code == (0 if scenario == "complete" else 4)
    assert sender.call_count == (1 if scenario == "complete" else 0)
    if scenario not in ("complete", "stale"):
        assert not out.exists()
        assert not ledger.exists()
