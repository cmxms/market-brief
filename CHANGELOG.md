# Changelog

Notable changes to market-brief, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/). This project does not publish
version numbers; entries are dated.

## 2026-11-17

### Fixed

- **A market holiday could make fresh Treasury data read as STALE.** Staleness counted weekdays, so
  the morning after a holiday the previous session's closing file looked a day old. It now counts
  **market sessions**, excluding weekends and the ten US market holidays (including Good Friday, and
  observed-date shifts when a holiday lands on a weekend). A false alarm on the data-quality flag is
  worse than no flag: it teaches the reader to ignore the one warning that matters.
- **A degraded options chain said something had failed without saying what.** The warning now names
  the affected view (`near_term` or `full_chain`) and the expiries whose fetch failed, so a clean
  view can no longer read as a symbol-wide problem.
- **Two headlines were fetched and never printed.** `finnhub_news` kept 14 general headlines and the
  renderer printed 12. All collected headlines now reach the brief; a cap, if ever needed, must say
  how many were withheld.
- **The source cross-check validated a re-download instead of the published number.** A feed could be
  correct at 06:01 and wrong at 06:00 and still pass. The check now compares the samples the brief
  actually used; a re-download happens only with nothing to compare, and says so.

### Removed

- **`BACKLOG.md` retired: 170 lines down to 26.** Every remaining entry was fixed, moot, or too
  small to keep tracking, and two sections already had a better owner — the ISM / Fed-speaker
  sweep is a run-time procedure documented in `playbook.md` step 2, and the calendar entries are
  in `t1_calendar.yaml`. Open work for this project now goes on the workspace list
  (`CMXMS/WORKLIST.md`), which is the one list that gets read. The four still-real imperfections
  were carried over there rather than dropped; the full reasoning for every closed item stays in
  git history.

### Fixed

- Two documents described `brief-live` as a live consumer of `gather.py` ("so playbook.md and
  brief-live keep working"). That dashboard was retired with CMTMS and no longer exists; the
  statements now say what is actually true and record that it is gone.

### Added

- `briefs/2026-11-17.md` — the scheduled brief, archived.

## 2026-11-16

### Changed

- **Live on a schedule.** The first scheduled run fired at 07:35 Chicago and exited 0:
  compose produced 5,189 bytes, validation passed, 22 sourced claims were recorded,
  and 4 messages went to each of the two production webhooks. Both ledger entries
  are present, so the duplicate guard is working on the real path.
- `.gitignore`: `briefs/2026-11-15.md` is excluded. It was a Sunday dry run of
  compose/validate that was generated but never posted, so it is not part of the
  archive — that folder holds posted briefs only. The file is left on disk.

## 2026-11-14

### Added

- `run_brief.py`: one entry point that runs gather, compose, validate and deliver
  as separate steps, each with its own exit code so a failure is visible to Task
  Scheduler.
- `compose.py`: narrative generation through Hermes headless, restricted to the
  `web` and `file` toolsets, so the writer has no terminal and no Discord access.
- `validate.py`: structural checks before delivery — length ceiling, canonical
  section order, freshness stamp, no links, date match.
- `deliver.py`: Discord delivery with a ledger that refuses to post the same
  brief twice. Production fans out to every configured webhook; test mode posts
  to a single channel through the bot.
- `market_holidays.yaml`: NYSE full-day closures for 2026 and 2027 plus early
  closes, taken from the exchange's published calendar.
- A claims ledger per run: each hard factual claim with the URL it came from,
  kept out of the brief itself.
- GEX coverage reporting: expirations requested and failed, contracts, open
  interest used, and the share of open interest the flip repriced.
- `AGENTS.md`: the project's operating contract for agents.
- Delivery ledger records each destination separately, keyed by a SHA-256
  fingerprint of its webhook URL. A retry after a partial failure re-sends only
  the destinations still missing.
- `post_discord.sanitize()` scrubs webhook URLs, request paths and bare
  token-shaped strings from any text before it is logged or recorded.
- `tests/test_delivery.py` and `tests/test_compose_env.py`: first tests covering
  the delivery path and the compose environment.

### Changed

- Operational files moved out of the repository. Gathered data, logs and
  credentials now live under `runtime/market-brief/`; the repository holds code,
  tests, docs and the brief archive.
- Paths resolve from the project root and the runtime root, never the working
  directory.
- The compose step is instructed to write the brief in one pass and make at most
  two edits.
- The playbook gained a two-source rule for hard specifics (counts, names, deal
  sizes) and now names `GAMMA READ UNAVAILABLE` alongside `CHAIN INCOMPLETE`.
- The compose subprocess no longer inherits the parent environment. Keys declared
  in the runtime `config/.env` — webhooks, Finnhub, FRED, Tradier — and anything
  matching `DISCORD_WEBHOOK*` are removed before launch. `config.env_keys()`
  reports which keys came from that file, so the filter cannot go stale when a new
  secret is added.

### Fixed

- A failed expiration request returned an empty chain, was indistinguishable from
  an empty one, still counted toward `n_expirations`, and never entered the
  open-interest denominator — so the drop alarm could not see it. It now marks
  the view `degraded` and withholds regime, flip and walls rather than publishing
  an understated number.
- The flip is withheld when its repriced contracts cover less than 98% of usable
  open interest.
- The stale-block check read data blocks from beside the script, so it silently
  stopped finding calendar releases once data moved to the runtime root. It now
  reads the runtime location and falls back to the legacy path.
- Spot source (`last` or `close`) and its timestamp are recorded instead of
  discarded.
- A webhook failure could write a working token to the log: only the interpolated
  URL was redacted, while `{e}` carried requests' `HTTPError` text containing the
  full token-bearing URL. Failure text is now sanitized everywhere it can reach a
  log or the ledger, and non-2xx responses raise a status code plus fingerprint
  instead of `raise_for_status()`.
- Partial delivery was recorded as complete: with two webhooks, one success and one
  failure wrote `status: ok`, exited 0, and the retry skipped the failed
  destination while `--force` duplicated the successful one. Now any outstanding
  destination returns failure (exit 7).
- `pytest` could not run at all — three script-style test files called `sys.exit()`
  at import, aborting collection with `INTERNALERROR`. Guarded behind `__main__`;
  the suite now collects and passes.

### Known gaps

- The options provider supplies no greeks timestamp; their age is reported as
  unknown rather than estimated.
- Open interest is end-of-day, so walls describe the prior session's positioning.
- The dealer side of each contract is inferred by convention, never reported.
- A failure part-way through a single destination's chunk sequence re-sends that
  whole destination on retry, so its earlier chunks can appear twice. The ledger
  tracks destinations, not individual messages.
- The sanitizer redacts any 60+ character token-shaped string, so a long
  identifier in a ledger detail can be over-redacted. Erring toward redaction is
  deliberate.
- The compose `file` tool's readable-path boundary has not been probed. The
  environment filter is verified; what the file tool can reach is not.
- Holiday coverage stops at 2027. Extend `market_holidays.yaml` before then.
