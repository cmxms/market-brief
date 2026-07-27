# Market Brief + Dashboard — Backlog

Next-session to-do. Newest intent at top; check off / retire with a trail.

## Dashboard remodel (light 3-col board, built 2026-07-17)

The layout spike landed and the user likes it (old dark board was "dreary"). The
3-column light board is live in `dashboard/static/`; old board backed up in
`dashboard/_backup_darkboard/`. Follow-ups, in priority order:

- [ ] **Fear & Greed fetcher.** Center panel has a stub. Wire a real source for the
      CNN Fear & Greed index (or an equivalent breadth/sentiment gauge). Add as a
      slow-cadence poller in `dashboard/server.py` (it moves once a day). Candidate:
      CNN's undocumented JSON endpoint (`production.dataviz.cnn.io/index/fearandgreed/...`)
      — verify it's still live and stable before depending on it.

- [ ] **Non-T1 economic calendar — SHARED gap (brief + dashboard).** Both the brief
      and the dashboard's Economic Calendar panel only show T1 (FOMC/CPI/jobs). The
      "important" non-T1 prints (PPI, retail sales, PCE, jobless claims, ISM) and
      scheduled Fed speakers slip through. Today (7/17) that gap hid the 10:00 UMich
      print, which mattered. **Need a reliable programmatic source** so it's caught by
      DATA, not a manual web sweep each morning. Evaluate: Trading Economics calendar,
      Econoday, FMP economic-calendar API, or the Fed/BLS release schedules directly.
      Once sourced, feed BOTH the brief's Step-2 sweep and the dashboard panel from it.
      Cross-ref: `playbook.md` Step 2 currently patches this with a manual dated search.

- [ ] **dataviz pass on the light board.** The new light theme reused the dark board's
      direction colors darkened by eye — NOT validated. Run the `dataviz` skill's
      method: validate green-up/red-down for CVD legibility on the light surface, check
      WCAG contrast, lock a proper palette. Only worth doing once the layout is settled.
      (The old dark board WAS validated — see the retired comments in `_backup_darkboard/style.css`.)

- [ ] **Portfolio + Recent Trades panels** (right column) are stubs. Needs a source
      decision first: flow-trader paper book (`chads-shit/flow-trader/data/`) vs a live
      broker. Then wire balance / day P&L / 30d perf + recent fills + open positions.

- [ ] **AI-analysis panel is fed by the MORNING brief, not live analysis.** (Surfaced
      2026-07-19.) `read_brief()` parses the newest `briefs/{date}.md` for title + stamp
      + TL;DR. Consequence: **running `gather.py` does NOT update that panel** — it only
      writes `data/{date}.json` + `.data.md`; the brief markdown has to be written
      (in-session) for the panel to change. The server docstring already concedes the
      panel "ultimately wants" continuously-updated intraday analysis rather than a
      once-a-day brief. Decide: either (a) relabel the panel honestly as "This morning's
      brief" with its own date, or (b) build a real intraday analysis generator. Until
      then the panel can silently show a stale read — on 7/19 it displayed Friday's brief
      until a Sunday one was written.

## Notes
- Dashboard server must be RESTARTED (not just refreshed) to pick up `server.py`
  changes; static file edits serve fresh on refresh.
- The `brief` poller caches for **300s**, so a newly written brief takes up to ~5 minutes
  to appear on the board (verified 7/19). Market/GEX/rotation panels are live pollers
  calling `gather` functions directly and refresh on their own cadence.
- `gather.py` never posts anywhere — Discord is a separate script (`post_discord.py`).
  Running gather is always safe.
