"""D1 offline guards, runnable unchanged against a scratch baseline via D1_BASELINE."""
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from test_compose_env import BODY

sys.path.insert(0, os.environ.get("D1_BASELINE", str(Path(__file__).resolve().parents[1])))
import config
import deliver as D
import post_discord as P
import run_brief as R

DATE = "2026-11-19"
STAMP = "Thu Nov 19, 2026 9:20 AM ET"
BRIEF = BODY.replace("Sat November 14, 2026", "Thu November 19, 2026").replace(
    "Sat Nov 14, 2026 8:00 AM ET", STAMP) + "\n-# Run: " + "a" * 32 + "\n<!-- market-brief complete: attempt-d1 -->"
ET = ZoneInfo("America/New_York")
WEBHOOKS = ["https://example.invalid/a", "https://example.invalid/b"]
REAL_SEND = P._send
BANNER = ("⚠ STALE DATA — pulled 2026-11-19 09:20 ET; sending 2026-11-19 09:31 ET. "
          "A release window or the open has passed since the pull: 09:30 ET market open.")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    data_warning = R._data_warning
    monkeypatch.setattr(R, "_data_warning", lambda payload, now=None: data_warning(
        payload, now if now is not None else P._now_et()))
    for name in ("DATA", "STATE", "BRIEFS", "LOGS", "PROJECT"):
        monkeypatch.setattr(config, name, tmp_path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "load_settings", dict)
    monkeypatch.setattr(D, "LEDGER", tmp_path / "delivery.jsonl")
    monkeypatch.setattr(P, "_resolve", lambda w: WEBHOOKS if w is None else [w])
    monkeypatch.setattr(P, "_now_et", lambda: dt.datetime(2026, 11, 19, 9, 31, tzinfo=ET))
    monkeypatch.setattr(P.time, "sleep", lambda _: None)

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network or model subprocess")

    monkeypatch.setattr(P.requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(D.subprocess, "run", forbidden)
    monkeypatch.setattr(__import__("gather"), "main", Mock(side_effect=RuntimeError("offline gather stop")))
    monkeypatch.setattr(__import__("compose"), "run", forbidden)
    send = Mock()
    monkeypatch.setattr(P, "_send", send)
    payload = {"run_id": "a" * 32, "as_of_et": STAMP, "event_gate": {"today_events": [], "today_tier2": []},
               "gex": {s: {v: {"regime": "positive"} for v in ("near_term", "full_chain")}
                       for s in ("SPY", "QQQ")}}
    for quote in payload["gex"].values():
        quote.update(spot=100, spot_time="2026-11-19T09:20:00-05:00", spot_source="Tradier last")
    (tmp_path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / f"{DATE}.md").write_text(BRIEF, encoding="utf-8")
    (tmp_path / f"{DATE}.data.md").write_text("-# Run: " + "a" * 32, encoding="utf-8")
    return tmp_path, send, payload


def run(*flags):
    return R.main(["--date", DATE, "--deliver-only", "--ignore-window", *flags])


def rows():
    return [json.loads(line) for line in D.LEDGER.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("boundary", ["open", "window", "calendar"])
@pytest.mark.parametrize("route", ["production", "test"])
def test_stale_posts_banner_ledger_log_and_unchanged_file(isolated, monkeypatch, boundary, route):
    path, send, payload = isolated
    if route == "test":
        monkeypatch.setattr(config, "load_settings", lambda: {"discord": {"test_channel_id": "fake"}})

        def child(args, **kwargs):
            send("fake", Path(args[-1]).read_text(encoding="utf-8"))
            return Mock(returncode=0)

        monkeypatch.setattr(D.subprocess, "run", child)
    expected = "09:30 ET market open"
    if boundary != "open":
        monkeypatch.setattr(P, "_now_et", lambda: dt.datetime(2026, 11, 19, 9, 21, tzinfo=ET))
        if boundary == "window":
            monkeypatch.setattr(P, "_RELEASE_CLOCK", [("09:21", "release window")])
            expected = "09:21 ET release window"
        else:
            payload["event_gate"]["today_tier2"] = [{"time_et": "09:21", "name": "test release"}]
            (path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
            expected = "09:21 ET test release"
    before = hashlib.sha256((path / f"{DATE}.md").read_bytes()).hexdigest()
    assert run(*(["--test"] if route == "test" else [])) == 0
    first = send.call_args_list[0].args[1]
    assert first.startswith("⚠ STALE DATA") and expected in first
    if boundary == "open":
        assert first.startswith(BANNER + "\n\n")
        print("BANNER:", BANNER)
    assert all(len(c.args[1]) <= P.LIMIT for c in send.call_args_list)
    assert all("market-brief complete:" not in c.args[1] for c in send.call_args_list)
    assert all(r["stale"] and expected in r["stale_reason"] for r in rows())
    # A split layout is durably planned before either fragment can be sent.
    assert all(r["status"] in {"planned", "chunk_ok", "ok"} for r in rows())
    assert rows()[-1]["status"] == "ok"
    assert "stale:" in (path / f"{DATE}.log").read_text(encoding="utf-8")
    assert expected in (path / f"{DATE}.log").read_text(encoding="utf-8")
    assert hashlib.sha256((path / f"{DATE}.md").read_bytes()).hexdigest() == before


@pytest.mark.parametrize("defect", ["unreadable", "mismatch", "prior", "future"])
def test_untrusted_stamp_posts_only_failure_alert(isolated, defect):
    path, send, payload = isolated
    values = {"unreadable": "nonsense ET", "mismatch": "Thu Nov 19, 2026 9:19 AM ET",
              "prior": "Wed Nov 18, 2026 9:20 AM ET", "future": "Thu Nov 19, 2026 9:32 AM ET"}
    value = values[defect]
    (path / f"{DATE}.md").write_text(BRIEF.replace(STAMP, value), encoding="utf-8")
    if defect != "mismatch":
        payload["as_of_et"] = value
        (path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
    assert run() == (5 if defect in ("unreadable", "mismatch") else 7)
    assert send.call_count == 2
    assert all("Market brief failed" in c.args[1] and "Nothing was posted." in c.args[1]
               for c in send.call_args_list)
    assert not D.LEDGER.exists()


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("test", [False, True])
def test_validation_alert_ignores_dry_and_test(isolated, dry, test):
    path, send, _ = isolated
    (path / f"{DATE}.md").write_text("bad brief", encoding="utf-8")
    assert run(*(["--dry"] if dry else []), *(["--test"] if test else [])) == 5
    assert [c.args[0] for c in send.call_args_list] == WEBHOOKS
    assert all("validate:" in c.args[1] and "Exit code 5. Nothing was posted." in c.args[1]
               for c in send.call_args_list)


@pytest.mark.parametrize("code", [3, 4, 5, 6, 7, 8, 9])
def test_every_failure_code_alerted_and_preserved(isolated, monkeypatch, code):
    path, send, _ = isolated

    def failed(argv, state):
        state.update(stage="gather", reason="vendor unavailable", log_path=path / "failed.log")
        return code

    monkeypatch.setattr(R, "_run", failed, raising=False)
    assert R.main([]) == code
    assert send.call_count == 2
    message = f"Market brief failed — gather: vendor unavailable Exit code {code}. Nothing was posted."
    assert send.call_args.args[1] == message
    assert message in (path / "failed.log").read_text(encoding="utf-8")
    if code == 3:
        print("ALERT:", message)


@pytest.mark.parametrize("code", [0, 2])
def test_success_and_skip_do_not_alert(isolated, monkeypatch, code):
    _, send, _ = isolated
    monkeypatch.setattr(R, "_run", lambda *_: code, raising=False)
    assert R.main([]) == code
    send.assert_not_called()


def test_alert_transport_failure_logged_original_exit_retained(isolated):
    path, send, _ = isolated
    send.side_effect = RuntimeError("Discord down")
    (path / f"{DATE}.md").write_text("bad brief", encoding="utf-8")
    assert run("--dry") == 5
    assert send.call_count == 2
    assert "failure alert failed:" in (path / f"{DATE}.log").read_text(encoding="utf-8")
    assert "Discord down" in (path / f"{DATE}.log").read_text(encoding="utf-8")


def test_unexpected_exception_alerted_with_stage_without_private_details(isolated, monkeypatch):
    path, send, _ = isolated

    def failed(argv, state):
        state.update(stage="compose", log_path=path / "exception.log")
        raise ValueError("private response body")

    monkeypatch.setattr(R, "_run", failed, raising=False)
    assert R.main([]) == 1
    assert "compose: unexpected ValueError Exit code 1" in send.call_args.args[1]
    assert "private" not in send.call_args.args[1]


@pytest.mark.parametrize("initially_stale", [False, True])
def test_banner_rechunks_near_limit_and_retry_keeps_acknowledged_prefix(isolated, monkeypatch, initially_stale):
    _, send, _ = isolated
    if not initially_stale:
        monkeypatch.setattr(P, "_now_et", lambda: dt.datetime(2026, 11, 19, 9, 21, tzinfo=ET))
    md = (f"*Data pulled {STAMP}*\n" + "x" * 1800 + "\n## Next\n" + "y" * 1600
          + "\n---\n*Not financial advice — situational awareness only.*\n-# Run: " + "a" * 32)
    attempts = []
    fail = True

    def transport(target, content):
        attempts.append((target, content))
        if fail and "## Next" in content:
            raise RuntimeError("mock failure")

    monkeypatch.setattr(P, "_send", transport)
    assert not D.deliver_production(md, DATE)[0]
    acknowledged = [(w, p) for w, p in attempts if "## Next" not in p]
    assert acknowledged
    assert all(len(p) <= P.LIMIT for _, p in attempts)
    fail = False
    attempts.clear()
    monkeypatch.setattr(P, "_now_et", lambda: dt.datetime(2026, 11, 19, 9, 40, tzinfo=ET))
    assert D.deliver_production(md, DATE)[0]
    assert all(p not in wire for _, p in acknowledged for _, wire in attempts)
    assert all(next(p for w, p in attempts if w == target).startswith("⚠ STALE DATA")
               for target in WEBHOOKS)
    attempts.clear()
    assert D.deliver_production(md, DATE)[0]
    assert not attempts
    send.assert_not_called()


def test_partial_delivery_alert_is_truthful(isolated, monkeypatch):
    _, send, _ = isolated
    original = P._send

    def transport(target, content):
        if content.startswith("Market brief failed"):
            original(target, content)
        elif target == WEBHOOKS[1]:
            raise RuntimeError("mock failure")

    monkeypatch.setattr(P, "_send", transport)
    assert run() == 7
    assert send.call_count == 2
    assert "Delivery may be partial" in send.call_args.args[1]
    assert "Nothing was posted" not in send.call_args.args[1]


@pytest.mark.parametrize("code", [3, 4, 5, 6, 7, 8, 9])
def test_real_run_failure_paths_alert(isolated, monkeypatch, code):
    path, send, payload = isolated
    real_datetime = dt.datetime

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 19, 14, 31, tzinfo=dt.UTC).astimezone(tz)

    monkeypatch.setattr(R.dt, "datetime", Clock)
    args = ["--date", DATE, "--ignore-window"]
    if code == 4:
        args.append("--skip-gather")
        monkeypatch.setattr(__import__("compose"), "run", lambda *_, **kw: (False, "writer unavailable"))
    elif code in (5, 7):
        args.append("--deliver-only")
        if code == 5:
            (path / f"{DATE}.md").write_text("bad brief", encoding="utf-8")
        else:
            future = "Thu Nov 19, 2026 9:32 AM ET"
            payload["as_of_et"] = future
            (path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
            (path / f"{DATE}.md").write_text(BRIEF.replace(STAMP, future), encoding="utf-8")
    elif code == 6:
        args.remove("--ignore-window")
    elif code == 8:
        monkeypatch.setattr(config, "load_settings", lambda: {"limits": {"monthly_usd_cap": 0}})
    elif code == 9:
        args[1] = "2000-01-03"
    assert R.main(args) == code
    assert send.call_count == 2
    stage = {3: "gather", 4: "compose", 5: "validate", 6: "setup", 7: "deliver", 8: "setup", 9: "setup"}[code]
    assert f"— {stage}:" in send.call_args.args[1]
    assert f"Exit code {code}. Nothing was posted." in send.call_args.args[1]


@pytest.mark.parametrize("complete", [False, True])
def test_pre_d1_chunk_progress_survives_new_banner_budget(isolated, complete):
    _, send, _ = isolated
    md = (f"*Data pulled {STAMP}*\n" + "prefix " * 220 + "\n## Tail\n" + "suffix " * 220
          + "\n---\n*Not financial advice — situational awareness only.*\n-# Run: " + "a" * 32)
    original = P.chunk(md)
    old_id = D._identity(md, original)
    D.LEDGER.write_text("\n".join(json.dumps({
        "date": DATE, "target": "production:" + P.fingerprint(target), "brief_id": old_id,
        "status": "ok" if complete else "chunk_ok", "chunk_index": 0,
    }) for target in WEBHOOKS) + "\n", encoding="utf-8")
    assert D.deliver_production(md, DATE)[0]
    if complete:
        send.assert_not_called()
    else:
        assert send.call_count
        assert all("prefix" not in c.args[1] for c in send.call_args_list)
        assert any("suffix" in c.args[1] for c in send.call_args_list)
    send.reset_mock()
    assert D.deliver_production(md, DATE)[0]
    send.assert_not_called()


@pytest.mark.parametrize("scenario", ["dry", "weekend", "holiday"])
def test_real_success_and_no_work_remain_silent(isolated, scenario):
    path, send, _ = isolated
    if scenario == "dry":
        code = run("--dry")
        assert code == 0
    elif scenario == "holiday":
        (path / "market_holidays.yaml").write_text(f"holidays:\n  {DATE}: test closure\n", encoding="utf-8")
        assert run() == 2
    else:
        assert R.main(["--date", "2026-11-21", "--deliver-only", "--ignore-window"]) == 2
    send.assert_not_called()


@pytest.mark.parametrize("scenario", ["arguments", "date", "json"])
def test_actual_unexpected_failures_alert(isolated, scenario):
    path, send, _ = isolated
    if scenario == "arguments":
        code = R.main(["--unknown-option"])
    elif scenario == "date":
        code = R.main(["--date", "not-a-date"])
    else:
        (path / f"{DATE}.json").write_text("[", encoding="utf-8")
        code = run()
    assert code == 1
    assert send.call_count == 2
    assert "Exit code 1. Nothing was posted." in send.call_args.args[1]


def test_alert_failure_at_one_destination_does_not_block_other(isolated):
    path, send, _ = isolated
    send.side_effect = [RuntimeError("Discord down"), None]
    (path / f"{DATE}.md").write_text("bad brief", encoding="utf-8")
    assert run("--dry") == 5
    assert [c.args[0] for c in send.call_args_list] == WEBHOOKS
    assert "failure alert failed:" in (path / f"{DATE}.log").read_text(encoding="utf-8")


def test_alert_reason_is_short_single_line_and_webhook_redacted(isolated):
    _, send, _ = isolated
    secret = "https://discord.com/api/webhooks/123/" + "x" * 70
    P.alert_failure("compose", f"unavailable {secret}\n" + "reason " * 200, 4)
    content = send.call_args.args[1]
    assert "[REDACTED]" in content and secret not in content
    assert "\n" not in content and len(content) < P.LIMIT


DIAGNOSTICS = ["<!DOCTYPE html>", "<!-- unmatched", "-->", "<?",
               "<!-- outer <!-- inner --> -->"]


@pytest.mark.parametrize("reason", DIAGNOSTICS)
@pytest.mark.parametrize("stage,code", [("gather", 3), ("compose", 4), ("validate", 5)])
def test_failure_alert_sends_literal_diagnostics_per_destination(isolated, stage, code, reason):
    _, send, _ = isolated
    message = P.alert_failure(stage, reason, code)
    assert [c.args for c in send.call_args_list] == [(w, message) for w in WEBHOOKS]
    assert reason in message


@pytest.mark.parametrize("long", [False, True])
def test_failure_alert_short_and_split_messages_stay_bounded(isolated, monkeypatch, long):
    _, send, _ = isolated
    # The normal 500-character reason cap fits LIMIT. Lower the transport limit
    # to exercise splitting without weakening reason truncation.
    monkeypatch.setattr(P, "LIMIT", 160)
    reason = "vendor unavailable" if not long else "<!-- ``` reason " * 100
    message = P.alert_failure("gather", reason, 3)
    assert " ".join(reason.split())[:500] in message
    for target in WEBHOOKS:
        parts = [c.args[1] for c in send.call_args_list if c.args[0] == target]
        assert (len(parts) > 1) if long else (len(parts) == 1)
        assert all(0 < len(part) <= P.LIMIT for part in parts)
        assert "".join(parts) == message


def test_failure_alert_redacts_token_before_literal_splitting(isolated, monkeypatch):
    _, send, _ = isolated
    monkeypatch.setattr(P, "LIMIT", 80)
    token = "synthetic_token_" * 8
    message = P.alert_failure("gather", f"<!DOCTYPE html> token={token}", 3)
    assert token not in message and "[REDACTED]" in message
    for target in WEBHOOKS:
        text = "".join(c.args[1] for c in send.call_args_list if c.args[0] == target)
        assert text == message and token not in text


def test_literal_failure_alert_attempts_every_destination_after_failure(isolated):
    _, send, _ = isolated
    send.side_effect = [RuntimeError("transport down"), None]
    with pytest.raises(RuntimeError, match="failure alert not posted"):
        P.alert_failure("gather", "<!DOCTYPE html>", 3)
    assert [c.args[0] for c in send.call_args_list] == WEBHOOKS


def test_literal_failure_alert_suppresses_mentions(isolated, monkeypatch):
    request = Mock(return_value=Mock(status_code=204))
    monkeypatch.setattr(P, "_send", REAL_SEND)
    monkeypatch.setattr(P.requests, "post", request)
    P.alert_failure("gather", "<!-- @everyone @here <@123> -->", 3)
    assert [c.args[0] for c in request.call_args_list] == WEBHOOKS
    assert all(c.kwargs["json"]["allowed_mentions"] == {"parse": []}
               for c in request.call_args_list)


@pytest.mark.parametrize("name", DIAGNOSTICS)
def test_banner_preserves_literal_calendar_diagnostics(isolated, name):
    path, send, payload = isolated
    payload["event_gate"]["today_events"] = [{"time_et": "09:25", "name": name}]
    (path / f"{DATE}.json").write_text(json.dumps(payload), encoding="utf-8")
    assert D.deliver_production(BRIEF, DATE)[0]
    for target in WEBHOOKS:
        parts = [c.args[1] for c in send.call_args_list if c.args[0] == target]
        assert parts[0].startswith("⚠ STALE DATA") and name in parts[0]
        assert all(len(part) <= P.LIMIT for part in parts)
