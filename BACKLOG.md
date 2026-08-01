# Market Brief — Backlog

Next-session to-do. Newest intent at top; check off / retire with a trail.

Scope note (2026-08-01): this repo does two things — **gather the numbers and write the
brief**, then **post it to Discord**. The Brief Live dashboard was split out to
`CMTMS/brief-live/`; its backlog went with it. Don't add board work here.

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
