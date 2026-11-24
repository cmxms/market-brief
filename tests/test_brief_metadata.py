"""Reader-visible length, playbook dates, and transport metadata regressions."""
import datetime as dt
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import test_compose_env as environment
from test_compose_env import BODY, DATE, FOOTER, outputs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compose as C
import config
import post_discord as P
import run_brief as R
import validate as V

composer = environment.composer

MARKER = "<!-- market-brief complete: attempt-metadata-test -->"


def writer(*, brief=BODY):
    def child(cmd, **kwargs):
        candidate, claims, end = outputs(cmd)
        # Exact LF bytes keep the character boundary independent of host newlines.
        candidate.write_bytes((brief + "\n" + end).encode("utf-8"))
        claims.write_bytes(b"[]")
        return Mock(returncode=7, stdout="", stderr="synthetic provider failure")
    return child


def sized_brief(size):
    padding = "x" * (size - len(BODY) - 1)
    brief = BODY.replace(FOOTER, padding + "\n" + FOOTER)
    assert len(brief) == size
    return brief


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("trailing", ["", "\n\n"])
def test_marker_does_not_count_against_ceiling(extra, trailing):
    brief = sized_brief(5200 + extra)
    errors, warnings = V.check(
        brief + "\n" + MARKER + trailing, DATE, "Sat November 14, 2026", 5200, None
    )
    assert errors == (["length 5201 exceeds ceiling 5200"] if extra else [])
    assert warnings == []


@pytest.mark.parametrize("extra", [0, 1])
def test_marker_ceiling_at_promotion(composer, extra):
    run, process, out, _ = composer
    process.side_effect = writer(brief=sized_brief(5200 + extra))
    ok, detail = run()
    assert ok is (extra == 0), detail
    assert out.exists() is (extra == 0)
    if extra:
        assert "length 5201 exceeds ceiling 5200" in detail


def test_promoted_marker_never_reaches_message_chunks(composer):
    run, process, out, _ = composer
    process.side_effect = writer()
    ok, detail = run()
    assert ok, detail
    promoted = out.read_text(encoding="utf-8")
    marker = V.COMPLETION_RE.search(promoted)
    assert marker is not None  # Prove the promoted input still carries metadata.
    chunks = P.chunk(promoted)
    assert chunks
    assert all(marker.group().strip() not in chunk for chunk in chunks)
    assert all("market-brief complete:" not in chunk for chunk in chunks)
    assert FOOTER in "\n".join(chunks)


@pytest.mark.parametrize("day,weekday", [(1, "Tue"), (5, "Sat"), (9, "Wed"), (15, "Tue"), (31, "Thu")])
@pytest.mark.parametrize("padded", [False, True], ids=["playbook", "padded-tolerance"])
@pytest.mark.parametrize("gate", ["compose", "runner"])
def test_playbook_title_date_at_both_gates(composer, monkeypatch, day, weekday, padded, gate):
    _, process, out, log = composer
    date = dt.date(2026, 12, day).isoformat()
    display = f"{weekday} December {day}, 2026"
    title_date = f"{weekday} December {day:02d}, 2026" if padded else display
    brief = BODY.replace("Sat November 14, 2026", title_date)
    check = Mock(wraps=V.check)
    monkeypatch.setattr(V, "check", check)
    if gate == "compose":
        process.side_effect = writer(brief=brief)
        ok, detail = C.run(date, config.STATE / "data.md", out, log)
        assert ok, detail
    else:
        now = dt.datetime.fromisoformat("2026-11-14T08:00:00-05:00")
        data_warning = R._data_warning
        monkeypatch.setattr(R, "_data_warning", lambda payload: data_warning(payload, now))
        monkeypatch.setattr(config, "ensure_dirs", lambda: None)
        monkeypatch.setattr(config, "load_env", lambda: None)
        monkeypatch.setattr(config, "DATA", config.STATE)
        monkeypatch.setattr(config, "LOGS", config.STATE)
        (config.BRIEFS / f"{date}.md").write_text(brief + "\n-# Run: " + "a" * 32 + "\n" + MARKER, encoding="utf-8")
        (config.DATA / f"{date}.json").write_text(json.dumps({
            "run_id": "a" * 32,
            "as_of_et": "Sat Nov 14, 2026 8:00 AM ET",
            "gex": {s: {"spot": 100, "spot_time": now.isoformat(), "spot_source": "Tradier last",
                        **{v: {"regime": "positive"} for v in ("near_term", "full_chain")}}
                    for s in ("SPY", "QQQ")},
        }), encoding="utf-8")
        # Exercise validation and real chunking, never a delivery transport.
        code = R.main(["--date", date, "--deliver-only", "--dry", "--ignore-window", "--force-weekend"])
        assert code == 0
    check.assert_called_once()
    assert check.call_args.args[2] == display


def test_wrong_title_date_remains_rejected():
    brief = BODY.replace("Sat November 14, 2026", "Sat December 06, 2026")
    errors, _ = V.check(brief, "2026-12-05", "Sat December 5, 2026", 5200, None)
    assert errors == ["title does not carry the expected date (Sat December 5, 2026)"]
