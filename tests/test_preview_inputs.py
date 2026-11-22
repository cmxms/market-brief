"""Offline regressions for preview inputs and pre-model budget failures."""
import datetime as dt
import json
import subprocess
from unittest.mock import Mock

import pytest
import test_dry_promotion as environment
from test_compose_env import BODY, DATE
from test_dry_promotion import FLAGS, ID, digest

import compose as C
import config
import gather as G
import run_brief as R

composer = environment.composer
preview = environment.preview
REAL_GATHER_MAIN = G.main


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("dry", [True, False])
def test_gather_preserves_preview_inputs_and_real_archive(preview, monkeypatch, failure, dry):
    process, send, alert = preview
    today = dt.date.fromisoformat(DATE)
    real_datetime = dt.datetime

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 14, 8, tzinfo=R.ET).astimezone(tz)

    monkeypatch.setattr(R.dt, "datetime", Clock)
    monkeypatch.setattr(G.pd, "Timestamp", Mock(now=Mock(return_value=Mock(date=lambda: today))))
    monkeypatch.setattr(G, "main", REAL_GATHER_MAIN)
    monkeypatch.setattr(G, "DATA_DIR", config.DATA)
    monkeypatch.setattr(G, "BRIEFS_DIR", config.BRIEFS)
    data = config.DATA / f"{DATE}.json"
    md = config.DATA / f"{DATE}.data.md"
    out = config.BRIEFS / f"{DATE}.md"
    claims = config.STATE / f"claims-{DATE}.json"
    payload = json.loads(data.read_text(encoding="utf-8")) | {"date": DATE}
    monkeypatch.setattr(G, "gather", Mock(return_value=payload.copy()))
    monkeypatch.setattr(G, "render_md", lambda p: "synthetic\n-# Run: " + p["run_id"])
    out.write_bytes((BODY + "\n-# Run: " + ID).encode("utf-8"))
    claims.write_bytes(b"[]\r\n")
    before = {p: digest(p) for p in (data, md, out, claims)}
    original_data = data.read_bytes()
    runs = config.DATA / "runs"
    runs.mkdir()
    retained = runs / "retained.json"
    retained.write_bytes(b"retained archive")
    archives = {p: digest(p) for p in runs.iterdir()}
    assert R.main(FLAGS + ["--deliver-only"]) == 0
    if failure:
        process.side_effect = None  # Child exits nonzero without writing artifacts.
        process.return_value.returncode = 7
    import deliver
    delivery = Mock(return_value=(True, "stubbed transport"))
    monkeypatch.setattr(deliver, "deliver_production", delivery)
    flags = [f for f in FLAGS if f != "--skip-gather" and (dry or f != "--dry")]
    assert R.main(flags) == (4 if failure else 0)
    if dry:
        assert {p: digest(p) for p in before} == before
        assert {p: digest(p) for p in runs.iterdir()} == archives
        assert R.main(FLAGS + ["--deliver-only"]) == 0
        print(f"CHECK dry failure={failure}: canonical JSON/Markdown, brief, claims and archives byte-identical; deliver-only dry exit=0")
        delivery.assert_not_called()
    else:
        assert (runs / f"{DATE}-{ID}.json").read_bytes() == original_data
        assert digest(runs / f"{DATE}-{ID}.data.md") == before[md]
        assert digest(retained) == archives[retained]
        assert json.loads(data.read_text(encoding="utf-8"))["run_id"] != ID
        if failure:
            assert digest(out) == before[out]
    process.assert_called_once()
    send.assert_not_called()
    assert alert.call_count == int(failure)


@pytest.mark.parametrize("allocation", ["preview-", "attempt-"])
def test_allocation_failure_does_not_consume_budget(preview, monkeypatch, allocation):
    process, send, alert = preview
    monkeypatch.setattr(config, "load_settings", lambda: {
        "limits": {"monthly_usd_cap": 0.05, "estimated_usd_per_run": 0.05}})
    allocate = R.tempfile.mkdtemp

    def fail(*args, **kwargs):
        if kwargs.get("prefix") == allocation:
            raise OSError("synthetic allocation failure")
        return allocate(*args, **kwargs)

    monkeypatch.setattr(R.tempfile, "mkdtemp", fail)
    assert R.main(FLAGS) == 4
    process.assert_not_called()
    alert.assert_called_once()
    monkeypatch.setattr(R.tempfile, "mkdtemp", allocate)
    assert R.main(FLAGS) == 0  # A phantom 0.05 charge would return 8.
    process.assert_called_once()
    rows = [json.loads(row) for row in (config.STATE / "usage.jsonl").read_text().splitlines()]
    assert [row["estimated_usd"] for row in rows] == [0.05]
    send.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_legacy_preview_upgrades_only_its_copy(preview, monkeypatch, failure):
    process, send, _ = preview
    data = config.DATA / f"{DATE}.json"
    md = config.DATA / f"{DATE}.data.md"
    payload = json.loads(data.read_text(encoding="utf-8"))
    del payload["run_id"]
    payload["as_of"] = "2026-11-14T08:00:00-05:00"
    data.write_text(json.dumps(payload), encoding="utf-8")
    md.write_bytes(b"legacy input\r\n")
    before = data.read_bytes(), md.read_bytes()
    monkeypatch.setattr(G, "render_md", lambda p: "synthetic\n-# Run: " + p["run_id"])
    if failure:
        process.side_effect = None
    assert R.main(FLAGS) == (4 if failure else 0)
    assert (data.read_bytes(), md.read_bytes()) == before
    assert not (config.DATA / "runs").exists()
    local = next((config.STATE / "compose-attempts").glob("preview-*"))
    assert (local / "runs" / f"{DATE}-0800.json").read_bytes() == before[0]
    assert (local / "runs" / f"{DATE}-0800.data.md").read_bytes() == before[1]
    assert "run_id" in json.loads((local / data.name).read_text(encoding="utf-8"))
    send.assert_not_called()


@pytest.mark.parametrize("outcome", ["success", "nonzero", "timeout", "launch-failure"])
def test_invocation_accounting_preserves_real_charges(preview, outcome):
    process, _, _ = preview
    charge = Mock()
    if outcome == "nonzero":
        process.side_effect = None
        process.return_value.returncode = 7
    elif outcome == "timeout":
        process.side_effect = subprocess.TimeoutExpired("fake-hermes", 1)
    elif outcome == "launch-failure":
        process.side_effect = FileNotFoundError(2, "synthetic missing launcher")
    ok, _ = C.run(DATE, config.DATA / f"{DATE}.data.md",
                  config.STATE / "output.md", config.STATE / "compose.log",
                  run_id=ID, record_invocation=charge)
    assert ok is (outcome == "success")
    assert charge.call_count == int(outcome != "launch-failure")
    process.assert_called_once()
