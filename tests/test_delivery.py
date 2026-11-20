"""Offline regression tests for Discord credential handling and delivery retries."""
import hashlib
import json
import sys
import traceback
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deliver as D
import post_discord as P

TOKEN_A = "A1_-" * 17
TOKEN_B = "B2_-" * 17
WEBHOOK_A = f"https://discord.com/api/webhooks/111111111111111111/{TOKEN_A}"
WEBHOOK_B = f"https://canary.discordapp.com/api/webhooks/222222222222222222/{TOKEN_B}"
DATE = "2026-11-14"


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    # Fail closed if a test accidentally reaches a real transport or pacing delay.
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network, subprocess, or sleep")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(D.subprocess, "run", forbidden)
    monkeypatch.setattr(P.time, "sleep", forbidden)
    monkeypatch.setattr(D, "LEDGER", tmp_path / "delivery.jsonl")
    # These tests isolate transport/ledger behaviour. Real freshness gates are
    # exercised without this stub in test_brief_truth.py.
    monkeypatch.setattr(D, "_freshness_failure", lambda *_: None)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_A)
    monkeypatch.setenv("DISCORD_WEBHOOK_URLS", WEBHOOK_B)


def rows():
    return [json.loads(line) for line in D.LEDGER.read_text(encoding="utf-8").splitlines()]


def assert_no_credentials(text):
    for secret in (TOKEN_A, TOKEN_B, WEBHOOK_A, WEBHOOK_B, "/api/webhooks/", "https://"):
        assert secret not in text


@pytest.mark.parametrize("host", [
    "discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com",
    "ptb.discordapp.com", "canary.discordapp.com",
])
def test_sanitize_http_error_hosts(host):
    url = f"https://{host}/api/webhooks/111111111111111111/{TOKEN_A}"
    error = requests.HTTPError(f"401 Client Error: Unauthorized for url: {url}")
    clean = P.sanitize(str(error))
    assert "401 Client Error: Unauthorized" in clean
    assert_no_credentials(clean)


@pytest.mark.parametrize("render", [str, repr])
def test_sanitize_connection_error_and_bare_path(render):
    error = requests.ConnectionError(
        f"HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded "
        f"with url: /api/webhooks/111111111111111111/{TOKEN_A}"
    )
    clean = P.sanitize(render(error))
    assert "Max retries exceeded" in clean
    assert_no_credentials(clean)


def test_sanitize_bare_token_and_traceback():
    try:
        raise RuntimeError(f"request failed: {WEBHOOK_A}; token={TOKEN_B}")
    except RuntimeError:
        clean = P.sanitize(traceback.format_exc())
    assert "RuntimeError" in clean
    assert_no_credentials(clean)


def test_sanitize_ordinary_text_unchanged():
    text = "Connection refused; retry later (401)."
    assert P.sanitize(text) == text


@pytest.mark.parametrize("send_file", [False, True])
@pytest.mark.parametrize("failure", ["http", "connection"])
def test_send_raises_only_safe_messages(monkeypatch, tmp_path, send_file, failure):
    if failure == "http":
        response = requests.Response()
        response.status_code = 401
        response.url = WEBHOOK_A
        request = Mock(return_value=response)
    else:
        request = Mock(side_effect=requests.ConnectionError(f"failed {WEBHOOK_A}"))
    monkeypatch.setattr(P.requests, "post", request)
    path = tmp_path / "card.png"
    path.write_bytes(b"test image; no real upload")
    with pytest.raises(RuntimeError) as caught:
        if send_file:
            P._send_file(WEBHOOK_A, path)
        else:
            P._send(WEBHOOK_A, "brief")
    assert_no_credentials(str(caught.value))
    assert_no_credentials(repr(caught.value))
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert_no_credentials(rendered)


@pytest.mark.parametrize("image", [False, True])
def test_fanout_warning_sanitized(monkeypatch, tmp_path, capsys, image):
    def send(webhook, *args):
        if webhook == WEBHOOK_A:
            raise requests.HTTPError(f"401 Client Error: Unauthorized for url: {webhook}; {TOKEN_A}")

    if image:
        path = tmp_path / "card.png"
        path.write_bytes(b"test")
        monkeypatch.setattr(P, "_send_file", send)
        summary = P.post_image(path)
    else:
        monkeypatch.setattr(P, "_send", send)
        summary = P.post("brief")
    output = capsys.readouterr().out
    assert "401 Client Error" in output
    assert "FAILED" in summary
    assert_no_credentials(output + summary)


def test_partial_failure_and_retry_only_missing_destination(monkeypatch, capsys):
    calls = Counter()
    failing = {WEBHOOK_B}

    def send(webhook, content):
        calls[webhook] += 1
        if webhook in failing:
            raise requests.ConnectionError(f"request failed: {webhook}; token={TOKEN_B}")

    monkeypatch.setattr(P, "_send", send)
    ok, detail = D.deliver_production("brief", DATE)
    assert ok is False
    targets = {w: "production:" + hashlib.sha256(w.encode("utf-8")).hexdigest()
               for w in (WEBHOOK_A, WEBHOOK_B)}
    assert [(r["date"], r["target"], r["status"]) for r in rows()] == [
        (DATE, targets[WEBHOOK_A], "ok"), (DATE, targets[WEBHOOK_B], "failed"),
    ]
    assert targets[WEBHOOK_B] in detail
    assert targets[WEBHOOK_A] not in detail
    assert calls == {WEBHOOK_A: 1, WEBHOOK_B: 1}
    assert_no_credentials(D.LEDGER.read_text(encoding="utf-8") + capsys.readouterr().out + detail)

    failing.clear()
    ok, detail = D.deliver_production("brief", DATE)
    assert ok is True
    assert calls == {WEBHOOK_A: 1, WEBHOOK_B: 2}
    assert rows()[-1]["target"] == targets[WEBHOOK_B]
    assert rows()[-1]["status"] == "ok"
    assert_no_credentials(D.LEDGER.read_text(encoding="utf-8") + capsys.readouterr().out + detail)

    # A third run finds both destinations complete and sends nothing.
    ok, detail = D.deliver_production("brief", DATE)
    assert ok is True
    assert "already delivered" in detail
    assert calls == {WEBHOOK_A: 1, WEBHOOK_B: 2}
    assert len(rows()) == 3


def test_all_destinations_fail(monkeypatch, capsys):
    send = Mock(side_effect=requests.HTTPError(f"401 for url: {WEBHOOK_A}; {WEBHOOK_B}"))
    monkeypatch.setattr(P, "_send", send)
    ok, detail = D.deliver_production("brief", DATE)
    assert ok is False
    assert send.call_count == 2
    assert [r["status"] for r in rows()] == ["failed", "failed"]
    assert all(r["target"] in detail for r in rows())
    assert_no_credentials(D.LEDGER.read_text(encoding="utf-8") + capsys.readouterr().out + detail)


def test_force_resends_every_destination(monkeypatch):
    send = Mock()
    monkeypatch.setattr(P, "_send", send)
    assert D.deliver_production("brief", DATE)[0] is True
    assert D.deliver_production("brief", DATE, force=True)[0] is True
    assert Counter(call.args[0] for call in send.call_args_list) == {WEBHOOK_A: 2, WEBHOOK_B: 2}
    assert [r["status"] for r in rows()] == ["ok"] * 4


def test_other_date_and_legacy_production_do_not_skip_destinations(monkeypatch):
    D._record(DATE, "production", "ok", "legacy aggregate row")
    send = Mock()
    monkeypatch.setattr(P, "_send", send)
    assert D.deliver_production("brief", DATE)[0] is True
    assert send.call_count == 2
    assert D.deliver_production("brief", "2026-11-15")[0] is True
    assert send.call_count == 4


def test_production_exception_detail_sanitized(monkeypatch):
    post = Mock(side_effect=RuntimeError(f"failed {WEBHOOK_A}; token={TOKEN_B}"))
    monkeypatch.setattr(P, "post", post)
    ok, detail = D.deliver_production("brief", DATE)
    assert ok is False
    assert [r["status"] for r in rows()] == ["failed", "failed"]
    assert_no_credentials(D.LEDGER.read_text(encoding="utf-8") + detail)


def test_missing_configuration_returns_failure(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    monkeypatch.delenv("DISCORD_WEBHOOK_URLS")
    ok, detail = D.deliver_production("brief", DATE)
    assert ok is False
    assert "missing DISCORD_WEBHOOK_URL" in detail
    assert rows()[0]["status"] == "failed"


def test_legacy_test_row_recognized():
    D._record(DATE, "test", "ok", "legacy test delivery", 1)
    ok, detail = D.deliver_test("brief", DATE, "fake-test-channel")
    assert ok is True
    assert "already delivered" in detail
    assert len(rows()) == 1


def test_test_channel_send_and_force(monkeypatch, tmp_path):
    monkeypatch.setattr(D.config, "STATE", tmp_path)
    monkeypatch.setattr(D.config, "hermes_bin", lambda: "fake-hermes")
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(D.subprocess, "run", run)
    assert D.deliver_test("brief", DATE, "fake-test-channel")[0] is True
    assert D.deliver_test("brief", DATE, "fake-test-channel", force=True)[0] is True
    assert run.call_count == 2
    assert [r["target"] for r in rows()] == ["test", "test"]
    assert not list(tmp_path.glob("*.md"))
    assert run.call_args.kwargs["check"] is False


def test_test_channel_failure_scrubbed_before_truncation(monkeypatch, tmp_path):
    monkeypatch.setattr(D.config, "STATE", tmp_path)
    monkeypatch.setattr(D.config, "hermes_bin", lambda: "fake-hermes")
    stderr = "failure " * 34 + WEBHOOK_A + " " + TOKEN_B
    monkeypatch.setattr(D.subprocess, "run", Mock(return_value=Mock(returncode=1, stderr=stderr)))
    assert D.deliver_test("brief", DATE, "fake-test-channel")[0] is False
    detail = rows()[0]["detail"]
    assert_no_credentials(D.LEDGER.read_text(encoding="utf-8"))
    assert TOKEN_A[:12] not in detail
    assert not list(tmp_path.glob("*.md"))
