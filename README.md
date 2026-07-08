# market-brief

A pre-open **market brief** for traders: a deterministic data-gatherer pulls the hard
numbers, an LLM writes the narrative, and the finished brief is posted to Discord. It's
general situational awareness — what happened, what's rotating, where the dealer-gamma
and vol regime sit, and what's on the event calendar. **Not** financial advice.

## What it pulls

- **Index futures** (ES/YM/NQ/RTY) + **oil** — live via yfinance
- **Macro pulse** — Treasury yields (US Treasury par curve → CNBC/Yahoo live → FRED),
  VIX + VIX9D term structure, with a CNBC-vs-Yahoo cross-check gate
- **Sector rotation** — the 11 SPDR sectors + semis/software/growth, ranked relative to SPY
- **Dealer gamma (GEX)** for SPY + QQQ — computed from the live **Tradier** options chain
  (`gamma · OI · 100 · spot² · 0.01`, calls +, puts −). Two views:
  **near-term** (nearest 4 expirations — the intraday regime) and **full-chain**
  (every expiration — the structural zero-gamma flip)
- **Event gate** — upcoming high-impact events from a bundled calendar
- **Headlines** — overnight wire via Finnhub, trusted-source-ranked + theme-filtered

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt          # macOS/Linux
cp .env.example .env      # then fill in your keys
```

Keys (see `.env.example`): `TRADIER_API_KEY` (GEX), `FINNHUB_API_KEY` (headlines),
`FRED_API_KEY` (optional yield fallback), `DISCORD_WEBHOOK_URL` (one or more, comma-separated).

## Run

```bash
.venv/Scripts/python.exe gather.py                 # writes data/{date}.json + .data.md
# ... an LLM reads the data block and writes briefs/{date}.md per playbook.md ...
.venv/Scripts/python.exe post_discord.py briefs/{date}.md   # fan out to Discord
```

See **`playbook.md`** for the exact brief-writing steps, canonical section layout, and
the hard rules (never invent a number, flag stale data, honor the source cross-check).

## Files

| File | Role |
|------|------|
| `gather.py` | deterministic data layer — every hard number |
| `post_discord.py` | multi-server Discord webhook fan-out |
| `playbook.md` | how the brief is written (for the LLM) |
| `t1_calendar.yaml` | high-impact event calendar (refresh quarterly) |
| `data/` | gather output (gitignored) |
| `briefs/` | finished daily briefs (the archive) |
