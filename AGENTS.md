# market-brief

Produces the pre-open market brief and posts it to Discord.

## Pipeline

`run_brief.py` runs four separate steps, and a later step only runs if the
earlier one succeeded:

1. **gather** — `gather.py` pulls the hard numbers into a data block.
2. **compose** — Hermes writes the brief from `playbook.md` plus that block.
3. **validate** — structural and policy checks. A failure here stops the run.
4. **deliver** — Discord, through a ledger that refuses to post the same brief twice.

`playbook.md` is the source of truth for what the brief says and how it is shaped.
This file is only the operating contract around it.

## Rules

- **The compose step never posts.** It runs with the `web` and `file` toolsets
  only, so it has no terminal and no delivery credentials. Posting is a separate
  process that runs after validation.
- **Never invent a number.** Every figure comes from the data block or a page the
  agent fetched itself.
- **No links, citations or source lists in the brief.** Verify sources privately;
  keep them in the run log or the data block.
- **A brief that fails validation is not published**, and the failure is reported
  with a non-zero exit code.

## Where things live

- This repository: code, tests, `playbook.md`, the briefs archive.
- `runtime/market-brief/data/` — gathered data blocks.
- `runtime/market-brief/logs/` — run logs.
- `runtime/market-brief/state/` — delivery ledger, usage log, prompts.
- `runtime/market-brief/config/` — `.env` (credentials) and `settings.toml`.

Nothing resolves paths from the working directory.

## Running it

    python run_brief.py --dry --ignore-window --force-weekend   # checks only, prints the split
    python run_brief.py --test  --ignore-window                 # posts to the test channel
    python run_brief.py                                          # production

Exit codes: 0 ok, 2 weekend, 3 gather, 4 compose, 5 validation, 6 outside window,
7 delivery, 8 spend estimate.

Known gap: US market holidays are not detected yet. Do not run this unattended on
a holiday until a verified calendar is in place.
