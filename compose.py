"""Narrative generation.

Hermes runs headless with the `web` and `file` toolsets only, so it has no
terminal and cannot execute anything. It is also launched with a filtered
environment: every key declared in the runtime `config/.env`, and anything
matching `DISCORD_WEBHOOK*`, is removed before the child starts. Posting is a
separate process that runs after validation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import config
import run_identity
import validate

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
Each object must have nonempty string fields claim, source (HTTP(S) URL), and retrieved (YYYY-MM-DD); use [] if there are no such claims.

Rules that override everything else:
- Do not post anywhere and do not attempt to. You have no tool that can.
- Do not include source links, citations, URLs or a Sources section in the brief.
- Never invent a number. If a figure is missing, leave it out or say it is unavailable.
- Never state a value marked DO NOT PUBLISH; its corresponding section must say unavailable or unverified, with the stated reason.
- If a gathered section is unavailable, say so and include its stated reason in the
  corresponding brief section. Never silently omit a failed section.
- Aim for about {target_chars} characters and stay under {max_chars}. The ceiling is a
  hard limit that validation enforces; the target is what you write to first time.
- Write the brief in ONE `write_file` call, complete and already short enough. Do not
  write it long and then shave it with `patch` edits: one pass, then leave it alone.
- Then write the claims ledger in ONE `write_file` call. Two write calls in total.
  Spend the rest of your turns on reading sources before you write, not on editing after.
- Keep the canonical section headers and their order exactly as playbook.md fixes them.
- The title line is exactly `# 📊 Market Brief — {{weekday}} {{full month}} {{D}}, {{YYYY}}` with the
  month spelled out (e.g. `Thu November 19, 2026`). Validation rejects an abbreviated month.
- Include the stamp line `*Data pulled ... ET*`, using the data block's own as_of_et.
- After the disclaimer, append `{end_marker}` as the last thing in the brief file (no text after it).
- Write the two files and nothing else. Do not summarise your work back to me.
"""


def run(date: str, data_md: Path, out: Path, log_path: Path,
        *, run_id: str | None = None, claims_out: Path | None = None,
        record_invocation: Callable[[], None] | None = None) -> tuple[bool, str]:
    """Generate and atomically save validated artifacts. Returns (ok, detail).

    Preview callers supply attempt-local destinations for both artifacts.
    Otherwise the claims destination defaults to the canonical date ledger.
    record_invocation charges a launched child, including failures and timeouts,
    but not preparation failures or an OS refusal to launch.
    """
    settings = config.load_settings()
    model_cfg = settings.get("model", {})
    brief_cfg = settings.get("brief", {})
    reference = config.BRIEFS / str(brief_cfg.get("reference", "2026-07-14.md"))

    def result(ok: bool, detail: str) -> tuple[bool, str]:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(detail + "\n")
        return ok, detail

    if not data_md.exists():
        return result(False, f"failed: data block missing: {data_md}")
    if not reference.exists():
        return result(False, f"failed: reference brief missing: {reference}")
    block_text = data_md.read_text(encoding="utf-8")
    har_enabled = os.environ.get("MARKET_BRIEF_HAR_ENABLED") == "1"
    har_line = None
    if har_enabled:
        # This line was rendered by gather, never by the narrative model. A
        # reused block lacking the optional estimate cannot supply a fallback.
        matches = re.findall(r"^- (Volatility \(HAR, NQ\): .+)$", block_text, re.MULTILINE)
        if (len(matches) == 1 and block_text.startswith(f"# BRIEF DATA BLOCK — {date}\n")
                and re.fullmatch(r"Volatility \(HAR, NQ\): (?:~\d+ pts expected range \(10–90: \d+–\d+\)(?: — outside training range)?|omitted — [\w (),.-]+)", matches[0])):
            har_line = "- " + matches[0]
        else:
            har_line = "- Volatility (HAR, NQ): omitted — no estimate in this session's data block"
    # Never expose an old enabled block's line to a flag-off writer.
    block_text = re.sub(r"^- Volatility \(HAR, NQ\): .*\n?", "", block_text, flags=re.MULTILINE)
    if run_id is not None:
        errors = run_identity.errors(block_text, run_id)
        if errors:
            return result(False, "failed: data block identity: " + "; ".join(errors))

    # mkdtemp exclusively creates a new namespace; neither wall time nor mtimes
    # establish ownership. Retain failed attempts for diagnosis, never reuse them.
    try:
        attempts = config.STATE / "compose-attempts"
        attempts.mkdir(parents=True, exist_ok=True)
        attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=attempts))
        candidate = attempt / "brief.md"
        claims_path = attempt / "claims.json"
        end_marker = f"<!-- market-brief complete: {attempt.name} -->"
        snapshot = attempt / "data.md"
        snapshot.write_text(block_text, encoding="utf-8")
    except OSError as exc:
        return result(False, f"failed: attempt creation ({type(exc).__name__})")

    prompt = PROMPT.format(
        date=date,
        data_md=snapshot,
        reference=reference,
        out_path=candidate,
        claims_path=claims_path,
        end_marker=end_marker,
        max_chars=int(brief_cfg.get("max_chars", 5200)) - (len(run_identity.line(run_id)) + 1 if run_id else 0) - (len(har_line) + 2 if har_line else 0),
        target_chars=int(brief_cfg.get("target_chars", 4800)) - (len(run_identity.line(run_id)) + 1 if run_id else 0) - (len(har_line) + 2 if har_line else 0),
    )
    prompt_file = attempt / "prompt.txt"
    if run_id is not None:
        prompt += (f"\nThe data block run_id is {run_id}. Do not copy its Run line: "
                   "the parent appends the trace footer within the reserved character budget.\n")
    if har_enabled:
        prompt += "\nDo not write a HAR range line. The parent inserts it in Macro pulse within the reserved budget.\n"
    try:
        prompt_file.write_text(prompt, encoding="utf-8")
    except OSError as exc:
        return result(False, f"failed: attempt prompt write ({type(exc).__name__})")

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

    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n=== compose start {date} ===\n")
        log.write("compose environment: filtered (runtime config keys and DISCORD_WEBHOOK* excluded)\n")
        log.write(" ".join(cmd) + "\n")
        try:
            proc = subprocess.run(cmd, env=child_env, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=int(model_cfg.get("run_budget_seconds", 900)) + 120,
                                  check=False)
        except subprocess.TimeoutExpired:
            if record_invocation is not None:
                record_invocation()
            log.write("failed: compose timed out\n")
            return False, "failed: compose timed out"
        except OSError as exc:
            detail = f"failed: compose launch failed with filtered environment ({type(exc).__name__}, errno {exc.errno})"
            log.write(detail + "\n")
            return False, detail
        if record_invocation is not None:
            record_invocation()
        log.write(proc.stdout or "")
        if proc.stderr:
            log.write("\n--- stderr ---\n" + proc.stderr)
        log.write(f"\n=== compose exit {proc.returncode} ===\n")

    try:
        # Read once: validate, attest and promote the same bytes, not a later read.
        # Symlinks could otherwise import artifacts from another attempt.
        if candidate.is_symlink() or claims_path.is_symlink():
            raise ValueError("attempt artifacts must not be symlinks")
        brief_bytes = candidate.read_bytes()
        claims_bytes = claims_path.read_bytes()
        brief = brief_bytes.decode("utf-8")
        if not brief.rstrip().endswith(end_marker):
            raise ValueError("missing attempt end-of-document marker or text after marker")
        if not har_enabled:
            # A retained reference or an uncooperative writer cannot enable HAR.
            without_har = re.sub(r"^(?:- )?Volatility \(HAR, NQ\): .*\r?\n?", "", brief, flags=re.MULTILINE)
            if without_har != brief:
                brief = without_har
                brief_bytes = brief.encode("utf-8")
        if har_line:
            header = "## 📈 Macro pulse (moves, not levels)"
            if brief.count(header) != 1:
                raise ValueError("HAR insertion requires one canonical Macro pulse section")
            # Strip a model-written duplicate, then insert immediately before
            # the next section. All final structural/length checks still apply.
            brief = re.sub(r"^(?:- )?Volatility \(HAR, NQ\): .*\n?", "", brief, flags=re.MULTILINE)
            position = brief.find("\n## ", brief.index(header) + len(header))
            if position < 0:
                raise ValueError("HAR insertion requires the next canonical section")
            brief = brief[:position].rstrip() + "\n" + har_line + "\n" + brief[position:]
            brief_bytes = brief.encode("utf-8")
        if run_id is not None:
            brief = brief.rstrip()[:-len(end_marker)].rstrip() + "\n" + run_identity.line(run_id) + "\n" + end_marker
            brief_bytes = brief.encode("utf-8")
        claims = json.loads(claims_bytes.decode("utf-8"))
        if not isinstance(claims, list) or any(
            not isinstance(row, dict) or any(
                not isinstance(row.get(field), str) or not row[field].strip()
                for field in ("claim", "source", "retrieved")
            ) for row in claims
        ):
            raise ValueError("claims ledger schema: expected array of objects with nonempty claim/source/retrieved strings")
        for row in claims:
            source = urlsplit(row["source"])
            if source.scheme not in ("http", "https") or not source.hostname:
                raise ValueError("claims ledger schema: source must be an HTTP(S) URL")
            try:
                retrieved = dt.date.fromisoformat(row["retrieved"])
            except ValueError:
                raise ValueError("claims ledger schema: retrieved must be YYYY-MM-DD") from None
            if retrieved.isoformat() != row["retrieved"]:
                raise ValueError("claims ledger schema: retrieved must be YYYY-MM-DD")
        day = dt.date.fromisoformat(date)
        display = validate.display_date(day)
        errors, _ = validate.check(
            brief, date, display, int(brief_cfg.get("max_chars", 5200)), None
        )
        if run_id is not None:
            errors.extend(run_identity.errors(validate.visible_text(brief), run_id, footer=True))
        if errors:
            raise ValueError("brief validation: " + "; ".join(errors))

        # The parent's completion record attests to the exact checked artifacts.
        # It costs no writer turn and is never inferred from file existence.
        record = {
            "attempt": attempt.name, "date": date, "child_exit": proc.returncode,
            "run_id": run_id,
            "brief": {"path": candidate.name, "sha256": hashlib.sha256(brief_bytes).hexdigest()},
            "claims": {"path": claims_path.name, "sha256": hashlib.sha256(claims_bytes).hexdigest()},
        }
        candidate.write_bytes(brief_bytes)
        (attempt / "complete.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        # Stage beside each destination for same-filesystem atomic replacement.
        # No canonical artifact is touched until every completeness check passed.
        claims_out = claims_out if claims_out is not None else config.STATE / f"claims-{date}.json"
        staged_claims = claims_out.with_suffix(f".{attempt.name}.tmp")
        staged_brief = out.with_suffix(f".{attempt.name}.tmp")
        staged_claims.write_bytes(claims_bytes)
        staged_brief.write_bytes(brief_bytes)
        staged_claims.replace(claims_out)
        staged_brief.replace(out)
    except (OSError, UnicodeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return result(False, f"failed (hermes exit {proc.returncode}, {attempt.name}): {reason}")

    outcome = ("hermes exit 0" if proc.returncode == 0 else
               f"recovered after a failed child (complete attempt); hermes exit {proc.returncode}")
    return result(True, f"{outcome}; {attempt.name}; validated and promoted {len(brief_bytes)} bytes")
