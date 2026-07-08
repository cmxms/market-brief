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
- **VIX term structure (contango vs backwardation) is computed from the LIVE values** —
  quote it from the data block, don't recompute.
- **Source cross-check (REQUIRED gate):** the block has a "Source cross-check (CNBC vs
  Yahoo)" section. ✓ all match = confirmed. ⚠️ MISMATCH = **do NOT publish that number**
  until you've verified which source is right. "Only one source" = degraded but usable.
- **Hold two-sided tension.** If there's a bullish counterweight to a bearish tape (or
  vice-versa), show both. Don't write a one-note story.

## Canonical headers — COPY VERBATIM (do not improvise emoji or wording)

The title line, every section's emoji+title, and their ORDER are FIXED. Only the content
under each changes day to day.

**Title line:** `# 📊 Market Brief — {Wkd} {Month} {D}, {YYYY}`
- `{Wkd}` = 3-letter weekday; `{Month}` = full name; `{D}` = no leading zero; include the year.

**Freshness stamp (REQUIRED, right under the title):** an italic line with the ET
data-pull time from the gather block's `as_of_et` (e.g. `*Data pulled Wed Jul 8, 2026 8:42 AM ET*`).
Use the gatherer's stamp verbatim.

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
5. **🚨 Event gate** — T1 today + this week; bullish/bearish wildcards; what to watch.
6. **Regime read (generalized)** — gamma + vol → what it means for traders (range vs
   trend, where fades/breakouts get punished). Two GEX views are in the data block:
   **near-term** (nearest 4 expirations — the intraday regime, walls, flip) and
   **full-chain** (every expiration — the structural zero-gamma flip). Lead with the
   near-term regime + flip + walls; cite the full-chain flip when it differs meaningfully.
   Generalize — describe the condition, don't tell anyone what to do.

## FORMAT FOR DISCORD (important — Discord rendering differs from a plain .md)
- **No markdown tables** — Discord doesn't render them. Put aligned data (the ES/YM/NQ/RTY
  grid, GEX levels, the rotation grid) in a ``` **code block** so columns line up.
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
