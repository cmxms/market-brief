# market-brief — data sources and behaviour contract

Every number in a brief comes from a chain of sources tried in a fixed order, and the order is part
of the product: it decides what a reader sees when a provider is down. This document is the
reference for those chains, and for the behaviours that were measured into place afterwards.

**Read it as a contract.** If a chain below changes order or loses a field, that is a regression
until someone decides otherwise on purpose. Each section names the function that implements it, so
this document and the code can be read side by side.

---

## The idea behind the source chains

Nearly every number has **more than one source, tried in a fixed order**, because a single
provider going down at 8am shouldn't blank the brief. The order is not arbitrary — the most
authoritative source is first, and the fallbacks are progressively less exact. Every value
carries the source it came from and how old it is, so a fallback is visible rather than silent.

**Keep the order. Keep the labels. Keep the staleness flags.**

---

## 1. Treasury yields — end of day

`treasury_yields()` · tenors 2Y, 5Y, 10Y, 30Y

| Order | Source | Note |
|---|---|---|
| 1 | US Treasury Daily Par Yield Curve | The authoritative one. These *are* the constant-maturity rates FRED republishes a day later |
| 2 | FRED — DGS2/DGS5/DGS10/DGS30 | Needs `FRED_API_KEY` |
| 3 | yfinance — ^FVX, ^TNX, ^TYX | ⚠️ **no 2Y proxy exists here** — that tenor drops if it gets this far |

Tradier is deliberately **not** in this chain — it doesn't quote Treasury yield indices.

Returns per tenor: `last`, `prev`, `chg`. Plus `source`, `last_date`, `as_of`,
`sessions_behind`, `stale`.

`sessions_behind` counts **market sessions**, not weekdays: weekends and the ten US market holidays
are excluded (New Year, MLK, Washington's Birthday, Good Friday, Memorial, Juneteenth, Independence,
Labor, Thanksgiving, Christmas; Saturday holidays observed Friday, Sunday observed Monday). So a
Treasury file from Friday is fresh when read on Monday, and it is the *missing Monday* that flips it
stale on Tuesday. Before 2026-11-17 this counted weekdays, so the morning after a holiday flagged
fresh data as STALE — the one warning that must never cry wolf.

## 2. Treasury yields — live

`live_yields()`

| Order | Source | Note |
|---|---|---|
| 1 | CNBC quote webservice (US2Y/US5Y/US10Y/US30Y) | The actual cash yield |
| 2 | Yahoo v8 chart endpoint | The CBOE yield *indices* — close but not the same instrument |

## 3. Source cross-check ⚠️ don't drop this

`source_crosscheck()` compares CNBC against Yahoo each morning and records whether they agree
within a tolerance. **This is a data-quality gate, not decoration** — it's how a bad feed gets
caught before it reaches the brief. Output per key: `primary`, `fallback`, `diff`, `match`.

It compares **the samples the brief actually published**, not a fresh download. Each relay is
fetched once by the gathering pass and those values are handed to the check, so a feed that was good
at 06:01 and bad at 06:00 cannot pass on a re-fetch. Only when no sample exists at all does it
re-download, and the result is labelled "Last-resort re-fetch — not the published sample".

## 4. Volatility

`_cnbc_vol()` → `_yahoo_vol()`. `^VIX` (30-day) and `^VIX9D` (9-day) — both needed, because the
*relationship between them* is the term-structure read.

⚠️ **The two legs are usually NOT from the same moment** (updated 2026-08-02). VIX publishes
through the pre-open; **VIX9D only recomputes in RTH**, so pre-open its `last` is the prior
session's close — true on **17 of 20** July runs. Subtracting them raw compares two different
days and it flips signs, not just magnitudes: on 2026-07-30 the raw spread read
**BACKWARDATION / "acute near-term fear"** (+1.47); aligned at the 7/29 close it is
**CONTANGO** (−0.28).

`_vol_term_structure()` therefore reports the spread **at the last close both legs shared** —
VIX's close for that session is recovered as `last - chg`, and VIX9D's `last` already *is* that
close — and carries the live VIX move separately. Fields: `spread_9d_minus_30d` (always the
safe-to-quote number), `state`, `as_of`, `aligned`, `mixed_dates`, plus
`spread_raw_unaligned` kept **only** so the block can say don't quote it. When both legs share
a date the behaviour is unchanged (verified byte-identical on the 3 such days).

## 5. Index futures and oil

yfinance: `ES=F`, `YM=F`, `NQ=F`, `RTY=F`, and `CL=F` (WTI crude).

`pull_intraday_nq()` also returns the overnight gap. Two things it must keep doing (fixed
2026-08-02): pull **`period="5d"`**, because a 2-calendar-day window collapses to a single
session over a weekend and then there is no prior close at all; and find the prior close by
**walking back** to the most recent earlier date that has bars at/before the 16:00 ET settle,
never `dates[-2]`. yfinance labels futures bars by calendar date (00:09–23:58 ET), so a Sunday
holds only the 18:00 evening open — `dates[-2]` on a Monday IS Sunday, a `<=16:00` filter there
matches nothing, and the gap came back `None` on **every Monday** in the archive (3 of 3, vs
14 of 14 other weekdays fine). Walking back lands on Friday's settle and skips holidays too.
`prior_close_date` now says which session the gap is measured against.

## 6. Sector rotation

The 11 SPDR sector ETFs, ranked by performance **relative to SPY** — relative is the point, it
separates rotation from the market simply moving. Plus `SMH` (semis), `IGV` (software) and
`ARKK` (growth/speculative appetite) for within-tech detail and a risk-appetite gauge.

## 7. Dealer gamma

Tradier option chains for `SPY` and `QQQ`. Two reads: the **nearest 4 expirations** for the
intraday regime, and the **full chain** for the structural flip level. Zero-gamma level, call
wall and put wall are computed locally from the chain — not fetched from anyone.

**Incomplete-chain guard** (added 2026-08-02). A contract with no gamma or no open interest
can't be used and is skipped. That's normally harmless, but if Tradier's greeks degrade, GEX is
computed from a partial chain, comes out **understated**, and every field still looks
plausible — and a shrinking GEX is exactly what a genuine regime flip looks like. Demonstrated
on a synthetic chain: dropping 30% of contracts took GEX from **+7.19bn "positive"** to
**−6.52bn "negative"** while regime, flip, walls and `n_contracts` all still read normally.

So every run records `n_dropped`, `dropped_pct`, `dropped_oi_pct` and `n_dropped_oi_unknown`
in the JSON — **always, healthy or not**, so the threshold can be retuned from real data
rather than one sample. `data_warning` is set past `GEX_DROP_WARN_PCT` (2.0), and the rendered
block prints a `🔴 CHAIN INCOMPLETE` line only when it's set.

**🔑 The alarm fires on `dropped_oi_pct` ONLY — never on the raw count** *(retuned 2026-08-06,
and this is the whole point of the guard)*. Open interest is the multiplier in the GEX formula,
so a contract with **OI == 0 contributes exactly zero GEX**; excluding it cannot understate
anything however many there are. A count cannot tell that apart from a real degradation.

The original 2% threshold was set against a single 2026-08-02 sample that happened to read 0
dropped of 13,898 SPY / 12,386 QQQ. That baseline was **not representative**, and the gate was
`max(dropped_pct, dropped_oi_pct)`, so the count could trip it alone. What actually happens:
Tradier returns **no `greeks` block at all** for newly-listed strikes, which also carry **zero
open interest**. New listings arrive in bursts, so the count spikes on arbitrary mornings.

Measured over every run with instrumentation (2026-08-03 → 08-06), **`dropped_oi_pct` was 0.0%
in 100% of samples** while `dropped_pct` ran to **5.42%**. On 2026-08-06, **every single drop**
was this shape — SPY 656 of 656, QQQ 700 of 700, all with the greeks block absent and OI == 0.
The count gate suppressed a perfectly good gamma read on **three consecutive mornings**
(08-04, 08-05, 08-06). Re-running 08-06 after the retune returned **byte-identical** GEX,
flip and walls with no warning — proof the drops never touched the number.

⚠️ **`n_dropped_oi_unknown` is the case the OI measure cannot see.** If open interest itself is
missing, the loss is *unbounded*, not zero — so it alarms on sight. The old code did
`float(oi or 0)`, which silently booked an unknown as harmless; it now counts separately. This
has never fired on real data (0 occurrences in every sample), but it is the failure mode that
would make `dropped_oi_pct` lie.

Guard behaviour is pinned by `tests/test_gex_guard.py` (5 cases, no pytest needed): zero-OI
drops stay silent, greeks lost on contracts holding real OI fire, absent OI fires, a clean
chain stays silent, and zero-OI drops are asserted to leave GEX/flip/walls bit-identical.

⚠️ **This is a note to the brief WRITER, never brief copy** — see playbook.md's "never publish
process notes."

**A second rendering of the block taught the same lesson twice** (a graphic card existed from
2026-08-05 to 2026-08-17; it is gone, the lessons are not). Its number strip read
`gex.SPY.near_term.regime` unconditionally and printed `SPY GAMMA: POSITIVE` on 2026-08-05 while the
brief — correctly — said the gamma read was unavailable. **The graphic contradicted the text it was
rendered from.** It was fixed to print `N/A` whenever `data_warning` is set, which is the standing
rule for anything that renders this block a second time: never assert a regime the brief withheld.

🔑 **But the trigger underneath it was wrong, and the card fix hid that.** The 2026-08-05
"first real trigger" (420 of 14,018 SPY, 352 of 12,566 QQQ, `dropped_oi_pct` **0.0%** on both)
was never a degraded chain — it was newly-listed zero-OI strikes, and the count alarm fired
while the alarm that tracks missing GEX did not. Making the graphic agree with the brief made
both of them consistently wrong, and because it now looked right, the false positive
read as handled for three more mornings. **Retuned 2026-08-06** — see the guard section above.
Worth remembering as a shape: *reconciling two outputs is not the same as validating either.*

## 8. Event gate

`t1_calendar.yaml`, 21-day horizon. **Two tiers, deliberately not merged** (added 2026-07-30):

| Key | What it is | Drives |
|---|---|---|
| `events` | a prop firm's restricted-trading list — FOMC, FOMC Minutes, Employment Situation, CPI | `today_is_t1` |
| `tier2` | Market-moving but NOT T1 — PCE, GDP, PPI, retail sales, jobless claims | `today_tier2` |

**`today_is_t1` still means only what it always meant.** Tier 2 must never be folded into it
or the prop-firm gate silently changes meaning. Why the split exists: on 2026-07-30 the gate
reported no events on a morning that had GDP + PCE + claims all printing at 08:30, because
none of the three is on that restricted list. Today's tier-2 releases now render first and in red.

Dates come from the **FRED release calendar** (`fred/release/dates`, IDs in the YAML header).
🔑 This matters because **bls.gov returns 403 to automated fetch** — tested with and without a
browser User-Agent, identical refusal, so it's an IP block we can't UA our way around. FRED
republishes the BLS calendar, so it's how we read BLS dates at all.

**ISM is in neither tier** — FRED dropped it over redistribution restrictions. It stays a job
for the playbook's step-2 web sweep, along with Fed speakers.

**Fails soft, in two layers** (added 2026-08-02). This was the ONLY section that could take the
whole run down: `gather()` raised before writing anything, so one typo in the YAML cost the
entire morning — no data block, no brief, and the futures/GEX/news calls never even ran.
Measured on copies, **5 of 7 realistic typos did exactly that** (13th month, day 32, an unpadded
date, a one-space indent slip, a stray tab).

1. **Per-entry guard** — a bad row is skipped and listed in `malformed`; the rest of the
   calendar still works. An unpadded date now costs you that one event, not the calendar.
2. **Outer net** — if the file is unparseable as a whole (indent slip, tab, garbage),
   `event_gate` returns `{"error": ...}` like every other section and the brief still gets
   written without it.

🔑 **`name` is now read for EVERY entry, not just in-window ones.** It used to be read only
inside the 21-day horizon test, which made a missing `name:` a **time bomb** — harmless on the
day you edit and verify the file, then fatal every morning from the day the event drifted inside
21 days. Measured on a 2026-08-26 entry: fine on 08-04, dead from 08-05. Now reported on the
first run.

⚠️ **A blind gate must never read as a quiet calendar.** When the gate errors, the block says so
in red and explicitly tells the writer *not* to infer "no events today" — the 2026-07-30 mistake
in its worst form. Verified: `today_is_t1` is absent rather than `False`.

## 9. News

Finnhub. Filtered to a trusted-source list (Reuters, Bloomberg, CNBC, WSJ, FT, AP, …) and
tagged against a theme keyword list, because a pure macro search misses sector catalysts.

---

## Outputs — the other half of the contract

1. `data/{date}.json` — every number, machine-readable. **Always "the latest run"** — the path
   never changes, so `playbook.md` and any other reader of that path keep working.
2. `data/{date}.data.md` — the same thing as a human-readable block
3. `data/runs/{date}-{HHMM}.json` + `.data.md` — a **superseded** run, moved here automatically
   before a re-run overwrites it (added 2026-08-02). `HHMM` is the same time the brief's
   freshness stamp carries — **ET, like every timestamp this repo publishes** — so a stamped
   brief resolves to exactly one block. *(Fixed 2026-08-04: the name came off a naive LOCAL
   `as_of`, so on a non-ET machine every archived file was offset from the stamp it was
   supposed to match — 9:31 AM ET archived as `-0831`. Both clocks are now tz-aware ET.)*
   **Why this exists:** the whole point of `gather.py` is that the brief is never built on the
   model's memory — the stored block is the proof. A second run used to overwrite it silently,
   which happened on **3 of 19** July days (7/16, 7/27, 7/30) and left the published numbers
   unreconcilable with the only surviving data; `data/` is gitignored, so the original was gone.
4. `briefs/{date}.md` — the narrative, written by an LLM from the above per `playbook.md`,
   including a web-search pass for the *why*
5. `post_discord.py` fans it out

The expensive LLM synthesis happens **once per day, here.** Anything downstream consumes the
result rather than redoing it.

---

## Keys it needs

`TRADIER_API_KEY` and `FINNHUB_API_KEY` are required; `FRED_API_KEY` is optional and only backs up
the Treasury-yield chain. Where the file goes, and what each key is for, is in the setup section of
`README.md`.
