# market-brief — how it works today

Written 2026-07-26, before any rewrite. **This is the contract.** When the data-fetching half
moves into Discount Bloomberg, every source chain below must still work in the same order and
produce the same fields. Anything that changes here is a regression, not an improvement.

Verified by a full `gather.py` run from the new CMTMS location on 2026-07-26.

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
`weekdays_behind`, `stale`.

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

## 4. Volatility

`_cnbc_vol()` → `_yahoo_vol()`. `^VIX` (30-day) and `^VIX9D` (9-day) — both needed, because the
*relationship between them* is the term-structure read.

## 5. Index futures and oil

yfinance: `ES=F`, `YM=F`, `NQ=F`, `RTY=F`, and `CL=F` (WTI crude).

## 6. Sector rotation

The 11 SPDR sector ETFs, ranked by performance **relative to SPY** — relative is the point, it
separates rotation from the market simply moving. Plus `SMH` (semis), `IGV` (software) and
`ARKK` (growth/speculative appetite) for within-tech detail and a risk-appetite gauge.

## 7. Dealer gamma

Tradier option chains for `SPY` and `QQQ`. Two reads: the **nearest 4 expirations** for the
intraday regime, and the **full chain** for the structural flip level. Zero-gamma level, call
wall and put wall are computed locally from the chain — not fetched from anyone.

## 8. Event gate

`t1_calendar.yaml`, 21-day horizon.

## 9. News

Finnhub. Filtered to a trusted-source list (Reuters, Bloomberg, CNBC, WSJ, FT, AP, …) and
tagged against a theme keyword list, because a pure macro search misses sector catalysts.

---

## Outputs — the other half of the contract

1. `data/{date}.json` — every number, machine-readable
2. `data/{date}.data.md` — the same thing as a human-readable block
3. `briefs/{date}.md` — the narrative, written by an LLM from the above per `playbook.md`,
   including a web-search pass for the *why*
4. `post_discord.py` fans it out

The expensive LLM synthesis happens **once per day, here.** Anything downstream consumes the
result rather than redoing it.

---

## Keys it needs

`FRED_API_KEY`, `TRADIER_API_KEY`, `FINNHUB_API_KEY` — currently its own `.env`. After the
rewrite these should live in Discount Bloomberg only.
