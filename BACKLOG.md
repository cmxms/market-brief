# Market Brief — Backlog

**Empty by design since 2026-11-17.** The 170-line engineering backlog that lived here was
retired: every item in it was fixed, moot, or too small to keep tracking. Two of its sections
already had a better home, and a second to-do list is how open items get lost.

- Open work for this project goes on the workspace list, `CMXMS/WORKLIST.md`, as
  `[kind] market-brief — one line` (kinds are `idea`, `todo`, `bug`).
- Shipped work goes in `CHANGELOG.md`.
- The ISM / Fed-speaker manual sweep is a **run-time procedure**, not a task — it lives in
  `playbook.md` step 2, which is where it was always documented properly.
- The retired file, including the reasoning behind every closed item, is recoverable with
  `git log -- BACKLOG.md`.

Four imperfections were carried here on 2026-11-17 and **all four were fixed the same day**: the
staleness check now counts market sessions (holidays included), a degraded chain names the view and
the failing expiries, the source cross-check validates the published samples rather than a re-fetch,
and all 14 collected headlines are printed. See `CHANGELOG.md`.

Nothing else. If an entry here ever needs more than a line, it belongs on the workspace list.
