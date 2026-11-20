"""Discord delivery, with a ledger so the same brief is never posted twice.

Production posts to every webhook configured in DISCORD_WEBHOOK_URL (A and B).
Test posts to a single channel through Hermes's own Discord bot, which needs no
webhook and no new secret.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import config

LEDGER = config.STATE / "delivery.jsonl"


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def already_delivered(date: str, target: str) -> bool:
    if not LEDGER.exists():
        return False
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("date") == date and row.get("target") == target and row.get("status") == "ok":
            return True
    return False


def _record(date: str, target: str, status: str, detail: str, chunks: int = 0) -> None:
    from post_discord import sanitize

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "date": date, "target": target, "status": status,
            "detail": sanitize(detail), "chunks": chunks, "at": _now(),
        }) + "\n")


def _chunks(md: str) -> list[str]:
    import post_discord
    return post_discord.chunk(md)


def _freshness_failure(md: str, date: str) -> str | None:
    """Fail closed, then reuse the standalone release/open crossing policy.

    There is no elapsed-minutes threshold: a block becomes stale when it crosses
    a release/open boundary. Prior-day and future stamps cannot establish current
    freshness, even though the standalone CLI deliberately stays quiet for them.
    """
    import post_discord
    import validate

    try:
        payload = json.loads((config.DATA / f"{date}.json").read_text(encoding="utf-8"))
        errors = validate.stamp_errors(md, payload.get("as_of_et"))
        if errors:
            return "could not determine freshness: " + "; ".join(errors)
        stamp = post_discord._parse_stamp(md)
        if stamp is None:
            return "could not determine freshness: unreadable stamp"
        now = post_discord._now_et()
        if stamp.date() < now.date():
            return "stale block: stamp is from a prior ET day"
        if stamp > now:
            return "could not determine freshness: stamp is in the future"
        gate = payload.get("event_gate")
        if not isinstance(gate, dict) or gate.get("error"):
            return "could not determine freshness: event calendar unavailable"
        for key in ("today_events", "today_tier2"):
            events = gate.get(key)
            if not isinstance(events, list):
                return "could not determine freshness: event calendar incomplete"
            for event in events:
                datetime.strptime(event["time_et"], "%H:%M")
        if post_discord.stale_check(md):
            return "stale block: release or market-open boundary crossed; gather and rewrite"
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return f"could not determine freshness: {type(exc).__name__}"
    return None


def deliver_production(md: str, date: str, force: bool = False) -> tuple[bool, str]:
    import post_discord

    failure = _freshness_failure(md, date)
    if failure:
        return False, failure

    try:
        webhooks = post_discord._resolve(None)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        _record(date, "production", "failed", repr(exc))
        return False, f"production delivery failed: {post_discord.sanitize(str(exc))}"

    parts = _chunks(md)
    failed: list[str] = []
    sent = 0
    for webhook in webhooks:
        target = "production:" + post_discord.fingerprint(webhook)
        # Legacy aggregate production rows cannot establish which destination
        # succeeded; only destination-specific rows suppress a send.
        if not force and already_delivered(date, target):
            continue
        try:
            summary = post_discord.post(md, webhook=webhook)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            _record(date, target, "failed", repr(exc), len(parts))
            failed.append(target)
        else:
            _record(date, target, "ok", summary, len(parts))
            sent += 1
    if failed:
        return False, "production delivery failed; outstanding destinations: " + ", ".join(failed)
    if not sent:
        return True, "already delivered to production (ledger)"
    return True, f"posted {len(parts)} message(s) to {sent} production destination(s)"


def deliver_test(md: str, date: str, channel_id: str, force: bool = False) -> tuple[bool, str]:
    failure = _freshness_failure(md, date)
    if failure:
        return False, failure
    if already_delivered(date, "test") and not force:
        return True, "already delivered to test channel (ledger)"
    parts = _chunks(md)
    sent = 0
    for i, part in enumerate(parts, 1):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md",
                                         delete=False, dir=config.STATE) as tmp:
            tmp.write(part)
            tmp_path = Path(tmp.name)
        try:
            proc = subprocess.run(
                [config.hermes_bin(), "send", "--to", f"discord:{channel_id}", "--file", str(tmp_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                check=False,
            )
        finally:
            tmp_path.unlink(missing_ok=True)
        if proc.returncode != 0:
            from post_discord import sanitize

            reason = sanitize(proc.stderr.strip())[:300]
            _record(date, "test", "failed", f"chunk {i}/{len(parts)}: {reason}", sent)
            return False, f"test delivery failed on chunk {i}/{len(parts)}"
        sent += 1
    _record(date, "test", "ok", f"sent {sent} chunk(s) to test channel", sent)
    return True, f"sent {sent} message(s) to the test channel"
