"""Checks a finished brief before it can be delivered.

These are structural and policy checks, not a proof of correctness. They catch the
failure modes that would make a brief wrong to publish: wrong date, missing
sections, missing stamp, links, and a length overrun. They cannot tell whether a
number is true; that is what the gather block and the playbook rules are for.
"""

from __future__ import annotations

import re

# Emoji-prefixed section headers, in the order the playbook fixes them.
SECTION_MARKERS = ("## 📰", "## 📊", "## 🔁", "## 📈", "## 🚨", "## 🧭")

STAMP_RE = re.compile(r"\*Data pulled\s+(.+?)\s*ET\*")
TITLE_RE = re.compile(r"^# 📊 Market Brief — ")
URL_RE = re.compile(r"https?://", re.IGNORECASE)


def check(brief: str, expected_date: str, display_date: str, max_chars: int,
          data_warning: str | None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Any error means do not publish."""
    errors: list[str] = []
    warnings: list[str] = []

    text = brief.strip()
    if not text:
        return ["brief is empty"], warnings

    if len(text) > max_chars:
        errors.append(f"length {len(text)} exceeds ceiling {max_chars}")
    if len(text) < 800:
        errors.append(f"length {len(text)} is implausibly short")

    title_line = text.splitlines()[0]
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

    if not STAMP_RE.search(text):
        errors.append("missing '*Data pulled ... ET*' stamp")

    if URL_RE.search(text):
        errors.append("contains a URL; the standing rule forbids source links")
    if re.search(r"^#+\s*Sources?\b", text, re.MULTILINE | re.IGNORECASE):
        errors.append("contains a Sources section")

    if data_warning:
        lowered = text.lower()
        disclosure = ("gamma" in lowered and ("unavailable" in lowered or "incomplete" in lowered))
        if not disclosure:
            warnings.append(
                "gather reported a GEX data warning but the brief does not say the gamma read is unavailable"
            )

    return errors, warnings
