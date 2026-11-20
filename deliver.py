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
BANNER_ROOM = 800


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
            *, brief_id: str | None = None, chunk_index: int | None = None,
            banner: str = "") -> None:
    from post_discord import sanitize

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "date": date, "target": target, "status": status,
            "detail": sanitize(detail), "chunks": chunks, "at": _now(),
            **({"brief_id": brief_id} if brief_id is not None else {}),
            **({"chunk_index": chunk_index} if chunk_index is not None else {}),
            "stale": bool(banner), "stale_reason": sanitize(banner),
        }) + "\n")


def _chunks(md: str, *, reserve: bool = False) -> list[str]:
    import post_discord
    # Fixed body boundaries, independent of the clock: a retry can prepend its
    # current warning to the first outstanding chunk without replaying any ack.
    parts = (post_discord.chunk(md, limit=post_discord.LIMIT - BANNER_ROOM)
             if reserve or post_discord._parse_stamp(md) is not None else post_discord.chunk(md))
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
    """Fail closed on broken provenance or an unreadable event calendar.

    Release/open crossings alone are labelled at send time, never refused.
    Prior-day and future stamps still cannot establish trustworthy provenance.
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
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return f"could not determine freshness: {type(exc).__name__}"
    return None


def _banner(md: str, date: str) -> str:
    import post_discord as post

    stamp = post._parse_stamp(md)
    if stamp is None:  # Transport-only tests may stub the provenance gate.
        return ""
    now = post._now_et()
    payload = json.loads((config.DATA / f"{date}.json").read_text(encoding="utf-8"))
    gate = payload["event_gate"]
    events = [(e["time_et"], e.get("name", "scheduled release"))
              for e in gate["today_events"] + gate["today_tier2"]]
    boundaries = post.crossed_boundaries(stamp, now, events)
    return post.stale_banner(stamp, now, boundaries) if boundaries else ""


def _plan(md: str, date: str, target: str, force: bool) -> tuple[str, list[str], str, str]:
    """Identify body progress independently of the send-time warning."""
    import post_discord

    parts = _chunks(md)
    # Preserve progress written before banner room was reserved. Re-chunk only
    # the unacknowledged old suffix, never replay its acknowledged prefix.
    original = post_discord.chunk(md)
    old_id = _identity(md, original)
    rows = [r for r in _rows() if r.get("date") == date and r.get("target") == target]
    full_id = _identity(md, parts)
    modern_full = any(r.get("brief_id") == full_id and "stale" in r for r in rows)
    old_rows = [r for r in rows if r.get("brief_id") == old_id and "stale" not in r]
    if old_rows and not modern_full and not force and original != parts:
        start = _progress(date, target, old_id, len(original))
        if start == len(original):
            return md, original, old_id, ""
        if start:
            parts = _chunks("\n\n".join(original[start:]), reserve=True)
    brief_id = _identity(md, parts)
    banner = _banner(md, date)
    wire = banner + "\n\n" + md if banner else md
    return wire, parts, brief_id, banner


def _with_banner(parts: list[str], banner: str, start: int) -> list[str]:
    import post_discord

    wire_parts = parts.copy()
    if banner and start < len(parts):
        # Keep the full reason in the ledger; bound only the visible envelope.
        visible = banner if len(banner) <= BANNER_ROOM - 2 else banner[:BANNER_ROOM - 5] + "..."
        wire_parts[start] = visible + "\n\n" + parts[start]
    post_discord._validate_chunks(wire_parts)
    return wire_parts


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

    failed: list[str] = []
    sent = 0
    messages_sent = 0
    stale_reasons: set[str] = set()
    for webhook in webhooks:
        target = "production:" + post_discord.fingerprint(webhook)
        try:
            wire, parts, brief_id, banner = _plan(md, date, target, force)
        except RuntimeError as exc:
            return False, f"production delivery failed: {exc}"
        start = 0 if force else _progress(date, target, brief_id, len(parts), ("production",))
        if start == len(parts):
            continue
        try:
            if force:
                _record(date, target, "started", "forced delivery", len(parts), brief_id=brief_id, banner=banner)

            def acknowledge(index: int, destination: str = target, count: int = len(parts),
                            identity: str = brief_id, warning: str = banner) -> None:
                _record(date, destination, "chunk_ok", "acknowledged", count,
                        brief_id=identity, chunk_index=index, banner=warning)

            if banner:
                stale_reasons.add(banner)
            summary = post_discord.post(wire, webhook=webhook, start=start, on_ack=acknowledge,
                                        parts=_with_banner(parts, banner, start))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            _record(date, target, "failed", repr(exc), len(parts), brief_id=brief_id, banner=banner)
            failed.append(target)
        else:
            _record(date, target, "ok", summary, len(parts), brief_id=brief_id, banner=banner)
            sent += 1
            messages_sent += len(parts) - start
    if failed:
        return False, "production delivery failed; outstanding destinations: " + ", ".join(failed) + (
            "; stale: " + " | ".join(sorted(stale_reasons)) if stale_reasons else "")
    if not sent:
        return True, "already delivered to production (ledger)"
    return True, f"posted {messages_sent} message(s) to {sent} production destination(s)" + (
        "; stale: " + " | ".join(sorted(stale_reasons)) if stale_reasons else "")


def deliver_test(md: str, date: str, channel_id: str, force: bool = False) -> tuple[bool, str]:
    from post_discord import sanitize

    failure = _freshness_failure(md, date)
    if failure:
        return False, failure
    target = "test:" + channel_id
    try:
        _wire, parts, brief_id, banner = _plan(md, date, target, force)
    except RuntimeError as exc:
        return False, f"test delivery failed: {exc}"
    start = 0 if force else _progress(date, target, brief_id, len(parts), ("test",))
    if start == len(parts):
        return True, "already delivered to test channel (ledger)"
    parts = _with_banner(parts, banner, start)
    config.STATE.mkdir(parents=True, exist_ok=True)
    if force:
        _record(date, target, "started", "forced delivery", len(parts), brief_id=brief_id, banner=banner)
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
            _record(date, target, "failed", repr(exc), len(parts), brief_id=brief_id, banner=banner)
            return False, f"test delivery failed on chunk {i}/{len(parts)}"
        finally:
            tmp_path.unlink(missing_ok=True)
        if proc.returncode != 0:
            reason = sanitize(proc.stderr.strip())[:300]
            _record(date, target, "failed", f"chunk {i}/{len(parts)}: {reason}",
                    len(parts), brief_id=brief_id, banner=banner)
            return False, f"test delivery failed on chunk {i}/{len(parts)}"
        _record(date, target, "chunk_ok", "acknowledged", len(parts),
                brief_id=brief_id, chunk_index=index, banner=banner)
    _record(date, target, "ok", f"sent {len(parts)} chunk(s) to test channel",
            len(parts), brief_id=brief_id, banner=banner)
    return True, f"sent {len(parts) - start} message(s) to the test channel" + (
        f"; stale: {banner}" if banner else "")
