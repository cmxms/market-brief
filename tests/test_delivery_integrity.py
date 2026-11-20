"""Offline chunk and resume contracts; also runnable against an extracted baseline."""
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("DELIVERY_BASELINE", str(ROOT)))
import deliver as D
import post_discord as P

DATE = "2026-11-20"
WEBHOOK = "https://discord.com/api/webhooks/123/mock-only"


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected real transport")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(D.subprocess, "run", forbidden)
    monkeypatch.setattr(P.time, "sleep", lambda _: None)
    monkeypatch.setattr(D, "LEDGER", tmp_path / "delivery.jsonl")
    monkeypatch.setattr(D.config, "STATE", tmp_path)
    monkeypatch.setattr(D.config, "hermes_bin", lambda: "mock-hermes")
    monkeypatch.setattr(D, "_freshness_failure", lambda *_: None)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.delenv("DISCORD_WEBHOOK_URLS", raising=False)


def ledger():
    return [json.loads(line) for line in D.LEDGER.read_text(encoding="utf-8").splitlines()]


def four_chunks():
    return "\n".join(f"## Section {i}\n" + str(i) * 1100 for i in range(4))


@pytest.mark.parametrize("fence", ["", "```", "```python"])
@pytest.mark.parametrize("line", ["x" * 2100, "wholeword " * 410])
def test_oversized_line_fits_and_preserves_content(fence, line):
    source = "## Title\n" + (fence + "\n" if fence else "") + line + ("\n```" if fence else "")
    parts = P.chunk(source)
    print("chunk lengths:", [len(p) for p in parts])
    assert all(len(p) <= P.LIMIT for p in parts)
    if fence:
        assert all(sum(ln.startswith("```") for ln in p.splitlines()) % 2 == 0 for p in parts)
        assert all(p.startswith(fence) for p in parts[1:])
    payload = "".join(ln for p in parts for ln in p.splitlines()
                      if not ln.startswith(("```", "## ")))
    assert payload == line
    if " " in line:
        assert all(p.removesuffix("\n```").endswith((" ", "```")) for p in parts[:-1]
                   if "wholeword" in p)


def test_fence_closure_overhead_fits():
    parts = P.chunk("```\n" + "a" * 1894 + "\n" + "b" * 100 + "\n```")
    assert all(len(p) <= P.LIMIT for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)


@pytest.mark.parametrize("route", ["post", "production", "test"])
def test_preflight_rejects_entire_destination(monkeypatch, route):
    send = Mock()
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(P, "_send", send)
    monkeypatch.setattr(D.subprocess, "run", run)
    monkeypatch.setattr(P, "chunk", lambda _: ["valid prefix", "x" * 1901])
    if route == "post":
        with pytest.raises(RuntimeError):
            P.post("brief", WEBHOOK)
    elif route == "production":
        assert D.deliver_production("brief", DATE)[0] is False
    else:
        assert D.deliver_test("brief", DATE, "mock-channel")[0] is False
    assert send.call_count == run.call_count == 0


def test_unfit_fence_fails_before_transport(monkeypatch):
    send = Mock()
    monkeypatch.setattr(P, "_send", send)
    with pytest.raises(RuntimeError):
        P.post("prefix\n## Block\n```" + "l" * 1900 + "\nbody\n```", WEBHOOK)
    send.assert_not_called()


@pytest.mark.parametrize("route", ["production", "test"])
@pytest.mark.parametrize("failure", ["rejected", "timeout"])
def test_retry_resumes_acknowledged_prefix(monkeypatch, route, failure):
    md = four_chunks()
    parts = P.chunk(md)
    assert len(parts) == 4
    calls = Counter()
    fail = True

    def send(content):
        calls[content] += 1
        if fail and content == parts[1]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired("mock-hermes", 120) if route == "test" else requests.Timeout("no ack")
            return False
        return True

    def webhook_send(_, content):
        if not send(content):
            raise RuntimeError("mock rejection")

    def child(args, **kwargs):
        ok = send(Path(args[-1]).read_text(encoding="utf-8"))
        return Mock(returncode=0 if ok else 1, stderr="mock rejection")

    monkeypatch.setattr(P, "_send", webhook_send)
    monkeypatch.setattr(D.subprocess, "run", child)

    def deliver():
        return D.deliver_production(md, DATE) if route == "production" else D.deliver_test(md, DATE, "mock-channel")

    assert deliver()[0] is False
    partial = ledger()
    assert [r["chunk_index"] for r in partial if r["status"] == "chunk_ok"] == [0]
    assert partial[-1]["status"] == "failed"
    assert len({r["brief_id"] for r in partial}) == 1
    assert not any(r["status"] == "ok" for r in partial)
    assert not list(D.config.STATE.glob("*.md"))
    fail = False
    assert deliver()[0] is True
    print("chunk attempts:", [calls[p] for p in parts])
    assert [calls[p] for p in parts] == [1, 2, 1, 1]
    assert [r["chunk_index"] for r in ledger() if r["status"] == "chunk_ok"] == [0, 1, 2, 3]
    assert deliver()[0] is True
    assert [calls[p] for p in parts] == [1, 2, 1, 1]


def test_legacy_aggregate_suppresses_production(monkeypatch):
    D._record(DATE, "production", "ok", "old aggregate")
    send = Mock()
    monkeypatch.setattr(P, "_send", send)
    assert D.deliver_production("brief", DATE)[0] is True
    send.assert_not_called()


@pytest.mark.parametrize("row", [None, [], 1, "legacy", {"chunks": "unknown"}, {"status": "failed"}])
def test_legacy_non_object_or_incomplete_row_never_raises(monkeypatch, row):
    D.LEDGER.write_text(json.dumps(row) + "\nnot-json\n", encoding="utf-8")
    monkeypatch.setattr(P, "_send", Mock())
    assert D.already_delivered(DATE, "production") is False
    assert D.deliver_production("brief", DATE)[0] is True


def test_changed_identity_and_channel_do_not_reuse_progress(monkeypatch):
    send = Mock()
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(P, "_send", send)
    monkeypatch.setattr(D.subprocess, "run", run)
    for md in ("brief", "changed brief"):
        assert D.deliver_production(md, DATE)[0]
        assert D.deliver_test(md, DATE, "channel-a")[0]
    assert D.deliver_test("brief", DATE, "channel-b")[0]
    assert send.call_count == 2
    assert run.call_count == 3


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("route", ["production", "test"])
def test_force_failure_resumes_forced_attempt(monkeypatch, legacy, route):
    md = four_chunks()
    parts = P.chunk(md)
    send = Mock()
    if route == "production":
        monkeypatch.setattr(P, "_send", lambda _, content: send(content))
    else:
        def child(args, **kwargs):
            send(Path(args[-1]).read_text(encoding="utf-8"))
            return Mock(returncode=0)
        monkeypatch.setattr(D.subprocess, "run", child)

    def deliver(force=False):
        return (D.deliver_production(md, DATE, force=force) if route == "production" else
                D.deliver_test(md, DATE, "mock-channel", force=force))

    if legacy:
        D._record(DATE, "production" if route == "production" else "test", "ok", "legacy")
    else:
        assert deliver()[0]
    send.side_effect = [None, OSError("mock failure")]
    assert not deliver(force=True)[0]
    send.reset_mock(side_effect=True)
    assert deliver()[0]
    assert [call.args[0] for call in send.call_args_list] == parts[1:]


@pytest.mark.parametrize("route", ["production", "test"])
def test_destination_never_acknowledges(monkeypatch, route):
    send = Mock(side_effect=requests.Timeout("no acknowledgement"))
    if route == "production":
        monkeypatch.setattr(P, "_send", send)
    else:
        send.side_effect = subprocess.TimeoutExpired("mock-hermes", 120)
        monkeypatch.setattr(D.subprocess, "run", send)
    for _ in range(2):
        result = (D.deliver_production(four_chunks(), DATE) if route == "production" else
                  D.deliver_test(four_chunks(), DATE, "mock-channel"))
        assert not result[0]
    assert send.call_count == 2
    assert [r["status"] for r in ledger()] == ["failed", "failed"]


def test_multichunk_progress_is_destination_specific(monkeypatch):
    other = WEBHOOK + "-other"
    monkeypatch.setenv("DISCORD_WEBHOOK_URLS", other)
    parts = P.chunk(four_chunks())
    calls = Counter()
    fail = True

    def send(destination, content):
        calls[destination, content] += 1
        if fail and destination == other and content == parts[1]:
            raise RuntimeError("mock rejection")
        # The previous acknowledgement must be on disk before the next send.
        index = parts.index(content)
        if index:
            target = "production:" + P.fingerprint(destination)
            assert any(r["target"] == target and r["status"] == "chunk_ok"
                       and r["chunk_index"] == index - 1 for r in ledger())

    monkeypatch.setattr(P, "_send", send)
    assert not D.deliver_production(four_chunks(), DATE)[0]
    fail = False
    assert D.deliver_production(four_chunks(), DATE)[0]
    assert [calls[WEBHOOK, p] for p in parts] == [1, 1, 1, 1]
    assert [calls[other, p] for p in parts] == [1, 2, 1, 1]


@pytest.mark.parametrize("name,expected", [
    ("2026-11-19.md", [
        "5951c827fb68b0759c9d0ffcc36dff11e3381f0b762f0034d9ecca9752410ba0",
        "d751cbf94e34ef321d5d6b3e3af6949cb9ef5d2b36e035a42d6e0a4d6b11e94c",
        "d08281286acdc06929be5d47a5648f360fd735adbe23fdeaecfe32ff472c8a14",
        "9ee12b57080a4bf33e60b7990ea5ce9842258cd7f9e5602d9929a6fdf6b32bea",
    ]),
    ("2026-11-20.md", [
        "f04648446efb581ac024a934b8180dc56ae1a2bf5f864f767092f3529414cfb0",
        "b6527a74397a28f66bfe3cc4fc75169e569e1257b18cf7e94d7309ac363e8fd8",
        "97766ca3b102442ca196418339853aeec1883a612bfd0506afdff85d2ee7abda",
    ]),
])
def test_committed_brief_boundaries(name, expected, monkeypatch):
    md = (ROOT / "briefs" / name).read_text(encoding="utf-8")
    parts = P.chunk(md)
    assert [hashlib.sha256(p.encode("utf-8")).hexdigest() for p in parts] == expected
    monkeypatch.setattr(D, "_banner", lambda *_: "")
    sent = []
    monkeypatch.setattr(P, "_send", lambda _, content: sent.append(content))
    assert D.deliver_production(md, DATE)[0]
    print(name, "no banner:", len(sent), "lengths:", [len(p) for p in sent])
    assert len(sent) == len(expected)
    assert sent == parts


def release_banner():
    stamp = datetime(2026, 11, 20, 8, 0, tzinfo=ZoneInfo(P.ET))
    now = stamp.replace(hour=9, minute=31)
    events = [("08:30", "test GDP.")]
    return P.stale_banner(stamp, now, P.crossed_boundaries(stamp, now, events))


def capture_route(monkeypatch, route, sent):
    if route == "production":
        monkeypatch.setattr(P, "_send", lambda _, content: sent(content))
        return lambda md, force=False: D.deliver_production(md, DATE, force=force)

    def child(args, **kwargs):
        try:
            sent(Path(args[-1]).read_text(encoding="utf-8"))
        except RuntimeError:
            return Mock(returncode=1, stderr="mock rejection")
        return Mock(returncode=0)

    monkeypatch.setattr(D.subprocess, "run", child)
    return lambda md, force=False: D.deliver_test(md, DATE, "mock-channel", force=force)


@pytest.mark.parametrize("route", ["production", "test"])
@pytest.mark.parametrize("name,count", [("2026-11-20.md", 4), ("2026-11-19.md", 4)])
def test_committed_banner_splits_only_attached_message(monkeypatch, route, name, count):
    md = (ROOT / "briefs" / name).read_text(encoding="utf-8")
    original = P.chunk(md)
    banner = release_banner()
    assert len(banner) == 268
    monkeypatch.setattr(D, "_banner", lambda *_: banner)
    sent = []
    deliver = capture_route(monkeypatch, route, sent.append)
    assert deliver(md)[0]
    print(name, route, "banner:", len(sent), "lengths:", [len(p) for p in sent])
    assert len(sent) == count
    assert sent[0].startswith(banner + "\n\n")
    if len(banner) + 2 + len(original[0]) > P.LIMIT:
        assert len(sent) == len(original) + 1
        assert sent[2:] == original[1:]
        assert sent[0].removeprefix(banner + "\n\n") + sent[1] == original[0]
    else:
        assert sent == [banner + "\n\n" + original[0], *original[1:]]
    P._validate_chunks(sent)
    sent.clear()
    assert deliver(md)[0]
    assert not sent


@pytest.mark.parametrize("route", ["production", "test"])
@pytest.mark.parametrize("fail_at", [1, 2])
def test_new_banner_on_retry_never_replays_acknowledged_body(monkeypatch, route, fail_at):
    md = "\n".join(f"## Section {i}\n" + (str(i) + " ") * 880 for i in range(4))
    original = P.chunk(md)
    banner = ""
    monkeypatch.setattr(D, "_banner", lambda *_: banner)
    sent = []
    failing = True

    def send(content):
        if failing and len(sent) == fail_at:
            raise RuntimeError("mock rejection")
        sent.append(content)

    deliver = capture_route(monkeypatch, route, send)
    assert not deliver(md)[0]
    assert sent == original[:fail_at]
    sent.clear()
    banner = release_banner()
    failing = False
    assert deliver(md)[0]
    assert sent[0].startswith(banner + "\n\n")
    assert sent[2:] == original[fail_at + 1:]
    assert not any(p in sent for p in original[:fail_at])
    P._validate_chunks(sent)


@pytest.mark.parametrize("route", ["production", "test"])
@pytest.mark.parametrize("retry_banner", ["same", "longer", "none"])
def test_retry_between_banner_fragments_preserves_progress(monkeypatch, route, retry_banner):
    md = "## First\n" + "body " * 360 + "\n## Next\n" + "tail " * 300
    banner = release_banner()
    monkeypatch.setattr(D, "_banner", lambda *_: banner)
    sent = []
    failing = True

    def send(content):
        if failing and sent:
            raise RuntimeError("mock rejection")
        sent.append(content)

    deliver = capture_route(monkeypatch, route, send)
    assert not deliver(md)[0]
    assert len(sent) == 1
    acknowledged_body = sent[0].split("\n\n", 1)[1]
    planned = next(r["body_parts"] for r in ledger() if r["status"] == "planned")
    assert planned[0] == acknowledged_body
    if retry_banner == "longer":
        banner += " Additional release: " + "event " * 300
    elif retry_banner == "none":
        banner = ""
    sent.clear()
    failing = False
    assert deliver(md)[0]
    assert sent[-1] == planned[-1]
    assert all(acknowledged_body not in p for p in sent)
    P._validate_chunks(sent)
    sent.clear()
    assert deliver(md)[0]
    assert not sent


@pytest.mark.parametrize("route", ["production", "test"])
def test_oversized_banner_shortened_but_full_reason_recorded(monkeypatch, route):
    md = "## Body\n" + "word " * 370
    banner = release_banner() + " calendar " * 400
    monkeypatch.setattr(D, "_banner", lambda *_: banner)
    sent = []
    deliver = capture_route(monkeypatch, route, sent.append)
    assert deliver(md)[0]
    assert len(sent) == 2
    assert sent[0].split("\n\n", 1)[0].endswith("...")
    assert all(r["stale_reason"] == banner for r in ledger())
    P._validate_chunks(sent)


@pytest.mark.parametrize("route", ["production", "test"])
def test_previous_reserved_layout_acknowledgements_remain_valid(monkeypatch, route):
    md = "## First\n" + "first " * 290 + "\n## Next\n" + "next " * 290
    reserved = P.chunk(md, limit=P.LIMIT - D.BANNER_ROOM)
    target = "production:" + P.fingerprint(WEBHOOK) if route == "production" else "test:mock-channel"
    D._record(DATE, target, "chunk_ok", "previous version", len(reserved),
              brief_id=D._identity(md, reserved), chunk_index=0)
    monkeypatch.setattr(D, "_banner", lambda *_: "")
    sent = []
    deliver = capture_route(monkeypatch, route, sent.append)
    assert deliver(md)[0]
    assert sent == reserved[1:]


@pytest.mark.parametrize("fence", ["```", "```python", "```" + "language" * 170])
def test_banner_split_keeps_fences_balanced_and_body_intact(fence):
    body = fence + "\n" + "word " * ((1800 - len(fence)) // 5) + "\n```"
    original = [body, "## Unchanged\ntail"]
    banner = release_banner() + " reason " * 200
    parts = D._with_banner(original, banner, 0)
    P._validate_chunks(parts)
    assert len(parts) == 3
    assert parts[-1] == original[-1]
    first_body = parts[0].split("\n\n", 1)[1]
    assert all(sum(ln.lstrip().startswith("```") for ln in p.splitlines()) % 2 == 0
               for p in (first_body, parts[1]))
    assert first_body.removesuffix("\n```") + parts[1].removeprefix(fence + "\n") == body


@pytest.mark.parametrize("route", ["production", "test"])
def test_force_after_split_starts_again_with_canonical_boundaries(monkeypatch, route):
    md = "## First\n" + "body " * 360 + "\n## Next\n" + "tail " * 300
    banner = release_banner()
    monkeypatch.setattr(D, "_banner", lambda *_: banner)
    sent = []
    deliver = capture_route(monkeypatch, route, sent.append)
    assert deliver(md)[0]
    sent.clear()
    banner = ""
    assert deliver(md, force=True)[0]
    assert sent == P.chunk(md)
    sent.clear()
    assert deliver(md)[0]
    assert not sent
