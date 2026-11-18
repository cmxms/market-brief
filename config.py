"""Paths and settings for the market brief.

Nothing here depends on the working directory: the project root is resolved from
this file, and the runtime root from `MARKET_BRIEF_RUNTIME`, a sibling workspace
runtime, or a `runtime/` folder inside the repository — in that order.

Secrets live in <runtime>/config/.env, outside version control.
Ordinary settings live in <runtime>/config/settings.toml.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent            # the repository root
_SIBLING_RUNTIME = PROJECT.parents[1] / "runtime" if len(PROJECT.parents) > 1 else None


def _runtime_root() -> Path:
    """Where data, logs, state and credentials live.

    An explicit `MARKET_BRIEF_RUNTIME` always wins. Otherwise a runtime folder
    beside the repository's parent directory is used, which is the layout this
    project was built in; failing that, a `runtime/` folder inside the repository,
    so a plain clone runs without configuration.
    """
    explicit = os.environ.get("MARKET_BRIEF_RUNTIME")
    if explicit:
        return Path(explicit)
    if _SIBLING_RUNTIME is not None and _SIBLING_RUNTIME.is_dir():
        return _SIBLING_RUNTIME / "market-brief"
    return PROJECT / "runtime"


RUNTIME = _runtime_root()
DATA = RUNTIME / "data"
LOGS = RUNTIME / "logs"
STATE = RUNTIME / "state"
CONFIG = RUNTIME / "config"

BRIEFS = PROJECT / "briefs"
SETTINGS_PATH = CONFIG / "settings.toml"
ENV_PATH = CONFIG / ".env"
_ENV_KEYS: set[str] = set()


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
        key = key.strip()
        # Track declarations even when a real environment variable takes precedence.
        _ENV_KEYS.add(key)
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def env_keys() -> frozenset[str]:
    """Keys declared by runtime env files loaded in this process (no values)."""
    return frozenset(_ENV_KEYS)


def hermes_bin() -> str:
    settings = load_settings()
    configured = settings.get("runtime", {}).get("hermes_bin")
    if configured:
        return str(configured)
    return "hermes"
