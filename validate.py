"""Checks a finished brief before it can be delivered.

These are structural and policy checks, not a proof of correctness. They catch the
failure modes that would make a brief wrong to publish: wrong date, missing
sections, missing stamp, links, and a length overrun. They cannot tell whether a
number is true; that is what the gather block and the playbook rules are for.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import run_identity
from reader_text import visible_text

# Emoji-prefixed section headers, in the order the playbook fixes them.
SECTION_MARKERS = ("## 📰", "## 📊", "## 🔁", "## 📈", "## 🚨", "## 🧭")

STAMP_RE = re.compile(r"\*Data pulled\s+(.+?)\s*ET\*")
TITLE_RE = re.compile(r"^# 📊 Market Brief — ")
URL_RE = re.compile(r"https?://", re.IGNORECASE)
COMPLETION_RE = re.compile(r"\n<!-- market-brief complete: attempt-[A-Za-z0-9_-]+ -->\s*\Z")
FOOTER_RE = re.compile(
    r"(?m)^---\s*\n\s*\*Not financial advice — situational awareness only\.\*"
    r"\s*(?:-# Run: [0-9a-f]{32}\s*)?\Z"
)


_NO_BLOCK = object()


@dataclass(frozen=True)
class DataIssue:
    section: str
    metric: str
    reason: str


def data_issues(payload: dict, gex_warning: str | None = None) -> list[DataIssue]:
    """Use failed selected data, never failed attempts followed by a good fallback."""
    issues = []

    def add(section, metric, reason):
        if reason:
            # Vendor text is data, not Markdown or instructions. Bound the public
            # reason; the original payload retains the complete diagnostic.
            reason = re.sub(r"https?://\S+|[<>`*_#]", "", str(reason), flags=re.IGNORECASE)
            reason = " ".join(reason.split())[:240]
            issue = DataIssue(section, metric, reason)
            if issue not in issues:
                issues.append(issue)

    for key, section, label in (
        ("index_recap", "## 📊", "Index recap"),
        ("macro_pulse", "## 📈", "Macro pulse"),
        ("sector_rotation", "## 🔁", "Sector rotation"),
        ("finnhub", "## 📰", "Finnhub headlines"),
        ("event_gate", "## 🚨", "Event calendar"),
    ):
        block = payload.get(key) or {}
        add(section, label, block.get("error"))
        if key in ("index_recap", "macro_pulse"):
            for metric, value in block.items():
                if isinstance(value, dict) and value.get("error"):
                    add(section, metric, value["error"])
    for group in ("yields", "vol"):
        crosscheck = (payload.get("source_crosscheck") or {}).get(group) or {}
        for metric, row in (crosscheck.get("rows") or {}).items():
            if not row.get("match"):
                add("## 📈", metric, "CNBC vs Yahoo disagreement")
    if gex_warning:
        add("## 🧭", "Gamma read", gex_warning)
    else:
        for symbol, views in (payload.get("gex") or {}).items():
            if not isinstance(views, dict):
                add("## 🧭", f"{symbol} GEX", "missing chains")
                continue
            add("## 🧭", f"{symbol} GEX", views.get("error") or views.get("data_warning"))
            for name in ("near_term", "full_chain"):
                view = views.get(name) or {}
                add("## 🧭", f"{symbol} GEX {name}", view.get("error") or view.get("data_warning")
                    or (view.get("degraded_reason") if view.get("status") == "degraded" else None))
    return issues


def section_bodies(brief: str) -> dict[str, tuple[int, int]]:
    """Exact body slices, excluding the disclaimer and completion metadata."""
    footer = FOOTER_RE.search(COMPLETION_RE.sub("", brief).rstrip())
    end = footer.start() if footer else len(brief)
    headings = list(re.finditer(r"^##\s+[^\n]*(?:\n|$)", brief[:end], re.MULTILINE))
    return {marker: (h.end(), headings[i + 1].start() if i + 1 < len(headings) else end)
            for i, h in enumerate(headings) for marker in SECTION_MARKERS
            if h.group().startswith(marker)}


def unavailable_line(issues: list[DataIssue]) -> str:
    return "Unavailable: " + "; ".join(dict.fromkeys(f"{i.metric} — {i.reason}" for i in issues)) + "."


def outside_data_errors(brief: str, issues: list[DataIssue]) -> list[str]:
    return [f"{i.section} {i.metric} unverified: claim outside its section"
            for _, _, affected in outside_claims(brief, issues) for i in affected]


def outside_claims(brief: str, issues: list[DataIssue]) -> list[tuple[int, int, list[DataIssue]]]:
    """Locate copied claims by line, including qualitative calls, not just numbers.

    Feed-wide failures use their instrument vocabulary; individual failures use
    their metric identifier. This is deterministic detection, not fact checking.
    """
    bodies = section_bodies(brief)
    vocabulary = {
        "Index recap": r"\b(?:ES|YM|NQ|RTY|S&P|Nasdaq|Dow|Russell)\b",
        "Macro pulse": r"\b(?:yields?|Treasur\w*|VIX\w*|WTI|crude|oil)\b",
        "Sector rotation": r"\b(?:XL[A-Z]|SMH|IGV|ARKK|semis|sectors?|staples|utilities|REITs)\b",
        "Finnhub headlines": r"\b(?:headlines?|Finnhub)\b",
        "Event calendar": r"\b(?:events?|calendar|CPI|FOMC|PCE|payrolls?)\b",
        "yields": r"\b(?:yields?|Treasur\w*|[123567]0?Y)\b",
        "vol": r"\b(?:VIX\w*|volatility)\b",
    }
    hits = []
    offset = 0
    for line in brief.splitlines(keepends=True):
        end = offset + len(line)
        plain = re.sub(r"[*_`]", "", line)
        affected = []
        disclosures = [f"{i.metric} — {i.reason}" for i in issues]
        if line.strip().startswith("Unavailable: ") and all(
            part in disclosures for part in line.strip()[13:].removesuffix(".").split("; ")
        ):
            offset = end
            continue
        for issue in issues:
            start_body, end_body = bodies.get(issue.section, (0, 0))
            if start_body <= offset < end_body:
                continue
            if line.strip() == unavailable_line([issue]):
                continue
            claim_text = plain
            if line.startswith(issue.section) and issue.metric in vocabulary:
                # A canonical heading names the feed, but cannot carry a claim.
                label = "Event gate" if issue.metric == "Event calendar" else issue.metric
                claim_text = re.sub(re.escape(label), "", plain, count=1, flags=re.IGNORECASE)
            if issue.section == "## 🧭":
                pattern = (r"\b(?:gamma|gex|put walls?|call walls?)\b|"
                           r"\b(?:the|a|nearby)\s+flip\b|\bflip\s+(?:level|is|at)\b")
            else:
                pattern = vocabulary.get(issue.metric,
                                         rf"(?<!\w){re.escape(issue.metric.lstrip('^'))}(?!\w)")
            if re.search(pattern, claim_text, re.IGNORECASE):
                affected.append(issue)
        if affected:
            hits.append((offset, end, affected))
        offset = end
    return hits


def availability_errors(brief: str, issues: list[DataIssue]) -> dict[str, list[DataIssue]]:
    """Name recoverable sections. Only disclosure-only copy is safe for a failed feed.

    Requiring no residual narrative avoids accepting rounded figures, derived
    figures or qualitative regime claims after a token 'unavailable' disclaimer.
    Mixed good/bad copy is conservatively quarantined, never numerically repaired.
    """
    bodies = section_bodies(brief)
    errors = {}
    for section in dict.fromkeys(i.section for i in issues):
        affected = [i for i in issues if i.section == section]
        if section not in bodies:
            continue  # Missing sections are structural errors, not repairable.
        start, end = bodies[section]
        body = re.sub(r"[*_`]", "", brief[start:end]).strip()
        if body != unavailable_line(affected):
            # Also accept the writer's natural disclosure, but not residual claims.
            lines = [line.lstrip("-• ") for line in body.splitlines() if line.strip()]
            if not lines or not all(
                any(line.rstrip(".").lower() == f"{i.metric} unavailable: {i.reason}".rstrip(".").lower()
                    or line.rstrip(".").lower() == f"{i.metric} unverified: {i.reason}".rstrip(".").lower()
                    for i in affected) for line in lines
            ) or not all(any(i.metric.lower() in line.lower() and i.reason in line
                             for line in lines) for i in affected):
                errors[section] = affected
    return errors


def quarantine(brief: str, errors: dict[str, list[DataIssue]],
               issues: list[DataIssue] | None = None) -> tuple[str, list[dict]]:
    bodies = section_bodies(brief)
    edits = []
    # Plan against the original text so every removal is exactly recoverable.
    tldr = re.search(r"(?m)^\*\*TL;DR:\*\*[^\n]*(?:\n|$)", brief)
    tldr_end = min((s for s, _ in bodies.values()), default=len(brief))
    if tldr:
        heading = re.search(r"(?m)^##\s", brief[tldr.end():])
        tldr_end = tldr.end() + heading.start() if heading else tldr_end
    tldr_start = tldr.start() if tldr else -1
    line_edits: dict[str, list[tuple[int, int, list[DataIssue]]]] = {}
    for start, end, affected in outside_claims(brief, issues or []):
        section = next((s for s, (a, b) in bodies.items() if a <= start < b), None)
        if section in errors:
            continue
        if section is None and tldr and tldr.start() <= start < tldr_end:
            section = "TL;DR"
        if section is not None:
            line_edits.setdefault(section, []).append((start, end, affected))
        # Title, headings, stamp, footer and metadata are not repairable bodies.
    errors = dict(errors)
    for section, lines in line_edits.items():
        a, b = (tldr_start, tldr_end) if section == "TL;DR" else bodies[section]
        remaining = brief[a:b]
        for start, end, _ in reversed(lines):
            remaining = remaining[:start - a] + remaining[end - a:]
        content = re.sub(r"<!--.*?-->", "", remaining, flags=re.DOTALL)
        content = re.sub(r"(?m)^\s*```[^\n]*$", "", content)
        if section != "TL;DR" and not re.search(r"\w", content):
            errors[section] = list(dict.fromkeys(i for _, _, group in lines for i in group))
            continue
        for start, end, affected in lines:
            replacement = ""
            if section == "TL;DR" and tldr_start == start:
                replacement = "**TL;DR:**\n"
            edits.append((start, end, section, replacement, affected, "line_removal"))
        if section == "TL;DR" and not re.search(r"\w", content.replace("**TL;DR:**", "")):
            start, end, sec, _, affected, action = edits[-len(lines)]
            label = "**TL;DR:** " if start == tldr_start else ""
            edits[-len(lines)] = (start, end, sec, label + "Unavailable: see sections.\n",
                                  affected, action)
    records = []
    for section in errors:
        start, end = bodies[section]
        replacement = unavailable_line(errors[section]) + "\n\n"
        edits.append((start, end, section, replacement, errors[section], "section_replacement"))
    for start, end, section, replacement, affected, action in sorted(edits, reverse=True):
        records.append({"section": section, "action": action, "start": start, "end": end,
                        "removed": brief[start:end],
                        "replacement": replacement,
                        "reasons": [{"metric": i.metric, "reason": i.reason} for i in affected]})
        brief = brief[:start] + replacement + brief[end:]
    return brief, list(reversed(records))


def parse_stamp(value: str) -> dt.datetime:
    """Parse the gatherer's minute-resolution ET value; no time tolerance."""
    parsed = dt.datetime.strptime(value, "%a %b %d, %Y %I:%M %p ET")
    return parsed.replace(tzinfo=ZoneInfo("America/New_York"))


def stamp_errors(text: str, as_of_et: object = _NO_BLOCK) -> list[str]:
    stamps = list(STAMP_RE.finditer(text))
    if not stamps:
        return ["missing '*Data pulled ... ET*' stamp"]
    if len(stamps) != 1:
        return ["expected exactly one Data pulled stamp"]
    value = stamps[0].group(1).strip() + " ET"
    try:
        stamp = parse_stamp(value)
    except ValueError:
        return ["unparsable Data pulled stamp"]
    # Compose's structural gate has no payload. The orchestrator always supplies
    # this argument, including None when a block/stamp is missing.
    if as_of_et is not _NO_BLOCK:
        if not isinstance(as_of_et, str):
            return ["gathered block as_of_et is missing or unparsable"]
        try:
            block_stamp = parse_stamp(as_of_et)
        except ValueError:
            return ["gathered block as_of_et is missing or unparsable"]
        if stamp != block_stamp or stamps[0].group() != f"*Data pulled {as_of_et}*":
            return ["Data pulled stamp does not exactly match gathered block as_of_et"]
    return []


def display_date(day: dt.date) -> str:
    """The playbook title uses a day number, not a zero-padded day."""
    return f"{day:%a %B} {day.day}, {day:%Y}"


def check(brief: str, expected_date: str, display_date: str, max_chars: int,
          data_warning: str | None, as_of_et: object = _NO_BLOCK,
          run_id: object = _NO_BLOCK) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Any error means do not publish."""
    errors: list[str] = []
    warnings: list[str] = []

    # The ceiling is on the reader-visible brief, not transport metadata.
    # Strip the completion marker before measuring or checking any content.
    try:
        text = visible_text(brief)
    except ValueError as exc:
        return [str(exc)], warnings
    if not text:
        return ["brief is empty"], warnings

    if len(text) > max_chars:
        errors.append(f"length {len(text)} exceeds ceiling {max_chars}")
    if len(text) < 800:
        errors.append(f"length {len(text)} is implausibly short")

    # Tolerate a writer padding the day; the playbook form remains canonical.
    title_line = re.sub(r"(?<= )0([1-9])(?=, )", r"\1", text.splitlines()[0])
    if not TITLE_RE.match(text):
        errors.append("missing or malformed title line")
    elif display_date not in title_line and expected_date not in title_line:
        errors.append(f"title does not carry the expected date ({display_date})")

    if "**TL;DR:**" not in text:
        errors.append("missing TL;DR line")
    if len(re.findall(r"(?m)^\s*```", text)) % 2:
        errors.append("unbalanced code fence")

    positions = []
    for marker in SECTION_MARKERS:
        idx = text.find(marker)
        if idx < 0:
            errors.append(f"missing section {marker}")
        else:
            positions.append(idx)
    if len(positions) == len(SECTION_MARKERS) and positions != sorted(positions):
        errors.append("sections are out of the canonical order")

    # The footer is metadata, never a section body.
    footer = FOOTER_RE.search(text)
    if footer is None:
        errors.append("missing footer/disclaimer")
    body_text = text[:footer.start()] if footer else text
    headings = list(re.finditer(r"^##\s+[^\n]*(?:\n|$)", body_text, re.MULTILINE))
    for i, heading in enumerate(headings):
        marker = next((m for m in SECTION_MARKERS if heading.group().startswith(m)), None)
        if marker is None:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body_text)
        body = body_text[heading.end():end]
        body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
        if not re.search(r"\w", body):
            errors.append(f"empty section body {marker}")

    errors.extend(stamp_errors(text, as_of_et))
    if run_id is not _NO_BLOCK:
        errors.extend(run_identity.errors(text, run_id, footer=True))

    if URL_RE.search(text):
        errors.append("contains a URL; the standing rule forbids source links")
    if re.search(r"^#+\s*Sources?\b", text, re.MULTILINE | re.IGNORECASE):
        errors.append("contains a Sources section")

    if data_warning:
        gex = ""
        for i, heading in enumerate(headings):
            if heading.group().startswith("## 🧭"):
                end = headings[i + 1].start() if i + 1 < len(headings) else len(body_text)
                gex = body_text[heading.end():end]
        # Same sentence/line in GEX, not an unrelated unavailable feed.
        disclosure = re.search(
            r"\b(?:gamma(?:\s+read)?|gex)(?:\s+(?:near-term|full-chain|view|read|is|are|both|and))*"
            r"\s*[:—-]?\s*unavailable\b", re.sub(r"[*_`]", "", gex), re.IGNORECASE
        )
        if not disclosure:
            errors.append(
                "gather reported a GEX data warning but the brief does not say the gamma read is unavailable"
            )
        # TL;DR and Regime read can also carry unsupported claims.
        plain = re.sub(r"[*_`]", "", text.lower())
        for sentence in re.split(r"[.!?\n]", plain):
            claim = re.search(
                r"\b(?:positive|negative|long|short)[ -]+gamma\b|"
                r"\bgamma\s*(?::|is|remains|stays)?\s*(?:deeply\s+|strongly\s+)?(?:positive|negative)\b|"
                r"\b(?:gamma\s+regime|(?:gamma\s+)?flip(?:\s+level)?)\b", sentence
            )
            if claim and not re.fullmatch(
                r"\s*(?:[-•]\s*)?(?:gamma\s+regime|(?:gamma\s+)?flip(?:\s+level)?)"
                r"\s+(?:is\s+)?unavailable\s*", sentence
            ):
                errors.append("GEX unavailable: forbidden gamma regime/flip claim")
                break

    return errors, warnings
