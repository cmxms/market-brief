"""Accepted audit regressions: isolated artifacts and forbidden real transports."""
import json
import re
from unittest.mock import Mock

import pytest
from test_daily_delivery import BRIEF, DATE, STAMP
from test_daily_delivery import isolated as daily_isolated

import deliver as D
import post_discord as P
import run_brief as R
import validate as V

isolated = daily_isolated
ID = "a" * 32
TRACE = "-# Run: " + ID
FOOTER = "---\n*Not financial advice — situational awareness only.*"


def check(brief):
    return V.check(brief, DATE, "Thu November 19, 2026", 5200, None,
                   as_of_et=STAMP, run_id=ID)[0]


def test_unmatched_opener_draft_rejected(isolated):
    draft = BRIEF.replace(FOOTER, "<!-- dangling\n" + FOOTER)
    assert check(draft)
    with pytest.raises(ValueError):
        P.chunk(draft)


@pytest.mark.parametrize("route", ["production", "test", "runner"])
def test_deliver_only_hidden_trace_rejected(isolated, monkeypatch, route):
    path, send, _ = isolated
    child = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(D.subprocess, "run", child)
    brief = BRIEF.replace(TRACE, "").replace("**TL;DR:**", "<!--\n" + TRACE + "\n-->\n**TL;DR:**")
    (path / f"{DATE}.md").write_text(brief, encoding="utf-8")
    monkeypatch.setattr(P, "alert_failure", Mock())
    if route == "runner":
        assert R.main(["--date", DATE, "--deliver-only", "--dry", "--ignore-window"]) == 5
    else:
        ok, detail = (D.deliver_production(brief, DATE) if route == "production" else
                      D.deliver_test(brief, DATE, "fake"))
        assert not ok and "Run trace" in detail
    send.assert_not_called()
    child.assert_not_called()


@pytest.mark.parametrize("route", ["production", "test", "runner"])
def test_untraced_legacy_brief_never_delivers(isolated, monkeypatch, route):
    path, send, payload = isolated
    child = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(D.subprocess, "run", child)
    payload.pop("run_id")
    (path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
    brief = BRIEF.replace(TRACE, "")
    (path / f"{DATE}.md").write_text(brief, encoding="utf-8")
    monkeypatch.setattr(P, "alert_failure", Mock())
    if route == "runner":
        assert R.main(["--date", DATE, "--deliver-only", "--dry", "--ignore-window"]) == 5
    else:
        ok, detail = (D.deliver_production(brief, DATE) if route == "production" else
                      D.deliver_test(brief, DATE, "fake"))
        assert not ok and "run_id" in detail
    send.assert_not_called()
    child.assert_not_called()


@pytest.mark.parametrize("comment", ["<!--", "-->", "<!-- nested <!-- x --> -->",
                                     "<!-- x -- y -->", "<!bogus>", "<?comment?>"])
def test_unsupported_comments_fail_validation_and_chunking(isolated, comment):
    brief = BRIEF.replace("**TL;DR:**", comment + "\n**TL;DR:**")
    assert check(brief)
    with pytest.raises(ValueError):
        P.chunk(brief)


def test_visible_length_uses_shared_comment_transform_includes_footer(isolated):
    brief = BRIEF.replace("**TL;DR:**", "<!-- hidden metadata -->\n**TL;DR:**")
    visible = V.visible_text(brief)
    assert "hidden metadata" not in visible
    assert visible.endswith(FOOTER + "\n" + TRACE)
    assert V.visible_text is P.visible_text
    assert check(brief) == []
    assert any("exceeds ceiling" in e for e in V.check(
        brief, DATE, "Thu November 19, 2026", len(visible) - 1, None, run_id=ID)[0])


def test_visible_trace_must_be_at_footer(isolated):
    brief = BRIEF.replace(TRACE, "").replace("**TL;DR:**", TRACE + "\n**TL;DR:**")
    assert check(brief)
    assert D._freshness_failure(brief, DATE)


def test_malformed_extra_run_line_rejected(isolated):
    brief = BRIEF.replace("**TL;DR:**", "-# Run: invalid\n**TL;DR:**")
    assert check(brief)
    assert D._freshness_failure(brief, DATE)


def test_reused_block_has_distinct_execution_ids_and_unchanged_brief(isolated, capsys):
    path, send, _ = isolated
    before = (path / f"{DATE}.md").read_bytes()
    flags = ["--date", DATE, "--deliver-only", "--dry", "--ignore-window"]
    for _ in range(2):
        assert R.main(flags) == 0
    log = (path / f"{DATE}.log").read_text(encoding="utf-8")
    starts = [line for line in log.splitlines() if "run start " in line]
    executions = re.findall(r"execution_id=([0-9a-f]{32})", "\n".join(starts))
    assert len(executions) == 2
    assert len(set(executions)) == 2
    assert all(f"run_id={ID}" in line for line in starts)
    assert all("execution_id=" in line for line in log.splitlines())
    assert (path / f"{DATE}.md").read_bytes() == before
    output = capsys.readouterr().out
    assert output.count(TRACE) == 2
    assert not any(e in before.decode() for e in executions)
    send.assert_not_called()
