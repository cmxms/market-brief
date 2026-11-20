"""D3 line-level repair regressions; all I/O uses the D2 isolated fixture."""
import json

import pytest
from test_data_quarantine import (
    BRIEF,
    DATE,
    STAMP,
    D,
    R,
    V,
    mismatch,
    run,
    save,
)
from test_data_quarantine import isolated as base_isolated


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    return base_isolated.__wrapped__(monkeypatch, tmp_path)


def summary(text):
    start = BRIEF.index("**TL;DR:**")
    end = BRIEF.index("## 📰")
    return BRIEF[:start] + text + "\n\n" + BRIEF[end:]


def records(path):
    log = (path / f"{DATE}.log").read_text(encoding="utf-8")
    return [json.loads(line.split("QUARANTINE ", 1)[1]) for line in log.splitlines()
            if "QUARANTINE " in line]


@pytest.mark.parametrize("route", ["production", "test", "dry"])
def test_copied_disputed_figure_posts_and_is_recoverable(isolated, monkeypatch, capsys, route):
    path, payload, send = isolated
    mismatch(payload)
    source = summary("**TL;DR:**\nVIX around 31.\nKeep this independent sentence.")
    if route == "test":
        from pathlib import Path
        from unittest.mock import Mock

        def child(args, **kwargs):
            send("fake", Path(args[-1]).read_text(encoding="utf-8"))
            return Mock(returncode=0)
        monkeypatch.setattr(D.subprocess, "run", child)
    save(isolated, source)
    assert run(*{"production": [], "test": ["--test"], "dry": ["--dry"]}[route]) == 0
    edits = records(path)
    removed = next(r for r in edits if r["action"] == "line_removal")
    assert removed["section"] == "TL;DR"
    assert removed["removed"] == "VIX around 31.\n"
    assert removed["replacement"] == ""
    assert removed["reasons"] == [{"metric": "^VIX", "reason": "CNBC vs Yahoo disagreement"}]
    expected = source
    for r in reversed(edits):
        assert expected[r["start"]:r["end"]] == r["removed"]
        expected = expected[:r["start"]] + r["replacement"] + expected[r["end"]:]
    assert "**TL;DR:**\nKeep this independent sentence." in expected
    if route == "dry":
        assert not D.LEDGER.exists()
        assert "Keep this independent sentence." in capsys.readouterr().out
    else:
        wire = "\n".join(c.args[1] for c in send.call_args_list)
        assert "VIX around 31." not in wire
        assert "Keep this independent sentence." in wire
        row = next(r for r in D._rows() if r["target"] == "validation")
        assert row["sections"] == edits
        assert row["brief_id"] == D._identity(expected, D._chunks(expected))
        # Restore in source order: restored prefixes recover the original offsets.
        restored = expected
        for r in edits:
            at = r["start"]
            restored = restored[:at] + r["removed"] + restored[at + len(r["replacement"]):]
        assert restored == source
    assert (path / f"{DATE}.md").read_text(encoding="utf-8") == source


@pytest.mark.parametrize("body", ["**TL;DR:** VIX 31.", "**TL;DR:**\nVIX 31.\nVIX elevated."])
def test_empty_summary_gets_one_fallback(isolated, body):
    path, payload, _ = isolated
    mismatch(payload)
    save(isolated, summary(body))
    assert run() == 0
    wire = "\n".join(c.args[1] for c in isolated[2].call_args_list)
    assert wire.count("**TL;DR:**") == 1
    assert "Unavailable: see sections." in wire
    assert sum(r["action"] == "line_removal" for r in records(path)) == body.count("VIX")


@pytest.mark.parametrize("empty", [False, True])
def test_copied_claim_in_other_section_uses_smallest_safe_unit(isolated, empty):
    path, payload, _ = isolated
    mismatch(payload)
    a, b = V.section_bodies(BRIEF)["## 📰"]
    body = "VIX around 31.\n" + ("" if empty else "Keep the other headline.\n")
    source = BRIEF[:a] + body + BRIEF[b:]
    save(isolated, source)
    assert run() == 0
    edit = next(r for r in records(path) if r["section"] == "## 📰")
    assert edit["action"] == ("section_replacement" if empty else "line_removal")
    assert edit["removed"] == "VIX around 31.\n"
    assert bool(edit["replacement"]) == empty


@pytest.mark.parametrize("location", ["title", "heading", "stamp", "completion", "fence"])
def test_claim_in_protected_structure_still_refuses(isolated, location):
    _, payload, send = isolated
    mismatch(payload)
    source = summary("**TL;DR:** VIX 31.")
    if location == "title":
        source = source.replace("Market Brief", "Market Brief VIX 31", 1)
    elif location == "heading":
        source = source.replace("## 📰", "## 📰 VIX 31", 1)
    elif location == "stamp":
        source = source.replace(STAMP, STAMP + " VIX 31")
    elif location == "completion":
        source += "\n<!-- market-brief complete: attempt-VIX -->\n"
    else:
        source = source.replace("## 📰 Last", "```VIX\n## 📰 Last", 1) + "\n```"
    save(isolated, source)
    assert run() == 5
    send.assert_not_called()


@pytest.mark.parametrize("metric,claim", [("^VIX", "VIX elevated."), ("10Y", "10Y down 7bp.")])
def test_source_error_not_only_disagreement_repairs_copied_claim(isolated, metric, claim):
    path, payload, _ = isolated
    payload["macro_pulse"] = {metric: {"error": "vendor timeout"}}
    save(isolated, summary("**TL;DR:** " + claim))
    assert run() == 0
    assert any(r["section"] == "TL;DR" for r in records(path))


def test_many_issues_on_one_line_record_all_reasons_once(isolated):
    path, payload, _ = isolated
    mismatch(payload)
    payload["gex"]["SPY"] = {"error": "missing chains"}
    save(isolated, summary("**TL;DR:** VIX 31, short gamma."))
    assert run() == 0
    edit = next(r for r in records(path) if r["section"] == "TL;DR")
    assert len(edit["reasons"]) == 2
    assert edit["removed"] == "**TL;DR:** VIX 31, short gamma.\n"


def test_ordinary_flip_verb_is_not_gamma_claim(isolated):
    _, payload, _ = isolated
    payload["gex"]["SPY"] = {"error": "missing chains"}
    issues = V.data_issues(payload, R._data_warning(payload))
    assert not any("would flip this" in BRIEF[a:b] for a, b, _ in V.outside_claims(BRIEF, issues))


def test_removing_fence_delimiter_cannot_publish_damaged_copy(isolated):
    _, payload, send = isolated
    mismatch(payload)
    source = BRIEF.replace("```\nES", "```VIX\nES", 1)
    save(isolated, source)
    assert run() == 5
    send.assert_not_called()


def test_gamma_claim_emptying_another_section_posts_disclosure(isolated):
    path, payload, _ = isolated
    payload["gex"]["SPY"] = {"error": "missing chains"}
    a, b = V.section_bodies(BRIEF)["## 📰"]
    save(isolated, BRIEF[:a] + "Short gamma.\n" + BRIEF[b:])
    assert run() == 0
    edit = next(r for r in records(path) if r["section"] == "## 📰")
    assert edit["action"] == "section_replacement"
    assert edit["removed"] == "Short gamma.\n"


def test_claim_in_unavailable_metrics_own_heading_still_refuses(isolated):
    _, payload, send = isolated
    mismatch(payload)
    save(isolated, BRIEF.replace("## 📈 Macro pulse", "## 📈 VIX 31 Macro pulse"))
    assert run() == 5
    send.assert_not_called()
