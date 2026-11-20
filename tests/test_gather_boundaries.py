"""Offline identity, archive retention, and shared newline measurement guards."""
import datetime as dt
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import test_compose_env as environment
from test_brief_metadata import sized_brief
from test_compose_env import DATE, outputs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import gather as G
import run_brief as R
import validate as V

composer = environment.composer


@pytest.fixture
def runner(monkeypatch, tmp_path):
    for name in ("STATE", "LOGS", "DATA", "BRIEFS", "PROJECT"):
        monkeypatch.setattr(config, name, tmp_path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "load_settings", dict)
    gather = Mock(side_effect=AssertionError("must not gather"))
    monkeypatch.setattr(G, "main", gather)
    return tmp_path, gather


@pytest.mark.parametrize("date", ["2000-01-03", "2099-01-05"])
@pytest.mark.parametrize("skip", [False, True])
def test_noncurrent_live_date_is_logged_and_refused(runner, date, skip):
    path, gather = runner
    args = ["--date", date, "--dry", "--ignore-window", "--force-weekend"]
    if skip:
        args.append("--skip-gather")  # Missing historical data must not trigger live gathering.
    assert R.main(args) == 9
    log = (path / f"{date}.log").read_text(encoding="utf-8")
    assert "live gathering only supports today's ET date" in log
    assert date in log and "--skip-gather" in log and "--deliver-only" in log
    gather.assert_not_called()


def test_existing_historical_block_can_skip_gather(runner, monkeypatch):
    path, gather = runner
    (path / f"{DATE}.json").write_text("{}", encoding="utf-8")
    import compose
    process = Mock(return_value=(False, "synthetic stop after reuse"))
    monkeypatch.setattr(compose, "run", process)
    assert R.main(["--date", DATE, "--skip-gather", "--dry", "--ignore-window", "--force-weekend"]) == 4
    process.assert_called_once()
    gather.assert_not_called()


def test_gather_entry_refuses_noncurrent_before_io(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(G, "BRIEFS_DIR", tmp_path / "briefs")
    fetch = Mock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(G, "gather", fetch)
    with pytest.raises(ValueError, match="live gathering only supports today's ET date"):
        G.main(dt.date(2000, 1, 3))
    fetch.assert_not_called()
    assert not (tmp_path / "data").exists()


def test_same_minute_archives_retain_every_block(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path)
    date = "2026-11-20"
    for number in range(3):
        block = {"as_of": "2026-11-20T08:00:00-05:00", "sample": number}
        (tmp_path / f"{date}.json").write_text(json.dumps(block), encoding="utf-8")
        (tmp_path / f"{date}.data.md").write_text(f"sample {number}", encoding="utf-8")
        G._archive_previous_run(date)
    files = sorted((tmp_path / "runs").glob("*.json"))
    assert len(files) == 3
    assert {json.loads(p.read_text(encoding="utf-8"))["sample"] for p in files} == {0, 1, 2}
    for p in files:
        number = json.loads(p.read_text(encoding="utf-8"))["sample"]
        assert p.with_suffix(".data.md").read_text(encoding="utf-8") == f"sample {number}"
    assert (tmp_path / "runs" / f"{date}-0800.json").exists()


@pytest.mark.parametrize("collision", ["json", "data.md"])
def test_archive_exclusive_create_refuses_racing_collision(monkeypatch, tmp_path, collision):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path)
    date = "2026-11-20"
    original = json.dumps({"as_of": "2026-11-20T08:00:00-05:00"}).encode()
    live_json = tmp_path / f"{date}.json"
    live_md = tmp_path / f"{date}.data.md"
    live_json.write_bytes(original)
    live_md.write_bytes(b"original markdown")
    target = tmp_path / "runs" / f"{date}-0800.{collision}"
    original_open = Path.open

    def race(path, mode="r", *args, **kwargs):
        if path == target and mode == "xb":
            with original_open(path, "wb") as competing:
                competing.write(b"competing archive")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", race)
    with pytest.raises(FileExistsError):
        G._archive_previous_run(date)
    assert target.read_bytes() == b"competing archive"
    assert live_json.read_bytes() == original
    assert live_md.read_bytes() == b"original markdown"


def test_orphan_markdown_archive_is_not_overwritten(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    orphan = runs / "2026-11-20-0800.data.md"
    orphan.write_bytes(b"older archive")
    (tmp_path / "2026-11-20.json").write_text(
        json.dumps({"as_of": "2026-11-20T08:00:00-05:00"}), encoding="utf-8")
    assert G._archive_previous_run("2026-11-20") == "0800-001"
    assert orphan.read_bytes() == b"older archive"
    assert (runs / "2026-11-20-0800-001.json").exists()


def test_gather_entry_passes_current_et_date(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(G, "BRIEFS_DIR", tmp_path / "briefs")
    current = G.pd.Timestamp.now(tz="America/New_York").date()
    fetch = Mock(return_value={"date": str(current)})
    monkeypatch.setattr(G, "gather", fetch)
    monkeypatch.setattr(G, "render_md", lambda _: "synthetic block")
    G.main(current)
    fetch.assert_called_once_with(current)
    assert (G.DATA_DIR / f"{current}.json").exists()


def test_two_same_minute_gathers_keep_old_and_latest_pairs(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(G, "BRIEFS_DIR", tmp_path / "briefs")
    current = G.pd.Timestamp.now(tz="America/New_York").date()
    stamp = f"{current}T08:00:00-05:00"
    fetch = Mock(side_effect=[{"date": str(current), "as_of": stamp, "sample": n} for n in range(3)])
    monkeypatch.setattr(G, "gather", fetch)
    monkeypatch.setattr(G, "render_md", lambda block: f"sample {block['sample']}")
    for _ in range(3):
        G.main(current)
    # Two replacements after an initial block archive both superseded pairs.
    runs = G.DATA_DIR / "runs"
    assert sorted(p.name for p in runs.iterdir()) == sorted([
        f"{current}-0800.json", f"{current}-0800.data.md",
        f"{current}-0800-001.json", f"{current}-0800-001.data.md",
    ])
    for suffix, sample in (("0800", 0), ("0800-001", 1)):
        assert json.loads((runs / f"{current}-{suffix}.json").read_text())["sample"] == sample
        assert (runs / f"{current}-{suffix}.data.md").read_text() == f"sample {sample}"
    assert json.loads((G.DATA_DIR / f"{current}.json").read_text())["sample"] == 2
    assert (G.DATA_DIR / f"{current}.data.md").read_text() == "sample 2"


def test_default_date_tracks_et_not_chicago_midnight(runner, monkeypatch):
    path, gather = runner
    real_datetime = dt.datetime

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 21, 5, 30, tzinfo=dt.UTC).astimezone(tz)

    monkeypatch.setattr(R.dt, "datetime", Clock)
    assert R.main(["--dry", "--ignore-window", "--force-weekend"]) == 3
    assert (path / "2026-11-21.log").exists()
    gather.assert_called_once_with(dt.date(2026, 11, 21))


@pytest.mark.parametrize("extra", [0, 1])
def test_crlf_has_same_ceiling_at_both_real_gates(composer, monkeypatch, extra):
    run, process, out, _ = composer
    brief = sized_brief(5200 + extra)

    def child(cmd, **kwargs):
        candidate, claims, end = outputs(cmd)
        candidate.write_bytes((brief + "\n" + end).replace("\n", "\r\n").encode("utf-8"))
        claims.write_bytes(b"[]")
        return Mock(returncode=0, stdout="", stderr="")

    process.side_effect = child
    ok, detail = run()
    # Independently exercise runner even when compose rejects the candidate.
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "DATA", config.STATE)
    monkeypatch.setattr(config, "LOGS", config.STATE)
    target = config.BRIEFS / f"{DATE}.md"
    target.write_bytes((brief + "\n<!-- market-brief complete: attempt-crlf -->").replace("\n", "\r\n").encode())
    (config.DATA / f"{DATE}.json").write_text(json.dumps({
        "as_of_et": "Sat Nov 14, 2026 8:00 AM ET",
        "gex": {s: {v: {"regime": "positive"} for v in ("near_term", "full_chain")}
                for s in ("SPY", "QQQ")},
    }), encoding="utf-8")
    code = R.main(["--date", DATE, "--deliver-only", "--dry", "--ignore-window", "--force-weekend"])
    assert code == (5 if extra else 0)
    assert ok is (extra == 0), detail
    if not extra:
        assert out.read_bytes().startswith(brief.replace("\n", "\r\n").encode("utf-8"))
    else:
        assert "length 5201 exceeds ceiling 5200" in detail


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_newline_normalization_at_validator(newline):
    brief = sized_brief(5200).replace("\n", newline)
    assert V.check(brief, DATE, "Sat November 14, 2026", 5200, None) == ([], [])
