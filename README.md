# market-brief

A pre-open market brief: a deterministic data gatherer pulls the hard numbers, an
LLM writes the narrative, and the finished brief is posted to Discord. It is
situational awareness — what happened, what is rotating, where the dealer-gamma
and volatility regime sit, what is on the event calendar. **Not** financial
advice.

## Pipeline

`run_brief.py` runs four steps, and a later step only runs if the earlier one
succeeded:

1. **gather** — `gather.py` pulls the numbers into a data block.
2. **compose** — Hermes writes the brief from `playbook.md` plus that block.
3. **validate** — structural and policy checks; a failure stops the run.
4. **deliver** — Discord, through a per-destination ledger that refuses to post
   the same brief to the same destination twice.

`playbook.md` is the source of truth for what the brief says and how it is
shaped. This file is the operating contract around it.

## What it pulls

- **Index futures** (ES/YM/NQ/RTY) and oil.
- **Macro pulse** — Treasury par yields, plus the VIX curve from Cboe's settled
  closes with the VIX/VIX3M regime ratio.
- **Sector rotation** — the 11 SPDR sectors plus semis/software/growth, ranked
  against SPY.
- **Dealer gamma (GEX)** for SPY and QQQ: near-term and full-chain views.
- **Event gate** — high-impact events from the bundled calendar.
- **Headlines** — overnight wire via Finnhub, trusted-source ranked.

## Rules

- **The compose step never posts.** It runs with the `web` and `file` toolsets
  only, so it has no terminal, and it receives a filtered environment: keys
  declared in `runtime/market-brief/config/.env` and anything matching
  `DISCORD_WEBHOOK*` are stripped before launch.
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
- `runtime/market-brief/state/` — delivery ledger, usage log, prompts, claims.
- `runtime/market-brief/config/` — `.env` (credentials) and `settings.toml`.

Nothing resolves paths from the working directory. Credentials are never stored
in the repository; `.env.example` names the variables and nothing more.

## Running it

```
.venv/Scripts/python.exe run_brief.py --dry --ignore-window --force-weekend   # checks only, prints the split
.venv/Scripts/python.exe run_brief.py --test --ignore-window                  # posts to the test channel
.venv/Scripts/python.exe run_brief.py                                          # production
```

The scheduled run fires weekdays at 07:35 Chicago and only proceeds inside the
07:30–08:30 window. Weekends and NYSE holidays exit 2 without posting.

Exit codes: 0 ok, 2 skipped (weekend or holiday), 3 gather, 4 compose,
5 validation, 6 outside window, 7 delivery, 8 spend estimate reached.

A delivery that reaches some destinations but not all exits 7. The ledger records
each destination separately, so the next run re-sends only the ones still
missing — it does not duplicate the ones that worked.

## Tests

```
.venv/Scripts/python.exe -m pytest -q
```

Covers the gamma flip, the GEX fallbacks, the roll guard, the stale-block check,
delivery ledger and redaction, and the compose environment. Three of the older
test files are script-style and also run directly (`python tests/test_x.py`).
