"""Checks a finished brief before it can be delivered.

These are structural and policy checks, not a proof of correctness. They catch the
failure modes that would make a brief wrong to publish: wrong date, missing
sections, missing stamp, links, and a length overrun. They cannot tell whether a
number is true; that is what the gather block and the playbook rules are for.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

# Emoji-prefixed section headers, in the order the playbook fixes them.
SECTION_MARKERS = ("## 📰", "## 📊", "## 🔁", "## 📈", "## 🚨", "## 🧭")

STAMP_RE = re.compile(r"\*Data pulled\s+(.+?)\s*ET\*")
TITLE_RE = re.compile(r"^# 📊 Market Brief — ")
URL_RE = re.compile(r"https?://", re.IGNORECASE)
COMPLETION_RE = re.compile(r"\n<!-- market-brief complete: attempt-[A-Za-z0-9_-]+ -->\s*\Z")
FOOTER_RE = re.compile(
    r"(?m)^---\s*\n\s*\*Not financial advice — situational awareness only\.\*\s*\Z"
)


_NO_BLOCK = object()


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
          data_warning: str | None, as_of_et: object = _NO_BLOCK) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Any error means do not publish."""
    errors: list[str] = []
    warnings: list[str] = []

    # The ceiling is on the reader-visible brief, not transport metadata.
    # Strip the completion marker before measuring or checking any content.
    text = COMPLETION_RE.sub("", brief.strip()).strip()
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
