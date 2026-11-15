"""Paths and settings for the market brief.

Nothing here depends on the working directory: the project root is resolved from
this file, and the runtime root from the workspace layout (or MARKET_BRIEF_RUNTIME).

Secrets live in runtime/market-brief/config/.env, outside the repository.
Ordinary settings live in runtime/market-brief/config/settings.toml.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent            # .../CMXMS/projects/market-brief
WORKSPACE = PROJECT.parents[1]                       # .../CMXMS

RUNTIME = Path(
    os.environ.get("MARKET_BRIEF_RUNTIME") or (WORKSPACE / "runtime" / "market-brief")
)
DATA = RUNTIME / "data"
LOGS = RUNTIME / "logs"
STATE = RUNTIME / "state"
CONFIG = RUNTIME / "config"

BRIEFS = PROJECT / "briefs"
SETTINGS_PATH = CONFIG / "settings.toml"
ENV_PATH = CONFIG / ".env"


def ensure_dirs() -> None:
    for d in (DATA, LOGS, STATE, CONFIG, BRIEFS):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    with SETTINGS_PATH.open("rb") as f:
        return tomllib.load(f)


def load_env() -> None:
    """Read KEY=VALUE pairs from the runtime config without overriding real env vars."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def hermes_bin() -> str:
    settings = load_settings()
    configured = settings.get("runtime", {}).get("hermes_bin")
    if configured:
        return str(configured)
    return "hermes"
