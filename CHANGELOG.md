# Changelog

Notable changes to market-brief, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/). This project does not publish
version numbers; entries are dated.

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

### Known gaps

- The options provider supplies no greeks timestamp; their age is reported as
  unknown rather than estimated.
- Open interest is end-of-day, so walls describe the prior session's positioning.
- The dealer side of each contract is inferred by convention, never reported.
