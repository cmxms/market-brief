"""Discord delivery with per-destination, per-brief acknowledged chunk progress.

Production posts to every webhook configured in DISCORD_WEBHOOK_URL (A and B).
Test posts to a single channel through Hermes's own Discord bot, which needs no
webhook and no new secret.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import config

LEDGER = config.STATE / "delivery.jsonl"


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _rows() -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def already_delivered(date: str, target: str) -> bool:
    for row in _rows():
        if row.get("date") == date and row.get("target") == target and row.get("status") == "ok":
            return True
    return False


def _record(date: str, target: str, status: str, detail: str, chunks: int = 0,
            *, brief_id: str | None = None, chunk_index: int | None = None) -> None:
    from post_discord import sanitize

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "date": date, "target": target, "status": status,
            "detail": sanitize(detail), "chunks": chunks, "at": _now(),
            **({"brief_id": brief_id} if brief_id is not None else {}),
            **({"chunk_index": chunk_index} if chunk_index is not None else {}),
        }) + "\n")


def _chunks(md: str) -> list[str]:
    import post_discord
    parts = post_discord.chunk(md)
    post_discord._validate_chunks(parts)
    return parts


def _identity(md: str, parts: list[str]) -> str:
    # Bind progress to both the exact source and its ordered chunk boundaries.
    return hashlib.sha256(json.dumps([md, parts], ensure_ascii=False).encode("utf-8")).hexdigest()


def _progress(date: str, target: str, brief_id: str, count: int,
              legacy_targets: tuple[str, ...] = ()) -> int:
    acknowledged: set[int] = set()
    for row in _rows():
        if row.get("date") != date:
            continue
        if "brief_id" not in row:
            # Old aggregate successes retain their date-wide suppression meaning.
            if row.get("target") in (target, *legacy_targets) and row.get("status") == "ok":
                acknowledged.update(range(count))
            continue
        if row.get("target") != target or row.get("brief_id") != brief_id:
            continue
        if row.get("status") == "started":
            acknowledged.clear()  # --force begins a new delivery, including after failure.
        elif row.get("status") == "ok":
            acknowledged.update(range(count))
        elif row.get("status") == "chunk_ok":
            index = row.get("chunk_index")
            if type(index) is int and 0 <= index < count:
                acknowledged.add(index)
    return next((i for i in range(count) if i not in acknowledged), count)


# An ambiguous timeout is NOT an acknowledgement. Retry that chunk (at-least-once):
# skipping it could silently omit content. Discord supplies no idempotency key here,
# so an accepted request whose response was lost can still be duplicated.


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

    try:
        parts = _chunks(md)
    except RuntimeError as exc:
        return False, f"production delivery failed: {exc}"
    brief_id = _identity(md, parts)
    failed: list[str] = []
    sent = 0
    messages_sent = 0
    for webhook in webhooks:
        target = "production:" + post_discord.fingerprint(webhook)
        start = 0 if force else _progress(date, target, brief_id, len(parts), ("production",))
        if start == len(parts):
            continue
        try:
            if force:
                _record(date, target, "started", "forced delivery", len(parts), brief_id=brief_id)

            def acknowledge(index: int, destination: str = target) -> None:
                _record(date, destination, "chunk_ok", "acknowledged", len(parts),
                        brief_id=brief_id, chunk_index=index)

            summary = post_discord.post(md, webhook=webhook, start=start, on_ack=acknowledge)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            _record(date, target, "failed", repr(exc), len(parts), brief_id=brief_id)
            failed.append(target)
        else:
            _record(date, target, "ok", summary, len(parts), brief_id=brief_id)
            sent += 1
            messages_sent += len(parts) - start
    if failed:
        return False, "production delivery failed; outstanding destinations: " + ", ".join(failed)
    if not sent:
        return True, "already delivered to production (ledger)"
    return True, f"posted {messages_sent} message(s) to {sent} production destination(s)"


def deliver_test(md: str, date: str, channel_id: str, force: bool = False) -> tuple[bool, str]:
    from post_discord import sanitize

    failure = _freshness_failure(md, date)
    if failure:
        return False, failure
    try:
        parts = _chunks(md)
    except RuntimeError as exc:
        return False, f"test delivery failed: {exc}"
    brief_id = _identity(md, parts)
    target = "test:" + channel_id
    start = 0 if force else _progress(date, target, brief_id, len(parts), ("test",))
    if start == len(parts):
        return True, "already delivered to test channel (ledger)"
    config.STATE.mkdir(parents=True, exist_ok=True)
    if force:
        _record(date, target, "started", "forced delivery", len(parts), brief_id=brief_id)
    for index in range(start, len(parts)):
        i, part = index + 1, parts[index]
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
        except (OSError, subprocess.SubprocessError) as exc:
            _record(date, target, "failed", repr(exc), len(parts), brief_id=brief_id)
            return False, f"test delivery failed on chunk {i}/{len(parts)}"
        finally:
            tmp_path.unlink(missing_ok=True)
        if proc.returncode != 0:
            reason = sanitize(proc.stderr.strip())[:300]
            _record(date, target, "failed", f"chunk {i}/{len(parts)}: {reason}",
                    len(parts), brief_id=brief_id)
            return False, f"test delivery failed on chunk {i}/{len(parts)}"
        _record(date, target, "chunk_ok", "acknowledged", len(parts),
                brief_id=brief_id, chunk_index=index)
    _record(date, target, "ok", f"sent {len(parts)} chunk(s) to test channel",
            len(parts), brief_id=brief_id)
    return True, f"sent {len(parts) - start} message(s) to the test channel"
