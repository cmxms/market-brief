"""Narrative generation.

Hermes runs headless with the `web` and `file` toolsets only, so it has no
terminal and cannot execute anything. It is also launched with a filtered
environment: every key declared in the runtime `config/.env`, and anything
matching `DISCORD_WEBHOOK*`, is removed before the child starts. Posting is a
separate process that runs after validation.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import config

PROMPT = """You are writing today's market brief for {date}.

Read these files first, in this order:
1. playbook.md - the source of truth for structure, tone and hard rules.
2. {data_md} - today's gathered numbers. Every number you publish must come from
   this block or from a page you fetch yourself.
3. {reference} - the house-style reference brief.

Write the finished brief to:
{out_path}

Then write a claims ledger to:
{claims_path}

The ledger is a JSON array, one object per specific factual claim that names a
person, a count, a date, or a figure not present in the data block:
  {{"claim": "what the brief asserts", "source": "https://...", "retrieved": "YYYY-MM-DD"}}
Include only claims you actually sourced. If a claim has no source, leave it out
of the brief entirely.

Rules that override everything else:
- Do not post anywhere and do not attempt to. You have no tool that can.
- Do not include source links, citations, URLs or a Sources section in the brief.
- Never invent a number. If a figure is missing, leave it out or say it is unavailable.
- Stay under {max_chars} characters.
- Keep the canonical section headers and their order exactly as playbook.md fixes them.
- Include the stamp line `*Data pulled ... ET*`, using the data block's own as_of_et.
- Write the two files and nothing else. Do not summarise your work back to me.
"""


def run(date: str, data_md: Path, out: Path, log_path: Path) -> tuple[bool, str]:
    """Generate the brief. Returns (ok, detail)."""
    settings = config.load_settings()
    model_cfg = settings.get("model", {})
    brief_cfg = settings.get("brief", {})
    reference = config.BRIEFS / str(brief_cfg.get("reference", "2026-07-14.md"))

    if not data_md.exists():
        return False, f"data block missing: {data_md}"
    if not reference.exists():
        return False, f"reference brief missing: {reference}"

    prompt = PROMPT.format(
        date=date,
        data_md=data_md,
        reference=reference,
        out_path=out,
        claims_path=config.STATE / f"claims-{date}.json",
        max_chars=brief_cfg.get("max_chars", 5200),
    )
    prompt_file = config.STATE / f"prompt-{date}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    cmd = [
        config.hermes_bin(), "chat",
        "--query-file", str(prompt_file),
        "--oneshot", "-Q",
        "--in", str(config.PROJECT),
        "-t", "web,file",
        "-m", str(model_cfg.get("name", "deepseek/deepseek-v4.1-flash-1017")),
        "--max-turns", str(model_cfg.get("max_turns", 40)),
        "--run-budget", str(model_cfg.get("run_budget_seconds", 900)),
    ]

    # Preserve launcher/OS variables and unrelated settings, but never runtime
    # config secrets. Compare case-insensitively for Windows environment names.
    excluded = {key.upper() for key in config.env_keys()}
    child_env = {key: value for key, value in os.environ.items()
                 if key.upper() not in excluded
                 and not key.upper().startswith("DISCORD_WEBHOOK")}

    started = time.time()
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n=== compose start {date} ===\n")
        log.write("compose environment: filtered (runtime config keys and DISCORD_WEBHOOK* excluded)\n")
        log.write(" ".join(cmd) + "\n")
        try:
            proc = subprocess.run(cmd, env=child_env, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=int(model_cfg.get("run_budget_seconds", 900)) + 120)
        except subprocess.TimeoutExpired:
            log.write("compose timed out\n")
            return False, "compose timed out"
        except OSError as exc:
            detail = f"compose launch failed with filtered environment ({type(exc).__name__}, errno {exc.errno})"
            log.write(detail + "\n")
            return False, detail
        log.write(proc.stdout or "")
        if proc.stderr:
            log.write("\n--- stderr ---\n" + proc.stderr)
        log.write(f"\n=== compose exit {proc.returncode} ===\n")

    if not out.exists():
        return False, f"no brief written (hermes exit {proc.returncode})"
    if out.stat().st_mtime < started - 5:
        return False, "brief file was not rewritten by this run"
    return True, f"hermes exit {proc.returncode}, {out.stat().st_size} bytes"
