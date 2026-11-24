"""Dry previews retain attempts, never replace delivered artifacts (offline)."""
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import test_compose_env as environment
from test_compose_env import BODY, DATE, outputs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import gather as G
import post_discord as P
import run_brief as R

composer = environment.composer
ID = "a" * 32
FLAGS = ["--date", DATE, "--skip-gather", "--dry", "--ignore-window", "--force-weekend"]


@pytest.fixture
def preview(composer, monkeypatch):
    now = R.dt.datetime.fromisoformat("2026-11-14T08:00:00-05:00")
    data_warning = R._data_warning
    monkeypatch.setattr(R, "_data_warning", lambda payload: data_warning(payload, now))
    _, process, _, _ = composer
    briefs = config.STATE / "briefs"
    briefs.mkdir()
    monkeypatch.setattr(config, "BRIEFS", briefs)
    (briefs / "2026-07-14.md").write_bytes(b"reference")
    (briefs / "nested").mkdir()
    (briefs / "nested" / "retained.md").write_bytes(b"retained archive")
    monkeypatch.setattr(config, "DATA", config.STATE)
    monkeypatch.setattr(config, "LOGS", config.STATE)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(G, "main", Mock(side_effect=AssertionError("must reuse data")))
    send = Mock(side_effect=AssertionError("must not post"))
    monkeypatch.setattr(P, "_send", send)
    alert = Mock()
    monkeypatch.setattr(P, "alert_failure", alert)
    payload = {"run_id": ID, "as_of_et": "Sat Nov 14, 2026 8:00 AM ET",
               "gex": {s: {v: {"regime": "positive"} for v in ("near_term", "full_chain")}
                       for s in ("SPY", "QQQ")}}
    for quote in payload["gex"].values():
        quote.update(spot=100, spot_time=now.isoformat(), spot_source="Tradier last")
    (config.DATA / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
    (config.DATA / f"{DATE}.data.md").write_text("-# Run: " + ID, encoding="utf-8")

    def child(cmd, **kwargs):
        candidate, claims, end = outputs(cmd)
        candidate.write_bytes((BODY + "\n" + end).encode("utf-8"))
        claims.write_bytes(b"[]")
        return Mock(returncode=0, stdout="", stderr="")

    process.side_effect = child
    return process, send, alert


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("existing", [True, False])
def test_dry_preserves_promoted_artifacts_and_prints_current_attempt(preview, capsys, existing):
    process, send, alert = preview
    out = config.BRIEFS / f"{DATE}.md"
    claims = config.STATE / f"claims-{DATE}.json"
    if existing:
        out.write_bytes(b"already delivered brief\r\n")
        claims.write_bytes(b'[{"previous": true}]\r\n')
    before = {p: digest(p) for p in (out, claims) if p.exists()}
    brief_tree = {p: digest(p) for p in config.BRIEFS.rglob("*") if p.is_file()}
    assert R.main(FLAGS) == 0
    assert {p: digest(p) for p in (out, claims) if p.exists()} == before
    assert {p: digest(p) for p in config.BRIEFS.rglob("*") if p.is_file()} == brief_tree
    candidate, ledger, _ = outputs(process.call_args.args[0])
    record = json.loads((candidate.parent / "complete.json").read_text(encoding="utf-8"))
    assert digest(candidate) == record["brief"]["sha256"]
    assert digest(ledger) == record["claims"]["sha256"]
    assert candidate.is_relative_to(config.STATE / "compose-attempts")
    text = candidate.read_bytes().decode("utf-8")
    assert "-# Run: " + ID in text
    parts = P.chunk(text)
    output = capsys.readouterr().out
    printed = output.split("----- message ")[1:]
    assert len(printed) == len(parts)
    for index, (section, part) in enumerate(zip(printed, parts), 1):
        assert section == f"{index}/{len(parts)} ({len(part)} chars) -----\n{part}\n"
    assert "claims ledger: 0 sourced claim(s)" in output
    process.assert_called_once()
    send.assert_not_called()
    alert.assert_not_called()


@pytest.mark.parametrize("failure,code", [("compose", 4), ("validate", 5)])
def test_failed_dry_keeps_archive_and_exit_code(preview, failure, code):
    process, send, alert = preview
    out = config.BRIEFS / f"{DATE}.md"
    claims = config.STATE / f"claims-{DATE}.json"
    out.write_bytes(b"already delivered")
    claims.write_bytes(b"[]\n")
    before = digest(out), digest(claims)
    if failure == "compose":
        process.side_effect = None  # Successful child exit without any artifacts.
    else:
        payload_path = config.DATA / f"{DATE}.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        payload["as_of_et"] = "Sat Nov 14, 2026 7:59 AM ET"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
    assert R.main(FLAGS) == code
    assert (digest(out), digest(claims)) == before
    send.assert_not_called()
    alert.assert_called_once()


def test_deliver_only_dry_reuses_archive_without_composing(preview, capsys):
    process, send, alert = preview
    out = config.BRIEFS / f"{DATE}.md"
    out.write_bytes((BODY + "\n-# Run: " + ID).encode("utf-8"))
    claims = config.STATE / f"claims-{DATE}.json"
    claims.write_bytes(b"[]\n")
    before = digest(out), digest(claims)
    assert R.main(FLAGS + ["--deliver-only"]) == 0
    assert (digest(out), digest(claims)) == before
    assert not (config.STATE / "compose-attempts").exists()
    assert "DRY: 2 Discord message(s), nothing posted" in capsys.readouterr().out
    process.assert_not_called()
    send.assert_not_called()
    alert.assert_not_called()


def test_preview_creation_failure_keeps_compose_exit_code(preview, monkeypatch):
    process, send, alert = preview
    out = config.BRIEFS / f"{DATE}.md"
    claims = config.STATE / f"claims-{DATE}.json"
    out.write_bytes(b"already delivered")
    claims.write_bytes(b"[]\n")
    before = digest(out), digest(claims)
    monkeypatch.setattr(R.tempfile, "mkdtemp", Mock(side_effect=OSError("synthetic failure")))
    assert R.main(FLAGS) == 4
    assert (digest(out), digest(claims)) == before
    process.assert_not_called()
    send.assert_not_called()
    alert.assert_called_once()
