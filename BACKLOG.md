# Market Brief — Backlog

**Empty by design since 2026-11-17.** The 170-line engineering backlog that lived here was
retired: every item in it was fixed, moot, or too small to keep tracking. Two of its sections
already had a better home, and a second to-do list is how open items get lost.

- Open work for this project goes on the workspace list, `CMXMS/WORKLIST.md`, tagged
  `[me] market-brief` or `[you] market-brief`.
- Shipped work goes in `CHANGELOG.md`.
- The ISM / Fed-speaker manual sweep is a **run-time procedure**, not a task — it lives in
  `playbook.md` step 2, which is where it was always documented properly.
- The retired file, including the reasoning behind every closed item, is recoverable with
  `git log -- BACKLOG.md`.

Four small imperfections were carried to the workspace list rather than dropped, because none of
them changes a published number today and all four are real:

- `render_md` surfaces a chain warning without naming which view degraded (`near_term` vs
  `full_chain`), so a clean view can read as symbol-wide.
- `_weekdays_behind` is holiday-blind: the session after a market holiday can raise a false STALE
  on fresh Treasury data. Predicted, never observed.
- `source_crosscheck()` re-fetches CNBC and Yahoo instead of comparing the numbers the brief
  actually published, so in a fast tape it can validate a sample nobody used.
- `finnhub_news` keeps 14 general headlines; `render_md` prints 12.

Nothing else. If an entry here ever needs more than a line, it belongs on the workspace list.
