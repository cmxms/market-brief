# Market Brief — Backlog

Next-session to-do. Newest intent at top; check off / retire with a trail.

Which list to use (2026-11-14): this file is **project-level engineering detail**
for market-brief. The workspace list `CMXMS/WORKLIST.md` is the one the user
reads, and anything that needs their attention or decision goes there. Do not
let the same open item live in both.

Scope note (2026-08-01): this repo does two things — **gather the numbers and write the
brief**, then **post it to Discord**. The Brief Live dashboard was split out to
`CMTMS/brief-live/`; its backlog went with it. Don't add board work here.

## Open (2026-08-04) — the CHAIN INCOMPLETE flag needs two small fixes

Both surfaced when the flag fired for the **first time in 22 runs** — and it was a false
positive. A run stamped 9:31 AM ET (≈1 min after the open) caught Tradier's greeks
mid-repopulate; re-pulled 20 min later, 0 dropped of 13,944 SPY / 12,632 QQQ. The three prior
post-open runs (9:59 / 10:26 / 10:51) were clean, so the unsafe window is the **first few
minutes of RTH**, not "after the open" generally.

- 🟡 **`render_md` doesn't say WHICH view is degraded.** It does
  `warn = fc.get("data_warning") or nt.get("data_warning")` and prints one unlabelled line under
  the symbol. On 08-04 only `full_chain` was affected — `near_term` was 0-dropped on both
  symbols — but it reads as symbol-wide, so the gamma call got killed on a clean view. Label the
  view, or warn per-view.
- [x] 🟡 **`tldr_card.py` chain-incomplete awareness — DONE 2026-08-05.** ~~It printed
  `SPY GAMMA POSITIVE` straight from the JSON regardless.~~ It now prints `SPY GAMMA N/A` in
  muted ink when `near_term.data_warning` is set. **The bug fired for real on 08-05:** the brief
  correctly said the gamma read was unavailable and the card stamped POSITIVE on the graphic
  from the same JSON file. 🔑 **The design assumption was wrong, and it's worth stating.**
  "Card and brief read the same JSON, so they can't disagree" protects against *stale numbers*
  only — it does nothing about a **playbook rule that gags a conclusion**, because the card
  never knew the rule existed. **Audit the rest of the number strip for the same shape** before
  the card goes live: any tile reading a field the playbook can suppress has this bug.
  ⚠️ **Moot as of 2026-08-17 — the card is retired and the brief posts as text only.** The
  audit only comes back if the card does. The *lesson* doesn't retire: any second renderer of
  the same JSON re-opens this hole, because it won't know the playbook's suppression rules.
- 🟢 **Threshold evidence — now TWO independent samples, and they agree.** Warn is
  `max(drop_pct, drop_oi_pct) >= 2.0`. On 08-04 `drop_pct` was 2.34% / 4.7% with
  **`drop_oi_pct` 0.0%**. On 08-05, both runs (07:54 and 08:22, identical figures) gave
  **SPY 420/14,018 = 3.0%** and **QQQ 352/12,566 = 2.8%**, again with **`drop_oi_pct` 0.0%**
  on both symbols. Gating on `drop_oi_pct` alone would not have fired either day. Two false
  positives, zero true positives, same signature every time: dead strikes with no open
  interest, contributing exactly zero gamma. **Recommend gating on `drop_oi_pct` and keeping
  `drop_pct` as a logged-only diagnostic.**
  ⚠️ **This also REFUTES the 08-04 hypothesis written directly above.** That note blamed
  "Tradier's greeks mid-repopulate in the **first few minutes of RTH**" — 08-05 fired at
  **07:54 and 08:22, both pre-market**, nowhere near the open. Whatever this is, it isn't an
  RTH-open artifact. Cost so far: the gamma read killed on 2 of 24 runs, both times on a
  chain that was fine.

## Fixed 2026-08-04 — archive filenames were on a local clock, not ET

`gather.py` stored two timestamps per run: `as_of_et` (tz-aware ET, what the brief's
freshness stamp prints — always correct) and `as_of` (**naive local**). The `data/runs/`
archive name came off the naive one, so on this machine (UTC−1 vs ET) every archived block
was offset an hour from the stamp it's supposed to key to: the 9:31 AM ET run filed as
`2026-08-04-0831`. Silent — no published number was ever wrong, but the "a stamped brief
resolves to exactly one block" guarantee in `playbook.md` / `CURRENT-BEHAVIOUR.md` was false
on every archived run since the feature landed 2026-08-02.

Fix: `as_of` is now tz-aware ET (`_et.isoformat()`), and the mtime fallback converts to ET
before naming. Existing archive backfilled (`-0831` → `-0931`); the live block's `as_of` was
rewritten in ET so the next re-run archives correctly. Docs synced in the same pass.

⚠️ **Worth re-checking:** the naive-clock offset may account for part of the 7/16, 7/27, 7/30
stamp-vs-block gaps quoted in `playbook.md` (4 / 91 / 41 min). 91 min is bigger than the
1-hour offset, so that one likely has a real re-run gap under it too — but the 41 and 4 minute
figures were measured with the broken key and may not mean what they say.

## Code + logic audit (2026-08-02) — what's left

Full audit of `gather.py` / `post_discord.py`, every claim tested against the 20 saved
data blocks + 20 briefs. **Three fixed the same day** (re-run overwrite, Monday NQ gap,
VIX9D term structure — see `CURRENT-BEHAVIOUR.md`). These are the rest:

- [x] 🟡 **GEX counts what it used, never what it dropped — DONE 2026-08-02.**
      ~~`_gex_from_options` skips any contract missing greeks or OI with no counter.~~
      Now records `n_dropped` / `dropped_pct` / `dropped_oi_pct` every run, and sets
      `data_warning` past `GEX_DROP_WARN_PCT` (2.0). The block prints
      `🔴 CHAIN INCOMPLETE` **only** when set — **silent on a healthy chain**, per the
      user's call: counts belong in the JSON, not in front of the reader.
      Baseline measured live: **0 dropped of 13,898 SPY / 12,386 QQQ = 0.0%.**
      Why it mattered: on a synthetic chain, losing 30% of contracts moved GEX from
      **+7.19bn "positive"** to **−6.52bn "negative"** with regime, flip, walls and
      `n_contracts` all still reading normally — a data outage wearing a regime flip's
      clothes, the same shape as `brief-live`'s dead-GEX-reports-health-OK trap.
      🔎 **The counts are logged even at 0% on purpose** — the 2% threshold came from a
      single Sunday sample. After a few weeks of real runs it can be set from evidence.
- [ ] 🟡 **False STALE alarm the session after a market holiday.** `_weekdays_behind` is
      holiday-blind (its docstring says so; the consequence isn't documented). The day
      after a holiday the Treasury curve's last date is one weekday back, so `stale: True`
      fires and the block prints "⚠️ STALE — do NOT present as live" over perfectly fresh
      data. **Predicted, not observed** — no holiday falls in the 7/8–7/31 archive.
      Fix: skip dates absent from the Treasury series itself, don't count raw weekdays.
- [ ] 🟡 **The cross-check validates a different sample than the brief publishes.**
      `source_crosscheck()` re-fetches CNBC + Yahoo independently of `macro_pulse()`, so
      the numbers it compares aren't the ones in the block (plus 4 redundant HTTP calls).
      Harmless in a quiet tape; in a fast one the gate can pass on a sample nobody used.
      Fix: pass the already-fetched dicts in. **The gate itself works** — it fired on 4 of
      20 days, and on 7/8 the brief correctly hedged to "~18, up ~2".
- [x] 🟢 **`event_gate()` was the only unguarded section — DONE 2026-08-02.**
      ~~One malformed YAML entry kills the whole run.~~ It was worse than "wrap it":
      `gather()` raised before writing anything, so a single typo cost the **entire
      morning** — no data block, no brief, and the futures/GEX/news calls never ran.
      **5 of 7 realistic typos did that** (13th month, day 32, unpadded date, one-space
      indent slip, stray tab). Now two layers: a per-entry guard (bad row skipped and
      listed in `malformed`) plus an outer net (`{"error": ...}` like every other
      section). Re-tested all 7 plus a garbage file: **0 of 8 raise.**
      🔑 **`name` is now read for every entry, not just in-window ones** — it was a time
      bomb: fine the day you edit and verify the file, fatal every morning once the event
      drifted inside 21 days (measured: fine 08-04, dead 08-05). Now caught on the first run.
      ⚠️ And a blind gate now says so loudly instead of reading as a quiet calendar —
      the 2026-07-30 mistake in its worst form.
- [ ] 🟢 **`render_md` assumes `d["gex"][sym]` is either an error dict or fully formed.**
      A `{}` or partially-built gex dict raises `KeyError: 'near_term'`. **Not reachable
      from `gather()`** — `gex_tradier` always populates both symbols — so this is only a
      hazard for callers that build a partial dict by hand, i.e. `brief-live`, which
      imports these functions live. One-line guard (`or "near_term" not in v`). Found
      twice while writing audit harnesses, which is the only reason it's recorded.
- [ ] 🟢 `finnhub_news` keeps 14 general headlines, `render_md` prints 12 — two always
      fetched, never shown. Pick one number.
- [ ] 🟢 ~21 **sequential** yfinance downloads across `index_recap` + `sector_rotation`.
      Batching would cut run time materially. Cosmetic unless the run feels slow.
- [ ] 🟢 Dead locals in `pull_intraday_nq` (`hi`/`lo` are computed and never used — the
      overnight range reads from `h` directly). Left alone deliberately during the fix to
      keep that diff minimal.

## Event calendar

- [x] **Non-T1 economic calendar — BRIEF HALF DONE 2026-07-30.** ~~The brief only shows
      T1 (FOMC/CPI/jobs).~~ **Source found: the FRED release calendar**
      (`api.stlouisfed.org/fred/release/dates?release_id=<id>`, key already in `.env`).
      Release IDs: **10** CPI · **50** Employment Situation · **53** GDP · **54** PCE ·
      **46** PPI · **9** Retail Sales · **180** Jobless Claims.
      Returns scheduled FUTURE dates. Beat the alternatives on the list because it needs no
      new key, no scraping, and 🔑 **it republishes the BLS calendar — bls.gov itself 403s
      automated fetch** (tested both with and without a browser UA; it's an IP block).
      Added as a `tier2:` block in `t1_calendar.yaml` + `today_tier2` in `event_gate()`.
      Triggered by 7/30, when the T1 gate said "no events" on a GDP + PCE + claims morning.
      - [ ] **ISM + Fed speakers + second-tier prints — STILL A MANUAL SWEEP.** No
            programmatic source adopted. What was ruled out on 2026-07-30:
            - **scanline — NO economic calendar.** Its 27 tools wrap TradingView's stock
              *screener*; `earnings_radar` is company earnings only. Dead end, don't re-check.
            - **ismworld.org — SSO-walled.** The release-date calendar 302s to a login. Its
              "Manufacturing = 1st business day / Services = 3rd, 10:00 ET" rule has real
              holiday exceptions (Jan 5 2026; July Services moved for the Jul 3 observance),
              so **never hand-write ISM dates into the YAML**.
            - **traderdaddy-pro `get_economic_calendar` — evaluated, works, NOT ADOPTED**
              (user's call, 2026-07-30). For the record if it's ever reconsidered: it's
              ForexFactory-backed and does carry ISM + speakers + UoM + CB confidence with
              impact levels, but it's **current-week only** (an explicit August range AND
              `week='next'` both returned 0) and **`actual` is always null** even for prints
              that already landed, with forecasts that drift (had Advance GDP 2.1% vs a 1.5%
              print). It could never have replaced FRED, only supplemented it.
            So Step 2's dated web search stays the mechanism. It has worked — it's what
            caught GDP + PCE + claims on 7/30 — but it depends on the sweep being run.
      - [ ] **Quarterly re-verify is now a diff, not a hunt** — next pass due **~2026-10-30**
            (noted in the YAML header). Pull the 7 release IDs, diff against the file, promote
            whatever the Fed has newly scheduled, extend both tiers into 2027. Consider the
            deferred `--verify-calendar` flag on `gather.py` to automate the diff — hold until
            tier 2 has earned its place over a few briefs.

## Notes
- `gather.py` never posts anywhere — Discord is a separate script (`post_discord.py`).
  Running gather is always safe.
- `CMTMS/brief-live/` imports this repo's `gather.py` live (not a copy) and borrows this
  repo's `.venv`. A breaking change to `gather.py`'s function signatures breaks that board.
