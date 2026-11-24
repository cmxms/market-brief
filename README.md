# market-brief

A pre-open market brief: a deterministic data gatherer pulls the hard numbers, an
LLM writes the narrative, and the finished brief is posted to Discord. It is
situational awareness — what happened, what is rotating, where the dealer-gamma
and volatility regime sit, what is on the event calendar. **Not** financial
advice.

Recent output is in [`briefs/`](briefs/). The source chain behind every number,
and why each fallback exists, is in [`DATA-SOURCES.md`](DATA-SOURCES.md).

## Pipeline

`run_brief.py` runs four steps, and a later step only runs if the earlier one
succeeded:

1. **gather** — `gather.py` pulls the numbers into a data block. No model, no
   cost, deterministic.
2. **compose** — a headless Hermes agent writes the brief from `playbook.md`
   plus that block.
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
- **Dealer gamma (GEX)** for SPY and QQQ: near-term and full-chain views,
  computed from the Tradier chain rather than taken from a vendor.
- **Event gate** — high-impact events from the bundled calendar, in two tiers.
- **Headlines** — overnight wire via Finnhub, trusted-source ranked.

## Rules

- **The compose step never posts.** It runs with the `web` and `file` toolsets
  only, so it has no terminal, and it receives a filtered environment: keys
  declared in the runtime `.env` and anything matching `DISCORD_WEBHOOK*` are
  stripped before launch. Posting is a separate process after validation.
- **Never invent a number.** Every figure comes from the data block or a page the
  agent fetched itself. Claims that need a source go in a claims ledger, not the
  brief.
- **No links, citations or source lists in the brief.** Verify sources privately;
  keep them in the run log or the data block.
- **A brief that fails validation is not published**, and the failure is reported
  with a non-zero exit code.

Gamma comparison prints are acquired **when data is gathered**, not during composition.
Gathering takes the newest dated print across the yfinance one-minute pre/post bar and the
Tradier last trade — whichever timestamp is later, ties to Yahoo — with no session window and no
age limit: the brief runs once, premarket, and the price in force then is the one it uses. The
time travels with the number, and a print dated before the current ET session is labelled a
**prior close** rather than presented as live. Only an undated print is refused, and with no
dated print the gamma comparison is withheld with its reason. Neither a prior close nor a
futures-implied level is ever substituted silently. Routes that reuse a retained block without
gathering (`--skip-gather` or `--deliver-only`) carry that block's own print and its time.
Composition makes no spot-fetch call. The chain's own spot and timestamp remain separate
diagnostic fields, and chain arithmetic still uses that chain spot.

## Requirements

- **Python 3.11 or newer** — settings are read with the standard library's TOML
  parser. Developed and run on 3.13.
- **A model the brief writer can use.** The compose step shells out to the
  [Hermes Agent](https://hermes-agent.nousresearch.com/docs) CLI (`hermes chat`)
  with a prompt and a tool allowlist, so Hermes must be installed and configured
  with a provider before the pipeline can produce a brief. Gathering, validation
  and delivery do not need it.
- **Credentials** — two required, see below.

## Setup

```bash
git clone https://github.com/cmxms/market-brief.git
cd market-brief
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # macOS / Linux
```

### Where the runtime lives

Data blocks, logs, the delivery ledger and credentials all live in a **runtime
folder outside version control**. Its location, in order of precedence:

1. `$MARKET_BRIEF_RUNTIME`, if set.
2. A `runtime/` folder beside the repository's parent directory — the layout
   this project was built in.
3. `runtime/` inside the repository — the default for a plain clone, already
   gitignored.

To see which one your checkout resolves to:

```bash
.venv/Scripts/python.exe -c "import config; print(config.RUNTIME)"
```

### Credentials

Copy `.env.example` to `<runtime>/config/.env` and fill it in. Real environment
variables override the file, so CI secrets work too.

| Variable | Required | Used for |
|---|---|---|
| `TRADIER_API_KEY` | **yes** | Live option chain; without it the gamma section is unavailable. A free developer account works. |
| `FINNHUB_API_KEY` | **yes** | Overnight headlines. Free tier is enough. |
| `FRED_API_KEY` | no | Backs up the Treasury-yield chain, and is how the event calendar reads BLS dates. |
| `DISCORD_WEBHOOK_URL` | to post | One or more incoming-webhook URLs, comma- or whitespace-separated. `DISCORD_WEBHOOK_URLS` is merged in as well. |

Nothing else is needed: futures, sector ETFs, VIX and the Treasury par curve
come from public endpoints with no key.

### Settings

Optional. Copy `settings.example.toml` to `<runtime>/config/settings.toml`; every
key in it is documented with its default, so a missing file changes nothing.
Two worth setting on a fresh install: `[model] name` (which model writes the
brief) and `[discord] test_channel_id` (where `--test` posts).

### Optional NQ session range (default off)

HAR estimates the next full NQ/CME session's high-low range in index points,
not direction and not dollar P&L. It is disabled unless
`MARKET_BRIEF_HAR_ENABLED=1` exactly. The other values below do nothing while
it is disabled. Set them in the process environment or `<runtime>/config/.env`;
no launch-directory-relative data paths are accepted.

| Variable | Default | Meaning |
|---|---|---|
| `MARKET_BRIEF_HAR_ENABLED` | unset/off | Only `1` enables gathering and the brief line. |
| `MARKET_BRIEF_NQ_PARQUET` | unset | Absolute path to the external continuous NQ one-minute parquet. No bundled data or personal-path default. |


The reader uses pandas' parquet support and requires PyArrow to produce a forecast;
the pinned engine is included in `requirements.txt`. Without an engine, or with
missing/unreadable/stale/insufficient data, the range is omitted
and one plain line gives the reason. Nothing installs software automatically and
no previous estimate, median or zero is substituted. Reusing a data block that
has no HAR estimate also produces an omission, not a fresh estimate.
Freshness always requires zero missing eligible sessions between the last usable
session and the target. There is no stale-session allowance; the former
`MARKET_BRIEF_HAR_MAX_STALE_SESSIONS` variable has no effect.

Input columns: timezone-aware `ts_event` (start-stamped, ordered unique whole
minutes), `open`, `high`, `low`, `close`, `symbol`, `source`, `adj_offset`.
Prices must be finite positive NQ index points and carry additive adjustment
provenance. Outright NQ contract symbols or `NQ=F` are accepted. A session with
an internal contract/source/offset seam is excluded; `vendor` and `state` origins
for the same zero-offset `NQ=F` instrument are equivalent.

Session D runs from D-1 18:00 to D 17:00 America/New_York. Every bar at or after
D's opening cutoff is excluded before price or identity validation. Historical
sessions need at least 95% of aligned five-minute closes and valid OHLC; weekends,
holidays and conservative short-session dates (July 3, December 24, Friday after
Thanksgiving) are excluded. This coverage threshold can miss session extrema;
it is not a guarantee that every minute is present. Lags bridge excluded sessions.
Vendor continuous-contract identity remains limited by the supplied provenance.

The fit uses exactly 400 prior observations after a 22-session lag warm-up:
log previous range, log arithmetic mean of five prior ranges, and log arithmetic
mean of 22 prior ranges. The point uses the log-normal correction (residual
SSE/396), without clipping to historical extrema. The 10–90 bounds use mu plus
empirical training log-residual quantiles, not independent calibration data.
The result includes an `extrapolated` flag when the corrected point falls outside
the observed training-target range; the brief appends `— outside training range`.
The result also includes as-of/target dates, fit size, and an optional percentile
against causal forecasts in the prior 365 days (at least 100 needed). The brief
does not display that percentile or imply a directional probability.

Gather retains the structured estimate in its data block. Compose inserts its
plain, nearest-five-point line deterministically in Macro pulse, within the
unchanged character ceiling, before validation. Example shape (not market data):
`Volatility (HAR, NQ): ~800 pts expected range (10–90: 450–1200)`.
Point and band values remain unrounded until this single half-up rendering step.
No writer-selected figures, confidence claims or hit rates are added.

With the flag off, gathering does not read the parquet or add HAR data, and the
gather renderer is unchanged. Compose deliberately strips matching HAR lines
from retained inputs and writer output even when disabled. Writer bytes are
preserved when no such line needs removal; unconditional byte-identical output
is not promised for retained HAR content.

The initial audit found defects; these repairs are **not audited**. The feature
remains off pending fresh-session review of the repair.
Adding it does not change any schedule or delivery configuration.

### Check the pipeline

```bash
.venv/Scripts/python.exe run_brief.py --dry --ignore-window --force-weekend
```

`--dry` gathers, composes and validates, then prints the messages it *would*
post. Nothing reaches Discord. Exit code 0 means the whole chain works.

## Running it

```bash
.venv/Scripts/python.exe run_brief.py                          # production: gather, compose, validate, post
.venv/Scripts/python.exe run_brief.py --test --ignore-window   # same, but to the test channel
.venv/Scripts/python.exe run_brief.py --dry --ignore-window    # no posting at all
.venv/Scripts/python.exe run_brief.py --deliver-only           # re-post today's existing brief
```

Useful flags: `--skip-gather` to reuse the existing data block, `--deliver-only` to
validate and post an existing brief, `--force` to bypass the delivery ledger,
`--ignore-window` outside 07:30–08:30 Chicago, `--force-weekend`,
`--force-holiday`. `--date` selects which session's files are used, but live
gathering only ever runs for today's ET date: asking for a past date without
`--skip-gather` or `--deliver-only` is refused with exit 9, because gathering a
past session is not supported and silently substituting today's data would be
worse than refusing.

Exit codes: 0 ok, 2 skipped (weekend or holiday), 3 gather, 4 compose,
5 validation, 6 outside window, 7 delivery, 8 spend estimate reached, 9 a
requested date live gathering cannot honour.

A delivery that reaches some destinations but not all exits 7. The ledger records
each destination separately — and, since 2026-11-20, each acknowledged message
within a destination — so the next run re-sends only what is still missing rather
than duplicating what already arrived.

Weekends and NYSE holidays exit 2 without posting. Market holidays come from
`market_holidays.yaml` (closures for 2026 and 2027). If that file is missing the
run warns and continues rather than refusing to start, so confirm it is present
after any environment change.

`run_brief.py` also refuses to run once the month's estimated model spend would
pass `[limits] monthly_usd_cap`; raise it deliberately, not by accident.

## Scheduling

`scheduled/run-brief.ps1` is a Windows Task Scheduler launcher that runs the
pipeline with the project's own interpreter and returns its exit code unchanged,
so a failed run shows up as failed. On other platforms the same thing is a cron
entry calling `run_brief.py`. The production schedule is weekdays at 07:35
Chicago, inside the 07:30–08:30 window.

## Where things live

### Run provenance

The **block ID**, stored as `run_id`, is a UUID4 (32 lowercase hexadecimal
characters) created before gathering. It identifies the data, not an execution.
The same ID is stored in `data/<date>.json`, printed in `<date>.data.md`,
logged on `run start` and `compose ok`, and appended after the
brief's disclaimer as one Discord subtext line: `-# Run: <run_id>`.
The parent adds this line deterministically; the writer's budget reserves its
space. It remains reader-visible, counts against `brief.max_chars` (still 5,200),
and survives validation, quarantine and Discord message splitting.

Compose reads a retained snapshot of its input in its attempt directory.
Validation and delivery use the same reader-facing text transformation as
Discord chunking: well-formed HTML comments are removed; unmatched, nested or
unsupported comment constructs are rejected. Exactly one visible Run line must
end the disclaimer footer and match the JSON block. The entire visible footer
counts against the character ceiling. Delivery unconditionally rechecks the
identity immediately before sending, so a missing trace or different same-minute block
cannot silently pass on the freshness stamp alone. Superseded blocks live at
`data/runs/<date>-<run_id>.json` and `.data.md`. Existing HHMM-keyed legacy
archives remain readable and untouched; collision suffixes still preserve every
candidate rather than overwrite an archive. The latest block remains at the
date-only paths.

`--skip-gather` and `--deliver-only` reuse the existing block's ID, not a new one.
A legacy block without an ID can be recomposed with `--skip-gather`: its original
JSON/Markdown pair is retained under the legacy HHMM key before an ID is added
and the Markdown is regenerated from the same data. This does not establish
provenance for any old brief. `--deliver-only` refuses an untraced legacy brief
(exit 5); recompose it instead. Missing or mismatched identities fail closed.

The **execution ID**, `execution_id`, is a separate UUID4 generated fresh for
every invocation, including `--skip-gather` and `--deliver-only`. It accompanies
the runner's run-start, stage, validation, delivery and failure-alert log entries,
so retries using the same block remain distinguishable. Compose subprocess output
retains its separate attempt-directory identity. The execution ID never goes on
the brief and never relabels an existing block or brief.

### Storage

- Repository: code, tests, `playbook.md`, `market_holidays.yaml`,
  `t1_calendar.yaml`, the `briefs/` archive.
- `<runtime>/data/` — gathered data blocks, plus superseded runs.
- `<runtime>/logs/` — one log per date.
- `<runtime>/state/` — delivery ledger, spend log, prompts, claims.
- `<runtime>/config/` — `.env` and `settings.toml`.

Nothing resolves paths from the working directory. Credentials are never stored
in the repository; `.env.example` names the variables and nothing more.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

Covers the gamma flip, the GEX guards, the roll guard, the stale-block check,
delivery ledger and redaction, error visibility, and the compose environment.
Three of the older test files are script-style and also run directly
(`python tests/test_x.py`).
