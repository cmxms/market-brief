# Market Brief — Playbook (for Claude)

When the user says **"market brief"**, **"morning brief"**, **"daily brief"**, or
**"community brief"**, follow these steps exactly. The brief is **general situational
awareness** for traders — what happened, what's rotating, where the gamma/vol regime
sits, and what's on the event calendar. It is **not** personal and gives **no** buy/sell
calls: it informs the reader's own regime call.

## Steps

1. **Run the gatherer** (self-contained; uses this folder's own venv):
   ```
   .venv/Scripts/python.exe gather.py        # Windows
   .venv/bin/python gather.py                # macOS/Linux
   ```
   It prints + writes `data/{date}.json` and `data/{date}.data.md` — the hard numbers
   (live macro, futures, Tradier GEX, sector rotation, event gate, Finnhub headlines).

2. **Themed web pass.** Read the gather block, then run web searches to explain the
   *why* and surface catalysts the numbers can't. Always cover:
   - the dominant fresh headlines from Finnhub (verify + add depth),
   - the live themes: **AI / NVDA / semis, the Fed/CPI, oil/geopolitics**,
   - **TODAY's scheduled econ calendar — what the data block does NOT cover.** As of
     2026-07-30 the event gate has **two tiers**: T1 (FOMC/minutes/CPI/jobs) and **tier 2**
     (PCE, GDP, PPI, retail sales, jobless claims), and today's tier-2 releases print at the
     top of the block in red. **Read that section before searching** — it is now the
     authority for those five.
     **Still NOT in the data, and still a manual web sweep every run:**
       - **ISM** — FRED doesn't carry it (redistribution limits) and ismworld.org's release
         calendar is behind an SSO login. Its "Manufacturing = 1st business day / Services =
         3rd, 10:00 ET" rule has real holiday exceptions (Jan 5 2026; July Services moved for
         the Jul 3 observance), so **don't hand-write ISM dates into the YAML** — search for
         it on the day.
       - **Scheduled Fed-speaker / Chair appearances and testimony** — and check whether a
         **blackout** is in force, because then there are none (7/30 was inside one).
       - **One-off and second-tier prints** — UMich sentiment, CB consumer confidence,
         Chicago PMI, ECI, durable goods, Treasury refunding. The 7/17 brief lost the 10:00
         UMich print exactly here.
     Every run, do a dated search like `"US economic calendar {today}"` + `"Fed speakers
     {today}"` (TradingView's econ calendar is the lean source) and fold hits into 📰 Last 24h
     and 🚨 Event gate. **`today_is_t1: False` means "not a prop-firm restricted day" — it has
     never meant "nothing happens today."**
   - the next T1 event's setup.
   Macro-only searches miss sector catalysts — search the themes explicitly.
   - **MONDAYS / post-holiday opens: dig harder on headlines.** A lot moves over a
     weekend that the Finnhub feed under-captures — do extra targeted, date-bounded
     searches; don't lean on the prior framework.

3. **Write the brief** to the 6-section template below.

4. **Output:** print the brief copy-paste-ready in the CLI, save it to
   `briefs/{YYYY-MM-DD}.md`, then post it to Discord:
   ```
   .venv/Scripts/python.exe post_discord.py briefs/{YYYY-MM-DD}.md
   ```
   (fans out to every webhook in `DISCORD_WEBHOOK_URL`; use `--dry` to preview the split).

## Hard rules (non-negotiable)

- **Never invent a number.** Every price/level/yield comes from the gather block or a
  cited source. If you didn't fetch it, don't state it.
- **Flag stale data.** The data block prints each yield's MODE (LIVE/EOD) + SOURCE +
  as-of + a ⚠️ STALE flag. Quote the LIVE number when present; if it shows EOD/STALE,
  say so — never present a stale yield as live.
- **VIX term structure — quote `spread_9d_minus_30d` and `state` from the block, and
  never subtract the two VIX numbers yourself.** VIX publishes through the pre-open but
  **VIX9D only recomputes in RTH**, so pre-open the two legs are usually from *different
  sessions* (17 of 20 July runs). The block now detects that and reports the spread at
  the last close both legs shared — a real same-moment reading — and prints the raw
  live-minus-stale figure only to tell you not to use it. If you see `⚠️ legs are from
  DIFFERENT sessions`, say the term structure is **as of that prior close** and quote
  today's VIX move separately. *(This is not hypothetical: on 2026-07-30 the naive
  subtraction read BACKWARDATION / "acute near-term fear"; aligned, it was CONTANGO.)*
- **Source cross-check (REQUIRED gate):** the block has a "Source cross-check (CNBC vs
  Yahoo)" section. ✓ all match = confirmed. ⚠️ MISMATCH = **do NOT publish that number**
  until you've verified which source is right. "Only one source" = degraded but usable.
- **Hold two-sided tension.** If there's a bullish counterweight to a bearish tape (or
  vice-versa), show both. Don't write a one-note story.
- **LENGTH CEILING — hard: ~800 words / ~5,000 chars.** Reference briefs: `2026-07-13`
  (4.3k) and `2026-07-14` (5.1k) are the house style — go re-read one before writing.
  Anchor on THOSE, never on yesterday's brief; anchoring on yesterday ratchets (7/13 4.3k
  → 7/17 9.5k, 2.2x, purely by "just a little more" each day). Over ceiling = cut, don't
  justify.
- **NEVER publish process notes.** Source-verification work (cross-check mismatches, which
  feed was stale, unconfirmed calendar entries), data-integrity caveats, and methodology
  belong in the CLI summary to the user — **not in the brief.** Readers want the number,
  not the audit trail. Exception: if a number is stale AND you're publishing it anyway,
  one short clause — "10Y 4.52% (CNBC live)" — never a paragraph.
- **Don't over-precision a pre-market number.** GEX/flip/walls come from *prior-session*
  greeks and move at the open. Round them (`~750.7`, not `750.78`). Cent-level precision
  on a stale-greek number is false confidence.
- **`🔴 EVENT CALENDAR UNAVAILABLE` = the gate is BLIND, not quiet.** The calendar file
  failed to parse, so the block has NO event information — which is not the same as
  there being no events. **Never write "no events today" off a blind gate.** Build the
  🚨 Event gate section entirely from the step-2 web sweep, and tell the user in the CLI
  so the file gets fixed. `⚠️ N calendar entries skipped as malformed` is the milder
  version: the calendar below it is usable, just treat it as possibly missing one event.
- **`🔴 CHAIN INCOMPLETE` in the GEX section = do NOT write a regime call.** It means part
  of the option chain came back without greeks or open interest, so GEX is understated —
  and an understated GEX *looks exactly like* a shift to negative gamma. Write the 🧭 Regime
  read from vol and price action instead, say the gamma read is unavailable today in one
  short clause, and **tell the user in the CLI, not in the brief** (this is a process note).
  It stays silent on a healthy chain, so if you see it, it's real.

## Canonical headers — COPY VERBATIM (do not improvise emoji or wording)

The title line, every section's emoji+title, and their ORDER are FIXED. Only the content
under each changes day to day.

**Title line:** `# 📊 Market Brief — {Wkd} {Month} {D}, {YYYY}`
- `{Wkd}` = 3-letter weekday; `{Month}` = full name; `{D}` = no leading zero; include the year.

**Freshness stamp (REQUIRED, right under the title):** an italic line with the ET
data-pull time from the gather block's `as_of_et` (e.g. `*Data pulled Wed Jul 8, 2026 8:42 AM ET*`).
Use the gatherer's stamp verbatim — exact wording `*Data pulled {as_of_et}*`, no variants.

🔑 **The stamp is the brief's only link back to the numbers it was built from, so it must
match the block you actually wrote from.** If you re-run `gather.py`, you are now writing
from a NEW block: re-read it and update the stamp, or don't re-run. The gatherer archives
the superseded block to `data/runs/{date}-{HHMM}.json` and prints a NOTE when it does —
that HHMM is the same one the stamp shows, so a stamped brief always resolves to exactly
one data file. *(On 7/16, 7/27 and 7/30 the stamp and the stored block disagreed by 4, 91
and 41 minutes; 7/27's published NQ change was 1.39pp off the stored numbers as a result.)*

**Then a short `**TL;DR:**` bold lead line** (community readers skim).

**Section headers, in this exact order:**
| # | Header |
|---|--------|
| 1 | `## 📰 Last 24h — what happened & why` |
| 2 | `## 📊 Breadth / divergence` |
| 3 | `## 🔁 Sector rotation (rel to SPY)` |
| 4 | `## 📈 Macro pulse (moves, not levels)` |
| 5 | `## 🚨 Event gate` |
| 6 | `## 🧭 Regime read (generalized)` |

Footer: a `---` rule, then `*Not financial advice — situational awareness only.*`

Rules: one emoji per section as above (no substitutes). Don't append ad-hoc suffixes to
a header — put emphasis in the body.

## Section guide

1. **Last 24h — what happened & WHY** — the catalysts; builds intuition.
2. **Breadth / divergence** — ES/YM/NQ/RTY moves; aligned (trend risk) vs narrow (fragile).
3. **Sector rotation** — from `sector_rotation` (SPDR + theme ETFs ranked REL to SPY;
   5d primary, 1d secondary). Name leaders (rotating IN) and laggards (rotating OUT), call
   out the within-tech tilt (semis/software/growth via SMH/IGV/ARKK), and flag when it
   **contradicts the index** (e.g. defensive rotation under a green tape).
   **DETAIL + PLAIN ENGLISH:** don't just dump the grid. After it, spell out (a) WHAT each
   side is — translate tickers ("XLP = consumer staples / defensive", "SMH = semis"),
   (b) the STORY — offense→defense = caution, (c) the SO-WHAT. Assume the reader doesn't
   know what "rel to SPY" or a sector ETF is. Put the ETF grid in a ``` code block.
4. **Macro pulse** — yields / VIX / VIX9D term structure / oil, as moves not levels.
5. **🚨 Event gate** — T1 today + this week, PLUS today's non-T1 scheduled data (PPI,
   retail sales, PCE, claims, ISM) and any Fed-speaker/Chair appearances from the Step-2
   calendar sweep; bullish/bearish wildcards; what to watch. The T1 gate in the data block
   is not the whole calendar — a soft PPI or a hawkish Fed-Chair testimony belongs here.
6. **Regime read (generalized)** — gamma + vol → what it means for traders (range vs
   trend, where fades/breakouts get punished). Two GEX views are in the data block:
   **near-term** (nearest 4 expirations — the intraday regime, walls, flip) and
   **full-chain** (every expiration — the structural zero-gamma flip). Lead with the
   near-term regime + flip + walls; cite the full-chain flip when it differs meaningfully.
   **"Meaningfully" means it changes the read** — if the two flips agree (e.g. 750.78 vs
   750.85), say "structural agrees" in three words or omit it. Don't print both in full.
   Prose or 2-3 bullets, rounded numbers, NO code block (see FORMAT).
   Generalize — describe the condition, don't tell anyone what to do.

## FORMAT FOR DISCORD (important — Discord rendering differs from a plain .md)
- **No markdown tables** — Discord doesn't render them. Put aligned data in a ```
  **code block** so columns line up. **Exactly two code blocks per brief: the ES/YM/NQ/RTY
  breadth grid and the rotation grid.** That's it.
- **The regime read gets NO code block.** Weave the 2-3 numbers that matter (flip, call
  wall, put wall) into prose or 2-3 tight bullets — see `2026-07-14`, which handled the
  same knife-edge-on-the-flip setup in two bullets. Don't dump the walls list, don't dump
  near-term AND full-chain side by side.
- `#`/`##` headers and ``` code blocks DO render natively — use them.
- Keep it tight; a non-Nitro message caps at **2,000 chars** (`post_discord.py` splits on
  `## ` boundaries automatically, but tighter sections read better).
- Always end with the one-line disclaimer.

## Notes
- Gatherer is fully self-contained: live yfinance + Tradier chain + Finnhub + the bundled
  `t1_calendar.yaml`. No local database, no sibling-project imports.
- GEX uses Tradier-supplied greeks; pre-market it reflects the prior session's greeks
  (options don't trade overnight) — label it as such if quoting pre-open.
- Finnhub free tier: news lag ~20-60min — fine for a daily look-back, not intraday.
- Refresh `t1_calendar.yaml` quarterly from the Fed + BLS calendars.
