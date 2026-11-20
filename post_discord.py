"""
post_discord.py — publish the brief to a Discord channel via an incoming webhook.

Why this exists: the Substack path can't run from the cloud (Cloudflare blocks datacenter
IPs). Discord webhooks have no such block, so this lets the WHOLE pipeline run unattended on
a free GitHub Actions cron — the point of the vacation automation.

Discord caps a message at 2000 chars, so we split the brief into ordered chunks on section
(`## `) boundaries and send them as sequential messages. Markdown (`##`, **bold**, ``` code
blocks ```) renders natively in Discord; the brief is already formatted for it (grids in code
blocks, not tables). Code fences are kept balanced across a split.

Creds (env / .env, or GitHub Actions secret):
  DISCORD_WEBHOOK_URL   (Server Settings -> Integrations -> Webhooks -> New Webhook -> Copy URL)
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

import requests

from reader_text import visible_text

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

LIMIT = 1900   # under Discord's 2000 hard cap, with margin for safety
logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load ./.env for local runs; real env vars (Actions secrets) always win."""
    f = Path(__file__).resolve().parent / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and not os.environ.get(k):
            os.environ[k] = v


_load_dotenv()


def _split_long(section: str, limit: int = LIMIT) -> list[str]:
    """Split by lines, then words, reserving room to close/reopen code fences."""
    out: list[str] = []
    buf, fence = "", ""
    for ln in section.split("\n"):
        is_fence = ln.lstrip().startswith("```")
        next_fence = ("" if fence else ln.strip()) if is_fence else fence
        closing = "\n```" if next_fence else ""
        candidate = ln if not buf else buf + "\n" + ln
        if len(candidate) + len(closing) > limit and buf:
            out.append(buf + ("\n```" if fence else ""))
            buf = fence if fence else ""
        prefix = buf + "\n" if buf else ""
        if is_fence and len(prefix + ln) + len(closing) > limit:
            raise RuntimeError("Discord code fence cannot fit within chunk limit")
        while len(prefix + ln) + len(closing) > limit:
            room = limit - len(prefix) - len(closing)
            if room <= 0:
                raise RuntimeError("Discord code fence leaves no room for content")
            # Keep whitespace as content; only an indivisible word is hard-split.
            cut = max(ln.rfind(space, 0, room) for space in " \t\r\v\f")
            cut = cut + 1 if cut >= 0 else room
            out.append(prefix + ln[:cut] + closing)
            ln = ln[cut:]
            prefix = fence + "\n" if fence else ""
        buf = prefix + ln
        fence = next_fence
    if buf:
        out.append(buf + ("\n```" if fence else ""))
    return out


def chunk(brief_md: str, *, limit: int = LIMIT) -> list[str]:
    """Split the brief into <=LIMIT-char messages on `## ` section boundaries.
    The title + TL;DR ride in the first chunk; each section packs in greedily."""
    brief_md = visible_text(brief_md)
    lines = brief_md.strip("\n").split("\n")
    sections: list[str] = []
    cur: list[str] = []
    for ln in lines:
        if ln.startswith("## ") and cur:
            sections.append("\n".join(cur))
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        sections.append("\n".join(cur))

    msgs: list[str] = []
    buf = ""
    for sec in (s.strip("\n") for s in sections):
        if not sec:
            continue
        if len(sec) > limit:
            if buf:
                msgs.append(buf)
                buf = ""
            msgs.extend(_split_long(sec, limit))
        elif buf and len(buf) + 2 + len(sec) > limit:
            msgs.append(buf)
            buf = sec
        else:
            buf = sec if not buf else buf + "\n\n" + sec
    if buf:
        msgs.append(buf)
    return msgs


def _send(webhook: str, content: str) -> None:
    """POST one message; retry on Discord's 429 rate-limit. allowed_mentions is locked
    to [] so nothing in the brief text can ever trigger an @everyone/@here/role ping."""
    for _ in range(4):
        try:
            r = requests.post(webhook, json={"content": content, "allowed_mentions": {"parse": []}}, timeout=20)
        except requests.RequestException as exc:
            raise RuntimeError(sanitize(str(exc))) from None
        if r.status_code == 429:
            wait = 2.0
            try:
                wait = float(r.json().get("retry_after", 2.0))
            except (ValueError, TypeError, AttributeError) as exc:
                logger.warning("Discord retry delay unreadable; using 2 seconds: %s", sanitize(str(exc)))
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Discord webhook HTTP {r.status_code} ({fingerprint(webhook)})")
        return
    raise RuntimeError("Discord webhook failed after retries (429)")


_WEBHOOK_URL = re.compile(
    r"(?:https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com)?"
    r"/api/(?:v\d+/)?webhooks/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"
    r"(?:[/?][^\s\"'<>\\)]*)?",
    re.IGNORECASE,
)
# Webhook tokens are long URL-safe strings. Err on the side of redacting other
# long identifiers too: bare tokens in exception reprs must not survive.
_BARE_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{60,}(?![A-Za-z0-9_-])")


def sanitize(text: str) -> str:
    """Remove webhook URLs, request paths and bare token-shaped strings from text."""
    return _BARE_TOKEN.sub("[REDACTED]", _WEBHOOK_URL.sub("[REDACTED]", text))


def fingerprint(webhook: str) -> str:
    """Stable destination identity without retaining any part of its credential."""
    return hashlib.sha256(webhook.encode("utf-8")).hexdigest()


def _webhooks_from_env() -> list[str]:
    """Collect all configured webhook URLs (multi-server fan-out). Reads DISCORD_WEBHOOK_URL
    (the original; may itself hold several URLs) AND DISCORD_WEBHOOK_URLS (plural, for a second
    server kept as its own secret). Either may be comma- / whitespace- / newline-separated. Order
    preserved, duplicates dropped."""
    import re
    raw = " ".join(v for v in (os.environ.get("DISCORD_WEBHOOK_URL", ""),
                               os.environ.get("DISCORD_WEBHOOK_URLS", "")) if v)
    seen: set[str] = set()
    out: list[str] = []
    for u in (s.strip() for s in re.split(r"[,\s]+", raw) if s.strip()):
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _validate_chunks(msgs: list[str]) -> None:
    """Preflight the entire destination before any message can reach transport."""
    if not msgs or any(not m or len(m) > LIMIT for m in msgs):
        raise RuntimeError(f"Discord chunks must contain 1..{LIMIT} characters")


def _post_one(webhook: str, msgs: list[str], start: int = 0,
              on_ack: Callable[[int], None] | None = None) -> None:
    """Send every chunk to ONE webhook, in order, with gentle pacing under the rate limit."""
    _validate_chunks(msgs)
    for i in range(start, len(msgs)):
        m = msgs[i]
        _send(webhook, m)
        if on_ack is not None:
            on_ack(i)
        if i < len(msgs) - 1:
            time.sleep(0.7)


def _send_file(webhook: str, path: Path, content: str = "") -> None:
    """POST one image as a multipart attachment (Discord renders PNGs inline). Same 429
    retry as _send, same locked-down allowed_mentions."""
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    for _ in range(4):
        with path.open("rb") as fh:
            try:
                r = requests.post(
                    webhook,
                    data={"payload_json": __import__("json").dumps(payload)},
                    files={"file": (path.name, fh, "image/png")},
                    timeout=45,
                )
            except requests.RequestException as exc:
                raise RuntimeError(sanitize(str(exc))) from None
        if r.status_code == 429:
            wait = 2.0
            try:
                wait = float(r.json().get("retry_after", 2.0))
            except (ValueError, TypeError, AttributeError) as exc:
                logger.warning("Discord image retry delay unreadable; using 2 seconds: %s", sanitize(str(exc)))
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Discord image upload HTTP {r.status_code} ({fingerprint(webhook)})")
        return
    raise RuntimeError("Discord image upload failed after retries (429)")


def post_image(path: str | Path, webhook: str | list[str] | None = None, content: str = "") -> str:
    """Upload one PNG to every configured webhook. Same independent-delivery contract as
    post(): one server failing never blocks the others, and we only raise if all failed."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    webhooks = _resolve(webhook)
    ok, failed = [], []
    for w in webhooks:
        try:
            _send_file(w, p, content)
            ok.append(w)
        except (RuntimeError, OSError, requests.RequestException) as e:
            failed.append(w)
            print(f"  WARN Discord image upload failed ({fingerprint(w)}): {sanitize(str(e))}", flush=True)
    if not ok:
        raise RuntimeError(f"all {len(webhooks)} Discord webhook(s) failed on image upload")
    return f"posted image to {len(ok)}/{len(webhooks)} webhook(s)" + (f" — {len(failed)} FAILED" if failed else "")


def _resolve(webhook: str | list[str] | None) -> list[str]:
    """Env, a single URL, or an explicit list -> the list of webhooks to deliver to."""
    if webhook is None:
        webhooks = _webhooks_from_env()
    elif isinstance(webhook, str):
        webhooks = [webhook.strip()] if webhook.strip() else []
    else:
        webhooks = [w.strip() for w in webhook if w and w.strip()]
    if not webhooks:
        raise RuntimeError("missing DISCORD_WEBHOOK_URL (or DISCORD_WEBHOOK_URLS)")
    return webhooks


def post(brief_md: str, webhook: str | list[str] | None = None, *, start: int = 0,
         on_ack: Callable[[int], None] | None = None, parts: list[str] | None = None) -> str:
    """Post the brief to every configured Discord webhook (fan-out to N servers).

    `webhook` overrides the env (a single URL or a list); otherwise the URLs come from
    DISCORD_WEBHOOK_URL (+ DISCORD_WEBHOOK_URLS). Each webhook is delivered independently: if one
    server's webhook fails (deleted/expired/rate-limited out), the others still get the brief and
    we only raise if EVERY webhook failed — so adding a friend's server can't break your own feed."""
    webhooks = _resolve(webhook)

    msgs = chunk(brief_md) if parts is None else parts
    _validate_chunks(msgs)
    ok: list[str] = []
    failed: list[str] = []
    for w in webhooks:
        try:
            _post_one(w, msgs, start, on_ack)
            ok.append(w)
        except (RuntimeError, requests.RequestException) as e:
            failed.append(w)
            print(f"  WARN Discord webhook failed ({fingerprint(w)}): {sanitize(str(e))}", flush=True)
    if not ok:
        raise RuntimeError(f"all {len(webhooks)} Discord webhook(s) failed")
    summary = f"posted {len(msgs) - start} message(s) to {len(ok)}/{len(webhooks)} Discord webhook(s)"
    return summary + (f" — {len(failed)} FAILED" if failed else "")


# --------------------------------------------------------------------------------------
# Stale-block check (added 2026-08-25)
#
# playbook.md step 4 says CHECK THE CLOCK AGAINST TODAY'S RELEASE TIMES BEFORE POSTING —
# writing the brief takes real time, and a scheduled print can land inside that gap, at
# which point the 🚨 Event gate advertises as *upcoming* something readers already know
# and the futures/yields under it are pre-print.
#
# That check was the last timestamp in this pipeline still done by hand every run, and on
# THIS machine the obvious way to do it is wrong: local is CDT, `date -u` is UTC, and
# `TZ=America/New_York date` ignores the TZ and prints UTC labelled GMT. There is no shell
# route to ET here. (Same root cause as the naive-LOCAL archive-name bug fixed 2026-08-04:
# 9:31 AM ET filed as -0831.) So it moves into code, tz-aware, like everything else.
#
# It WARNS, never blocks: it surfaces the gap and the release times inside it, and leaves
# the call to you. It cannot enumerate today's full calendar — the YAML carries T1/tier2
# only, so one-offs (Case-Shiller, Conf Board, ISM, Fed speakers) come from the playbook's
# step-2 web sweep and are invisible here. Hence the standard release-clock windows below:
# candidates to check, not a verdict.
# --------------------------------------------------------------------------------------

ET = "America/New_York"

# The US release clock. Not a calendar — a list of windows worth re-checking if the brief
# straddled one. Times are ET.
_RELEASE_CLOCK = [
    ("08:30", "BLS / BEA window (CPI, jobs, PPI, PCE, GDP, claims)"),
    ("09:00", "Case-Shiller / FHFA window"),
    ("09:45", "S&P flash PMI window"),
    ("10:00", "ISM / Conference Board / Census window"),
    ("10:30", "EIA petroleum window"),
]
_MARKET_OPEN = "09:30"


def _now_et():
    """Current ET, tz-aware. Never read a wall clock off the shell on this box."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(UTC).astimezone(ZoneInfo(ET))


def _parse_stamp(brief_md: str):
    """Pull the brief's own freshness stamp — `*Data pulled Tue Aug 25, 2026 8:43 AM ET*`.

    Deliberately the BRIEF's stamp and not the JSON's `as_of_et`: the stamp is what the
    reader sees and what the brief claims to be built from. Returns tz-aware ET or None."""
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    m = re.search(r"\*Data pulled\s+(.+?)\s*ET\*", brief_md)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).strip(), "%a %b %d, %Y %I:%M %p").replace(
            tzinfo=ZoneInfo(ET))
    except ValueError:
        return None


def _calendar_events_today(day) -> list[tuple[str, str]]:
    """(time_et, name) for T1 + tier2 events on `day`, from that date's gather JSON.
    Expected file failures warn and return empty; unexpected errors propagate.

    The data block lives in the configured runtime root, not beside this script. It
    used to sit in `./data/`, so moving operational files out of the repository
    silently disabled this check: the file was missing, the exception was swallowed,
    and the warning simply never fired. Falls back to the old in-project path so a
    legacy checkout still works.
    """
    import json
    candidates = []
    try:
        import config
        candidates.append(config.DATA / f"{day.isoformat()}.json")
    except ImportError as exc:
        raise RuntimeError(f"Stale-check configuration unavailable: {type(exc).__name__}") from None
    candidates.append(Path(__file__).resolve().parent / "data" / f"{day.isoformat()}.json")
    for p in candidates:
        try:
            eg = json.loads(p.read_text(encoding="utf-8")).get("event_gate", {})
        except (OSError, ValueError) as exc:
            logger.warning("Stale-check calendar unavailable at %s: %s", p, sanitize(str(exc)))
            continue
        return [(e.get("time_et", "??:??"), e.get("name", "?"))
                for e in (eg.get("today_events") or []) + (eg.get("today_tier2") or [])]
    return []


def crossed_boundaries(stamp, now, events) -> list[tuple[str, str]]:
    """Release windows, known events and the open in (pull, send], all ET."""
    def in_gap(hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
        return stamp < stamp.replace(hour=h, minute=m, second=0, microsecond=0) <= now

    candidates = [*events, *_RELEASE_CLOCK, (_MARKET_OPEN, "market open")]
    return sorted({(t, n) for t, n in candidates if in_gap(t)})


def stale_banner(stamp, now, boundaries) -> str:
    reason = "; ".join(f"{t} ET {' '.join(n.split())}" for t, n in boundaries)
    return (f"⚠ STALE DATA — pulled {stamp:%Y-%m-%d %H:%M} ET; "
            f"sending {now:%Y-%m-%d %H:%M} ET. "
            f"A release window or the open has passed since the pull: {reason}.")


def alert_failure(stage: str, reason: str, code: int, *, partial: bool = False) -> str:
    """Independent production fan-out: no freshness gate, ledger, or dry switch."""
    reason = " ".join(sanitize(reason).split())[:500]
    outcome = ("Delivery may be partial; check the delivery ledger." if partial else
               "Nothing was posted.")
    message = f"Market brief failed — {stage}: {reason} Exit code {code}. {outcome}"
    # Diagnostics are arbitrary text, not brief Markdown: never interpret HTML
    # comments or code fences. Slice literally after redaction and truncation.
    parts = [message[i:i + LIMIT] for i in range(0, len(message), LIMIT)]
    _validate_chunks(parts)
    failed = []
    for webhook in _resolve(None):
        try:
            _post_one(webhook, parts)
        except Exception as exc:  # noqa: BLE001 - attempt every destination
            failed.append(f"{fingerprint(webhook)}: {sanitize(str(exc))}")
    if failed:
        raise RuntimeError("failure alert not posted to " + "; ".join(failed))
    return message


def stale_check(brief_md: str) -> bool:
    """Warn if release times fall between the brief's stamp and now. True if anything fired.

    Returns False (quiet) when the stamp is unreadable or the brief isn't for today — a
    backfill or a re-post of an old brief shouldn't nag."""
    stamp = _parse_stamp(brief_md)
    if stamp is None:
        print("  NOTE stale-check skipped: no readable `*Data pulled ... ET*` stamp", flush=True)
        return False
    now = _now_et()
    if stamp.date() != now.date():
        return False

    gap_min = int((now - stamp).total_seconds() // 60)
    if gap_min < 0:
        print(f"  NOTE stamp is {-gap_min} min in the FUTURE vs ET now — check the stamp", flush=True)
        return False

    def _in_gap(hhmm: str) -> bool:
        h, m = (int(x) for x in hhmm.split(":"))
        t = stamp.replace(hour=h, minute=m, second=0, microsecond=0)
        return stamp < t <= now

    crossed = sorted((t, n) for t, n in _calendar_events_today(now.date()) if _in_gap(t))
    windows = [(t, lbl) for t, lbl in _RELEASE_CLOCK if _in_gap(t)]
    if _in_gap(_MARKET_OPEN):
        windows.append((_MARKET_OPEN, "MARKET OPEN — futures/yields in the brief are pre-open"))
    windows.sort()

    if not (crossed or windows):
        return False

    def _fmt(dt) -> str:
        # %-I is glibc, %#I is MSVC; this runs on both.
        return dt.strftime("%#I:%M %p" if os.name == "nt" else "%-I:%M %p")

    print(flush=True)
    print("  ⚠  STALE BLOCK CHECK", flush=True)
    print(f"  block as_of_et : {_fmt(stamp)} ET", flush=True)
    print(f"  now            : {_fmt(now)} ET  (+{gap_min} min)", flush=True)
    if crossed:
        print("  calendar releases crossed in that gap:", flush=True)
        for t, n in crossed:
            print(f"    - {t}  {n}", flush=True)
    if windows:
        print("  windows crossed (calendar has T1/tier2 ONLY — check your step-2 sweep):", flush=True)
        for t, lbl in windows:
            print(f"    - {t}  {lbl}", flush=True)
    print("  -> re-run gather.py and rewrite, or pass --stale-ok", flush=True)
    return True


if __name__ == "__main__":
    # Standalone: `python post_discord.py brief.md` posts; `--dry` just prints the split.
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    src = Path(args[0]).read_text(encoding="utf-8") if args else sys.stdin.read()
    if "--dry" in sys.argv:
        parts = chunk(src)
        for i, m in enumerate(parts, 1):
            print(f"----- message {i}/{len(parts)} ({len(m)} chars) -----\n{m}\n")
        if "--stale-ok" not in sys.argv:
            stale_check(src)
    else:
        if "--stale-ok" not in sys.argv and stale_check(src):
            print("\n  posting anyway in 5s (Ctrl-C to abort)...", flush=True)
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print("  aborted.", flush=True)
                sys.exit(1)
        print(post(src))
