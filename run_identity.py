"""Shared run identity syntax and reader-visible trace line."""
from __future__ import annotations

import re
import uuid

ID_RE = re.compile(r"[0-9a-f]{32}\Z")
TRACE_RE = re.compile(r"(?m)^-# Run: ([0-9a-f]{32})$")


def new() -> str:
    return uuid.uuid4().hex


def require(value: object) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("run_id must be 32 lowercase hexadecimal characters")
    return value


def line(value: str) -> str:
    return f"-# Run: {require(value)}"


def errors(brief: str, expected: object) -> list[str]:
    try:
        expected = require(expected)
    except ValueError:
        return ["gathered block run_id is missing or invalid"]
    matches = list(TRACE_RE.finditer(brief))
    if len(matches) != 1 or matches[0].group(1) != expected:
        return ["Run trace does not exactly match gathered block run_id"]
    return []
